"""Transactional SQLite persistence for resumable Strava uploads."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from .models import UploadRecord


def _now() -> str:
    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")


class UploadStateStore:
    """Persist one upload lifecycle per canonical adidas session UUID.

    The backing SQLite database is created lazily: read-only methods such
    as `get` tolerate a missing database file, while any method that
    writes calls `initialize` first so the schema always exists before use.

    Attributes:
        path: The SQLite database file this store reads from and writes
            to.
    """

    def __init__(self, path: Path) -> None:
        """Initialize the store without touching the filesystem yet.

        Args:
            path: The SQLite database file to use, created on first write.
        """
        self.path = path

    def initialize(self) -> None:
        """Create the `uploads` table and parent directory if missing.

        Safe to call repeatedly: the table is created only if it does not
        already exist, so existing rows and their `status` values are
        never reset.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS uploads (
                    session_id TEXT PRIMARY KEY,
                    source_tcx TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    attempt_timestamp TEXT,
                    submitted_timestamp TEXT,
                    completed_timestamp TEXT,
                    strava_upload_id INTEGER,
                    strava_activity_id INTEGER,
                    error_warning TEXT NOT NULL DEFAULT ''
                )
                """
            )

    def get(self, session_id: str) -> UploadRecord | None:
        """Return the persisted upload record for one adidas session.

        Args:
            session_id: The canonical adidas session UUID to look up.

        Returns:
            The stored record, or ``None`` if the database file does not
            exist yet or has no row for `session_id` (for example, in a
            dry run, which never creates the database).
        """
        if not self.path.exists():
            return None
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM uploads WHERE session_id = ?", (session_id,)
            ).fetchone()
        return self._record(row) if row else None

    def begin_attempt(self, session_id: str, source_tcx: Path, *, force: bool = False) -> None:
        """Record the start of a submission attempt with status ``submitting``.

        Args:
            session_id: The canonical adidas session UUID being submitted.
            source_tcx: The TCX file being submitted in this attempt.
            force: Whether this attempt should resubmit a previously
                completed session. When ``True``, or when no row exists
                yet, all Strava identifiers and timestamps from any prior
                attempt are cleared as part of starting fresh; when
                ``False`` and a row already exists, only the source file,
                status, attempt count, and timestamp are updated, leaving
                any previously recorded Strava upload ID in place so a
                genuinely interrupted (not forced) resubmission can still
                resume.
        """
        self.initialize()
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT attempt_count FROM uploads WHERE session_id = ?", (session_id,)
            ).fetchone()
            attempts = int(existing["attempt_count"]) + 1 if existing else 1
            if existing and not force:
                connection.execute(
                    """
                    UPDATE uploads
                    SET source_tcx = ?, status = 'submitting', attempt_count = ?,
                        attempt_timestamp = ?, error_warning = ''
                    WHERE session_id = ?
                    """,
                    (str(source_tcx), attempts, _now(), session_id),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO uploads (
                        session_id, source_tcx, status, attempt_count, attempt_timestamp
                    ) VALUES (?, ?, 'submitting', ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        source_tcx = excluded.source_tcx,
                        status = 'submitting',
                        attempt_count = excluded.attempt_count,
                        attempt_timestamp = excluded.attempt_timestamp,
                        submitted_timestamp = NULL,
                        completed_timestamp = NULL,
                        strava_upload_id = NULL,
                        strava_activity_id = NULL,
                        error_warning = ''
                    """,
                    (session_id, str(source_tcx), attempts, _now()),
                )

    def mark_submitted(self, session_id: str, upload_id: int) -> None:
        """Record a successful submission with status ``submitted``.

        Args:
            session_id: The canonical adidas session UUID that was
                submitted.
            upload_id: Strava's upload ID, stored so processing can be
                resumed later with `get`.
        """
        self._update(
            session_id,
            """
            status = 'submitted', strava_upload_id = ?,
            submitted_timestamp = ?, error_warning = ''
            """,
            (upload_id, _now()),
        )

    def mark_processing(self, session_id: str, status: str) -> None:
        """Record an in-progress poll result with status ``processing``.

        Args:
            session_id: The canonical adidas session UUID being polled.
            status: Strava's latest status text, truncated to 1000
                characters and stored for diagnostics.
        """
        self._update(
            session_id,
            "status = 'processing', error_warning = ?",
            (status[:1000],),
        )

    def mark_completed(
        self,
        session_id: str,
        activity_id: int,
        warning: str = "",
    ) -> None:
        """Record a finished upload with status ``completed``.

        Used both for a genuinely new activity and for a recognized
        duplicate, where `activity_id` is the pre-existing activity and
        `warning` carries Strava's duplicate-detection message.

        Args:
            session_id: The canonical adidas session UUID that finished.
            activity_id: The resulting (or pre-existing, for a duplicate)
                Strava activity ID.
            warning: An optional diagnostic message, truncated to 2000
                characters and stored alongside the completed status.
        """
        self._update(
            session_id,
            """
            status = 'completed', strava_activity_id = ?,
            completed_timestamp = ?, error_warning = ?
            """,
            (activity_id, _now(), warning[:2000]),
        )

    def mark_failed(self, session_id: str, error: str) -> None:
        """Record a terminal failure with status ``failed``.

        Args:
            session_id: The canonical adidas session UUID that failed.
            error: The failure message, truncated to 2000 characters.
        """
        self._update(
            session_id,
            "status = 'failed', error_warning = ?",
            (error[:2000],),
        )

    def _update(self, session_id: str, assignments: str, values: tuple[object, ...]) -> None:
        self.initialize()
        with self._connect() as connection:
            connection.execute(
                f"UPDATE uploads SET {assignments} WHERE session_id = ?",
                (*values, session_id),
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _record(row: sqlite3.Row) -> UploadRecord:
        return UploadRecord(
            session_id=row["session_id"],
            source_tcx=row["source_tcx"],
            status=row["status"],
            attempt_count=row["attempt_count"],
            attempt_timestamp=row["attempt_timestamp"],
            submitted_timestamp=row["submitted_timestamp"],
            completed_timestamp=row["completed_timestamp"],
            strava_upload_id=row["strava_upload_id"],
            strava_activity_id=row["strava_activity_id"],
            error_warning=row["error_warning"],
        )
