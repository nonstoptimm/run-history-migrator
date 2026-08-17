"""Standards-compliant TCX generation, atomic writing, and validation."""

from __future__ import annotations

import os
import tempfile
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

from .models import Session, TrackPoint

TCX_NS = "http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
SCHEMA_LOCATION = (
    "http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2 "
    "http://www.garmin.com/xmlschemas/TrainingCenterDatabasev2.xsd"
)

ET.register_namespace("", TCX_NS)
ET.register_namespace("xsi", XSI_NS)


def utc_timestamp(epoch_ms: int) -> str:
    return (
        datetime.fromtimestamp(epoch_ms / 1000, tz=UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def output_filename(session: Session) -> str:
    start = datetime.fromtimestamp(
        (session.start_time_ms + session.start_timezone_offset_ms) / 1000, tz=UTC
    )
    distance_km = session.distance_m / 1000
    return f"{start:%Y-%m-%d_%H%M%S}_{distance_km:.2f}km_{session.session_id[:8]}.tcx"


def build_tcx(session: Session, points: list[TrackPoint]) -> ET.ElementTree:
    root = ET.Element(
        f"{{{TCX_NS}}}TrainingCenterDatabase",
        {f"{{{XSI_NS}}}schemaLocation": SCHEMA_LOCATION},
    )
    activities = ET.SubElement(root, f"{{{TCX_NS}}}Activities")
    activity = ET.SubElement(activities, f"{{{TCX_NS}}}Activity", {"Sport": "Running"})
    ET.SubElement(activity, f"{{{TCX_NS}}}Id").text = utc_timestamp(session.start_time_ms)
    lap = ET.SubElement(
        activity,
        f"{{{TCX_NS}}}Lap",
        {"StartTime": utc_timestamp(session.start_time_ms)},
    )
    ET.SubElement(lap, f"{{{TCX_NS}}}TotalTimeSeconds").text = f"{session.duration_ms / 1000:.3f}"
    ET.SubElement(lap, f"{{{TCX_NS}}}DistanceMeters").text = f"{session.distance_m:.3f}"
    ET.SubElement(lap, f"{{{TCX_NS}}}Calories").text = str(session.calories)
    ET.SubElement(lap, f"{{{TCX_NS}}}Intensity").text = "Active"
    ET.SubElement(lap, f"{{{TCX_NS}}}TriggerMethod").text = "Manual"
    track = ET.SubElement(lap, f"{{{TCX_NS}}}Track")
    for point in points:
        trackpoint = ET.SubElement(track, f"{{{TCX_NS}}}Trackpoint")
        ET.SubElement(trackpoint, f"{{{TCX_NS}}}Time").text = utc_timestamp(point.timestamp_ms)
        position = ET.SubElement(trackpoint, f"{{{TCX_NS}}}Position")
        ET.SubElement(position, f"{{{TCX_NS}}}LatitudeDegrees").text = f"{point.latitude:.12g}"
        ET.SubElement(position, f"{{{TCX_NS}}}LongitudeDegrees").text = f"{point.longitude:.12g}"
        if point.altitude_m is not None:
            ET.SubElement(
                trackpoint, f"{{{TCX_NS}}}AltitudeMeters"
            ).text = f"{point.altitude_m:.3f}"
        if point.distance_m is not None:
            ET.SubElement(
                trackpoint, f"{{{TCX_NS}}}DistanceMeters"
            ).text = f"{point.distance_m:.3f}"
        if point.heart_rate is not None:
            heart_rate = ET.SubElement(
                trackpoint,
                f"{{{TCX_NS}}}HeartRateBpm",
                {f"{{{XSI_NS}}}type": "HeartRateInBeatsPerMinute_t"},
            )
            ET.SubElement(heart_rate, f"{{{TCX_NS}}}Value").text = str(point.heart_rate)
    ET.SubElement(
        activity, f"{{{TCX_NS}}}Notes"
    ).text = f"Converted from adidas Running session {session.session_id}"
    return ET.ElementTree(root)


def validate_tcx(path: Path) -> datetime:
    """Validate a generated TCX and return its timezone-aware activity start."""
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise ValueError(f"invalid TCX {path}: {exc}") from exc
    activity = root.find(f".//{{{TCX_NS}}}Activity")
    if activity is None or activity.get("Sport") != "Running":
        raise ValueError(f"TCX {path} does not contain a Running activity")
    activity_id = activity.findtext(f"{{{TCX_NS}}}Id")
    if not activity_id:
        raise ValueError(f"TCX {path} has no activity start time")
    if not activity.findall(f".//{{{TCX_NS}}}Trackpoint"):
        raise ValueError(f"TCX {path} has no trackpoints")
    try:
        parsed = datetime.fromisoformat(activity_id.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"TCX {path} has an invalid activity start time") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def write_tcx(tree: ET.ElementTree, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            tree.write(handle, encoding="utf-8", xml_declaration=True)
        os.replace(temporary_name, destination)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise
