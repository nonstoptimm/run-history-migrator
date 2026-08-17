"""Shared immutable domain models and manifest/upload records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path


@dataclass(frozen=True)
class Session:
    session_id: str
    start_time_ms: int
    start_timezone_offset_ms: int
    end_time_ms: int | None
    duration_ms: int
    pause_ms: int
    calories: int
    sport_type_id: str
    distance_m: float
    source_file: Path

    @property
    def local_start_datetime(self) -> datetime:
        """Return the activity start using the timezone offset stored by adidas."""
        offset = timezone(timedelta(milliseconds=self.start_timezone_offset_ms))
        return datetime.fromtimestamp(self.start_time_ms / 1000, tz=UTC).astimezone(offset)

    @property
    def local_start_date(self) -> date:
        """Return the activity's local calendar date."""
        return self.local_start_datetime.date()


@dataclass(frozen=True)
class TrackPoint:
    timestamp_ms: int
    latitude: float
    longitude: float
    altitude_m: float | None = None
    distance_m: float | None = None
    heart_rate: int | None = None


@dataclass(frozen=True)
class TimedValue:
    timestamp_ms: int
    value: float


@dataclass(frozen=True)
class CompanionFiles:
    gps_json: Path | None = None
    gps_gpx: Path | None = None
    heart_rate: Path | None = None
    elevation: Path | None = None


@dataclass
class ManifestRow:
    session_id: str
    start_time: str
    sport_type_id: str
    distance_m: str
    duration_ms: str
    source_session_file: str
    gps_file: str
    heart_rate_file: str
    elevation_file: str
    output_tcx: str
    status: str
    warning: str
    local_start_date: str = ""
    start_timezone_offset_ms: str = ""

    def as_dict(self) -> dict[str, str]:
        return vars(self)


@dataclass(frozen=True)
class UploadCandidate:
    """A converted adidas activity ready for upload selection."""

    session_id: str
    start_time: datetime
    local_start_date: date
    sport_type_id: str
    distance_m: float
    output_tcx: Path


@dataclass(frozen=True)
class UploadRecord:
    """Persistent Strava upload state for one adidas session."""

    session_id: str
    source_tcx: str
    status: str
    attempt_count: int
    attempt_timestamp: str | None = None
    submitted_timestamp: str | None = None
    completed_timestamp: str | None = None
    strava_upload_id: int | None = None
    strava_activity_id: int | None = None
    error_warning: str = ""
