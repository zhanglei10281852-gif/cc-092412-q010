from __future__ import annotations

import csv
import io
import json
import os
import re
import secrets
import sqlite3
from datetime import date

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError, ValidationError
from app.core.idcard import parse_id_card
from app.core.security import Principal, request_fingerprint
from app.repositories.imports import ImportBatchRepository, ImportRowRepository
from app.services.audit import AuditContext, AuditService

REQUIRED_HEADERS = ("name", "id_card", "gender", "birth_date", "address", "village")
OPTIONAL_HEADERS = ("phone", "household_head", "household_head_id_card")
ALL_HEADERS = REQUIRED_HEADERS + OPTIONAL_HEADERS
DIFF_FIELDS = ("name", "gender", "birth_date", "phone", "address", "village", "household_head")
WRITABLE_RESOLUTIONS = ("insert", "update")
_PHONE_PATTERN = re.compile(r"^[0-9+\-() ]{5,20}$")
_DATE_PATTERN = re.compile(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})$")


def _clean(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _optional(value: str) -> str | None:
    return value if value else None


class ResidentImportService:
    """居民数据分阶段导入：接收校验 -> 差异复核 -> 原子确认入库。"""

    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        self.batches = ImportBatchRepository(connection)
        self.rows = ImportRowRepository(connection)
        self.audit = AuditService(connection, self.clock)
        self.max_rows = int(os.getenv("TOWNSHIP_IMPORT_MAX_ROWS", "5000"))
        self.max_content_bytes = int(os.getenv("TOWNSHIP_IMPORT_MAX_CONTENT_BYTES", "2000000"))

    # ------------------------------------------------------------------ 提交

    def submit(self, principal: Principal, data: dict) -> tuple[dict, bool]:
        """接收结构化批次并完成校验。返回 (批次摘要, 是否重放)。"""
        principal.require("imports.write")
        source_name = data["source_name"].strip()
        file_format = data["file_format"]
        file_content = data["file_content"]
        params = data.get("params") or {}
        if len(file_content.encode("utf-8")) > self.max_content_bytes:
            raise ValidationError("文件内容超过大小限制")
        content_hash = request_fingerprint(
            {"file_format": file_format, "file_content": file_content, "params": params}
        )
        batch_key = data.get("batch_key")
        if batch_key:
            existing = self.batches.by_key_and_hash(batch_key, content_hash)
        else:
            existing = self.batches.latest_by_submitter_and_hash(principal.user_id, content_hash)
        if existing is not None:
            return self.summary(existing["id"], replayed=True), True

        parsed_rows = self._parse(file_format, file_content)
        if not parsed_rows:
            raise ValidationError("文件中没有可导入的数据行")
        if len(parsed_rows) > self.max_rows:
            raise ValidationError(f"单批次最多导入 {self.max_rows} 行")

        now = to_storage(self.clock.now())
        if batch_key:
            latest = self.batches.latest_version(batch_key)
            version = int(latest["version"]) + 1 if latest else 1
            if latest and latest["status"] == "pending_review":
                self.connection.execute(
                    "UPDATE import_batches SET status='superseded',updated_at=? WHERE id=?",
                    (now, latest["id"]),
                )
        else:
            batch_key = f"IMP-{self.clock.now():%Y%m%d}-{secrets.token_hex(4)}"
            version = 1

        validated = self._validate_rows(parsed_rows, params)
        counts = {
            "valid": sum(1 for row in validated if row["status"] == "valid"),
            "conflict": sum(1 for row in validated if row["status"] == "conflict"),
            "error": sum(1 for row in validated if row["status"] == "error"),
        }
        cursor = self.connection.execute(
            "INSERT INTO import_batches(batch_key,version,content_hash,source_name,file_format,params_json,"
            "status,total_rows,valid_rows,conflict_rows,error_rows,submitted_by,submitted_by_name,"
            "submitted_at,created_at,updated_at) VALUES(?,?,?,?,?,?,'pending_review',?,?,?,?,?,?,?,?,?)",
            (
                batch_key, version, content_hash, source_name, file_format,
                json.dumps(params, ensure_ascii=False, sort_keys=True),
                len(validated), counts["valid"], counts["conflict"], counts["error"],
                principal.user_id, principal.display_name, now, now, now,
            ),
        )
        batch_id = int(cursor.lastrowid)
        for row in validated:
            self.connection.execute(
                "INSERT INTO import_rows(batch_id,row_no,id_card,name,raw_json,normalized_json,status,"
                "errors_json,conflict_resident_id,existing_json,resolution,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    batch_id, row["row_no"], row["id_card"], row["name"],
                    json.dumps(row["raw"], ensure_ascii=False, sort_keys=True),
                    json.dumps(row["normalized"], ensure_ascii=False, sort_keys=True) if row["normalized"] else None,
                    row["status"], json.dumps(row["errors"], ensure_ascii=False),
                    row["conflict_resident_id"],
                    json.dumps(row["existing"], ensure_ascii=False, sort_keys=True) if row["existing"] else None,
                    row["resolution"], now, now,
                ),
            )
        self.audit.record(
            AuditContext(principal.user_id, principal.display_name),
            action="import.submit",
            resource_type="import_batch",
            resource_id=batch_id,
            after={"batch_key": batch_key, "version": version, "status": "pending_review"},
            metadata={"total_rows": len(validated), **counts},
        )
        return self.summary(batch_id, replayed=False), False

    # ------------------------------------------------------------------ 解析

    def _parse(self, file_format: str, content: str) -> list[dict]:
        if file_format == "csv":
            return self._parse_csv(content)
        return self._parse_json(content)

    def _parse_csv(self, content: str) -> list[dict]:
        text = content.lstrip("\ufeff")
        try:
            reader = csv.DictReader(io.StringIO(text))
            headers = [(_clean(name)) for name in (reader.fieldnames or [])]
        except csv.Error as exc:
            raise ValidationError(f"CSV 解析失败：{exc}") from exc
        missing = [name for name in REQUIRED_HEADERS if name not in headers]
        if not headers:
            raise ValidationError("CSV 缺少表头行")
        if missing:
            raise ValidationError(f"CSV 缺少必需列：{', '.join(missing)}")
        rows: list[dict] = []
        for record in reader:
            raw = {key: _clean(record.get(key)) for key in ALL_HEADERS}
            if not any(raw.values()):
                continue
            rows.append({"row_no": len(rows) + 1, "raw": raw})
        return rows

    def _parse_json(self, content: str) -> list[dict]:
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ValidationError(f"JSON 解析失败：{exc}") from exc
        if not isinstance(payload, list):
            raise ValidationError("JSON 文件内容必须是对象数组")
        rows: list[dict] = []
        for item in payload:
            row_no = len(rows) + 1
            if not isinstance(item, dict):
                rows.append({"row_no": row_no, "raw": {"_invalid": item}, "malformed": True})
                continue
            raw = {key: _clean(item.get(key)) for key in ALL_HEADERS}
            rows.append({"row_no": row_no, "raw": raw})
        return rows

    # ------------------------------------------------------------------ 校验

    def _validate_rows(self, parsed_rows: list[dict], params: dict) -> list[dict]:
        default_village = _clean(params.get("default_village") or "")
        results: list[dict] = []
        for parsed in parsed_rows:
            raw = parsed["raw"]
            errors: list[dict] = []

            def error(field: str, code: str, message: str) -> None:
                errors.append({"field": field, "code": code, "message": message})

            if parsed.get("malformed"):
                error("_row", "malformed", "数据行必须是对象")
            name = _clean(raw.get("name"))
            if not name:
                error("name", "required", "姓名不能为空")
            elif len(name) > 50:
                error("name", "too_long", "姓名长度不能超过 50 个字符")

            id_card = _clean(raw.get("id_card")).upper()
            card_info = None
            if not id_card:
                error("id_card", "required", "身份证号不能为空")
            else:
                card_info, card_error = parse_id_card(id_card)
                if card_error:
                    error("id_card", "invalid", card_error)

            gender = _clean(raw.get("gender"))
            if gender not in ("男", "女"):
                error("gender", "invalid", "性别必须为 男 或 女")

            birth_date = _clean(raw.get("birth_date"))
            normalized_birth = None
            match = _DATE_PATTERN.match(birth_date)
            if not match:
                error("birth_date", "invalid", "出生日期格式应为 YYYY-MM-DD")
            else:
                try:
                    normalized_birth = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
                except ValueError:
                    error("birth_date", "invalid", "出生日期不是有效日期")
            if normalized_birth is not None:
                if normalized_birth > self.clock.now().date():
                    error("birth_date", "invalid", "出生日期不能晚于当前日期")
                elif normalized_birth.year < 1900:
                    error("birth_date", "invalid", "出生日期不能早于 1900 年")

            if card_info is not None:
                if normalized_birth is not None and card_info.birth_date != normalized_birth:
                    error("birth_date", "mismatch", "出生日期与身份证号不一致")
                if gender in ("男", "女") and card_info.gender != gender:
                    error("gender", "mismatch", "性别与身份证号不一致")

            address = _clean(raw.get("address"))
            if not address:
                error("address", "required", "住址不能为空")
            elif len(address) > 200:
                error("address", "too_long", "住址长度不能超过 200 个字符")

            village = _clean(raw.get("village")) or default_village
            if not village:
                error("village", "required", "所属村不能为空")
            elif len(village) > 100:
                error("village", "too_long", "所属村长度不能超过 100 个字符")

            phone = _clean(raw.get("phone"))
            if phone and not _PHONE_PATTERN.match(phone):
                error("phone", "invalid", "联系电话格式不正确")

            household_head = _clean(raw.get("household_head"))
            if len(household_head) > 50:
                error("household_head", "too_long", "户主姓名长度不能超过 50 个字符")

            head_id_card = _clean(raw.get("household_head_id_card")).upper()
            if head_id_card:
                _, head_error = parse_id_card(head_id_card)
                if head_error:
                    error("household_head_id_card", "invalid", f"户主身份证号无效：{head_error}")
                elif card_info is not None and head_id_card == card_info.number:
                    error("household_head_id_card", "self_reference", "户主不能是本人")

            normalized = {
                "name": name,
                "id_card": card_info.number if card_info else id_card,
                "gender": gender,
                "birth_date": normalized_birth.isoformat() if normalized_birth else birth_date,
                "phone": _optional(phone),
                "address": address,
                "village": village,
                "household_head": _optional(household_head),
                "household_head_id_card": _optional(head_id_card),
            }
            results.append({
                "row_no": parsed["row_no"],
                "raw": raw,
                "normalized": normalized,
                "id_card": normalized["id_card"],
                "name": name,
                "errors": errors,
                "status": "error" if errors else "valid",
                "resolution": "pending",
                "conflict_resident_id": None,
                "existing": None,
            })

        self._validate_batch_uniqueness(results)
        self._validate_relationships(results)
        self._mark_resident_conflicts(results)
        return results

    def _validate_batch_uniqueness(self, results: list[dict]) -> None:
        by_card: dict[str, list[dict]] = {}
        for row in results:
            if row["id_card"]:
                by_card.setdefault(row["id_card"], []).append(row)
        for id_card, group in by_card.items():
            if len(group) > 1:
                for row in group:
                    row["errors"].append({
                        "field": "id_card",
                        "code": "duplicate_in_batch",
                        "message": f"身份证号在批内重复（第 {', '.join(str(item['row_no']) for item in group)} 行）",
                    })

    def _validate_relationships(self, results: list[dict]) -> None:
        in_batch = {row["id_card"]: row for row in results if row["id_card"]}
        for row in results:
            head_id_card = (row["normalized"] or {}).get("household_head_id_card")
            if not head_id_card or any(e["field"] == "household_head_id_card" for e in row["errors"]):
                continue
            head_name = row["normalized"].get("household_head")
            target_name = None
            if head_id_card in in_batch:
                target_name = in_batch[head_id_card]["name"]
            else:
                resident = self._resident_by_id_card(head_id_card)
                if resident:
                    target_name = resident["name"]
            if target_name is None:
                row["errors"].append({
                    "field": "household_head_id_card",
                    "code": "reference_not_found",
                    "message": "户主身份证号在批内与正式居民中均不存在",
                })
            elif head_name and head_name != target_name:
                row["errors"].append({
                    "field": "household_head",
                    "code": "reference_mismatch",
                    "message": f"户主姓名与证件号登记姓名（{target_name}）不一致",
                })

    def _mark_resident_conflicts(self, results: list[dict]) -> None:
        for row in results:
            if row["errors"]:
                row["status"] = "error"
                continue
            existing = self._resident_by_id_card(row["id_card"]) if row["id_card"] else None
            if existing:
                row["status"] = "conflict"
                row["resolution"] = "pending"
                row["conflict_resident_id"] = existing["id"]
                row["existing"] = existing
            else:
                row["status"] = "valid"
                row["resolution"] = "insert"

    def _resident_by_id_card(self, id_card: str) -> dict | None:
        row = self.connection.execute("SELECT * FROM residents WHERE id_card=?", (id_card,)).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------ 复核

    def recheck(self, principal: Principal, batch_id: int) -> dict:
        """重新比对正式居民数据，刷新冲突标记。"""
        principal.require("imports.write")
        batch = self.batches.require(batch_id)
        self._require_pending(batch)
        changed = self._recheck_conflicts(batch_id)
        now = to_storage(self.clock.now())
        self._refresh_counts(batch_id)
        self.connection.execute("UPDATE import_batches SET rechecked_at=?,updated_at=? WHERE id=?", (now, now, batch_id))
        if changed:
            self.audit.record(
                AuditContext(principal.user_id, principal.display_name),
                action="import.recheck",
                resource_type="import_batch",
                resource_id=batch_id,
                metadata={"changed_rows": [row["row_no"] for row in changed]},
            )
        return {"changed_rows": len(changed), "rows": changed, "batch": self.summary(batch_id)}

    def _recheck_conflicts(self, batch_id: int) -> list[dict]:
        """对照当前居民表重算冲突。返回发生变化的行（已持久化）。"""
        now = to_storage(self.clock.now())
        changed: list[dict] = []
        for row in self.rows.for_batch(batch_id):
            if row["status"] not in ("valid", "conflict"):
                continue
            normalized = json.loads(row["normalized_json"]) if row["normalized_json"] else {}
            id_card = normalized.get("id_card") or row["id_card"]
            existing = self._resident_by_id_card(id_card) if id_card else None
            if row["status"] == "valid" and existing is not None:
                self.connection.execute(
                    "UPDATE import_rows SET status='conflict',resolution='pending',conflict_resident_id=?,"
                    "existing_json=?,resolved_by=NULL,resolved_by_name=NULL,resolved_at=NULL,updated_at=? WHERE id=?",
                    (existing["id"], json.dumps(existing, ensure_ascii=False, sort_keys=True), now, row["id"]),
                )
                changed.append({"row_no": row["row_no"], "id_card": id_card, "change": "valid_to_conflict"})
            elif row["status"] == "conflict":
                if existing is None:
                    self.connection.execute(
                        "UPDATE import_rows SET status='valid',resolution='insert',conflict_resident_id=NULL,"
                        "existing_json=NULL,resolved_by=NULL,resolved_by_name=NULL,resolved_at=NULL,updated_at=? WHERE id=?",
                        (now, row["id"]),
                    )
                    changed.append({"row_no": row["row_no"], "id_card": id_card, "change": "conflict_to_valid"})
                elif existing["id"] != row["conflict_resident_id"] or self._snapshot_changed(row, existing):
                    self.connection.execute(
                        "UPDATE import_rows SET conflict_resident_id=?,existing_json=?,updated_at=? WHERE id=?",
                        (existing["id"], json.dumps(existing, ensure_ascii=False, sort_keys=True), now, row["id"]),
                    )
        return changed

    @staticmethod
    def _snapshot_changed(row: dict, existing: dict) -> bool:
        snapshot = json.loads(row["existing_json"]) if row["existing_json"] else None
        if snapshot is None:
            return True
        return any(snapshot.get(field) != existing.get(field) for field in DIFF_FIELDS)

    def _refresh_counts(self, batch_id: int) -> None:
        counts = self.rows.status_counts(batch_id)
        valid = counts.get("valid", 0)
        conflict = counts.get("conflict", 0)
        error = counts.get("error", 0)
        self.connection.execute(
            "UPDATE import_batches SET valid_rows=?,conflict_rows=?,error_rows=? WHERE id=?",
            (valid, conflict, error, batch_id),
        )

    def resolve_row(self, principal: Principal, batch_id: int, row_id: int, resolution: str) -> dict:
        """登记覆盖决定：valid 行可选 insert/skip，conflict 行可选 update/skip。"""
        principal.require("imports.write")
        batch = self.batches.require(batch_id)
        self._require_pending(batch)
        row = self.rows.require(row_id)
        if row["batch_id"] != batch_id:
            raise NotFoundError("导入行不属于该批次")
        if row["status"] == "error":
            raise ConflictError("失败行不能登记覆盖决定，请修正文件后重新提交新版本")
        if row["status"] not in ("valid", "conflict"):
            raise ConflictError("当前行状态不允许登记覆盖决定")
        allowed = ("insert", "skip") if row["status"] == "valid" else ("update", "skip")
        if resolution not in allowed:
            raise ConflictError(f"{row['status']} 行只允许 {'/'.join(allowed)} 决定")
        now = to_storage(self.clock.now())
        self.connection.execute(
            "UPDATE import_rows SET resolution=?,resolved_by=?,resolved_by_name=?,resolved_at=?,updated_at=? WHERE id=?",
            (resolution, principal.user_id, principal.display_name, now, now, row_id),
        )
        self.connection.execute("UPDATE import_batches SET updated_at=? WHERE id=?", (now, batch_id))
        self.audit.record(
            AuditContext(principal.user_id, principal.display_name),
            action="import.resolve",
            resource_type="import_row",
            resource_id=row_id,
            before={"resolution": row["resolution"]},
            after={"resolution": resolution},
            metadata={"batch_id": batch_id, "row_no": row["row_no"]},
        )
        return self._row_view(self.rows.require(row_id))

    # ------------------------------------------------------------------ 确认

    def confirm(self, principal: Principal, batch_id: int) -> dict:
        """确认入库：整批成功或完整回滚。自行管理事务以支持"标记后返回"。"""
        principal.require("imports.confirm")
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            batch = self.batches.require(batch_id)
            if batch["status"] == "confirmed":
                self.connection.commit()
                return self._confirm_result(self.batches.require(batch_id), replayed=True)
            self._require_pending(batch)
            if principal.user_id == batch["submitted_by"]:
                raise PermissionDeniedError("提交人不能确认自己提交的批次，需由另一名有权限人员确认")
            changed = self._recheck_conflicts(batch_id)
            self._refresh_counts(batch_id)
            if any(item["change"] == "valid_to_conflict" for item in changed):
                now = to_storage(self.clock.now())
                self.connection.execute(
                    "UPDATE import_batches SET rechecked_at=?,updated_at=? WHERE id=?", (now, now, batch_id)
                )
                self.audit.record(
                    AuditContext(principal.user_id, principal.display_name),
                    action="import.recheck",
                    resource_type="import_batch",
                    resource_id=batch_id,
                    metadata={"changed_rows": [item["row_no"] for item in changed], "trigger": "confirm"},
                )
                self.connection.commit()
                raise ConflictError(
                    "确认期间出现新的居民冲突，已重新标记待处理",
                    context={"changed_rows": changed},
                )
            self._preflight(batch_id)
            self._apply(batch_id, principal)
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return self._confirm_result(self.batches.require(batch_id), replayed=False)

    def _preflight(self, batch_id: int) -> None:
        rows = self.rows.for_batch(batch_id)
        error_rows = [row["row_no"] for row in rows if row["status"] == "error"]
        if error_rows:
            raise ConflictError(
                "批次存在失败行，不能确认；请修正文件后提交新版本",
                context={"error_rows": error_rows},
            )
        pending = [row["row_no"] for row in rows if row["status"] == "conflict" and row["resolution"] == "pending"]
        if pending:
            raise ConflictError(
                "批次存在未登记覆盖决定的冲突行",
                context={"pending_rows": pending},
            )
        violations = self._reference_violations(rows)
        if violations:
            raise ConflictError(
                "部分行的户主引用因覆盖决定无法解析，请调整决定",
                context={"reference_violations": violations},
            )

    def _reference_violations(self, rows: list[dict]) -> list[dict]:
        writable = {
            (json.loads(row["normalized_json"]) or {}).get("id_card")
            for row in rows
            if row["status"] in ("valid", "conflict") and row["resolution"] in WRITABLE_RESOLUTIONS
        }
        violations: list[dict] = []
        for row in rows:
            if row["status"] not in ("valid", "conflict") or row["resolution"] not in WRITABLE_RESOLUTIONS:
                continue
            normalized = json.loads(row["normalized_json"]) if row["normalized_json"] else {}
            head_id_card = normalized.get("household_head_id_card")
            if not head_id_card:
                continue
            if head_id_card in writable:
                continue
            if self._resident_by_id_card(head_id_card):
                continue
            violations.append({"row_no": row["row_no"], "household_head_id_card": head_id_card})
        return violations

    def _apply(self, batch_id: int, principal: Principal) -> None:
        now = to_storage(self.clock.now())
        rows = self.rows.for_batch(batch_id)
        writable_by_card: dict[str, dict] = {}
        for row in rows:
            if row["status"] in ("valid", "conflict") and row["resolution"] in WRITABLE_RESOLUTIONS:
                normalized = json.loads(row["normalized_json"]) if row["normalized_json"] else {}
                if normalized.get("id_card"):
                    writable_by_card[normalized["id_card"]] = normalized
        inserted = updated = skipped = 0
        for row in rows:
            normalized = json.loads(row["normalized_json"]) if row["normalized_json"] else {}
            if row["status"] in ("valid", "conflict") and row["resolution"] in WRITABLE_RESOLUTIONS:
                household_head = normalized.get("household_head")
                head_id_card = normalized.get("household_head_id_card")
                if not household_head and head_id_card:
                    head = self._resident_by_id_card(head_id_card)
                    if head is not None:
                        household_head = head["name"]
                    elif head_id_card in writable_by_card:
                        household_head = writable_by_card[head_id_card].get("name")
                if row["resolution"] == "insert":
                    cursor = self.connection.execute(
                        "INSERT INTO residents(name,id_card,gender,birth_date,phone,address,village,household_head,"
                        "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (
                            normalized["name"], normalized["id_card"], normalized["gender"],
                            normalized["birth_date"], normalized.get("phone"), normalized["address"],
                            normalized["village"], household_head, now, now,
                        ),
                    )
                    resident_id = int(cursor.lastrowid)
                    inserted += 1
                else:
                    resident_id = int(row["conflict_resident_id"])
                    self.connection.execute(
                        "UPDATE residents SET name=?,gender=?,birth_date=?,phone=?,address=?,village=?,"
                        "household_head=?,updated_at=? WHERE id=?",
                        (
                            normalized["name"], normalized["gender"], normalized["birth_date"],
                            normalized.get("phone"), normalized["address"], normalized["village"],
                            household_head, now, resident_id,
                        ),
                    )
                    updated += 1
                self.connection.execute(
                    "UPDATE import_rows SET status='written',written_resident_id=?,updated_at=? WHERE id=?",
                    (resident_id, now, row["id"]),
                )
            elif row["status"] in ("valid", "conflict") and row["resolution"] == "skip":
                skipped += 1
                self.connection.execute(
                    "UPDATE import_rows SET status='skipped',updated_at=? WHERE id=?", (now, row["id"])
                )
        self.connection.execute(
            "UPDATE import_batches SET status='confirmed',confirmed_by=?,confirmed_by_name=?,confirmed_at=?,"
            "inserted_rows=?,updated_rows=?,skipped_rows=?,updated_at=? WHERE id=?",
            (principal.user_id, principal.display_name, now, inserted, updated, skipped, now, batch_id),
        )
        self.audit.record(
            AuditContext(principal.user_id, principal.display_name),
            action="import.confirm",
            resource_type="import_batch",
            resource_id=batch_id,
            after={"status": "confirmed"},
            metadata={"inserted": inserted, "updated": updated, "skipped": skipped},
        )

    def _confirm_result(self, batch: dict, *, replayed: bool) -> dict:
        return {
            "id": batch["id"],
            "batch_key": batch["batch_key"],
            "version": batch["version"],
            "status": batch["status"],
            "inserted_rows": batch["inserted_rows"],
            "updated_rows": batch["updated_rows"],
            "skipped_rows": batch["skipped_rows"],
            "confirmed_by_name": batch["confirmed_by_name"],
            "confirmed_at": batch["confirmed_at"],
            "replayed": replayed,
        }

    # ------------------------------------------------------------------ 查询

    def list_batches(self, principal: Principal, *, status: str | None, limit: int, offset: int) -> tuple[int, list[dict]]:
        principal.require("imports.read")
        total = self.batches.count("status=?" if status else "", (status,) if status else ())
        rows = self.batches.list(status=status, limit=limit, offset=offset)
        return total, [self._batch_view(row) for row in rows]

    def detail(self, principal: Principal, batch_id: int) -> dict:
        principal.require("imports.read")
        batch = self.batches.require(batch_id)
        view = self._batch_view(batch)
        pending = self.batches.pending_decisions(batch_id)
        blockers: list[str] = []
        if batch["status"] == "confirmed":
            blockers.append("批次已确认入库")
        elif batch["status"] == "superseded":
            blockers.append("批次已被新版本取代")
        else:
            if batch["error_rows"]:
                blockers.append(f"存在 {batch['error_rows']} 条失败行")
            if pending:
                blockers.append(f"存在 {pending} 条待决定的冲突行")
            violations = self._reference_violations(self.rows.for_batch(batch_id))
            if violations:
                blockers.append(f"存在 {len(violations)} 条户主引用无法解析")
        view["pending_decisions"] = pending
        view["review"] = {"confirmable": not blockers, "blockers": blockers}
        view["versions"] = self.batches.versions(batch["batch_key"])
        return view

    def list_rows(
        self,
        principal: Principal,
        batch_id: int,
        *,
        status: str | None,
        resolution: str | None,
        limit: int,
        offset: int,
    ) -> tuple[int, list[dict]]:
        principal.require("imports.read")
        self.batches.require(batch_id)
        total = self.rows.count_for_batch(batch_id, status=status, resolution=resolution)
        rows = self.rows.list_for_batch(batch_id, status=status, resolution=resolution, limit=limit, offset=offset)
        return total, [self._row_view(row) for row in rows]

    def summary(self, batch_id: int, *, replayed: bool = False) -> dict:
        batch = self.batches.require(batch_id)
        view = self._batch_view(batch)
        view["pending_decisions"] = self.batches.pending_decisions(batch_id)
        view["replayed"] = replayed
        return view

    def _batch_view(self, batch: dict) -> dict:
        return {
            "id": batch["id"],
            "batch_key": batch["batch_key"],
            "version": batch["version"],
            "status": batch["status"],
            "source_name": batch["source_name"],
            "file_format": batch["file_format"],
            "content_hash": batch["content_hash"],
            "params": json.loads(batch["params_json"] or "{}"),
            "total_rows": batch["total_rows"],
            "valid_rows": batch["valid_rows"],
            "conflict_rows": batch["conflict_rows"],
            "error_rows": batch["error_rows"],
            "inserted_rows": batch["inserted_rows"],
            "updated_rows": batch["updated_rows"],
            "skipped_rows": batch["skipped_rows"],
            "submitted_by": batch["submitted_by"],
            "submitted_by_name": batch["submitted_by_name"],
            "submitted_at": batch["submitted_at"],
            "confirmed_by": batch["confirmed_by"],
            "confirmed_by_name": batch["confirmed_by_name"],
            "confirmed_at": batch["confirmed_at"],
            "rechecked_at": batch["rechecked_at"],
        }

    def _row_view(self, row: dict) -> dict:
        normalized = json.loads(row["normalized_json"]) if row["normalized_json"] else None
        existing = json.loads(row["existing_json"]) if row["existing_json"] else None
        view = {
            "id": row["id"],
            "batch_id": row["batch_id"],
            "row_no": row["row_no"],
            "status": row["status"],
            "id_card": row["id_card"],
            "name": row["name"],
            "raw": json.loads(row["raw_json"]),
            "data": normalized,
            "errors": json.loads(row["errors_json"] or "[]"),
            "resolution": row["resolution"],
            "resolved_by_name": row["resolved_by_name"],
            "resolved_at": row["resolved_at"],
            "conflict_resident_id": row["conflict_resident_id"],
            "existing": existing,
            "written_resident_id": row["written_resident_id"],
        }
        if existing and normalized:
            view["diff"] = {
                field: {"existing": existing.get(field), "incoming": normalized.get(field)}
                for field in DIFF_FIELDS
                if (existing.get(field) or None) != (normalized.get(field) or None)
            }
        else:
            view["diff"] = {}
        return view

    @staticmethod
    def _require_pending(batch: dict) -> None:
        if batch["status"] == "confirmed":
            raise ConflictError("批次已确认入库，不能再修改")
        if batch["status"] == "superseded":
            raise ConflictError("批次已被新版本取代，请操作最新版本")
