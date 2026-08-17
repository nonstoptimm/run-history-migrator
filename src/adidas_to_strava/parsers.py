"""Defensive parsers for adidas session, GPS, heart-rate, elevation, and GPX data."""

from __future__ import annotations

import bisect
import json
import math
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from pathlib import Path

from .models import CompanionFiles, Session, TimedValue, TrackPoint

JOIN_TOLERANCE_MS = 5_000

# Narrow shapes for the untyped JSON adidas exports: an object (keyed by
# string) and a top-level array. `object` (not `Any`) is used for
# values/elements so callers must explicitly narrow before use instead of
# silently propagating an untyped value.
type JSONObject = dict[str, object]
type JSONArray = list[object]


class ParseError(ValueError):
    """Raised when an adidas export file is missing, unreadable, or malformed."""


def _as_object(value: object) -> JSONObject | None:
    """Narrow `value` to a JSON object mapping, or `None` if it is not one."""
    return value if isinstance(value, dict) else None


def _as_array(value: object) -> JSONArray | None:
    """Narrow `value` to a JSON array, or `None` if it is not one."""
    return value if isinstance(value, list) else None


def _feature_attributes(data: JSONObject, feature_type: str) -> JSONObject:
    """Return the ``attributes`` mapping of the first feature block matching `feature_type`.

    adidas session JSON nests optional/legacy fields inside a top-level
    ``features`` array of typed blocks (for example ``"initial_values"``
    or ``"track_metrics"``); this looks up the first block by its
    ``type`` value.
    """
    features = _as_array(data.get("features"))
    if features is None:
        return {}
    for feature in features:
        feature_obj = _as_object(feature)
        if feature_obj is not None and feature_obj.get("type") == feature_type:
            return _as_object(feature_obj.get("attributes")) or {}
    return {}


def _number(value: object, default: float = 0) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    if isinstance(value, str):
        try:
            parsed = float(value)
        except ValueError:
            return default
        return parsed if math.isfinite(parsed) else default
    return default


def _integer(value: object, default: int = 0) -> int:
    number = _number(value, default)
    return int(number)


def parse_session(path: Path) -> Session:
    """Parse one adidas session JSON file into a `Session`.

    Args:
        path: Path to the session JSON file.

    Returns:
        The parsed `Session`.

    Raises:
        ParseError: If the file cannot be read, is not valid JSON, is not
            a JSON object, or is missing a required field (canonical
            session ID, start time, or sport type ID).
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ParseError(f"cannot read session JSON: {exc}") from exc
    data = _as_object(raw)
    if data is None:
        raise ParseError("session JSON is not an object")

    initial = _feature_attributes(data, "initial_values")
    metrics = _feature_attributes(data, "track_metrics")
    session_id = data.get("id")
    if not isinstance(session_id, str) or not session_id.strip():
        raise ParseError("missing canonical session id")

    start_time_ms = _integer(data.get("start_time") or initial.get("start_time"), -1)
    if start_time_ms < 0:
        raise ParseError("missing or invalid start_time")
    sport_type_id = data.get("sport_type_id")
    if sport_type_id is None:
        sport = _as_object(initial.get("sport_type"))
        sport_type_id = sport.get("id") if sport is not None else None
    if sport_type_id is None:
        raise ParseError("missing sport type id")

    end_value = data.get("end_time") or initial.get("end_time")
    end_time_ms = _integer(end_value, -1)
    return Session(
        session_id=session_id,
        start_time_ms=start_time_ms,
        start_timezone_offset_ms=_integer(data.get("start_time_timezone_offset")),
        end_time_ms=end_time_ms if end_time_ms >= 0 else None,
        duration_ms=max(0, _integer(data.get("duration") or initial.get("duration"))),
        pause_ms=max(0, _integer(data.get("pause") or initial.get("pause"))),
        calories=max(0, _integer(data.get("calories"))),
        sport_type_id=str(sport_type_id),
        distance_m=max(0.0, _number(metrics.get("distance") or initial.get("distance"))),
        source_file=path,
    )


def scan_sessions(sport_sessions_dir: Path) -> list[Path]:
    """Return every session JSON file directly under `sport_sessions_dir`, sorted by name."""
    return sorted(path for path in sport_sessions_dir.glob("*.json") if path.is_file())


def _id_from_companion_filename(path: Path) -> str | None:
    """Return the canonical session ID suffix of a companion filename, if present.

    Companion files are named ``<arbitrary-prefix>_<session_id>.<ext>``;
    this returns the part after the last underscore, or `None` if the
    filename has no underscore or an empty suffix.
    """
    if "_" not in path.stem:
        return None
    session_id = path.stem.rsplit("_", 1)[1]
    return session_id or None


def index_companions(sport_sessions_dir: Path) -> dict[str, CompanionFiles]:
    """Index each session's companion GPS/heart-rate/elevation files by session ID.

    Args:
        sport_sessions_dir: The adidas export's `Sport-sessions` directory.

    Returns:
        A mapping of canonical session ID to the `CompanionFiles` found
        for it. Sessions with no companion files at all are omitted. When
        multiple files match the same session and field, the
        lexicographically first path (by sorted glob order) wins.
    """
    indexed: dict[str, dict[str, Path]] = {}
    locations = (
        ("GPS-data", "*.json", "gps_json"),
        ("GPS-data", "*.gpx", "gps_gpx"),
        ("Heart-rate-data", "*.json", "heart_rate"),
        ("Elevation-data", "*.json", "elevation"),
    )
    for directory, pattern, field in locations:
        for path in sorted((sport_sessions_dir / directory).glob(pattern)):
            session_id = _id_from_companion_filename(path)
            if session_id:
                indexed.setdefault(session_id, {}).setdefault(field, path)
    return {session_id: CompanionFiles(**paths) for session_id, paths in indexed.items()}


def _load_array(path: Path, label: str) -> JSONArray:
    """Read and return a JSON file's top-level array.

    Args:
        path: Path to the JSON file.
        label: Human-readable description used in error messages.

    Raises:
        ParseError: If the file cannot be read, is not valid JSON, or its
            top-level value is not a JSON array.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ParseError(f"cannot read {label}: {exc}") from exc
    data = _as_array(raw)
    if data is None:
        raise ParseError(f"{label} is not a JSON array")
    return data


def parse_gps_json(path: Path) -> tuple[list[TrackPoint], list[str]]:
    """Parse an adidas GPS JSON stream into timestamp-sorted track points.

    Rows that are not JSON objects, or have a missing/invalid timestamp
    or out-of-range latitude/longitude, are dropped and reported as
    warnings. Other rows are kept even when altitude/distance are
    missing or invalid (those fields simply become `None`).

    Args:
        path: Path to the GPS JSON file.

    Returns:
        A tuple of the parsed, timestamp-sorted track points and any
        non-fatal per-row warnings.

    Raises:
        ParseError: If the file cannot be read or is not a JSON array.
    """
    points: list[TrackPoint] = []
    warnings: list[str] = []
    for index, raw_row in enumerate(_load_array(path, "GPS JSON")):
        row = _as_object(raw_row)
        if row is None:
            warnings.append(f"GPS row {index} is not an object")
            continue
        timestamp = _integer(row.get("timestamp"), -1)
        latitude = _number(row.get("latitude"), math.nan)
        longitude = _number(row.get("longitude"), math.nan)
        if timestamp < 0 or not (-90 <= latitude <= 90) or not (-180 <= longitude <= 180):
            warnings.append(f"GPS row {index} has invalid timestamp or coordinates")
            continue
        altitude = _number(row.get("altitude"), math.nan)
        distance = _number(row.get("distance"), math.nan)
        points.append(
            TrackPoint(
                timestamp_ms=timestamp,
                latitude=latitude,
                longitude=longitude,
                altitude_m=altitude if math.isfinite(altitude) else None,
                distance_m=distance if math.isfinite(distance) and distance >= 0 else None,
            )
        )
    points.sort(key=lambda point: point.timestamp_ms)
    return points, warnings


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_gpx(path: Path) -> tuple[list[TrackPoint], list[str]]:
    """Parse a GPX 1.1 track into timestamp-sorted track points.

    Only `<trkpt>` elements are considered. A point is dropped (with a
    warning) if its `lat`/`lon` attributes are missing/invalid or if it
    has no valid `<time>` child; `<ele>` and `<hr>` children are optional
    and populate `altitude_m`/`heart_rate` when present and valid.

    Args:
        path: Path to the GPX file.

    Returns:
        A tuple of the parsed, timestamp-sorted track points and any
        non-fatal per-point warnings.

    Raises:
        ParseError: If the file cannot be read or is not well-formed XML.
    """
    from datetime import datetime

    warnings: list[str] = []
    points: list[TrackPoint] = []
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise ParseError(f"cannot read GPX: {exc}") from exc
    for index, element in enumerate(root.iter()):
        if _local_name(element.tag) != "trkpt":
            continue
        try:
            latitude = float(element.attrib["lat"])
            longitude = float(element.attrib["lon"])
        except (KeyError, ValueError):
            warnings.append(f"GPX point {index} has invalid coordinates")
            continue
        timestamp_ms: int | None = None
        altitude: float | None = None
        heart_rate: int | None = None
        for child in element.iter():
            name = _local_name(child.tag)
            text = child.text.strip() if child.text else ""
            try:
                if name == "time" and text:
                    timestamp_ms = int(
                        datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000
                    )
                elif name == "ele" and text:
                    altitude = float(text)
                elif name == "hr" and text and int(float(text)) > 0:
                    heart_rate = int(float(text))
            except ValueError:
                continue
        if timestamp_ms is None:
            warnings.append(f"GPX point {index} has no valid timestamp")
            continue
        points.append(
            TrackPoint(
                timestamp_ms=timestamp_ms,
                latitude=latitude,
                longitude=longitude,
                altitude_m=altitude,
                heart_rate=heart_rate,
            )
        )
    points.sort(key=lambda point: point.timestamp_ms)
    return points, warnings


def parse_heart_rate(path: Path) -> tuple[list[TimedValue], list[str]]:
    """Parse a separate adidas heart-rate JSON stream into timestamped values.

    Rows that are not JSON objects are reported as warnings. Rows with a
    missing/invalid timestamp or a non-positive heart rate are silently
    dropped (not warned), matching this stream's existing tolerance for
    sparse/partial companion data.

    Args:
        path: Path to the heart-rate JSON file.

    Returns:
        A tuple of the parsed, timestamp-sorted values and any per-row
        warnings.

    Raises:
        ParseError: If the file cannot be read or is not a JSON array.
    """
    values: list[TimedValue] = []
    warnings: list[str] = []
    for index, raw_row in enumerate(_load_array(path, "heart-rate JSON")):
        row = _as_object(raw_row)
        if row is None:
            warnings.append(f"HR row {index} is not an object")
            continue
        timestamp = _integer(row.get("timestamp"), -1)
        heart_rate = _integer(row.get("heart_rate"), 0)
        if timestamp < 0 or heart_rate <= 0:
            continue
        values.append(TimedValue(timestamp, float(heart_rate)))
    values.sort(key=lambda item: item.timestamp_ms)
    return values, warnings


def parse_elevation(path: Path) -> tuple[list[TimedValue], list[str]]:
    """Parse a separate adidas elevation JSON stream into timestamped values.

    Rows that are not JSON objects are reported as warnings. Rows with a
    missing/invalid timestamp or a non-finite elevation are silently
    dropped (not warned), matching this stream's existing tolerance for
    sparse/partial companion data.

    Args:
        path: Path to the elevation JSON file.

    Returns:
        A tuple of the parsed, timestamp-sorted values and any per-row
        warnings.

    Raises:
        ParseError: If the file cannot be read or is not a JSON array.
    """
    values: list[TimedValue] = []
    warnings: list[str] = []
    for index, raw_row in enumerate(_load_array(path, "elevation JSON")):
        row = _as_object(raw_row)
        if row is None:
            warnings.append(f"elevation row {index} is not an object")
            continue
        timestamp = _integer(row.get("timestamp"), -1)
        elevation = _number(row.get("elevation"), math.nan)
        if timestamp < 0 or not math.isfinite(elevation):
            continue
        values.append(TimedValue(timestamp, elevation))
    values.sort(key=lambda item: item.timestamp_ms)
    return values, warnings


def _nearest(values: list[TimedValue], timestamp_ms: int) -> TimedValue | None:
    """Return the value in `values` closest to `timestamp_ms`, within `JOIN_TOLERANCE_MS`.

    `values` must already be sorted by `timestamp_ms`. Returns `None` if
    `values` is empty or the closest candidate is farther than
    `JOIN_TOLERANCE_MS` away.
    """
    if not values:
        return None
    timestamps = [item.timestamp_ms for item in values]
    position = bisect.bisect_left(timestamps, timestamp_ms)
    candidates = values[max(0, position - 1) : min(len(values), position + 1)]
    nearest = min(candidates, key=lambda item: abs(item.timestamp_ms - timestamp_ms))
    if abs(nearest.timestamp_ms - timestamp_ms) > JOIN_TOLERANCE_MS:
        return None
    return nearest


def merge_measurements(
    points: Iterable[TrackPoint],
    heart_rate: list[TimedValue],
    elevation: list[TimedValue],
) -> list[TrackPoint]:
    """Join separate heart-rate/elevation streams onto GPS track points.

    For each point, the nearest heart-rate and elevation sample within
    `JOIN_TOLERANCE_MS` (5 seconds) is used to fill in `heart_rate` and
    `altitude_m` respectively, falling back to the point's own values
    (from the GPS stream) when no sample is close enough or the stream is
    empty.

    Args:
        points: GPS track points to enrich, in any order.
        heart_rate: Timestamp-sorted heart-rate samples to join.
        elevation: Timestamp-sorted elevation samples to join.

    Returns:
        A new list of track points, one per input point and in the same
        order, with `heart_rate`/`altitude_m` filled in where possible.
    """
    merged: list[TrackPoint] = []
    for point in points:
        hr = _nearest(heart_rate, point.timestamp_ms)
        ele = _nearest(elevation, point.timestamp_ms)
        merged.append(
            TrackPoint(
                timestamp_ms=point.timestamp_ms,
                latitude=point.latitude,
                longitude=point.longitude,
                altitude_m=ele.value if ele else point.altitude_m,
                distance_m=point.distance_m,
                heart_rate=int(hr.value) if hr else point.heart_rate,
            )
        )
    return merged
