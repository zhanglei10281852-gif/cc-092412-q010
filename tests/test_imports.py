from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.core.idcard import check_digit, parse_id_card

HEADERS = "name,id_card,gender,birth_date,phone,address,village,household_head,household_head_id_card"


def make_id_card(region: str = "110101", birth: str = "19900101", seq: str = "001") -> str:
    """生成合法身份证号。seq 末位为奇数对应男性，偶数对应女性。"""
    body = f"{region}{birth}{seq}"
    return body + check_digit(body)


def person(**overrides) -> dict:
    data = {
        "name": "张三",
        "id_card": make_id_card(),
        "gender": "男",
        "birth_date": "1990-01-01",
        "phone": "13800000000",
        "address": "幸福路 1 号",
        "village": "幸福村",
        "household_head": "",
        "household_head_id_card": "",
    }
    data.update(overrides)
    return data


def csv_content(rows: list[dict]) -> str:
    lines = [HEADERS]
    for row in rows:
        lines.append(",".join(row.get(key, "") for key in HEADERS.split(",")))
    return "\n".join(lines)


def json_content(rows: list[dict]) -> str:
    return json.dumps(rows, ensure_ascii=False)


def submit_payload(rows, *, fmt="csv", batch_key=None, params=None, source="import.csv") -> dict:
    payload = {
        "source_name": source,
        "file_format": fmt,
        "file_content": csv_content(rows) if fmt == "csv" else json_content(rows),
    }
    if batch_key:
        payload["batch_key"] = batch_key
    if params:
        payload["params"] = params
    return payload


def submit(client, headers, rows, **kwargs):
    return client.post("/api/resident-imports", headers=headers, json=submit_payload(rows, **kwargs))


@pytest.fixture()
def reviewer(client, admin) -> dict:
    role = client.post(
        "/api/roles",
        headers=admin["headers"],
        json={"code": "imports.reviewer", "name": "导入复核员", "permission_codes": ["imports.read", "imports.write", "imports.confirm"]},
    )
    assert role.status_code == 201, role.text
    user = client.post(
        "/api/users",
        headers=admin["headers"],
        json={"username": "reviewer.one", "password": "Review!23456", "display_name": "复核员乙", "role_codes": ["imports.reviewer"]},
    )
    assert user.status_code == 201, user.text
    login = client.post("/api/auth/login", json={"username": "reviewer.one", "password": "Review!23456", "client_label": "tests"})
    assert login.status_code == 200, login.text
    return {"headers": {"Authorization": f"Bearer {login.json()['token']}"}}


def create_resident_direct(client, **overrides) -> int:
    data = person(**overrides)
    data.pop("household_head_id_card", None)
    data["household_head"] = data["household_head"] or None
    response = client.post("/residents", json=data)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def residents_total(client) -> int:
    return client.get("/residents").json()["total"]


def batch_rows(client, headers, batch_id, query="") -> list[dict]:
    response = client.get(f"/api/resident-imports/{batch_id}/rows?size=100{query}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_id_card_validator_anchor():
    info, error = parse_id_card("11010519491231002X")
    assert error is None
    assert info.birth_date.isoformat() == "1949-12-31"
    assert info.gender == "女"
    _, error = parse_id_card("110105194912310021")
    assert error == "身份证号校验位不正确"
    _, error = parse_id_card("123")
    assert error == "身份证号必须为 18 位"


def test_submit_stages_batch_without_touching_residents(client, admin):
    rows = [person(), person(name="李四", id_card=make_id_card(seq="032"), gender="女")]
    response = submit(client, admin["headers"], rows)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "pending_review"
    assert body["total_rows"] == 2 and body["valid_rows"] == 2
    assert body["replayed"] is False
    assert residents_total(client) == 0, "确认前不得改变正式居民数据"


def test_field_and_id_card_validation_errors(client, admin):
    valid_card = make_id_card()
    bad_card = valid_card[:-1] + ("0" if valid_card[-1] != "0" else "1")
    rows = [
        person(id_card=bad_card),
        person(name="性别错", id_card=make_id_card(seq="003"), gender="女"),
        person(name="生日错", id_card=make_id_card(seq="005"), birth_date="1991-01-01"),
        person(name="", address="", village=""),
        person(name="电话错", phone="abc"),
    ]
    response = submit(client, admin["headers"], rows)
    assert response.status_code == 201, response.text
    assert response.json()["error_rows"] == 5
    error_rows = batch_rows(client, admin["headers"], response.json()["id"], "&status=error")
    codes = {error["code"] for row in error_rows for error in row["errors"]}
    assert {"invalid", "mismatch", "required"} <= codes
    assert residents_total(client) == 0


def test_duplicate_id_card_within_batch_marks_all_rows(client, admin):
    duplicated = make_id_card(seq="007")
    response = submit(client, admin["headers"], [person(name="甲", id_card=duplicated), person(name="乙", id_card=duplicated)])
    assert response.json()["error_rows"] == 2
    rows = batch_rows(client, admin["headers"], response.json()["id"])
    assert all(any(e["code"] == "duplicate_in_batch" for e in row["errors"]) for row in rows)


def test_household_head_relationship_validation(client, admin):
    head = person(name="户主甲", id_card=make_id_card(seq="011"))
    self_ref = person(name="自引", id_card=make_id_card(seq="013"), household_head_id_card=make_id_card(seq="013"))
    missing_ref = person(name="空引", id_card=make_id_card(seq="015"), household_head_id_card=make_id_card(seq="099"))
    mismatch = person(name="错名", id_card=make_id_card(seq="017"), household_head="别人", household_head_id_card=head["id_card"])
    valid_ref = person(name="成员", id_card=make_id_card(seq="019"), household_head="户主甲", household_head_id_card=head["id_card"])
    response = submit(client, admin["headers"], [head, self_ref, missing_ref, mismatch, valid_ref])
    assert response.status_code == 201, response.text
    rows = batch_rows(client, admin["headers"], response.json()["id"])
    by_name = {row["name"]: row for row in rows}
    assert any(e["code"] == "self_reference" for e in by_name["自引"]["errors"])
    assert any(e["code"] == "reference_not_found" for e in by_name["空引"]["errors"])
    assert any(e["code"] == "reference_mismatch" for e in by_name["错名"]["errors"])
    assert by_name["成员"]["status"] == "valid"
    assert by_name["户主甲"]["status"] == "valid"


def test_same_file_and_params_replays_original_batch(client, admin):
    rows = [person()]
    first = submit(client, admin["headers"], rows, batch_key="village-2026-09")
    assert first.status_code == 201
    second = submit(client, admin["headers"], rows, batch_key="village-2026-09")
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["replayed"] is True
    keyless_first = submit(client, admin["headers"], [person(name="另一批", id_card=make_id_card(seq="021"))])
    keyless_second = submit(client, admin["headers"], [person(name="另一批", id_card=make_id_card(seq="021"))])
    assert keyless_second.json()["id"] == keyless_first.json()["id"]
    assert keyless_second.json()["replayed"] is True
    changed_params = submit(client, admin["headers"], rows, batch_key="village-params", params={"default_village": "幸福村"})
    same_file_other_params = submit(client, admin["headers"], rows, batch_key="village-params", params={"default_village": "别的村"})
    assert same_file_other_params.json()["id"] != changed_params.json()["id"], "参数不同应形成新版本"


def test_corrected_file_forms_new_version(client, admin):
    broken = person(id_card="110101199001010000")
    first = submit(client, admin["headers"], [broken], batch_key="village-fix")
    assert first.json()["version"] == 1
    assert first.json()["error_rows"] == 1
    second = submit(client, admin["headers"], [person()], batch_key="village-fix")
    assert second.status_code == 201
    assert second.json()["version"] == 2
    assert second.json()["error_rows"] == 0
    old = client.get(f"/api/resident-imports/{first.json()['id']}", headers=admin["headers"]).json()
    assert old["status"] == "superseded"
    detail = client.get(f"/api/resident-imports/{second.json()['id']}", headers=admin["headers"]).json()
    assert [item["version"] for item in detail["versions"]] == [1, 2]


def test_conflict_diff_resolution_and_confirm_by_another_user(client, admin, reviewer):
    resident_id = create_resident_direct(client, phone="13000000000")
    incoming = person(phone="13999999999", address="新址 2 号")
    newcomer = person(name="新居民", id_card=make_id_card(seq="023"))
    response = submit(client, admin["headers"], [incoming, newcomer])
    batch = response.json()
    assert batch["conflict_rows"] == 1 and batch["valid_rows"] == 1
    conflict_row = batch_rows(client, reviewer["headers"], batch["id"], "&status=conflict")[0]
    assert conflict_row["conflict_resident_id"] == resident_id
    assert conflict_row["diff"]["phone"] == {"existing": "13000000000", "incoming": "13999999999"}
    assert conflict_row["diff"]["address"]["incoming"] == "新址 2 号"
    decided = client.post(
        f"/api/resident-imports/{batch['id']}/rows/{conflict_row['id']}/resolution",
        headers=reviewer["headers"],
        json={"resolution": "update"},
    )
    assert decided.status_code == 200, decided.text
    assert decided.json()["resolved_by_name"] == "复核员乙"
    confirmed = client.post(f"/api/resident-imports/{batch['id']}/confirm", headers=reviewer["headers"])
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["inserted_rows"] == 1 and confirmed.json()["updated_rows"] == 1
    resident = client.get(f"/residents/{resident_id}").json()
    assert resident["phone"] == "13999999999" and resident["address"] == "新址 2 号"
    written = batch_rows(client, admin["headers"], batch["id"], "&status=written")
    assert len(written) == 2
    assert all(row["written_resident_id"] for row in written)


def test_submitter_cannot_confirm_own_batch(client, admin):
    response = submit(client, admin["headers"], [person()])
    confirm = client.post(f"/api/resident-imports/{response.json()['id']}/confirm", headers=admin["headers"])
    assert confirm.status_code == 403
    assert "另一名" in confirm.json()["error"]["message"]


def test_confirm_blocked_by_errors_and_pending_decisions(client, admin, reviewer):
    broken = submit(client, admin["headers"], [person(id_card="bad")])
    confirm = client.post(f"/api/resident-imports/{broken.json()['id']}/confirm", headers=reviewer["headers"])
    assert confirm.status_code == 409
    assert "失败行" in confirm.json()["error"]["message"]
    create_resident_direct(client)
    pending = submit(client, admin["headers"], [person()])
    assert pending.json()["conflict_rows"] == 1
    confirm = client.post(f"/api/resident-imports/{pending.json()['id']}/confirm", headers=reviewer["headers"])
    assert confirm.status_code == 409
    assert "未登记覆盖决定" in confirm.json()["error"]["message"]


def test_new_conflict_during_confirmation_remarks_pending(client, admin, reviewer):
    rows = [person(), person(name="同行", id_card=make_id_card(seq="031"))]
    response = submit(client, admin["headers"], rows)
    batch_id = response.json()["id"]
    assert response.json()["valid_rows"] == 2
    create_resident_direct(client)  # 确认窗口期内正式居民中出现了相同证件号
    confirm = client.post(f"/api/resident-imports/{batch_id}/confirm", headers=reviewer["headers"])
    assert confirm.status_code == 409
    assert "重新标记待处理" in confirm.json()["error"]["message"]
    batch = client.get(f"/api/resident-imports/{batch_id}", headers=admin["headers"]).json()
    assert batch["status"] == "pending_review"
    assert batch["conflict_rows"] == 1
    conflict_rows = batch_rows(client, admin["headers"], batch_id, "&status=conflict")
    assert conflict_rows[0]["resolution"] == "pending"
    assert residents_total(client) == 1, "确认失败时不得写入任何行"
    resolved = client.post(
        f"/api/resident-imports/{batch_id}/rows/{conflict_rows[0]['id']}/resolution",
        headers=reviewer["headers"],
        json={"resolution": "skip"},
    )
    assert resolved.status_code == 200
    confirmed = client.post(f"/api/resident-imports/{batch_id}/confirm", headers=reviewer["headers"])
    assert confirmed.status_code == 200
    assert confirmed.json()["inserted_rows"] == 1 and confirmed.json()["skipped_rows"] == 1
    assert residents_total(client) == 2


def test_recheck_endpoint_marks_new_conflicts(client, admin):
    response = submit(client, admin["headers"], [person()])
    batch_id = response.json()["id"]
    create_resident_direct(client)
    recheck = client.post(f"/api/resident-imports/{batch_id}/recheck", headers=admin["headers"])
    assert recheck.status_code == 200
    assert recheck.json()["changed_rows"] == 1
    assert recheck.json()["batch"]["conflict_rows"] == 1


def test_confirm_is_idempotent_replay(client, admin, reviewer):
    response = submit(client, admin["headers"], [person()])
    batch_id = response.json()["id"]
    first = client.post(f"/api/resident-imports/{batch_id}/confirm", headers=reviewer["headers"])
    assert first.status_code == 200
    second = client.post(f"/api/resident-imports/{batch_id}/confirm", headers=reviewer["headers"])
    assert second.status_code == 200
    assert second.json()["replayed"] is True
    assert second.json()["inserted_rows"] == 1
    assert residents_total(client) == 1


def test_skip_decision_with_dangling_reference_blocks_confirm(client, admin, reviewer):
    head = person(name="户主", id_card=make_id_card(seq="041"))
    member = person(name="成员", id_card=make_id_card(seq="043"), household_head_id_card=head["id_card"])
    response = submit(client, admin["headers"], [head, member])
    batch_id = response.json()["id"]
    rows = batch_rows(client, admin["headers"], batch_id)
    head_row = next(row for row in rows if row["name"] == "户主")
    member_row = next(row for row in rows if row["name"] == "成员")
    client.post(f"/api/resident-imports/{batch_id}/rows/{head_row['id']}/resolution", headers=reviewer["headers"], json={"resolution": "skip"})
    blocked = client.post(f"/api/resident-imports/{batch_id}/confirm", headers=reviewer["headers"])
    assert blocked.status_code == 409
    assert "户主引用" in blocked.json()["error"]["message"]
    client.post(f"/api/resident-imports/{batch_id}/rows/{member_row['id']}/resolution", headers=reviewer["headers"], json={"resolution": "skip"})
    confirmed = client.post(f"/api/resident-imports/{batch_id}/confirm", headers=reviewer["headers"])
    assert confirmed.status_code == 200
    assert confirmed.json()["skipped_rows"] == 2
    assert residents_total(client) == 0


def test_household_head_name_derived_from_reference(client, admin, reviewer):
    head = person(name="户主丙", id_card=make_id_card(seq="051"))
    member = person(name="成员乙", id_card=make_id_card(seq="053"), household_head_id_card=head["id_card"])
    response = submit(client, admin["headers"], [head, member])
    batch_id = response.json()["id"]
    confirmed = client.post(f"/api/resident-imports/{batch_id}/confirm", headers=reviewer["headers"])
    assert confirmed.status_code == 200, confirmed.text
    written = batch_rows(client, admin["headers"], batch_id, "&status=written")
    member_written = next(row for row in rows if row["name"] == "成员乙") if (rows := written) else None
    resident = client.get(f"/residents/{member_written['written_resident_id']}").json()
    assert resident["household_head"] == "户主丙"


def test_json_format_and_default_village_param(client, admin):
    rows = [person(village=""), person(name="有村", id_card=make_id_card(seq="061"), village="平原村")]
    response = submit(client, admin["headers"], rows, fmt="json", params={"default_village": "默认村"}, source="import.json")
    assert response.status_code == 201, response.text
    assert response.json()["valid_rows"] == 2
    rows_body = batch_rows(client, admin["headers"], response.json()["id"])
    by_name = {row["name"]: row for row in rows_body}
    assert by_name["张三"]["data"]["village"] == "默认村"
    assert by_name["有村"]["data"]["village"] == "平原村"


def test_unknown_param_rejected(client, admin):
    payload = submit_payload([person()], params={"unknown": 1})
    response = client.post("/api/resident-imports", headers=admin["headers"], json=payload)
    assert response.status_code == 422


def test_import_permissions_enforced(client, admin):
    user = client.post(
        "/api/users",
        headers=admin["headers"],
        json={"username": "no.perms", "password": "Clerk!23456", "display_name": "无权限", "role_codes": []},
    )
    assert user.status_code == 201
    login = client.post("/api/auth/login", json={"username": "no.perms", "password": "Clerk!23456", "client_label": "tests"})
    headers = {"Authorization": f"Bearer {login.json()['token']}"}
    assert submit(client, headers, [person()]).status_code == 403
    assert client.get("/api/resident-imports", headers=headers).status_code == 403
    unauthenticated = client.post("/api/resident-imports", json=submit_payload([person()]))
    assert unauthenticated.status_code == 401


def test_list_and_detail_endpoints(client, admin):
    submit(client, admin["headers"], [person()], batch_key="list-a")
    submit(client, admin["headers"], [person(id_card=make_id_card(seq="071"))], batch_key="list-b")
    listing = client.get("/api/resident-imports?status=pending_review", headers=admin["headers"])
    assert listing.status_code == 200
    assert listing.json()["total"] == 2
    batch_id = listing.json()["data"][0]["id"]
    detail = client.get(f"/api/resident-imports/{batch_id}", headers=admin["headers"])
    assert detail.status_code == 200
    assert detail.json()["review"]["confirmable"] is True
    missing = client.get("/api/resident-imports/99999", headers=admin["headers"])
    assert missing.status_code == 404


def test_superseded_batch_cannot_be_confirmed(client, admin, reviewer):
    first = submit(client, admin["headers"], [person()], batch_key="supersede-me")
    submit(client, admin["headers"], [person(name="修正", id_card=make_id_card(seq="081"))], batch_key="supersede-me")
    confirm = client.post(f"/api/resident-imports/{first.json()['id']}/confirm", headers=reviewer["headers"])
    assert confirm.status_code == 409


def test_restart_keeps_import_progress(client, admin):
    response = submit(client, admin["headers"], [person()], batch_key="durable")
    batch_id = response.json()["id"]
    from app.database import close_connection
    close_connection()
    from app.main import app
    with TestClient(app) as restarted:
        login = restarted.post("/api/auth/login", json={"username": "admin", "password": "Admin!23456", "client_label": "tests"})
        headers = {"Authorization": f"Bearer {login.json()['token']}"}
        detail = restarted.get(f"/api/resident-imports/{batch_id}", headers=headers)
        assert detail.status_code == 200
        assert detail.json()["batch_key"] == "durable"
        assert detail.json()["total_rows"] == 1


def test_confirm_rolls_back_everything_on_failure(client, admin, reviewer):
    rows = [person(name="第一", id_card=make_id_card(seq="091")), person(name="第二", id_card=make_id_card(seq="093"))]
    response = submit(client, admin["headers"], rows)
    batch_id = response.json()["id"]

    from app.core.security import Principal
    from app.database import get_connection
    from app.services.imports import ResidentImportService

    real = get_connection()

    class FlakyConnection:
        def __init__(self, inner):
            self.inner = inner
            self.inserts = 0

        def execute(self, sql, *args, **kwargs):
            if isinstance(sql, str) and sql.startswith("INSERT INTO residents"):
                self.inserts += 1
                if self.inserts == 2:
                    raise sqlite3.OperationalError("模拟写入失败")
            return self.inner.execute(sql, *args, **kwargs)

        def commit(self):
            return self.inner.commit()

        def rollback(self):
            return self.inner.rollback()

    principal = Principal(user_id=999, username="reviewer", display_name="复核员", department_id=None, permissions=frozenset({"imports.confirm"}), session_id=1)
    service = ResidentImportService(FlakyConnection(real))
    with pytest.raises(sqlite3.OperationalError):
        service.confirm(principal, batch_id)
    assert residents_total(client) == 0, "整批失败必须完整回滚"
    batch = client.get(f"/api/resident-imports/{batch_id}", headers=admin["headers"]).json()
    assert batch["status"] == "pending_review"
    recovered = client.post(f"/api/resident-imports/{batch_id}/confirm", headers=reviewer["headers"])
    assert recovered.status_code == 200
    assert recovered.json()["inserted_rows"] == 2
