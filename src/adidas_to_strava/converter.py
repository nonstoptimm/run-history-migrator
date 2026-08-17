"""Orchestrate adidas export conversion into local TCX files and a manifest."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .filters import DateRange, validate_sport
from .manifest import load_manifest, write_manifest
from .models import ManifestRow, Session, TimedValue, TrackPoint
from .parsers import (
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


@dataclass
class Summary:
    """Aggregate outcomes and manifest rows from one conversion run.

    Attributes:
        scanned_sessions: Session JSON files found before any filtering.
        before_since: Matching sessions earlier than the inclusive local ``since`` date.
        after_until: Matching sessions later than the inclusive local ``until`` date.
        non_sport: Date- and session-matching sessions with an unselected sport ID.
        malformed_sessions: Session files skipped after a parse warning.
        eligible_runs: Sessions remaining after filtering, ordering, and limiting.
        converted: TCX files successfully written; always zero during a dry run.
        skipped_no_gps: Eligible sessions without a usable timestamped GPS stream.
        skipped_duplicate: Eligible sessions skipped because output was already converted.
        missing_hr: GPS-backed sessions without usable optional heart-rate data.
        missing_elevation: GPS-backed sessions without a separate elevation file.
        errors: Eligible sessions whose TCX construction or write failed.
        rows: Ordered manifest records produced for processed eligible sessions.
    """

    scanned_sessions: int = 0
    before_since: int = 0
    after_until: int = 0
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
        local_start_date=session.local_start_date.isoformat(),
        start_timezone_offset_ms=str(session.start_timezone_offset_ms),
    )


def convert(
    export_path: Path,
    output_dir: Path,
    since: date | None,
    until: date | None,
    sport: str,
    dry_run: bool = False,
    limit: int | None = None,
    session_id: str | None = None,
    overwrite: bool = False,
) -> Summary:
    """Convert selected adidas sessions to TCX and update the conversion manifest.

    ``export_path`` is the export root containing ``Sport-sessions``. The optional
    date bounds are inclusive and compare each session's adidas local calendar date.
    ``sport`` is resolved to supported adidas sport IDs, and ``session_id`` selects
    one exact canonical session ID. Sessions are ordered by start timestamp and then
    session ID before ``limit`` is applied.

    GPS is required: JSON is preferred and GPX is used as a fallback. Missing or
    malformed heart-rate and elevation streams are non-fatal and are reflected in
    counters or row warnings. Malformed session files, unusable GPS, and TCX failures
    are logged as warnings or errors as appropriate.

    Unless ``overwrite`` is true, an existing destination or a manifest row already
    marked ``converted`` is treated as a duplicate. A real run writes TCX files and
    rewrites ``output_dir/manifest.csv`` with existing rows plus rows for duplicates,
    skipped sessions, successes, and errors. A dry run reads existing output and
    manifest state for duplicate detection but creates or modifies no files.

    Args:
        export_path: Root directory of the extracted adidas export.
        output_dir: Directory for TCX files and ``manifest.csv``.
        since: Earliest included adidas local calendar date, inclusive.
        until: Latest included adidas local calendar date, inclusive.
        sport: Supported sport selector mapped to adidas sport IDs.
        dry_run: If true, perform selection and parsing without writing files.
        limit: Maximum eligible sessions to process after chronological ordering.
        session_id: Exact canonical adidas session ID to process, if provided.
        overwrite: If true, process sessions even when output is already recorded.

    Returns:
        A summary of filtering, conversion outcomes, warnings, and manifest rows.

    Raises:
        ValueError: If the export layout, filters, or existing manifest are invalid.
        OSError: If the final manifest cannot be written.
    """
    sport_sessions = export_path / "Sport-sessions"
    if not sport_sessions.is_dir():
        raise ValueError(f"missing Sport-sessions directory: {sport_sessions}")
    sport_ids = validate_sport(sport)
    date_range = DateRange(since=since, until=until)

    summary = Summary()
    parsed: list[Session] = []
    session_paths = scan_sessions(sport_sessions)
    summary.scanned_sessions = len(session_paths)
    for path in session_paths:
        try:
            item = parse_session(path)
        except ParseError as exc:
            summary.malformed_sessions += 1
            LOGGER.warning("Skipping malformed session %s: %s", path, exc)
            continue
        if session_id and item.session_id != session_id:
            continue
        if since and item.local_start_date < since:
            summary.before_since += 1
            continue
        if until and item.local_start_date > until:
            summary.after_until += 1
            continue
        if not date_range.contains(item.local_start_date):
            continue
        if item.sport_type_id not in sport_ids:
            summary.non_sport += 1
            continue
        parsed.append(item)

    parsed.sort(key=lambda item: (item.start_time_ms, item.session_id))
    if limit is not None:
        parsed = parsed[:limit]
    summary.eligible_runs = len(parsed)

    companions = index_companions(sport_sessions)
    manifest_path = output_dir / "manifest.csv"
    existing_manifest: dict[str, dict[str, str]] = load_manifest(manifest_path)
    updated_manifest: dict[str, dict[str, str]] = dict(existing_manifest)

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
            previous = existing_manifest.get(item.session_id, {})
            status = "converted" if previous.get("status") == "converted" else "duplicate"
            row = _manifest_row(
                item,
                gps_file,
                hr_file,
                elevation_file,
                destination,
                status,
                "output already exists",
            )
            summary.rows.append(row)
            updated_manifest[item.session_id] = row.as_dict()
            continue

        points: list[TrackPoint] = []
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

        heart_rate: list[TimedValue] = []
        if hr_file:
            try:
                heart_rate, hr_warnings = parse_heart_rate(hr_file)
                warnings.extend(hr_warnings)
            except ParseError as exc:
                warnings.append(str(exc))
        if not heart_rate and not any(point.heart_rate for point in points):
            summary.missing_hr += 1

        elevation: list[TimedValue] = []
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
        row = _manifest_row(item, gps_file, hr_file, elevation_file, destination, status, warning)
        summary.rows.append(row)
        updated_manifest[item.session_id] = row.as_dict()

    if not dry_run:
        write_manifest(manifest_path, updated_manifest)
    return summary


def format_summary(summary: Summary) -> str:
    """Render conversion totals without the per-session manifest rows.

    Args:
        summary: Completed conversion summary.

    Returns:
        A newline-delimited report suitable for CLI output.
    """
    return "\n".join(
        (
            f"Scanned sessions: {summary.scanned_sessions}",
            f"Before since date: {summary.before_since}",
            f"After until date: {summary.after_until}",
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
