"""Conversion manifest persistence and upload candidate discovery."""

from __future__ import annotations

import csv
import logging
import os
import tempfile
from datetime import UTC, date, datetime
from pathlib import Path

from .models import ManifestRow, UploadCandidate
from .parsers import ParseError, parse_session

LOGGER = logging.getLogger(__name__)
MANIFEST_FIELDS = list(ManifestRow.__dataclass_fields__)


def load_manifest(path: Path) -> dict[str, dict[str, str]]:
    """Load a conversion manifest keyed by canonical adidas session ID."""
    if not path.exists():
        return {}
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            return {
                row["session_id"]: row for row in csv.DictReader(handle) if row.get("session_id")
            }
    except (OSError, csv.Error, KeyError) as exc:
        raise ValueError(f"cannot read conversion manifest {path}: {exc}") from exc


def write_manifest(path: Path, rows: dict[str, dict[str, str]]) -> None:
    """Atomically write conversion rows, preserving deterministic ordering."""
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
    """Resolve current and historical manifest path styles safely."""
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
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _local_date_for_row(row: dict[str, str], manifest_path: Path) -> date:
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
    """Load converted TCX activities from the conversion manifest."""
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
