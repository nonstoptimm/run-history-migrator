from __future__ import annotations

import csv
import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

from .models import ManifestRow, Session
from .parsers import (
    SPORT_TYPE_IDS,
    ParseError,
    index_companions,
    merge_measurements,
    parse_elevation,
    parse_gps_json,
    parse_gpx,
    parse_heart_rate,
    parse_session,
    scan_sessions,
)
from .tcx import build_tcx, output_filename, utc_timestamp, write_tcx

LOGGER = logging.getLogger(__name__)
MANIFEST_FIELDS = list(ManifestRow.__dataclass_fields__)


@dataclass
class Summary:
    scanned_sessions: int = 0
    before_since: int = 0
    non_sport: int = 0
    malformed_sessions: int = 0
    eligible_runs: int = 0
    converted: int = 0
    skipped_no_gps: int = 0
    skipped_duplicate: int = 0
    missing_hr: int = 0
    missing_elevation: int = 0
    errors: int = 0
    rows: list[ManifestRow] = field(default_factory=list)


def since_epoch_ms(value: date) -> int:
    return int(datetime(value.year, value.month, value.day, tzinfo=UTC).timestamp() * 1000)


def _load_manifest(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            return {
                row["session_id"]: row
                for row in csv.DictReader(handle)
                if row.get("session_id")
            }
    except (OSError, csv.Error, KeyError) as exc:
        LOGGER.warning("Could not read existing manifest %s: %s", path, exc)
        return {}


def _write_manifest(path: Path, rows: dict[str, dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=".manifest.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=MANIFEST_FIELDS)
            writer.writeheader()
            for session_id in sorted(rows):
                writer.writerow(rows[session_id])
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _manifest_row(
    session: Session,
    gps_file: Path | None,
    hr_file: Path | None,
    elevation_file: Path | None,
    output: Path,
    status: str,
    warning: str,
) -> ManifestRow:
    return ManifestRow(
        session_id=session.session_id,
        start_time=utc_timestamp(session.start_time_ms),
        sport_type_id=session.sport_type_id,
        distance_m=f"{session.distance_m:.3f}",
        duration_ms=str(session.duration_ms),
        source_session_file=str(session.source_file),
        gps_file=str(gps_file or ""),
        heart_rate_file=str(hr_file or ""),
        elevation_file=str(elevation_file or ""),
        output_tcx=str(output),
        status=status,
        warning=warning,
    )


def convert(
    export_path: Path,
    output_dir: Path,
    since: date,
    sport: str,
    dry_run: bool = False,
    limit: int | None = None,
    session_id: str | None = None,
    overwrite: bool = False,
) -> Summary:
    sport_sessions = export_path / "Sport-sessions"
    if not sport_sessions.is_dir():
        raise ValueError(f"missing Sport-sessions directory: {sport_sessions}")
    if sport not in SPORT_TYPE_IDS:
        raise ValueError(f"unsupported sport {sport!r}; supported: {', '.join(SPORT_TYPE_IDS)}")

    summary = Summary()
    parsed: list[Session] = []
    session_paths = scan_sessions(sport_sessions)
    summary.scanned_sessions = len(session_paths)
    cutoff = since_epoch_ms(since)
    for path in session_paths:
        try:
            item = parse_session(path)
        except ParseError as exc:
            summary.malformed_sessions += 1
            LOGGER.warning("Skipping malformed session %s: %s", path, exc)
            continue
        if session_id and item.session_id != session_id:
            continue
        if item.start_time_ms < cutoff:
            summary.before_since += 1
            continue
        if item.sport_type_id not in SPORT_TYPE_IDS[sport]:
            summary.non_sport += 1
            continue
        parsed.append(item)

    parsed.sort(key=lambda item: (item.start_time_ms, item.session_id))
    if limit is not None:
        parsed = parsed[:limit]
    summary.eligible_runs = len(parsed)

    companions = index_companions(sport_sessions)
    manifest_path = output_dir / "manifest.csv"
    existing_manifest = _load_manifest(manifest_path)
    updated_manifest = dict(existing_manifest)

    for item in parsed:
        files = companions.get(item.session_id)
        gps_file = files.gps_json if files else None
        hr_file = files.heart_rate if files else None
        elevation_file = files.elevation if files else None
        destination = output_dir / output_filename(item)
        warnings: list[str] = []

        duplicate = destination.exists() or (
            item.session_id in existing_manifest
            and existing_manifest[item.session_id].get("status") == "converted"
        )
        if duplicate and not overwrite:
            summary.skipped_duplicate += 1
            row = _manifest_row(
                item, gps_file, hr_file, elevation_file, destination, "duplicate", "output already exists"
            )
            summary.rows.append(row)
            updated_manifest[item.session_id] = row.as_dict()
            continue

        points = []
        if files and files.gps_json:
            try:
                points, gps_warnings = parse_gps_json(files.gps_json)
                warnings.extend(gps_warnings)
                gps_file = files.gps_json
            except ParseError as exc:
                warnings.append(str(exc))
        if not points and files and files.gps_gpx:
            try:
                points, gpx_warnings = parse_gpx(files.gps_gpx)
                warnings.extend(gpx_warnings)
                gps_file = files.gps_gpx
            except ParseError as exc:
                warnings.append(str(exc))
        if not points:
            summary.skipped_no_gps += 1
            warning = "; ".join(warnings + ["no usable timestamped GPS stream"])
            LOGGER.warning("Skipping %s: %s", item.session_id, warning)
            row = _manifest_row(
                item, gps_file, hr_file, elevation_file, destination, "skipped_no_gps", warning
            )
            summary.rows.append(row)
            updated_manifest[item.session_id] = row.as_dict()
            continue

        heart_rate = []
        if hr_file:
            try:
                heart_rate, hr_warnings = parse_heart_rate(hr_file)
                warnings.extend(hr_warnings)
            except ParseError as exc:
                warnings.append(str(exc))
        if not heart_rate and not any(point.heart_rate for point in points):
            summary.missing_hr += 1

        elevation = []
        if elevation_file:
            try:
                elevation, elevation_warnings = parse_elevation(elevation_file)
                warnings.extend(elevation_warnings)
            except ParseError as exc:
                warnings.append(str(exc))
        else:
            summary.missing_elevation += 1

        merged = merge_measurements(points, heart_rate, elevation)
        warning = "; ".join(warnings)
        if dry_run:
            status = "dry_run"
        else:
            try:
                write_tcx(build_tcx(item, merged), destination)
                status = "converted"
                summary.converted += 1
            except (OSError, ValueError) as exc:
                status = "error"
                summary.errors += 1
                warning = "; ".join(filter(None, (warning, str(exc))))
                LOGGER.error("Failed to convert %s: %s", item.session_id, exc)
        row = _manifest_row(
            item, gps_file, hr_file, elevation_file, destination, status, warning
        )
        summary.rows.append(row)
        updated_manifest[item.session_id] = row.as_dict()

    if not dry_run:
        _write_manifest(manifest_path, updated_manifest)
    return summary


def format_summary(summary: Summary) -> str:
    return "\n".join(
        (
            f"Scanned sessions: {summary.scanned_sessions}",
            f"Before since date: {summary.before_since}",
            f"Non-running: {summary.non_sport}",
            f"Malformed sessions: {summary.malformed_sessions}",
            f"Eligible runs: {summary.eligible_runs}",
            f"Converted: {summary.converted}",
            f"Skipped no GPS: {summary.skipped_no_gps}",
            f"Skipped duplicate: {summary.skipped_duplicate}",
            f"Missing HR: {summary.missing_hr}",
            f"Missing elevation: {summary.missing_elevation}",
            f"Errors: {summary.errors}",
        )
    )
