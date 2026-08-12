from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobStore:
    def __init__(self, database_path: str | Path):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def ensure_schema(self) -> None:
        with closing(self._connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id TEXT NOT NULL,
                    batch_index INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    draft_json TEXT NOT NULL,
                    selected_template TEXT NOT NULL,
                    selected_printer TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS session_overrides (
                    session_id TEXT NOT NULL,
                    job_id INTEGER NOT NULL,
                    data_json TEXT NOT NULL,
                    selected_template TEXT NOT NULL,
                    selected_printer TEXT NOT NULL,
                    preview_html TEXT,
                    render_error TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (session_id, job_id),
                    FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE
                );
                """
            )

    def create_job(
        self,
        *,
        batch_id: str,
        batch_index: int,
        payload: dict[str, Any],
        draft: dict[str, Any],
        selected_template: str,
        selected_printer: str,
        status: str = "queued",
    ) -> int:
        now = _utc_now()
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                INSERT INTO jobs (
                    batch_id,
                    batch_index,
                    payload_json,
                    draft_json,
                    selected_template,
                    selected_printer,
                    status,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    batch_id,
                    batch_index,
                    json.dumps(payload, ensure_ascii=False),
                    json.dumps(draft, ensure_ascii=False),
                    selected_template,
                    selected_printer,
                    status,
                    now,
                    now,
                ),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def list_jobs(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM jobs
                ORDER BY created_at DESC, id DESC
                """
            ).fetchall()
        return [self._decode_job(row) for row in rows]

    def list_session_overrides(self, session_id: str) -> dict[int, dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM session_overrides
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchall()
        return {
            int(row["job_id"]): self._decode_session_override(row)
            for row in rows
        }

    def get_job(self, job_id: int) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        return self._decode_job(row) if row is not None else None

    def save_session_override(
        self,
        *,
        session_id: str,
        job_id: int,
        data: dict[str, Any],
        selected_template: str,
        selected_printer: str,
        preview_data_url: str | None = None,
        render_error: str | None = None,
    ) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO session_overrides (
                    session_id,
                    job_id,
                    data_json,
                    selected_template,
                    selected_printer,
                    preview_html,
                    render_error,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id, job_id) DO UPDATE SET
                    data_json = excluded.data_json,
                    selected_template = excluded.selected_template,
                    selected_printer = excluded.selected_printer,
                    preview_html = excluded.preview_html,
                    render_error = excluded.render_error,
                    updated_at = excluded.updated_at
                """,
                (
                    session_id,
                    job_id,
                    json.dumps(data, ensure_ascii=False),
                    selected_template,
                    selected_printer,
                    preview_data_url,
                    render_error,
                    _utc_now(),
                ),
            )
            connection.commit()

    def get_session_override(
        self,
        session_id: str,
        job_id: int,
    ) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT *
                FROM session_overrides
                WHERE session_id = ? AND job_id = ?
                """,
                (session_id, job_id),
            ).fetchone()
        return self._decode_session_override(row) if row is not None else None

    def clear_session_override(self, session_id: str, job_id: int) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                """
                DELETE FROM session_overrides
                WHERE session_id = ? AND job_id = ?
                """,
                (session_id, job_id),
            )
            connection.commit()

    @staticmethod
    def _decode_job(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "batch_id": row["batch_id"],
            "batch_index": row["batch_index"],
            "payload": json.loads(row["payload_json"]),
            "draft": json.loads(row["draft_json"]),
            "selected_template": row["selected_template"],
            "selected_printer": row["selected_printer"],
            "status": row["status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _decode_session_override(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "session_id": row["session_id"],
            "job_id": row["job_id"],
            "data": json.loads(row["data_json"]),
            "selected_template": row["selected_template"],
            "selected_printer": row["selected_printer"],
            "preview_data_url": row["preview_html"],
            "render_error": row["render_error"],
            "updated_at": row["updated_at"],
        }
