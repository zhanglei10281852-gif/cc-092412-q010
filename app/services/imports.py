from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
from datetime import datetime
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError, ValidationError
from app.core.pagination import Page, page_result
from app.core.security import Principal, request_fingerprint
from app.repositories.imports import ImportRepository
from app.services.audit import AuditContext, AuditService

MAX_BATCH_ROWS = 2000
MAX_PARAMS_BYTES = 4000
ID_CARD_PATTERN = re.compile(r"^\d{17}[\dX]$")
RESIDENT_FIELDS = ("name", "id_card", "gender", "birth_date", "phone", "address", "village", "household_head")
IMPORT_ROW_FIELDS = RESIDENT_FIELDS + ("household_head_id_card",)
REQUIRED_CSV_HEADERS = ("name", "id_card", "gender", "birth_date", "address", "village")
BATCH_STATUSES = ("staged", "confirmed", "superseded")
ROW_STATUSES = ("valid", "invalid", "conflict", "written", "skipped")
DECISIONS = ("overwrite", "skip")
PARSE_ERROR_FIELD = "__parse_error__"


def require_import_view(principal: Principal) -> None:
    if not (principal.can("residents.import") or principal.can("residents.import.confirm")):
        raise PermissionDeniedError("缺少权限：residents.import 或 residents.import.confirm")


def parse_rows(content_csv: str | None, rows: list[dict] | None) -> list[dict]:
    """把 CSV 文本或 JSON 行统一解析为原始行字典列表，供指纹计算与校验使用。"""
    if rows is not None:
        parsed: list[dict] = []
        for index, item in enumerate(rows, start=1):
            if not isinstance(item, dict):
                raise ValidationError(f"第 {index} 行不是 JSON 对象")
            row: dict[str, Any] = {}
            for key, value in item.items():
                field = str(key)
                if value is None or isinstance(value, str):
                    row[field] = value
                elif isinstance(value, (int, float, bool)):
                    row[field] = str(value)
                else:
                    raise ValidationError(f"第 {index} 行字段 {field} 的值类型不支持")
            parsed.append(row)
        return parsed
    content = (content_csv or "").lstrip("\ufeff")
    try:
        reader = csv.DictReader(io.StringIO(content))
        fieldnames = [str(name).strip() for name in (reader.fieldnames or [])]
        reader.fieldnames = fieldnames
    except csv.Error as exc:
        raise ValidationError(f"CSV 内容解析失败：{exc}") from exc
    missing = [header for header in REQUIRED_CSV_HEADERS if header not in fieldnames]
    if missing:
        raise ValidationError("CSV 缺少必需列：" + "、".join(missing))
    parsed = []
    for raw in reader:
        row = {str(key): value for key, value in raw.items() if key is not None}
        if None in raw or any(value is None for value in raw.values()):
            row[PARSE_ERROR_FIELD] = "行列数与表头不一致"
        parsed.append(row)
    return parsed


def validate_row(raw: dict, params: dict) -> tuple[dict, list[dict]]:
    """字段级校验，返回规范化结果与错误列表；normalized 尽力而为地填充。"""
    errors: list[dict] = []

    def text(field: str) -> str | None:
        value = raw.get(field)
        if value is None:
            return None
        return str(value).strip()

    if raw.get(PARSE_ERROR_FIELD):
        errors.append({"field": "__row__", "message": raw[PARSE_ERROR_FIELD]})

    normalized: dict[str, Any] = {}

    name = text("name")
    if not name:
        errors.append({"field": "name", "message": "姓名不能为空"})
    elif len(name) > 50:
        errors.append({"field": "name", "message": "姓名长度不能超过 50 个字符"})
    normalized["name"] = name

    id_card = text("id_card")
    if id_card:
        id_card = id_card.upper()
    if not id_card:
        errors.append({"field": "id_card", "message": "证件号不能为空"})
    elif not ID_CARD_PATTERN.match(id_card):
        errors.append({"field": "id_card", "message": "证件号须为 18 位（17 位数字加数字或 X 校验位）"})
    normalized["id_card"] = id_card if id_card and ID_CARD_PATTERN.match(id_card) else None

    gender = text("gender")
    if gender not in ("男", "女"):
        errors.append({"field": "gender", "message": "性别须为 男 或 女"})
    normalized["gender"] = gender

    birth_date = text("birth_date")
    if not birth_date:
        errors.append({"field": "birth_date", "message": "出生日期不能为空"})
    else:
        try:
            datetime.strptime(birth_date, "%Y-%m-%d")
        except ValueError:
            errors.append({"field": "birth_date", "message": "出生日期须为 YYYY-MM-DD 格式的有效日期"})
    normalized["birth_date"] = birth_date

    phone = text("phone") or None
    if phone and len(phone) > 20:
        errors.append({"field": "phone", "message": "联系电话长度不能超过 20 个字符"})
    normalized["phone"] = phone

    address = text("address")
    if not address:
        errors.append({"field": "address", "message": "住址不能为空"})
    elif len(address) > 200:
        errors.append({"field": "address", "message": "住址长度不能超过 200 个字符"})
    normalized["address"] = address

    default_village = params.get("village")
    village = text("village") or (str(default_village).strip() if default_village else None)
    if not village:
        errors.append({"field": "village", "message": "所属村不能为空"})
    elif len(village) > 100:
        errors.append({"field": "village", "message": "所属村长度不能超过 100 个字符"})
    normalized["village"] = village

    household_head = text("household_head") or None
    if household_head and len(household_head) > 50:
        errors.append({"field": "household_head", "message": "户主姓名长度不能超过 50 个字符"})
    normalized["household_head"] = household_head

    head_card = text("household_head_id_card")
    if head_card:
        head_card = head_card.upper()
        if not ID_CARD_PATTERN.match(head_card):
            errors.append({"field": "household_head_id_card", "message": "户主证件号格式不正确"})
            head_card = None
    normalized["household_head_id_card"] = head_card

    return normalized, errors


def compute_diff(existing: dict, normalized: dict) -> list[dict]:
    diff = []
    for field in RESIDENT_FIELDS:
        current = existing.get(field)
        incoming = normalized.get(field)
        if (current or None) != (incoming or None):
            diff.append({"field": field, "current": current, "incoming": incoming})
    return diff


def serialize_batch(row: dict) -> dict:
    counts = {
        "valid": row["valid_rows"],
        "invalid": row["invalid_rows"],
        "conflict": row["conflict_rows"],
        "written": row["written_rows"],
        "skipped": row["skipped_rows"],
        "pending_decision": row["pending_decision_rows"],
    }
    return {
        "id": row["id"],
        "source_key": row["source_key"],
        "version": row["version"],
        "status": row["status"],
        "file_name": row["file_name"],
        "params": json.loads(row["params_json"] or "{}"),
        "content_hash": row["content_hash"],
        "total_rows": row["total_rows"],
        "counts": counts,
        "confirmable": row["status"] == "staged" and row["pending_decision_rows"] == 0,
        "submitted_by": {"id": row["submitted_by"], "name": row["submitted_by_name"]},
        "submitted_at": row["submitted_at"],
        "confirmed_by": {"id": row["confirmed_by"], "name": row["confirmed_by_name"]} if row["confirmed_by"] else None,
        "confirmed_at": row["confirmed_at"],
        "result": json.loads(row["result_json"]) if row["result_json"] else None,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


class ResidentImportService:
    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        self.repository = ImportRepository(connection)
        self.audit = AuditService(connection, self.clock)

    # ---------- 提交与暂存 ----------
    def submit(self, principal: Principal, data) -> dict:
        principal.require("residents.import")
        params = dict(data.params or {})
        params_json = json.dumps(params, ensure_ascii=False, sort_keys=True)
        if len(params_json.encode("utf-8")) > MAX_PARAMS_BYTES:
            raise ValidationError("导入参数过大")
        raw_rows = parse_rows(data.content_csv, data.rows)
        if not raw_rows:
            raise ValidationError("批次中没有数据行")
        if len(raw_rows) > MAX_BATCH_ROWS:
            raise ValidationError(f"单批次最多导入 {MAX_BATCH_ROWS} 行")
        content_hash = request_fingerprint({"params": params, "rows": raw_rows})
        existing = self.repository.find_by_content(data.source_key, content_hash)
        if existing is not None:
            return {"replayed": True, "superseded_batch_ids": [], "batch": self.batch_detail(existing["id"])}

        now = to_storage(self.clock.now())
        staged = self._stage_rows(raw_rows, params)
        version = self.repository.max_version(data.source_key) + 1
        try:
            batch_id = self.repository.insert_batch(
                source_key=data.source_key,
                version=version,
                content_hash=content_hash,
                file_name=data.file_name or "",
                params_json=params_json,
                total_rows=len(raw_rows),
                submitted_by=principal.user_id,
                submitted_by_name=principal.display_name,
                now=now,
            )
        except sqlite3.IntegrityError:
            existing = self.repository.find_by_content(data.source_key, content_hash)
            if existing is None:
                raise
            return {"replayed": True, "superseded_batch_ids": [], "batch": self.batch_detail(existing["id"])}
        for entry in staged:
            self.repository.insert_row(
                batch_id=batch_id,
                row_number=entry["row_number"],
                id_card=entry["normalized"].get("id_card"),
                payload_json=json.dumps(entry["payload"], ensure_ascii=False, sort_keys=True),
                normalized_json=json.dumps(entry["normalized"], ensure_ascii=False, sort_keys=True),
                status=entry["status"],
                errors_json=json.dumps(entry["errors"], ensure_ascii=False),
                existing_resident_id=entry["existing_resident_id"],
                now=now,
            )
        superseded = self.repository.supersede_staged(data.source_key, batch_id, now)
        detail = self.batch_detail(batch_id)
        self.audit.record(
            AuditContext(principal.user_id, principal.display_name),
            action="import.batch.submit",
            resource_type="import_batch",
            resource_id=batch_id,
            after={"source_key": data.source_key, "version": version, "total_rows": len(raw_rows), "counts": detail["counts"]},
            metadata={"superseded_batch_ids": superseded, "file_name": data.file_name or ""},
        )
        return {"replayed": False, "superseded_batch_ids": superseded, "batch": detail}

    def _stage_rows(self, raw_rows: list[dict], params: dict) -> list[dict]:
        entries = []
        for index, raw in enumerate(raw_rows, start=1):
            normalized, errors = validate_row(raw, params)
            entries.append({
                "row_number": index,
                "payload": raw,
                "normalized": normalized,
                "errors": errors,
                "status": "invalid",
                "existing_resident_id": None,
            })
        cards = sorted({entry["normalized"]["id_card"] for entry in entries if entry["normalized"].get("id_card")})
        head_cards = sorted({entry["normalized"]["household_head_id_card"] for entry in entries if entry["normalized"].get("household_head_id_card")})
        residents = self.repository.residents_by_id_cards(sorted(set(cards) | set(head_cards)))
        self._apply_duplicate_checks(entries)
        self._apply_link_checks(entries, residents)
        self._apply_cycle_checks(entries)
        for entry in entries:
            if entry["errors"]:
                continue
            resident = residents.get(entry["normalized"]["id_card"])
            if resident is not None:
                entry["status"] = "conflict"
                entry["existing_resident_id"] = resident["id"]
            else:
                entry["status"] = "valid"
        return entries

    @staticmethod
    def _card_index(entries: list[dict]) -> dict[str, list[int]]:
        by_card: dict[str, list[int]] = {}
        for position, entry in enumerate(entries):
            card = entry["normalized"].get("id_card")
            if card:
                by_card.setdefault(card, []).append(position)
        return by_card

    def _apply_duplicate_checks(self, entries: list[dict]) -> None:
        for card, positions in self._card_index(entries).items():
            if len(positions) <= 1:
                continue
            row_numbers = [entries[position]["row_number"] for position in positions]
            for position in positions:
                others = [number for number in row_numbers if number != entries[position]["row_number"]]
                entries[position]["errors"].append({
                    "field": "id_card",
                    "message": "证件号与本批次第 " + "、".join(str(number) for number in others) + " 行重复",
                })

    def _apply_link_checks(self, entries: list[dict], residents: dict[str, dict]) -> None:
        by_card = self._card_index(entries)
        for entry in entries:
            head_card = entry["normalized"].get("household_head_id_card")
            if not head_card:
                continue
            if head_card == entry["normalized"].get("id_card"):
                entry["errors"].append({"field": "household_head_id_card", "message": "户主不能是本人"})
                continue
            targets = by_card.get(head_card, [])
            if len(targets) > 1:
                entry["errors"].append({"field": "household_head_id_card", "message": "户主证件号在批内重复，无法确定关联"})
            elif len(targets) == 1:
                if entries[targets[0]]["errors"]:
                    entry["errors"].append({"field": "household_head_id_card", "message": "关联的户主行存在校验错误"})
            elif head_card not in residents:
                entry["errors"].append({"field": "household_head_id_card", "message": "户主证件号不在本批次或正式居民档案中"})

    def _apply_cycle_checks(self, entries: list[dict]) -> None:
        by_card = self._card_index(entries)
        edges: dict[int, int] = {}
        for position, entry in enumerate(entries):
            if entry["errors"]:
                continue
            head_card = entry["normalized"].get("household_head_id_card")
            if not head_card:
                continue
            targets = by_card.get(head_card, [])
            if len(targets) == 1 and not entries[targets[0]]["errors"]:
                edges[position] = targets[0]
        reported: set[int] = set()
        for start in edges:
            path: list[int] = []
            seen: dict[int, int] = {}
            node: int | None = start
            while node is not None and node in edges and node not in seen:
                seen[node] = len(path)
                path.append(node)
                node = edges.get(node)
            if node is not None and node in seen:
                for position in path[seen[node]:]:
                    if position in reported:
                        continue
                    reported.add(position)
                    entries[position]["errors"].append({"field": "household_head_id_card", "message": "户主关联存在循环"})

    # ---------- 查询 ----------
    def batch_detail(self, batch_id: int) -> dict:
        row = self.repository.batch_with_counts(batch_id)
        if row is None:
            raise NotFoundError("导入批次不存在")
        return serialize_batch(row)

    def detail(self, principal: Principal, batch_id: int) -> dict:
        require_import_view(principal)
        return self.batch_detail(batch_id)

    def list_batches(self, principal: Principal, *, status: str | None, source_key: str | None, page: Page) -> dict:
        require_import_view(principal)
        if status is not None and status not in BATCH_STATUSES:
            raise ValidationError(f"未知的批次状态：{status}")
        rows = self.repository.list_batches(status=status, source_key=source_key, limit=page.size, offset=page.offset)
        total = self.repository.count_batches(status=status, source_key=source_key)
        return page_result(total=total, page=page, rows=[serialize_batch(row) for row in rows])

    def list_rows(
        self,
        principal: Principal,
        batch_id: int,
        *,
        status: str | None,
        decision: str | None,
        page: Page,
    ) -> dict:
        require_import_view(principal)
        self.repository.require(batch_id)
        if status is not None and status not in ROW_STATUSES:
            raise ValidationError(f"未知的行状态：{status}")
        if decision is not None and decision not in DECISIONS:
            raise ValidationError(f"未知的裁决类型：{decision}")
        statuses = [status] if status else None
        rows = self.repository.rows_for_batch(batch_id, statuses=statuses, decision=decision, limit=page.size, offset=page.offset)
        total = self.repository.count_rows(batch_id, statuses=statuses, decision=decision)
        return page_result(total=total, page=page, rows=self._serialize_rows(rows))

    def list_writes(self, principal: Principal, batch_id: int) -> dict:
        require_import_view(principal)
        batch = self.repository.require(batch_id)
        rows = self.repository.rows_for_batch(batch_id, statuses=["written"])
        writes = []
        for row in rows:
            normalized = json.loads(row["normalized_json"] or "{}")
            writes.append({
                "row_number": row["row_number"],
                "resident_id": row["written_resident_id"],
                "action": row["write_action"],
                "id_card": row["id_card"],
                "name": normalized.get("name"),
                "written_at": row["updated_at"],
            })
        return {
            "batch_id": batch_id,
            "batch_status": batch["status"],
            "result": json.loads(batch["result_json"]) if batch["result_json"] else None,
            "writes": writes,
        }

    def _serialize_rows(self, rows: list[dict]) -> list[dict]:
        resident_ids = sorted({row["existing_resident_id"] for row in rows if row["existing_resident_id"]})
        residents = self.repository.residents_by_ids(resident_ids)
        return [self._serialize_row(row, residents.get(row["existing_resident_id"])) for row in rows]

    @staticmethod
    def _serialize_row(row: dict, existing: dict | None) -> dict:
        normalized = json.loads(row["normalized_json"]) if row["normalized_json"] else None
        return {
            "row_number": row["row_number"],
            "status": row["status"],
            "id_card": row["id_card"],
            "errors": json.loads(row["errors_json"] or "[]"),
            "payload": json.loads(row["payload_json"]),
            "normalized": normalized,
            "existing_resident_id": row["existing_resident_id"],
            "existing": existing,
            "diff": compute_diff(existing, normalized) if existing and normalized else [],
            "decision": row["decision"],
            "decided_by": {"id": row["decided_by"], "name": row["decided_by_name"]} if row["decided_by"] else None,
            "decided_at": row["decided_at"],
            "written_resident_id": row["written_resident_id"],
            "write_action": row["write_action"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    # ---------- 裁决 ----------
    def record_decisions(self, principal: Principal, batch_id: int, items: list) -> dict:
        principal.require("residents.import.confirm")
        batch = self.repository.require(batch_id)
        if batch["status"] != "staged":
            raise ConflictError("只有待确认的批次可以登记裁决")
        now = to_storage(self.clock.now())
        seen: set[int] = set()
        targets: list[tuple[dict, str]] = []
        for item in items:
            if item.row_number in seen:
                raise ValidationError(f"第 {item.row_number} 行的裁决重复")
            seen.add(item.row_number)
            row = self.repository.row_by_number(batch_id, item.row_number)
            if row is None:
                raise NotFoundError(f"批次中不存在第 {item.row_number} 行")
            if row["status"] != "conflict":
                raise ConflictError(
                    f"第 {item.row_number} 行当前状态为 {row['status']}，无需裁决",
                    context={"row_number": item.row_number, "status": row["status"]},
                )
            targets.append((row, item.decision))
        for row, decision in targets:
            self.repository.set_row_decision(row["id"], decision, principal.user_id, principal.display_name, now)
        self.repository.touch(batch_id, now)
        self.audit.record(
            AuditContext(principal.user_id, principal.display_name),
            action="import.rows.decide",
            resource_type="import_batch",
            resource_id=batch_id,
            metadata={"decisions": [{"row_number": row["row_number"], "decision": decision} for row, decision in targets]},
        )
        return self.batch_detail(batch_id)

    # ---------- 确认入库 ----------
    def confirm(self, principal: Principal, batch_id: int) -> dict:
        principal.require("residents.import.confirm")
        batch = self.repository.require(batch_id)
        if batch["status"] == "confirmed":
            return {"outcome": "confirmed", "replayed": True, "batch": self.batch_detail(batch_id)}
        if batch["status"] != "staged":
            raise ConflictError("批次已被新版本取代，不能确认入库")
        if principal.user_id == batch["submitted_by"]:
            raise PermissionDeniedError("导入批次须由另一名有权限人员确认入库")
        refreshed = self._refresh_batch(batch)
        if refreshed:
            self.audit.record(
                AuditContext(principal.user_id, principal.display_name),
                action="import.batch.conflicts_refreshed",
                resource_type="import_batch",
                resource_id=batch_id,
                outcome="failure",
                metadata={"refreshed_rows": refreshed},
            )
            return {"outcome": "refreshed", "batch_id": batch_id, "refreshed_rows": refreshed, "batch": self.batch_detail(batch_id)}
        rows = self.repository.rows_for_batch(batch_id)
        undecided = [row for row in rows if row["status"] == "conflict" and not row["decision"]]
        if undecided:
            raise ConflictError(
                "存在未裁决的冲突行，请先登记覆盖或跳过决定",
                context={"row_numbers": [row["row_number"] for row in undecided]},
            )
        now = to_storage(self.clock.now())
        head_names = self._head_name_map(rows)
        inserted = updated = skipped = 0
        writes: list[dict] = []
        for row in rows:
            if row["status"] not in ("valid", "conflict"):
                continue
            normalized = json.loads(row["normalized_json"])
            data = {field: normalized.get(field) for field in RESIDENT_FIELDS}
            if not data.get("household_head"):
                head_card = normalized.get("household_head_id_card")
                if head_card and head_card in head_names:
                    data["household_head"] = head_names[head_card]
            if row["status"] == "valid":
                cursor = self.connection.execute(
                    "INSERT INTO residents(name,id_card,gender,birth_date,phone,address,village,household_head) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (data["name"], data["id_card"], data["gender"], data["birth_date"],
                     data["phone"], data["address"], data["village"], data["household_head"]),
                )
                resident_id = int(cursor.lastrowid)
                action = "insert"
                inserted += 1
            elif row["decision"] == "skip":
                self.repository.mark_row_written(row["id"], "skipped", None, None, now)
                skipped += 1
                continue
            else:
                cursor = self.connection.execute(
                    "UPDATE residents SET name=?,gender=?,birth_date=?,phone=?,address=?,village=?,"
                    "household_head=?,updated_at=datetime('now') WHERE id=?",
                    (data["name"], data["gender"], data["birth_date"], data["phone"],
                     data["address"], data["village"], data["household_head"], row["existing_resident_id"]),
                )
                if cursor.rowcount != 1:
                    raise ConflictError("覆盖目标居民已不存在，整批已回滚", context={"row_number": row["row_number"]})
                resident_id = row["existing_resident_id"]
                action = "update"
                updated += 1
            self.repository.mark_row_written(row["id"], "written", resident_id, action, now)
            writes.append({"row_number": row["row_number"], "resident_id": resident_id, "action": action, "id_card": data["id_card"]})
        result = {
            "inserted": inserted,
            "updated": updated,
            "skipped": skipped,
            "invalid": sum(1 for row in rows if row["status"] == "invalid"),
            "writes": writes,
        }
        self.repository.mark_confirmed(
            batch_id, principal.user_id, principal.display_name,
            json.dumps(result, ensure_ascii=False, sort_keys=True), now,
        )
        self.refresh_conflicts_for_id_cards([write["id_card"] for write in writes])
        self.audit.record(
            AuditContext(principal.user_id, principal.display_name),
            action="import.batch.confirm",
            resource_type="import_batch",
            resource_id=batch_id,
            before={"status": "staged"},
            after={"status": "confirmed", "result": {key: value for key, value in result.items() if key != "writes"}},
        )
        return {"outcome": "confirmed", "replayed": False, "batch": self.batch_detail(batch_id)}

    def _head_name_map(self, rows: list[dict]) -> dict[str, str]:
        mapping: dict[str, str] = {}
        referenced: set[str] = set()
        for row in rows:
            normalized = json.loads(row["normalized_json"] or "{}")
            card = normalized.get("id_card")
            if card and normalized.get("name"):
                mapping[card] = normalized["name"]
            head_card = normalized.get("household_head_id_card")
            if head_card:
                referenced.add(head_card)
        missing = sorted(card for card in referenced if card not in mapping)
        for card, resident in self.repository.residents_by_id_cards(missing).items():
            mapping[card] = resident["name"]
        return mapping

    # ---------- 冲突重标记 ----------
    def refresh_conflicts_for_id_cards(self, id_cards: list[str]) -> list[int]:
        """居民档案变化后调用：重标记受影响的暂存批次行，返回发生变化的批次 id。"""
        unique = sorted({card for card in id_cards if card})
        if not unique:
            return []
        changed = []
        for batch_id in self.repository.staged_batch_ids_for_id_cards(unique):
            batch = self.repository.require(batch_id)
            if self._refresh_batch(batch):
                changed.append(batch_id)
        return changed

    def _refresh_batch(self, batch: dict) -> list[dict]:
        """按当前正式居民数据重算暂存行冲突状态；发生变化的行清空裁决、重新待处理。"""
        if batch["status"] != "staged":
            return []
        rows = self.repository.rows_for_batch(batch["id"], statuses=["valid", "conflict"])
        cards = sorted({row["id_card"] for row in rows if row["id_card"]})
        residents = self.repository.residents_by_id_cards(cards)
        now = to_storage(self.clock.now())
        changes = []
        for row in rows:
            resident = residents.get(row["id_card"]) if row["id_card"] else None
            desired_status = "conflict" if resident else "valid"
            desired_existing = resident["id"] if resident else None
            if row["status"] != desired_status or row["existing_resident_id"] != desired_existing:
                self.repository.set_row_conflict_state(row["id"], desired_status, desired_existing, now)
                changes.append({
                    "row_number": row["row_number"],
                    "id_card": row["id_card"],
                    "status": desired_status,
                    "existing_resident_id": desired_existing,
                })
        if changes:
            self.repository.touch(batch["id"], now)
        return changes
