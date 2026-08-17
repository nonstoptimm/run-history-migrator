from __future__ import annotations

import csv
import xml.etree.ElementTree as ET
from datetime import date

import pytest

from adidas_to_strava.cli import main
from adidas_to_strava.converter import convert
from adidas_to_strava.filters import DateRange
from adidas_to_strava.inspect_export import inspect_export
from adidas_to_strava.parsers import index_companions, parse_session
from adidas_to_strava.tcx import TCX_NS, output_filename, utc_timestamp, validate_tcx

from .helpers import make_export, write_gps, write_session


def test_epoch_ms_utc_and_local_calendar_date(tmp_path) -> None:
    _, sessions = make_export(tmp_path)
    session = parse_session(
        write_session(
            sessions,
            "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            start_ms=1_672_527_600_000,
            offset_ms=7_200_000,
        )
    )
    assert utc_timestamp(0) == "1970-01-01T00:00:00.000Z"
    assert session.local_start_date == date(2023, 1, 1)
    assert output_filename(session) == "2023-01-01_010000_1.00km_aaaaaaaa.tcx"


def test_inclusive_since_until_and_no_default_filter(tmp_path) -> None:
    export, sessions = make_export(tmp_path)
    for session_id, start_ms in (
        ("before", 1_672_444_800_000),
        ("first", 1_672_531_200_000),
        ("last", 1_672_617_600_000),
        ("after", 1_672_704_000_000),
    ):
        write_session(sessions, session_id, start_ms=start_ms)
        write_gps(sessions, session_id, start_ms=start_ms)
    bounded = convert(
        export,
        tmp_path / "bounded",
        date(2023, 1, 1),
        date(2023, 1, 2),
        "running",
        dry_run=True,
    )
    unbounded = convert(export, tmp_path / "all", None, None, "running", dry_run=True)
    assert bounded.eligible_runs == 2
    assert bounded.before_since == 1
    assert bounded.after_until == 1
    assert unbounded.eligible_runs == 4


def test_invalid_date_range() -> None:
    with pytest.raises(ValueError, match="--since"):
        DateRange(date(2024, 1, 2), date(2024, 1, 1))


def test_cli_rejects_invalid_date_range(tmp_path) -> None:
    export, _ = make_export(tmp_path)
    with pytest.raises(SystemExit):
        main(
            [
                "inspect",
                str(export),
                "--since",
                "2024-01-02",
                "--until",
                "2024-01-01",
            ]
        )


def test_running_filter_uuid_matching_and_malformed_input(tmp_path) -> None:
    export, sessions = make_export(tmp_path)
    write_session(sessions, "running")
    gps = write_gps(sessions, "running")
    write_session(sessions, "cycling", sport_id="2")
    write_gps(sessions, "cycling")
    write_session(sessions, "broken", malformed=True)
    summary = convert(export, tmp_path / "out", None, None, "running", dry_run=True)
    assert index_companions(sessions)["running"].gps_json == gps
    assert summary.eligible_runs == 1
    assert summary.non_sport == 1
    assert summary.malformed_sessions == 1


def test_tcx_missing_optional_streams_manifest_and_duplicates(tmp_path) -> None:
    export, sessions = make_export(tmp_path)
    session_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    write_session(sessions, session_id)
    write_gps(sessions, session_id)
    output = tmp_path / "out"
    first = convert(export, output, None, None, "running")
    second = convert(export, output, None, None, "running")
    assert first.converted == 1
    assert first.missing_hr == 1
    assert first.missing_elevation == 1
    assert second.skipped_duplicate == 1
    tcx = next(output.glob("*.tcx"))
    validate_tcx(tcx)
    root = ET.parse(tcx).getroot()
    ns = {"tcx": TCX_NS}
    assert len(root.findall(".//tcx:Trackpoint", ns)) == 2
    with (output / "manifest.csv").open(newline="", encoding="utf-8") as handle:
        row = next(csv.DictReader(handle))
    assert row["session_id"] == session_id
    assert row["status"] == "converted"
    assert row["local_start_date"] == "2023-01-01"


def test_missing_gps_dry_run_and_inspect_are_read_only(tmp_path) -> None:
    export, sessions = make_export(tmp_path)
    write_session(sessions, "no-gps")
    output = tmp_path / "not-created"
    converted = convert(export, output, None, None, "running", dry_run=True)
    inspected = inspect_export(export, None, None, "running")
    assert converted.skipped_no_gps == 1
    assert inspected.eligible == 1
    assert inspected.with_gps == 0
    assert not output.exists()


def test_validate_tcx_rejects_malformed_xml(tmp_path) -> None:
    path = tmp_path / "broken.tcx"
    path.write_text("<broken>", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid TCX"):
        validate_tcx(path)
