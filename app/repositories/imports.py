from __future__ import annotations

from typing import Any

from app.repositories.base import Repository, row_dict, rows_dict

COUNTS_SELECT = (
    "COUNT(r.id) AS stored_rows,"
    "COALESCE(SUM(CASE WHEN r.status='valid' THEN 1 ELSE 0 END),0) AS valid_rows,"
    "COALESCE(SUM(CASE WHEN r.status='invalid' THEN 1 ELSE 0 END),0) AS invalid_rows,"
    "COALESCE(SUM(CASE WHEN r.status='conflict' THEN 1 ELSE 0 END),0) AS conflict_rows,"
    "COALESCE(SUM(CASE WHEN r.status='written' THEN 1 ELSE 0 END),0) AS written_rows,"
    "COALESCE(SUM(CASE WHEN r.status='skipped' THEN 1 ELSE 0 END),0) AS skipped_rows,"
    "COALESCE(SUM(CASE WHEN r.status='conflict' AND r.decision IS NULL THEN 1 ELSE 0 END),0) AS pending_decision_rows"
)


class ImportRepository(Repository):
    table = "import_batches"
    entity_name = "居民导入批次"

    # ---------- 批次 ----------
    def find_by_content(self, source_key: str, content_hash: str) -> dict[str, Any] | None:
        return row_dict(self.connection.execute(
            "SELECT * FROM import_batches WHERE source_key=? AND content_hash=?",
            (source_key, content_hash),
        ).fetchone())

    def max_version(self, source_key: str) -> int:
        row = self.connection.execute(
            "SELECT COALESCE(MAX(version),0) FROM import_batches WHERE source_key=?", (source_key,)
        ).fetchone()
        return int(row[0])

    def insert_batch(
        self,
        *,
        source_key: str,
        version: int,
        content_hash: str,
        file_name: str,
        params_json: str,
        total_rows: int,
        submitted_by: int,
        submitted_by_name: str,
        now: str,
    ) -> int:
        cursor = self.connection.execute(
            "INSERT INTO import_batches(source_key,version,content_hash,file_name,params_json,status,total_rows,"
            "submitted_by,submitted_by_name,submitted_at,created_at,updated_at) "
            "VALUES(?,?,?,?,?,'staged',?,?,?,?,?,?)",
            (source_key, version, content_hash, file_name, params_json, total_rows,
             submitted_by, submitted_by_name, now, now, now),
        )
        return int(cursor.lastrowid)

    def supersede_staged(self, source_key: str, keep_id: int, now: str) -> list[int]:
        rows = self.connection.execute(
            "SELECT id FROM import_batches WHERE source_key=? AND status='staged' AND id<>?",
            (source_key, keep_id),
        ).fetchall()
        superseded = [int(row[0]) for row in rows]
        if superseded:
            self.connection.execute(
                "UPDATE import_batches SET status='superseded',updated_at=? "
                "WHERE source_key=? AND status='staged' AND id<>?",
                (now, source_key, keep_id),
            )
        return superseded

    def mark_confirmed(self, batch_id: int, confirmed_by: int, confirmed_by_name: str, result_json: str, now: str) -> None:
        self.connection.execute(
            "UPDATE import_batches SET status='confirmed',confirmed_by=?,confirmed_by_name=?,confirmed_at=?,"
            "result_json=?,updated_at=? WHERE id=?",
            (confirmed_by, confirmed_by_name, now, result_json, now, batch_id),
        )

    def touch(self, batch_id: int, now: str) -> None:
        self.connection.execute("UPDATE import_batches SET updated_at=? WHERE id=?", (now, batch_id))

    def list_batches(self, *, status: str | None, source_key: str | None, limit: int, offset: int) -> list[dict]:
        conditions: list[str] = []
        params: list[Any] = []
        if status:
            conditions.append("b.status=?")
            params.append(status)
        if source_key:
            conditions.append("b.source_key=?")
            params.append(source_key)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        params.extend([limit, offset])
        return rows_dict(self.connection.execute(
            f"SELECT b.*,{COUNTS_SELECT} FROM import_batches b LEFT JOIN import_rows r ON r.batch_id=b.id"
            + where + " GROUP BY b.id ORDER BY b.id DESC LIMIT ? OFFSET ?", tuple(params)
        ).fetchall())

    def count_batches(self, *, status: str | None, source_key: str | None) -> int:
        conditions: list[str] = []
        params: list[Any] = []
        if status:
            conditions.append("status=?")
            params.append(status)
        if source_key:
            conditions.append("source_key=?")
            params.append(source_key)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        return int(self.connection.execute("SELECT COUNT(*) FROM import_batches" + where, tuple(params)).fetchone()[0])

    def batch_with_counts(self, batch_id: int) -> dict[str, Any] | None:
        return row_dict(self.connection.execute(
            f"SELECT b.*,{COUNTS_SELECT} FROM import_batches b LEFT JOIN import_rows r ON r.batch_id=b.id "
            "WHERE b.id=? GROUP BY b.id",
            (batch_id,),
        ).fetchone())

    # ---------- 批次行 ----------
    def insert_row(
        self,
        *,
        batch_id: int,
        row_number: int,
        id_card: str | None,
        payload_json: str,
        normalized_json: str,
        status: str,
        errors_json: str,
        existing_resident_id: int | None,
        now: str,
    ) -> int:
        cursor = self.connection.execute(
            "INSERT INTO import_rows(batch_id,row_number,id_card,payload_json,normalized_json,status,errors_json,"
            "existing_resident_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (batch_id, row_number, id_card, payload_json, normalized_json, status, errors_json,
             existing_resident_id, now, now),
        )
        return int(cursor.lastrowid)

    def rows_for_batch(
        self,
        batch_id: int,
        *,
        statuses: list[str] | None = None,
        decision: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict]:
        conditions = ["batch_id=?"]
        params: list[Any] = [batch_id]
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            conditions.append(f"status IN ({placeholders})")
            params.extend(statuses)
        if decision is not None:
            conditions.append("decision=?")
            params.append(decision)
        sql = "SELECT * FROM import_rows WHERE " + " AND ".join(conditions) + " ORDER BY row_number"
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params.extend([limit, offset])
        return rows_dict(self.connection.execute(sql, tuple(params)).fetchall())

    def count_rows(self, batch_id: int, *, statuses: list[str] | None = None, decision: str | None = None) -> int:
        conditions = ["batch_id=?"]
        params: list[Any] = [batch_id]
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            conditions.append(f"status IN ({placeholders})")
            params.extend(statuses)
        if decision is not None:
            conditions.append("decision=?")
            params.append(decision)
        return int(self.connection.execute(
            "SELECT COUNT(*) FROM import_rows WHERE " + " AND ".join(conditions), tuple(params)
        ).fetchone()[0])

    def row_by_number(self, batch_id: int, row_number: int) -> dict[str, Any] | None:
        return row_dict(self.connection.execute(
            "SELECT * FROM import_rows WHERE batch_id=? AND row_number=?", (batch_id, row_number)
        ).fetchone())

    def set_row_conflict_state(self, row_id: int, status: str, existing_resident_id: int | None, now: str) -> None:
        """更新冲突状态并清空既有裁决，使该行重新进入待处理。"""
        self.connection.execute(
            "UPDATE import_rows SET status=?,existing_resident_id=?,decision=NULL,decided_by=NULL,"
            "decided_by_name=NULL,decided_at=NULL,updated_at=? WHERE id=?",
            (status, existing_resident_id, now, row_id),
        )

    def set_row_decision(self, row_id: int, decision: str, decided_by: int, decided_by_name: str, now: str) -> None:
        self.connection.execute(
            "UPDATE import_rows SET decision=?,decided_by=?,decided_by_name=?,decided_at=?,updated_at=? WHERE id=?",
            (decision, decided_by, decided_by_name, now, now, row_id),
        )

    def mark_row_written(self, row_id: int, status: str, written_resident_id: int | None, write_action: str | None, now: str) -> None:
        self.connection.execute(
            "UPDATE import_rows SET status=?,written_resident_id=?,write_action=?,updated_at=? WHERE id=?",
            (status, written_resident_id, write_action, now, row_id),
        )

    def staged_batch_ids_for_id_cards(self, id_cards: list[str]) -> list[int]:
        placeholders = ",".join("?" for _ in id_cards)
        rows = self.connection.execute(
            f"SELECT DISTINCT r.batch_id FROM import_rows r JOIN import_batches b ON b.id=r.batch_id "
            f"WHERE b.status='staged' AND r.status IN ('valid','conflict') AND r.id_card IN ({placeholders})",
            tuple(id_cards),
        ).fetchall()
        return [int(row[0]) for row in rows]

    # ---------- 正式居民 ----------
    def residents_by_id_cards(self, id_cards: list[str]) -> dict[str, dict]:
        if not id_cards:
            return {}
        placeholders = ",".join("?" for _ in id_cards)
        rows = self.connection.execute(
            f"SELECT * FROM residents WHERE id_card IN ({placeholders})", tuple(id_cards)
        ).fetchall()
        return {row["id_card"]: dict(row) for row in rows}

    def residents_by_ids(self, resident_ids: list[int]) -> dict[int, dict]:
        if not resident_ids:
            return {}
        placeholders = ",".join("?" for _ in resident_ids)
        rows = self.connection.execute(
            f"SELECT * FROM residents WHERE id IN ({placeholders})", tuple(resident_ids)
        ).fetchall()
        return {int(row["id"]): dict(row) for row in rows}
