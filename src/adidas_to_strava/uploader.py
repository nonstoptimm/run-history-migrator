"""Safe, resumable orchestration for Strava TCX uploads."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Protocol

from .filters import DateRange, select_activities
from .manifest import load_upload_candidates
from .models import UploadCandidate, UploadRecord
from .strava_client import StravaAPIError, StravaRateLimitError, UploadStatus
from .tcx import validate_tcx
from .upload_state import UploadStateStore

DUPLICATE_ACTIVITY_PATTERN = re.compile(r"/activities/(\d+)")


class UploadClient(Protocol):
    """The two asynchronous upload operations `upload_activities` needs.

    `StravaClient` satisfies this protocol directly; tests may supply a
    minimal fake implementing only these two methods instead of a full
    Strava client.
    """

    def submit_upload(self, tcx_path: Path, *, external_id: str) -> UploadStatus:
        """Submit one TCX file and return Strava's asynchronous upload record."""
        ...

    def get_upload_status(self, upload_id: int) -> UploadStatus:
        """Retrieve the current asynchronous upload-processing state."""
        ...


@dataclass
class UploadSummary:
    """Aggregate counts for one `upload_activities` run.

    Attributes:
        selected: Activities chosen for this run after filtering and
            skipping already-completed sessions (subject to `limit`).
        validated: Selected activities whose TCX file passed validation.
        dry_run: Validated activities that were reported as upload-ready
            without contacting Strava, because `dry_run` was ``True``.
        submitted: Activities newly submitted to Strava in this run
            (excludes resumed submissions and dry runs).
        completed: Activities that reached a Strava activity ID, whether
            newly created, resumed, or reconciled as a duplicate.
        skipped_completed: Activities skipped because they were already
            marked completed (or recognizable duplicates) and `force` was
            not set.
        failed: Activities that failed TCX validation, failed to upload,
            or did not finish processing within `max_polls`.
    """

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
    client: UploadClient | None,
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
    """Validate, submit, poll, and persist selected converted activities.

    In a dry run, no SQLite database is created or written and `client` is
    never called: activities are only validated and reported as upload
    ready. Otherwise, each selected activity is validated, submitted (or
    resumed from a prior interrupted attempt), and polled until Strava
    finishes processing it or `max_polls` is reached; every state
    transition is persisted via `UploadStateStore` so a later run can
    resume or skip it.

    A session already marked completed, or whose last recorded error is a
    recognizable Strava duplicate-upload message, is skipped without
    calling Strava again unless `force` is ``True``. When `force` is
    ``True``, such sessions are resubmitted from scratch, which may create
    duplicate Strava activities.

    Args:
        output_dir: The conversion output directory containing
            `manifest.csv`, the TCX files it references, and (unless
            `dry_run`) the `upload-state.sqlite3` state database.
        client: The Strava client used to submit and poll uploads;
            required unless `dry_run` is ``True``.
        since: The earliest local calendar date to include, inclusive.
        until: The latest local calendar date to include, inclusive.
        sport: The adidas sport to select; see `filters.validate_sport`.
        limit: The maximum number of actionable (not already completed)
            activities to process, or ``None`` for no limit.
        dry_run: Whether to only validate and report readiness, without
            touching Strava or the state database.
        force: Whether to resubmit sessions already marked completed or
            recognized as duplicates, instead of skipping them.
        poll_interval: The number of seconds to `sleep` between polls of
            an in-progress upload.
        max_polls: The maximum number of status polls per activity before
            giving up and leaving it resumable.
        sleep: The delay function called between polls; injectable for
            tests.
        progress: The callback used to report per-activity progress lines.

    Returns:
        Aggregate counts for this run.

    Raises:
        ValueError: If a non-dry-run activity needs uploading but `client`
            is ``None``.
        StravaRateLimitError: If Strava's rate limit is exhausted; raised
            immediately so the caller can stop the whole run rather than
            continue burning through remaining activities.
    """
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
    record: UploadRecord | None,
    client: UploadClient,
    store: UploadStateStore,
    force: bool,
    prefix: str,
    progress: Callable[[str], None],
) -> UploadStatus:
    """Resume a previously submitted upload, or submit a new one.

    A prior `record` is resumed (polling its existing Strava upload ID
    instead of submitting again) only when it is still `"submitted"` or
    `"processing"` and `force` is ``False``; otherwise a new submission is
    started, which also handles a duplicate-upload error by reconciling it
    as completed rather than treating it as a failure.

    Args:
        candidate: The activity being submitted or resumed.
        record: The previously persisted state for this session, if any.
        client: The upload client used to submit or poll.
        store: The state store to update as progress is made.
        force: Whether to force a fresh submission instead of resuming.
        prefix: The per-activity line prefix used in `progress` messages.
        progress: The callback used to report progress lines.

    Returns:
        The resulting or resumed upload status.

    Raises:
        StravaAPIError: If Strava reports a non-duplicate error for a new
            submission.
    """
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
    client: UploadClient,
    store: UploadStateStore,
    prefix: str,
    progress: Callable[[str], None],
    poll_interval: float,
    max_polls: int,
    sleep: Callable[[float], None],
) -> UploadStatus:
    """Poll an in-progress upload until it finishes or `max_polls` is reached.

    Each poll's status is persisted as `"processing"` before sleeping, so
    an interruption mid-poll leaves the upload resumable by a later run.
    Reaching `max_polls` without a result is treated the same way: the
    activity is left `"processing"` (not `"failed"`) so it can be resumed.

    Args:
        candidate: The activity being polled.
        initial: The status returned by the initiating submission or
            resume, polled from here.
        client: The upload client used to poll.
        store: The state store to update as progress is made.
        prefix: The per-activity line prefix used in `progress` messages.
        progress: The callback used to report progress lines.
        poll_interval: The number of seconds to `sleep` between polls.
        max_polls: The maximum number of polls before giving up.
        sleep: The delay function called between polls.

    Returns:
        The final upload status: completed (possibly via duplicate
        reconciliation), failed, or still processing after `max_polls`.
    """
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
    """Render upload totals as CLI-visible, human-readable text.

    Args:
        summary: The counts to render.

    Returns:
        One label-and-count line per `UploadSummary` field, in a stable
        order, joined with newlines.
    """
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
