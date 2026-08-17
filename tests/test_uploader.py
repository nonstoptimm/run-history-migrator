from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

from adidas_to_strava.converter import convert
from adidas_to_strava.strava_client import StravaAPIError, UploadStatus
from adidas_to_strava.upload_state import UploadStateStore
from adidas_to_strava.uploader import upload_activities

from .helpers import make_export, write_gps, write_session


class FakeUploadClient:
    def __init__(self, statuses: list[UploadStatus | StravaAPIError]) -> None:
        self.statuses = list(statuses)
        self.submissions = 0
        self.polls = 0

    def submit_upload(self, *args, **kwargs) -> UploadStatus:
        self.submissions += 1
        result = self.statuses.pop(0)
        if isinstance(result, StravaAPIError):
            raise result
        return result

    def get_upload_status(self, upload_id: int) -> UploadStatus:
        self.polls += 1
        result = self.statuses.pop(0)
        if isinstance(result, StravaAPIError):
            raise result
        return result


def converted_output(tmp_path: Path, count: int = 1) -> tuple[Path, list[str]]:
    export, sessions = make_export(tmp_path)
    session_ids = []
    for index in range(count):
        session_id = f"session-{index}"
        start_ms = 1_672_531_200_000 + index * 86_400_000
        write_session(sessions, session_id, start_ms=start_ms)
        write_gps(sessions, session_id, start_ms=start_ms)
        session_ids.append(session_id)
    output = tmp_path / "output"
    convert(export, output, None, None, "running")
    return output, session_ids


def test_dry_run_makes_zero_api_or_state_writes_and_limit_one(tmp_path) -> None:
    output, _ = converted_output(tmp_path, count=2)
    client = FakeUploadClient([])
    summary = upload_activities(
        output,
        client=client,
        since=None,
        until=None,
        sport="running",
        limit=1,
        dry_run=True,
        force=False,
        progress=lambda message: None,
    )
    assert summary.selected == 1
    assert summary.dry_run == 1
    assert client.submissions == 0
    assert client.polls == 0
    assert not (output / "upload-state.sqlite3").exists()


def test_successful_async_upload_persists_activity_id(tmp_path) -> None:
    output, session_ids = converted_output(tmp_path)
    client = FakeUploadClient(
        [
            UploadStatus(10, "processing", None, None),
            UploadStatus(10, "ready", None, 99),
        ]
    )
    summary = upload_activities(
        output,
        client=client,
        since=None,
        until=None,
        sport="running",
        limit=None,
        dry_run=False,
        force=False,
        sleep=lambda seconds: None,
        progress=lambda message: None,
    )
    record = UploadStateStore(output / "upload-state.sqlite3").get(session_ids[0])
    assert summary.completed == 1
    assert record is not None
    assert record.status == "completed"
    assert record.strava_upload_id == 10
    assert record.strava_activity_id == 99


def test_completed_duplicate_is_skipped(tmp_path) -> None:
    output, session_ids = converted_output(tmp_path)
    store = UploadStateStore(output / "upload-state.sqlite3")
    store.begin_attempt(session_ids[0], next(output.glob("*.tcx")))
    store.mark_submitted(session_ids[0], 10)
    store.mark_completed(session_ids[0], 99)
    client = FakeUploadClient([])
    summary = upload_activities(
        output,
        client=client,
        since=None,
        until=None,
        sport="running",
        limit=None,
        dry_run=False,
        force=False,
        progress=lambda message: None,
    )
    assert summary.skipped_completed == 1
    assert client.submissions == 0


def test_limit_counts_actionable_activities_after_completed_skip(tmp_path) -> None:
    output, session_ids = converted_output(tmp_path, count=2)
    store = UploadStateStore(output / "upload-state.sqlite3")
    first_tcx = sorted(output.glob("*.tcx"))[0]
    store.begin_attempt(session_ids[0], first_tcx)
    store.mark_submitted(session_ids[0], 10)
    store.mark_completed(session_ids[0], 99)
    summary = upload_activities(
        output,
        client=None,
        since=None,
        until=None,
        sport="running",
        limit=1,
        dry_run=True,
        force=False,
        progress=lambda message: None,
    )
    assert summary.selected == 1
    assert summary.skipped_completed == 1
    assert summary.dry_run == 1


def test_interrupted_submitted_upload_resumes_polling(tmp_path) -> None:
    output, session_ids = converted_output(tmp_path)
    store = UploadStateStore(output / "upload-state.sqlite3")
    store.begin_attempt(session_ids[0], next(output.glob("*.tcx")))
    store.mark_submitted(session_ids[0], 10)
    client = FakeUploadClient(
        [
            UploadStatus(10, "processing", None, None),
            UploadStatus(10, "ready", None, 99),
        ]
    )
    summary = upload_activities(
        output,
        client=client,
        since=None,
        until=None,
        sport="running",
        limit=None,
        dry_run=False,
        force=False,
        sleep=lambda seconds: None,
        progress=lambda message: None,
    )
    assert summary.completed == 1
    assert client.submissions == 0
    assert client.polls == 2


def test_poll_timeout_keeps_upload_resumable(tmp_path) -> None:
    output, session_ids = converted_output(tmp_path)
    client = FakeUploadClient(
        [
            UploadStatus(10, "processing", None, None),
            UploadStatus(10, "processing", None, None),
        ]
    )
    summary = upload_activities(
        output,
        client=client,
        since=None,
        until=None,
        sport="running",
        limit=None,
        dry_run=False,
        force=False,
        max_polls=1,
        sleep=lambda seconds: None,
        progress=lambda message: None,
    )
    record = UploadStateStore(output / "upload-state.sqlite3").get(session_ids[0])
    assert summary.failed == 1
    assert record is not None and record.status == "processing"
    assert record.strava_upload_id == 10


def test_poll_network_failure_keeps_upload_resumable(tmp_path) -> None:
    output, session_ids = converted_output(tmp_path)
    client = FakeUploadClient(
        [
            UploadStatus(10, "processing", None, None),
            StravaAPIError("temporary network failure"),
        ]
    )
    summary = upload_activities(
        output,
        client=client,
        since=None,
        until=None,
        sport="running",
        limit=None,
        dry_run=False,
        force=False,
        sleep=lambda seconds: None,
        progress=lambda message: None,
    )
    record = UploadStateStore(output / "upload-state.sqlite3").get(session_ids[0])
    assert summary.failed == 1
    assert record is not None and record.status == "processing"
    assert record.strava_upload_id == 10


def test_failed_upload_and_malformed_tcx_are_persisted(tmp_path) -> None:
    output, session_ids = converted_output(tmp_path)
    tcx = next(output.glob("*.tcx"))
    tcx.write_text("<broken>", encoding="utf-8")
    client = FakeUploadClient([])
    summary = upload_activities(
        output,
        client=client,
        since=None,
        until=None,
        sport="running",
        limit=None,
        dry_run=False,
        force=False,
        progress=lambda message: None,
    )
    record = UploadStateStore(output / "upload-state.sqlite3").get(session_ids[0])
    assert summary.failed == 1
    assert record is not None and record.status == "failed"


def test_strava_processing_failure_is_persisted(tmp_path) -> None:
    output, session_ids = converted_output(tmp_path)
    client = FakeUploadClient(
        [
            UploadStatus(
                10,
                "failed",
                "file duplicate of <a href='/activities/12345'>Run</a>",
                None,
            )
        ]
    )
    summary = upload_activities(
        output,
        client=client,
        since=None,
        until=None,
        sport="running",
        limit=None,
        dry_run=False,
        force=False,
        progress=lambda message: None,
    )
    record = UploadStateStore(output / "upload-state.sqlite3").get(session_ids[0])
    assert summary.completed == 1
    assert record is not None and record.status == "completed"
    assert record.strava_activity_id == 12345
    assert "duplicate" in record.error_warning


def test_previous_duplicate_failure_is_reconciled_without_api_call(tmp_path) -> None:
    output, session_ids = converted_output(tmp_path)
    tcx = next(output.glob("*.tcx"))
    store = UploadStateStore(output / "upload-state.sqlite3")
    store.begin_attempt(session_ids[0], tcx)
    store.mark_submitted(session_ids[0], 10)
    store.mark_failed(
        session_ids[0],
        "file duplicate of <a href='/activities/12345'>Run</a>",
    )
    client = FakeUploadClient([])
    summary = upload_activities(
        output,
        client=client,
        since=None,
        until=None,
        sport="running",
        limit=None,
        dry_run=False,
        force=False,
        progress=lambda message: None,
    )
    record = store.get(session_ids[0])
    assert summary.skipped_completed == 1
    assert client.submissions == 0
    assert client.polls == 0
    assert record is not None and record.strava_activity_id == 12345


def test_upload_date_filter_uses_activity_date(tmp_path) -> None:
    output, _ = converted_output(tmp_path, count=2)
    summary = upload_activities(
        output,
        client=None,
        since=date(2023, 1, 2),
        until=date(2023, 1, 2),
        sport="running",
        limit=None,
        dry_run=True,
        force=False,
        progress=lambda message: None,
    )
    assert summary.selected == 1


def test_current_manifest_format_is_backward_compatible(tmp_path, monkeypatch) -> None:
    output, _ = converted_output(tmp_path)
    manifest = output / "manifest.csv"
    with manifest.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    old_fields = [
        field for field in rows[0] if field not in {"local_start_date", "start_timezone_offset_ms"}
    ]
    with manifest.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=old_fields)
        writer.writeheader()
        writer.writerows([{field: row[field] for field in old_fields} for row in rows])
    monkeypatch.chdir(output.parent)
    summary = upload_activities(
        Path("output"),
        client=None,
        since=None,
        until=None,
        sport="running",
        limit=None,
        dry_run=True,
        force=False,
        progress=lambda message: None,
    )
    assert summary.selected == 1
