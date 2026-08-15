from __future__ import annotations

import csv
import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

from adidas_to_strava.converter import convert, since_epoch_ms
from adidas_to_strava.parsers import index_companions, parse_session
from adidas_to_strava.tcx import TCX_NS, output_filename, utc_timestamp


class ConverterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.export = self.root / "export"
        self.sessions = self.export / "Sport-sessions"
        for directory in ("GPS-data", "Heart-rate-data", "Elevation-data"):
            (self.sessions / directory).mkdir(parents=True)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_session(
        self,
        session_id: str = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        start_ms: int = 1_672_531_200_000,
        sport_id: str = "1",
    ) -> Path:
        data = {
            "id": session_id,
            "start_time": start_ms,
            "start_time_timezone_offset": 7_200_000,
            "end_time": start_ms + 10_000,
            "duration": 10_000,
            "pause": 0,
            "calories": 100,
            "sport_type_id": sport_id,
            "features": [
                {"type": "track_metrics", "attributes": {"distance": 1000}},
                {
                    "type": "initial_values",
                    "attributes": {
                        "sport_type": {"id": sport_id},
                        "distance": 1000,
                        "duration": 10_000,
                        "start_time": start_ms,
                    },
                },
            ],
        }
        path = self.sessions / f"2023-01-01_00-00-00-UTC_{session_id}.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def write_gps(self, session_id: str, start_ms: int = 1_672_531_200_000) -> Path:
        rows = [
            {
                "timestamp": start_ms,
                "latitude": 48.1,
                "longitude": 11.5,
                "altitude": 500,
                "distance": 0,
            },
            {
                "timestamp": start_ms + 10_000,
                "latitude": 48.2,
                "longitude": 11.6,
                "altitude": 501,
                "distance": 1000,
            },
        ]
        path = self.sessions / "GPS-data" / f"wrong-prefix_{session_id}.json"
        path.write_text(json.dumps(rows), encoding="utf-8")
        return path

    def test_epoch_ms_to_utc_timestamp(self) -> None:
        self.assertEqual(utc_timestamp(0), "1970-01-01T00:00:00.000Z")
        self.assertEqual(since_epoch_ms(date(2023, 1, 1)), 1_672_531_200_000)
        session = parse_session(self.write_session())
        self.assertEqual(
            output_filename(session),
            "2023-01-01_020000_1.00km_aaaaaaaa.tcx",
        )

    def test_filtering_before_2023(self) -> None:
        session_id = "before"
        self.write_session(session_id, 1_672_531_199_999)
        self.write_gps(session_id, 1_672_531_199_999)
        summary = convert(self.export, self.root / "out", date(2023, 1, 1), "running")
        self.assertEqual(summary.before_since, 1)
        self.assertEqual(summary.converted, 0)

    def test_running_sport_filter(self) -> None:
        session_id = "cycling"
        self.write_session(session_id, sport_id="2")
        self.write_gps(session_id)
        summary = convert(self.export, self.root / "out", date(2023, 1, 1), "running")
        self.assertEqual(summary.non_sport, 1)
        self.assertEqual(summary.converted, 0)

    def test_uuid_companion_matching_ignores_prefix(self) -> None:
        session_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        self.write_session(session_id)
        gps = self.write_gps(session_id)
        self.assertEqual(index_companions(self.sessions)[session_id].gps_json, gps)

    def test_tcx_generation_and_missing_optional_streams(self) -> None:
        session_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        self.write_session(session_id)
        self.write_gps(session_id)
        output = self.root / "out"
        summary = convert(self.export, output, date(2023, 1, 1), "running")
        self.assertEqual(summary.converted, 1)
        self.assertEqual(summary.missing_hr, 1)
        self.assertEqual(summary.missing_elevation, 1)
        tcx = next(output.glob("*.tcx"))
        root = ET.parse(tcx).getroot()
        ns = {"tcx": TCX_NS}
        activity = root.find(".//tcx:Activity", ns)
        self.assertEqual(activity.attrib["Sport"], "Running")
        self.assertEqual(len(root.findall(".//tcx:Trackpoint", ns)), 2)
        self.assertEqual(
            root.findtext(".//tcx:Lap/tcx:DistanceMeters", namespaces=ns), "1000.000"
        )
        with (output / "manifest.csv").open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(rows[0]["session_id"], session_id)
        self.assertEqual(rows[0]["status"], "converted")

    def test_duplicate_and_overwrite_behavior(self) -> None:
        session_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        self.write_session(session_id)
        self.write_gps(session_id)
        output = self.root / "out"
        first = convert(self.export, output, date(2023, 1, 1), "running")
        second = convert(self.export, output, date(2023, 1, 1), "running")
        third = convert(
            self.export, output, date(2023, 1, 1), "running", overwrite=True
        )
        self.assertEqual(first.converted, 1)
        self.assertEqual(second.skipped_duplicate, 1)
        self.assertEqual(third.converted, 1)
        self.assertEqual(len(list(output.glob("*.tcx"))), 1)

    def test_no_gps_is_skipped(self) -> None:
        self.write_session("no-gps")
        summary = convert(self.export, self.root / "out", date(2023, 1, 1), "running")
        self.assertEqual(summary.skipped_no_gps, 1)
        self.assertFalse(list((self.root / "out").glob("*.tcx")))

    def test_dry_run_has_no_side_effects(self) -> None:
        session_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        self.write_session(session_id)
        self.write_gps(session_id)
        output = self.root / "does-not-exist"
        summary = convert(
            self.export, output, date(2023, 1, 1), "running", dry_run=True
        )
        self.assertEqual(summary.eligible_runs, 1)
        self.assertEqual(summary.converted, 0)
        self.assertFalse(output.exists())

    def test_initial_values_fallback(self) -> None:
        path = self.write_session()
        data = json.loads(path.read_text())
        del data["sport_type_id"]
        del data["start_time"]
        path.write_text(json.dumps(data))
        session = parse_session(path)
        self.assertEqual(session.sport_type_id, "1")
        self.assertEqual(session.start_time_ms, 1_672_531_200_000)


if __name__ == "__main__":
    unittest.main()
