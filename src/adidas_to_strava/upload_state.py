"""Transactional SQLite persistence for resumable Strava uploads."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from .models import UploadRecord


def _now() -> str:
    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")


class UploadStateStore:
    """Persist one upload lifecycle per canonical adidas session UUID."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def initialize(self) -> None:
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
        if not self.path.exists():
            return None
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM uploads WHERE session_id = ?", (session_id,)
            ).fetchone()
        return self._record(row) if row else None

    def begin_attempt(self, session_id: str, source_tcx: Path, *, force: bool = False) -> None:
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
        self._update(
            session_id,
            """
            status = 'submitted', strava_upload_id = ?,
            submitted_timestamp = ?, error_warning = ''
            """,
            (upload_id, _now()),
        )

    def mark_processing(self, session_id: str, status: str) -> None:
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
        self._update(
            session_id,
            """
            status = 'completed', strava_activity_id = ?,
            completed_timestamp = ?, error_warning = ?
            """,
            (activity_id, _now(), warning[:2000]),
        )

    def mark_failed(self, session_id: str, error: str) -> None:
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
