"""Conversion manifest persistence and upload candidate discovery."""

from __future__ import annotations

import csv
import logging
import os
import tempfile
from datetime import UTC, date, datetime
from pathlib import Path

from .models import ManifestRow, ManifestRowMapping, UploadCandidate
from .parsers import ParseError, parse_session

LOGGER = logging.getLogger(__name__)
MANIFEST_FIELDS = list(ManifestRow.__dataclass_fields__)


def load_manifest(path: Path) -> dict[str, ManifestRowMapping]:
    """Load a conversion manifest keyed by canonical adidas session ID.

    Rows are read via `csv.DictReader` against whatever header the file
    actually has, so manifests written before newer optional columns
    (`local_start_date`, `start_timezone_offset_ms`) existed load
    unchanged; missing columns are simply absent from each row mapping
    rather than defaulted here (see `_local_date_for_row` for the
    fallback behavior consumers rely on).

    Args:
        path: Path to the `manifest.csv` file.

    Returns:
        A mapping of canonical session ID to its row mapping, or an
        empty mapping if `path` does not exist. If a session ID repeats,
        the last matching row wins.

    Raises:
        ValueError: If the manifest exists but cannot be read or parsed.
    """
    if not path.exists():
        return {}
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            return {
                row["session_id"]: row for row in csv.DictReader(handle) if row.get("session_id")
            }
    except (OSError, csv.Error, KeyError) as exc:
        raise ValueError(f"cannot read conversion manifest {path}: {exc}") from exc


def write_manifest(path: Path, rows: dict[str, ManifestRowMapping]) -> None:
    """Atomically write conversion rows, preserving deterministic ordering.

    Rows are written in ascending session-ID order using the current
    `ManifestRow` field set (`MANIFEST_FIELDS`); any field absent from a
    given row mapping is written as an empty string, and any extra keys
    are ignored. The file is written to a temporary sibling file and
    replaced with `os.replace`, so readers never observe a partially
    written manifest; the temporary file is removed if writing fails.

    Args:
        path: Destination `manifest.csv` path.
        rows: Row mappings keyed by canonical session ID.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=".manifest.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=MANIFEST_FIELDS, extrasaction="ignore")
            writer.writeheader()
            for session_id in sorted(rows):
                normalized = {field: rows[session_id].get(field, "") for field in MANIFEST_FIELDS}
                writer.writerow(normalized)
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def resolve_manifest_path(raw_path: str, manifest_path: Path) -> Path:
    """Resolve current and historical manifest path styles safely.

    Manifest-relative paths have been stored in a few different forms
    over time (absolute, relative to the current working directory, and
    relative to the manifest's own directory or its parents). This tries
    each candidate in that order and returns the first one that exists.

    Args:
        raw_path: The path string as stored in a manifest field.
        manifest_path: Path to the `manifest.csv` file it was read from.

    Returns:
        The resolved, absolute path. If no candidate exists on disk, this
        returns the manifest-directory-relative candidate (by filename)
        without checking existence, so callers get a stable, deterministic
        path even for not-yet-created output files.
    """
    manifest_path = manifest_path.expanduser().resolve()
    path = Path(raw_path).expanduser()
    candidates = (
        path,
        Path.cwd() / path,
        manifest_path.parent / path,
        manifest_path.parent / path.name,
        manifest_path.parent.parent / path,
        manifest_path.parent.parent.parent / path,
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    return (manifest_path.parent / path.name).resolve()


def _parse_timestamp(value: str) -> datetime:
    """Parse an ISO 8601 timestamp, defaulting to UTC when it has no offset."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _local_date_for_row(row: ManifestRowMapping, manifest_path: Path) -> date:
    """Return a manifest row's local calendar date, tolerating older manifest formats.

    Resolution order (source precedence), first match wins:

    1. The row's own `local_start_date` column, if present and non-empty.
    2. The `local_start_date` recomputed by re-parsing `source_session_file`
       (which carries the adidas-recorded local timezone offset), if that
       file can still be read.
    3. The UTC calendar date of `start_time`, as a last resort when
       neither of the above is available.
    """
    if row.get("local_start_date"):
        return date.fromisoformat(row["local_start_date"])
    source = row.get("source_session_file", "")
    if source:
        source_path = resolve_manifest_path(source, manifest_path)
        try:
            return parse_session(source_path).local_start_date
        except ParseError:
            LOGGER.warning(
                "Could not read source session for %s; using UTC date",
                row.get("session_id", "unknown"),
            )
    return _parse_timestamp(row["start_time"]).date()


def load_upload_candidates(output_dir: Path) -> list[UploadCandidate]:
    """Load converted TCX activities from the conversion manifest.

    Only rows with `status` of `"converted"` or `"duplicate"` are
    considered upload candidates; other statuses (for example
    `"skipped_no_gps"`, `"dry_run"`, `"error"`) are excluded.

    Args:
        output_dir: Directory containing `manifest.csv` and the TCX
            output files it references.

    Returns:
        One `UploadCandidate` per eligible manifest row, in manifest
        iteration order (see `load_manifest` for tie-breaking).
    """
    manifest_path = output_dir / "manifest.csv"
    rows = load_manifest(manifest_path)
    candidates: list[UploadCandidate] = []
    for row in rows.values():
        if row.get("status") not in {"converted", "duplicate"}:
            continue
        output_tcx = resolve_manifest_path(row.get("output_tcx", ""), manifest_path)
        candidates.append(
            UploadCandidate(
                session_id=row["session_id"],
                start_time=_parse_timestamp(row["start_time"]),
                local_start_date=_local_date_for_row(row, manifest_path),
                sport_type_id=row["sport_type_id"],
                distance_m=float(row.get("distance_m") or 0),
                output_tcx=output_tcx,
            )
        )
    return candidates
