"""Safe, resumable orchestration for Strava TCX uploads."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .filters import DateRange, select_activities
from .manifest import load_upload_candidates
from .models import UploadCandidate
from .strava_client import StravaAPIError, StravaClient, StravaRateLimitError, UploadStatus
from .tcx import validate_tcx
from .upload_state import UploadStateStore

DUPLICATE_ACTIVITY_PATTERN = re.compile(r"/activities/(\d+)")


@dataclass
class UploadSummary:
    selected: int = 0
    validated: int = 0
    dry_run: int = 0
    submitted: int = 0
    completed: int = 0
    skipped_completed: int = 0
    failed: int = 0


def upload_activities(
    output_dir: Path,
    *,
    client: StravaClient | None,
    since: date | None,
    until: date | None,
    sport: str,
    limit: int | None,
    dry_run: bool,
    force: bool,
    poll_interval: float = 3.0,
    max_polls: int = 100,
    sleep: Callable[[float], None] = time.sleep,
    progress: Callable[[str], None] = print,
) -> UploadSummary:
    """Validate, submit, poll, and persist selected converted activities."""
    eligible = select_activities(
        load_upload_candidates(output_dir),
        DateRange(since, until),
        sport,
    )
    store = UploadStateStore(output_dir / "upload-state.sqlite3")
    if not dry_run:
        store.initialize()
    candidates: list[UploadCandidate] = []
    skipped_completed = 0
    for candidate in eligible:
        record = store.get(candidate.session_id)
        duplicate_activity_id = _duplicate_activity_id(record.error_warning if record else "")
        if record and duplicate_activity_id and not force:
            if not dry_run:
                store.mark_completed(
                    candidate.session_id,
                    duplicate_activity_id,
                    record.error_warning,
                )
            skipped_completed += 1
            continue
        if record and record.status == "completed" and not force:
            skipped_completed += 1
            continue
        candidates.append(candidate)
        if limit is not None and len(candidates) >= limit:
            break
    summary = UploadSummary(
        selected=len(candidates),
        skipped_completed=skipped_completed,
    )
    for index, candidate in enumerate(candidates, start=1):
        prefix = f"[{index}/{len(candidates)}] {candidate.local_start_date} "
        prefix += f"{candidate.distance_m / 1000:.2f} km"
        record = store.get(candidate.session_id)
        try:
            validate_tcx(candidate.output_tcx)
            summary.validated += 1
        except ValueError as exc:
            summary.failed += 1
            progress(f"{prefix}\n  failed: {exc}")
            if not dry_run:
                store.begin_attempt(candidate.session_id, candidate.output_tcx, force=force)
                store.mark_failed(candidate.session_id, str(exc))
            continue
        if dry_run:
            summary.dry_run += 1
            progress(f"{prefix}\n  dry-run: ready ({candidate.session_id})")
            continue
        if client is None:
            raise ValueError("a Strava client is required for a real upload")
        try:
            status = _submit_or_resume(candidate, record, client, store, force, prefix, progress)
            if status.activity_id is not None:
                summary.completed += 1
                continue
            summary.submitted += 1
            final = _poll(
                candidate,
                status,
                client,
                store,
                prefix,
                progress,
                poll_interval,
                max_polls,
                sleep,
            )
            if final.activity_id is not None:
                summary.completed += 1
            else:
                summary.failed += 1
        except StravaRateLimitError:
            raise
        except StravaAPIError as exc:
            summary.failed += 1
            latest = store.get(candidate.session_id)
            if latest and latest.status == "failed":
                pass
            elif latest and latest.strava_upload_id:
                store.mark_processing(candidate.session_id, str(exc))
            else:
                store.mark_failed(candidate.session_id, str(exc))
            progress(f"{prefix}\n  failed: {exc}")
    return summary


def _submit_or_resume(
    candidate: UploadCandidate,
    record,
    client: StravaClient,
    store: UploadStateStore,
    force: bool,
    prefix: str,
    progress: Callable[[str], None],
) -> UploadStatus:
    if (
        record
        and record.strava_upload_id
        and record.status in {"submitted", "processing"}
        and not force
    ):
        progress(f"{prefix}\n  resuming upload {record.strava_upload_id}")
        return client.get_upload_status(record.strava_upload_id)
    store.begin_attempt(candidate.session_id, candidate.output_tcx, force=force)
    status = client.submit_upload(
        candidate.output_tcx,
        external_id=f"adidas-{candidate.session_id}.tcx",
    )
    store.mark_submitted(candidate.session_id, status.upload_id)
    progress(f"{prefix}\n  submitted -> upload {status.upload_id}")
    if status.error:
        duplicate_activity_id = _duplicate_activity_id(status.error)
        if duplicate_activity_id:
            store.mark_completed(
                candidate.session_id,
                duplicate_activity_id,
                status.error,
            )
            progress(f"  already exists as activity {duplicate_activity_id}")
            return UploadStatus(
                status.upload_id,
                status.status,
                None,
                duplicate_activity_id,
            )
        store.mark_failed(candidate.session_id, status.error)
        raise StravaAPIError(status.error)
    if status.activity_id is not None:
        store.mark_completed(candidate.session_id, status.activity_id)
        progress(f"  activity {status.activity_id}")
    return status


def _poll(
    candidate: UploadCandidate,
    initial: UploadStatus,
    client: StravaClient,
    store: UploadStateStore,
    prefix: str,
    progress: Callable[[str], None],
    poll_interval: float,
    max_polls: int,
    sleep: Callable[[float], None],
) -> UploadStatus:
    status = initial
    for poll_number in range(max_polls):
        if status.error:
            duplicate_activity_id = _duplicate_activity_id(status.error)
            if duplicate_activity_id:
                store.mark_completed(
                    candidate.session_id,
                    duplicate_activity_id,
                    status.error,
                )
                progress(f"  already exists as activity {duplicate_activity_id}")
                return UploadStatus(
                    status.upload_id,
                    status.status,
                    None,
                    duplicate_activity_id,
                )
            store.mark_failed(candidate.session_id, status.error)
            progress(f"  failed: {status.error}")
            return status
        if status.activity_id is not None:
            store.mark_completed(candidate.session_id, status.activity_id)
            progress(f"  activity {status.activity_id}")
            return status
        store.mark_processing(candidate.session_id, status.status or "processing")
        if poll_number == 0:
            progress("  processing...")
        sleep(poll_interval)
        status = client.get_upload_status(status.upload_id)
    message = f"upload {status.upload_id} did not finish within the polling timeout"
    store.mark_processing(candidate.session_id, message)
    progress(f"{prefix}\n  failed: {message}")
    return UploadStatus(status.upload_id, status.status, message, None)


def _duplicate_activity_id(error: str) -> int | None:
    """Extract the existing Strava activity ID from a duplicate-upload error."""
    if "duplicate" not in error.lower():
        return None
    match = DUPLICATE_ACTIVITY_PATTERN.search(error)
    return int(match.group(1)) if match else None


def format_upload_summary(summary: UploadSummary) -> str:
    """Render upload totals."""
    return "\n".join(
        (
            f"Selected: {summary.selected}",
            f"Validated: {summary.validated}",
            f"Dry-run ready: {summary.dry_run}",
            f"Submitted: {summary.submitted}",
            f"Completed: {summary.completed}",
            f"Skipped completed: {summary.skipped_completed}",
            f"Failed: {summary.failed}",
        )
    )
