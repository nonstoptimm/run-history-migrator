"""Garmin TCX v2 XML generation, atomic writing, and structural validation.

`validate_tcx` checks structural sanity (a Running activity with an
activity ID/start time and at least one trackpoint); it does not perform
XSD schema validation against the TCX v2 schema referenced by
`SCHEMA_LOCATION`.
"""

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
    """Convert a Unix epoch-millisecond timestamp to a TCX/ISO 8601 UTC string.

    Args:
        epoch_ms: Unix epoch time in milliseconds (UTC).

    Returns:
        The instant as an ISO 8601 string with millisecond precision and
        a `Z` UTC suffix, for example `"1970-01-01T00:00:00.000Z"`.
    """
    return (
        datetime.fromtimestamp(epoch_ms / 1000, tz=UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def output_filename(session: Session) -> str:
    """Build the local, human-readable TCX output filename for a session.

    The timestamp portion reflects adidas' recorded local wall-clock time
    (`start_time_ms` shifted by `start_timezone_offset_ms`), formatted
    using a UTC `datetime` purely as a formatting convenience -- it is
    local wall-clock time, not a true UTC instant. This differs from
    `Session.local_start_datetime`, which returns a properly
    timezone-aware datetime; both represent the same wall-clock numbers.

    Args:
        session: The session to name an output file for.

    Returns:
        A filename of the form
        `<YYYY-MM-DD>_<HHMMSS>_<distance_km>km_<session_id[:8]>.tcx`.
    """
    start = datetime.fromtimestamp(
        (session.start_time_ms + session.start_timezone_offset_ms) / 1000, tz=UTC
    )
    distance_km = session.distance_m / 1000
    return f"{start:%Y-%m-%d_%H%M%S}_{distance_km:.2f}km_{session.session_id[:8]}.tcx"


def build_tcx(session: Session, points: list[TrackPoint]) -> ET.ElementTree:
    """Build an in-memory TCX v2 XML document for one session and its track points.

    Produces a single `Activities/Activity` element hard-coded to
    `Sport="Running"`, containing one `Lap` (spanning the whole session,
    with `Intensity="Active"` and `TriggerMethod="Manual"`) and one
    `Track` with one `Trackpoint` per entry in `points`, in the given
    order. Optional per-point fields (`AltitudeMeters`, `DistanceMeters`,
    `HeartRateBpm`) are omitted when the corresponding `TrackPoint` field
    is `None`. All timestamps are written via `utc_timestamp` (UTC).

    Args:
        session: The parsed session to describe.
        points: Track points to include, in the order they should appear.

    Returns:
        The built `ElementTree`, not yet written to disk.
    """
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
    """Sanity-check a generated TCX file and return its timezone-aware activity start.

    This performs structural checks only -- well-formed XML, a `Running`
    `Activity` with a non-empty `Id`, and at least one `Trackpoint` -- and
    does not validate the document against the TCX v2 XSD schema.

    Args:
        path: Path to the TCX file to check.

    Returns:
        The activity's start time parsed from `Id`, as a timezone-aware
        `datetime` (defaulting to UTC if the parsed value has no offset).

    Raises:
        ValueError: If the file is not well-formed XML, has no `Running`
            activity, has no activity ID, has no trackpoints, or the
            activity ID is not a valid ISO 8601 timestamp.
    """
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
    """Atomically write a TCX document to `destination`.

    The tree is serialized to a temporary sibling file in `destination`'s
    parent directory and moved into place with `os.replace`, so readers
    never observe a partially written file; the temporary file is removed
    if writing fails.

    Args:
        tree: The TCX document to write, as built by `build_tcx`.
        destination: Final path to write the TCX file to. Parent
            directories are created if needed.
    """
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
