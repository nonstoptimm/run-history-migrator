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


class ParseError(ValueError):
    pass


def _feature_attributes(data: dict, feature_type: str) -> dict:
    features = data.get("features")
    if not isinstance(features, list):
        return {}
    for feature in features:
        if isinstance(feature, dict) and feature.get("type") == feature_type:
            attributes = feature.get("attributes")
            return attributes if isinstance(attributes, dict) else {}
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
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ParseError(f"cannot read session JSON: {exc}") from exc
    if not isinstance(data, dict):
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
        sport = initial.get("sport_type")
        sport_type_id = sport.get("id") if isinstance(sport, dict) else None
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
    return sorted(path for path in sport_sessions_dir.glob("*.json") if path.is_file())


def _id_from_companion_filename(path: Path) -> str | None:
    if "_" not in path.stem:
        return None
    session_id = path.stem.rsplit("_", 1)[1]
    return session_id or None


def index_companions(sport_sessions_dir: Path) -> dict[str, CompanionFiles]:
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


def _load_array(path: Path, label: str) -> list:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ParseError(f"cannot read {label}: {exc}") from exc
    if not isinstance(data, list):
        raise ParseError(f"{label} is not a JSON array")
    return data


def parse_gps_json(path: Path) -> tuple[list[TrackPoint], list[str]]:
    points: list[TrackPoint] = []
    warnings: list[str] = []
    for index, row in enumerate(_load_array(path, "GPS JSON")):
        if not isinstance(row, dict):
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
    values: list[TimedValue] = []
    warnings: list[str] = []
    for index, row in enumerate(_load_array(path, "heart-rate JSON")):
        if not isinstance(row, dict):
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
    values: list[TimedValue] = []
    warnings: list[str] = []
    for index, row in enumerate(_load_array(path, "elevation JSON")):
        if not isinstance(row, dict):
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
