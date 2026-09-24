from __future__ import annotations

import sqlite3
from typing import Any

from app.repositories.base import Repository, row_dict, rows_dict


class ImportBatchRepository(Repository):
    table = "import_batches"
    entity_name = "导入批次"

    def by_key_and_hash(self, batch_key: str, content_hash: str) -> dict[str, Any] | None:
        return row_dict(self.connection.execute(
            "SELECT * FROM import_batches WHERE batch_key=? AND content_hash=?",
            (batch_key, content_hash),
        ).fetchone())

    def latest_by_submitter_and_hash(self, submitted_by: int, content_hash: str) -> dict[str, Any] | None:
        return row_dict(self.connection.execute(
            "SELECT * FROM import_batches WHERE submitted_by=? AND content_hash=? ORDER BY id DESC LIMIT 1",
            (submitted_by, content_hash),
        ).fetchone())

    def versions(self, batch_key: str) -> list[dict]:
        return rows_dict(self.connection.execute(
            "SELECT id,version,status,submitted_at,confirmed_at FROM import_batches WHERE batch_key=? ORDER BY version",
            (batch_key,),
        ).fetchall())

    def latest_version(self, batch_key: str) -> dict[str, Any] | None:
        return row_dict(self.connection.execute(
            "SELECT * FROM import_batches WHERE batch_key=? ORDER BY version DESC LIMIT 1",
            (batch_key,),
        ).fetchone())

    def list(self, *, status: str | None, limit: int, offset: int) -> list[dict]:
        where = " WHERE status=?" if status else ""
        params: tuple = (status, limit, offset) if status else (limit, offset)
        return rows_dict(self.connection.execute(
            "SELECT * FROM import_batches" + where + " ORDER BY id DESC LIMIT ? OFFSET ?", params
        ).fetchall())

    def pending_decisions(self, batch_id: int) -> int:
        return int(self.connection.execute(
            "SELECT COUNT(*) FROM import_rows WHERE batch_id=? AND status='conflict' AND resolution='pending'",
            (batch_id,),
        ).fetchone()[0])


class ImportRowRepository(Repository):
    table = "import_rows"
    entity_name = "导入行"

    def for_batch(self, batch_id: int) -> list[dict]:
        return rows_dict(self.connection.execute(
            "SELECT * FROM import_rows WHERE batch_id=? ORDER BY row_no", (batch_id,)
        ).fetchall())

    def list_for_batch(
        self,
        batch_id: int,
        *,
        status: str | None,
        resolution: str | None,
        limit: int,
        offset: int,
    ) -> list[dict]:
        conditions = ["batch_id=?"]
        params: list[Any] = [batch_id]
        if status:
            conditions.append("status=?")
            params.append(status)
        if resolution:
            conditions.append("resolution=?")
            params.append(resolution)
        params.extend([limit, offset])
        return rows_dict(self.connection.execute(
            "SELECT * FROM import_rows WHERE " + " AND ".join(conditions) + " ORDER BY row_no LIMIT ? OFFSET ?",
            tuple(params),
        ).fetchall())

    def count_for_batch(self, batch_id: int, *, status: str | None, resolution: str | None) -> int:
        conditions = ["batch_id=?"]
        params: list[Any] = [batch_id]
        if status:
            conditions.append("status=?")
            params.append(status)
        if resolution:
            conditions.append("resolution=?")
            params.append(resolution)
        return int(self.connection.execute(
            "SELECT COUNT(*) FROM import_rows WHERE " + " AND ".join(conditions), tuple(params)
        ).fetchone()[0])

    def status_counts(self, batch_id: int) -> dict[str, int]:
        rows = self.connection.execute(
            "SELECT status,COUNT(*) AS total FROM import_rows WHERE batch_id=? GROUP BY status", (batch_id,)
        ).fetchall()
        return {str(row["status"]): int(row["total"]) for row in rows}
