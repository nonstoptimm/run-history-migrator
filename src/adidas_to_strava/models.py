"""Shared immutable domain models and manifest/upload records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

# A single manifest CSV row as read by `csv.DictReader`/written by
# `csv.DictWriter`: column name -> string value. Kept as a plain alias
# (rather than a new type) so it stays interchangeable with the stdlib
# `csv` module and with `ManifestRow.as_dict()`.
type ManifestRowMapping = dict[str, str]


@dataclass(frozen=True)
class Session:
    """A single parsed adidas Running activity.

    Attributes:
        session_id: Canonical adidas session UUID; the stable identity
            used for manifest rows, TCX output filenames, and upload
            state matching.
        start_time_ms: Activity start as Unix epoch milliseconds (UTC).
        start_timezone_offset_ms: UTC offset in milliseconds that adidas
            recorded for the device's local timezone at activity start.
            Used only to derive `local_start_datetime`/`local_start_date`;
            it does not affect any UTC timestamp stored elsewhere.
        end_time_ms: Activity end as Unix epoch milliseconds (UTC), or
            `None` when adidas did not record one.
        duration_ms: Recorded activity duration in milliseconds.
        pause_ms: Recorded paused duration in milliseconds.
        calories: Recorded calories burned (clamped to zero or above).
        sport_type_id: Raw adidas sport type identifier, for example
            `"1"` for running; see `filters.SPORT_TYPE_IDS`.
        distance_m: Recorded distance in meters (clamped to zero or
            above).
        source_file: Path to the session JSON file this was parsed from.
    """

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
        """Return the timezone-aware activity start in adidas' recorded local offset.

        The UTC instant from `start_time_ms` is converted to a fixed-offset
        timezone built from `start_timezone_offset_ms` -- the offset adidas
        captured on-device at the time of the activity, not the timezone of
        the machine running this code. The returned datetime is always
        timezone-aware and its wall-clock fields represent local time where
        the activity took place.
        """
        offset = timezone(timedelta(milliseconds=self.start_timezone_offset_ms))
        return datetime.fromtimestamp(self.start_time_ms / 1000, tz=UTC).astimezone(offset)

    @property
    def local_start_date(self) -> date:
        """Return the activity's local calendar date, per `local_start_datetime`."""
        return self.local_start_datetime.date()


@dataclass(frozen=True)
class TrackPoint:
    """One timestamped sample along an activity's GPS track.

    Attributes:
        timestamp_ms: Sample time as Unix epoch milliseconds (UTC).
        latitude: WGS84 latitude in degrees.
        longitude: WGS84 longitude in degrees.
        altitude_m: Altitude in meters, or `None` if unavailable.
        distance_m: Cumulative distance in meters as recorded by the
            source GPS stream, or `None` if unavailable.
        heart_rate: Heart rate in beats per minute, or `None` if
            unavailable from the GPS stream itself (it may later be
            filled in from a separate heart-rate stream).
    """

    timestamp_ms: int
    latitude: float
    longitude: float
    altitude_m: float | None = None
    distance_m: float | None = None
    heart_rate: int | None = None


@dataclass(frozen=True)
class TimedValue:
    """A single timestamped scalar measurement (heart rate or elevation).

    Attributes:
        timestamp_ms: Sample time as Unix epoch milliseconds (UTC).
        value: The measured value, in beats per minute or meters
            depending on which stream it was parsed from.
    """

    timestamp_ms: int
    value: float


@dataclass(frozen=True)
class CompanionFiles:
    """Optional companion data files discovered for one session.

    Attributes:
        gps_json: Path to the adidas GPS JSON stream, if present.
        gps_gpx: Path to a GPX GPS stream; only used as a fallback when
            `gps_json` is absent or has no usable points.
        heart_rate: Path to a separate heart-rate JSON stream, if present.
        elevation: Path to a separate elevation JSON stream, if present.
    """

    gps_json: Path | None = None
    gps_gpx: Path | None = None
    heart_rate: Path | None = None
    elevation: Path | None = None


@dataclass
class ManifestRow:
    """One row of the conversion manifest CSV.

    All fields are strings so rows round-trip verbatim through
    `csv.DictWriter`/`csv.DictReader`. `local_start_date` and
    `start_timezone_offset_ms` were added after the manifest format
    shipped; readers must tolerate older manifests that lack them (see
    `manifest.load_manifest` and `manifest.load_upload_candidates`, which
    fall back to recomputing the local date when it is missing).

    Attributes:
        session_id: Canonical adidas session UUID.
        start_time: UTC activity start, ISO 8601 with a `Z` suffix.
        sport_type_id: Raw adidas sport type identifier.
        distance_m: Recorded distance in meters, formatted to 3 decimals.
        duration_ms: Recorded duration in milliseconds.
        source_session_file: Path to the source adidas session JSON.
        gps_file: Path to the GPS stream used (JSON or GPX), or empty.
        heart_rate_file: Path to the heart-rate JSON stream, or empty.
        elevation_file: Path to the elevation JSON stream, or empty.
        output_tcx: Path to the generated (or would-be) TCX file.
        status: One of `"converted"`, `"duplicate"`, `"dry_run"`,
            `"skipped_no_gps"`, or `"error"`.
        warning: Semicolon-joined non-fatal warnings, or empty.
        local_start_date: ISO 8601 local calendar date; empty for rows
            written before this field existed.
        start_timezone_offset_ms: Local UTC offset in milliseconds at
            activity start; empty for rows written before this field
            existed.
    """

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

    def as_dict(self) -> ManifestRowMapping:
        """Return this row as a plain string-keyed mapping for CSV writing."""
        return vars(self)


@dataclass(frozen=True)
class UploadCandidate:
    """A converted adidas activity ready for upload selection.

    Attributes:
        session_id: Canonical adidas session UUID.
        start_time: Timezone-aware UTC activity start, parsed from the
            manifest's `start_time` column.
        local_start_date: Local calendar date used for `--since`/`--until`
            filtering; see `filters.DateRange`. Recomputed from the
            source session when a manifest row predates this column.
        sport_type_id: Raw adidas sport type identifier.
        distance_m: Recorded distance in meters.
        output_tcx: Path to the generated TCX file to upload.
    """

    session_id: str
    start_time: datetime
    local_start_date: date
    sport_type_id: str
    distance_m: float
    output_tcx: Path


@dataclass(frozen=True)
class UploadRecord:
    """Persistent Strava upload state for one adidas session.

    Attributes:
        session_id: Canonical adidas session UUID.
        source_tcx: Path to the TCX file most recently submitted.
        status: One of `"submitting"`, `"submitted"`, `"processing"`,
            `"completed"`, or `"failed"`.
        attempt_count: Number of upload attempts made for this session.
        attempt_timestamp: UTC timestamp of the most recent attempt, or
            `None` before the first attempt.
        submitted_timestamp: UTC timestamp when Strava accepted the
            upload, or `None` until submission succeeds.
        completed_timestamp: UTC timestamp when Strava finished
            processing the upload, or `None` until then.
        strava_upload_id: Strava's upload identifier, or `None` before
            submission.
        strava_activity_id: Strava's created activity identifier, or
            `None` until processing completes.
        error_warning: Most recent non-fatal warning or error message,
            or empty.
    """

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
