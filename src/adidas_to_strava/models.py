from __future__ import annotations

from dataclasses import dataclass
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

    def as_dict(self) -> dict[str, str]:
        return vars(self)
