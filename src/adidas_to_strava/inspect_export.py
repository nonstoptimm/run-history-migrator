"""Read-only adidas export inspection."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .filters import DateRange, validate_sport
from .models import Session
from .parsers import ParseError, index_companions, parse_session, scan_sessions

LOGGER = logging.getLogger(__name__)


@dataclass
class InspectionSummary:
    """Aggregate counts from a read-only export inspection.

    Attributes:
        scanned_sessions: Session JSON files found before any filtering.
        malformed_sessions: Session files skipped after a logged parse warning.
        before_since: Matching sessions earlier than the inclusive local ``since`` date.
        after_until: Matching sessions later than the inclusive local ``until`` date.
        non_sport: Date- and session-matching sessions with an unselected sport ID.
        eligible: Sessions remaining after filtering, ordering, and limiting.
        with_gps: Eligible sessions with a JSON or GPX companion file.
        with_heart_rate: Eligible sessions with an optional heart-rate companion file.
        with_elevation: Eligible sessions with an optional elevation companion file.
    """

    scanned_sessions: int = 0
    malformed_sessions: int = 0
    before_since: int = 0
    after_until: int = 0
    non_sport: int = 0
    eligible: int = 0
    with_gps: int = 0
    with_heart_rate: int = 0
    with_elevation: int = 0


def inspect_export(
    export_path: Path,
    since: date | None,
    until: date | None,
    sport: str,
    session_id: str | None = None,
    limit: int | None = None,
) -> InspectionSummary:
    """Inspect selected adidas sessions and companion-file availability.

    ``export_path`` is the export root containing ``Sport-sessions``. The optional
    date bounds are inclusive and compare each session's adidas local calendar date.
    ``sport`` is resolved to supported adidas sport IDs, and ``session_id`` selects
    one exact canonical session ID. Eligible sessions are deterministically ordered
    by local date and session ID before ``limit`` is applied.

    Inspection is read-only: it does not create TCX, manifest, or other output files.
    A missing GPS companion does not make a session ineligible, while heart-rate and
    elevation companions are optional. Companion counts report file presence rather
    than validating file contents. Malformed session files are logged and counted.

    Args:
        export_path: Root directory of the extracted adidas export.
        since: Earliest included adidas local calendar date, inclusive.
        until: Latest included adidas local calendar date, inclusive.
        sport: Supported sport selector mapped to adidas sport IDs.
        session_id: Exact canonical adidas session ID to inspect, if provided.
        limit: Maximum eligible sessions to inspect after deterministic ordering.

    Returns:
        A read-only summary of filtering and companion-file availability.

    Raises:
        ValueError: If the export layout, sport, or date range is invalid.
    """
    sport_sessions = export_path / "Sport-sessions"
    if not sport_sessions.is_dir():
        raise ValueError(f"missing Sport-sessions directory: {sport_sessions}")
    sport_ids = validate_sport(sport)
    date_range = DateRange(since, until)
    summary = InspectionSummary()
    eligible: list[Session] = []
    session_paths = scan_sessions(sport_sessions)
    summary.scanned_sessions = len(session_paths)
    for path in session_paths:
        try:
            session = parse_session(path)
        except ParseError as exc:
            summary.malformed_sessions += 1
            LOGGER.warning("Skipping malformed session %s: %s", path, exc)
            continue
        if session_id and session.session_id != session_id:
            continue
        if since and session.local_start_date < since:
            summary.before_since += 1
            continue
        if until and session.local_start_date > until:
            summary.after_until += 1
            continue
        if not date_range.contains(session.local_start_date):
            continue
        if session.sport_type_id not in sport_ids:
            summary.non_sport += 1
            continue
        eligible.append(session)
    eligible.sort(key=lambda item: (item.local_start_date, item.session_id))
    if limit is not None:
        eligible = eligible[:limit]
    summary.eligible = len(eligible)
    companions = index_companions(sport_sessions)
    for session in eligible:
        files = companions.get(session.session_id)
        if files and (files.gps_json or files.gps_gpx):
            summary.with_gps += 1
        if files and files.heart_rate:
            summary.with_heart_rate += 1
        if files and files.elevation:
            summary.with_elevation += 1
    return summary


def format_inspection(summary: InspectionSummary) -> str:
    """Render a concise inspection report.

    Args:
        summary: Completed inspection summary.

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
            f"Eligible runs: {summary.eligible}",
            f"With GPS: {summary.with_gps}",
            f"With heart rate: {summary.with_heart_rate}",
            f"With separate elevation: {summary.with_elevation}",
        )
    )
