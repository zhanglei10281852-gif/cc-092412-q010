from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

CSV_HEADER = "name,id_card,gender,birth_date,phone,address,village,household_head,household_head_id_card"

CARD_1 = "110101199001011234"
CARD_2 = "110101199002022345"
CARD_3 = "110101199003033456"
CARD_4 = "110101199004044567"
CARD_5 = "110101199005055678"
CARD_6 = "110101199006066789"
CARD_7 = "110101199007077890"
CARD_8 = "110101199008088901"


def make_user(client: TestClient, admin: dict, username: str, permissions: list[str]) -> dict:
    role = client.post(
        "/api/roles",
        headers=admin["headers"],
        json={"code": f"role.{username}", "name": f"角色{username}", "permission_codes": permissions},
    )
    assert role.status_code == 201, role.text
    created = client.post(
        "/api/users",
        headers=admin["headers"],
        json={"username": username, "password": "Clerk!23456", "display_name": username, "role_codes": [f"role.{username}"]},
    )
    assert created.status_code == 201, created.text
    login = client.post("/api/auth/login", json={"username": username, "password": "Clerk!23456", "client_label": "tests"})
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['token']}"}


@pytest.fixture()
def importer(client, admin) -> dict:
    return make_user(client, admin, "import.clerk", ["residents.import"])


@pytest.fixture()
def reviewer(client, admin) -> dict:
    return make_user(client, admin, "import.reviewer", ["residents.import.confirm"])


def csv_body(*lines: str) -> str:
    return "\n".join([CSV_HEADER, *lines])


def submit(client: TestClient, headers: dict, **overrides) -> dict:
    payload = {"source_key": "幸福村-2026-09", "file_name": "residents.csv", "params": {}, "content_csv": csv_body(
        f"张三,{CARD_1},男,1990-01-01,13800000001,幸福村1号,幸福村,,",
    )}
    payload.update(overrides)
    return client.post("/api/imports", headers=headers, json=payload)


def test_submit_stages_and_validates_rows(client, importer):
    response = submit(client, importer, content_csv=csv_body(
        f"张三,{CARD_1},男,1990-01-01,13800000001,幸福村1号,幸福村,,",
        f"李四,not-a-card,女,1992-02-02,,幸福村2号,幸福村,,",
        f"王五,{CARD_3},未知,1993-03-03,,幸福村3号,幸福村,,",
        f"赵六,{CARD_4},男,1993-13-01,,幸福村4号,幸福村,,",
        f"孙七,{CARD_5},男,1990-05-05,,幸福村5号,幸福村,,",
        f"孙七重,{CARD_5},男,1990-05-05,,幸福村5号,幸福村,,",
        f"周八,{CARD_6},男,1990-06-06,,幸福村6号,幸福村,,{CARD_8}",
        f"吴九,{CARD_7},男,1990-07-07,,幸福村7号,幸福村,,{CARD_7}",
    ))
    assert response.status_code == 201, response.text
    batch = response.json()["batch"]
    assert batch["status"] == "staged"
    assert batch["counts"]["valid"] == 1
    assert batch["counts"]["invalid"] == 7
    assert batch["confirmable"] is True

    rows = client.get(f"/api/imports/{batch['id']}/rows", headers=importer, params={"status": "invalid", "size": 100})
    assert rows.status_code == 200
    assert rows.json()["total"] == 7
    by_number = {row["row_number"]: row for row in rows.json()["data"]}
    assert any(error["field"] == "id_card" for error in by_number[2]["errors"])
    assert any(error["field"] == "gender" for error in by_number[3]["errors"])
    assert any(error["field"] == "birth_date" for error in by_number[4]["errors"])
    assert any("重复" in error["message"] for error in by_number[5]["errors"])
    assert any("重复" in error["message"] for error in by_number[6]["errors"])
    assert any("户主证件号不在本批次或正式居民档案中" in error["message"] for error in by_number[7]["errors"])
    assert any("户主不能是本人" in error["message"] for error in by_number[8]["errors"])

    residents = client.get("/residents")
    assert residents.json()["total"] == 0, "确认前不得改变正式居民数据"


def test_household_head_cycle_is_rejected(client, importer):
    response = submit(client, importer, content_csv=None, rows=[
        {"name": "甲", "id_card": CARD_1, "gender": "男", "birth_date": "1990-01-01", "address": "某村1号", "village": "某村", "household_head_id_card": CARD_2},
        {"name": "乙", "id_card": CARD_2, "gender": "女", "birth_date": "1990-02-02", "address": "某村2号", "village": "某村", "household_head_id_card": CARD_1},
    ])
    assert response.status_code == 201, response.text
    batch = response.json()["batch"]
    assert batch["counts"]["invalid"] == 2
    rows = client.get(f"/api/imports/{batch['id']}/rows", headers=importer, params={"status": "invalid"})
    for row in rows.json()["data"]:
        assert any("循环" in error["message"] for error in row["errors"])


def test_resubmit_replays_and_correction_versions(client, importer):
    first = submit(client, importer)
    assert first.status_code == 201
    first_batch = first.json()["batch"]
    assert first.json()["replayed"] is False
    assert first_batch["version"] == 1

    replay = submit(client, importer)
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["batch"]["id"] == first_batch["id"]

    corrected = submit(client, importer, content_csv=csv_body(
        f"张三,{CARD_1},男,1990-01-01,13800000001,幸福村1号,幸福村,,",
        f"李四,{CARD_2},女,1992-02-02,,幸福村2号,幸福村,,",
    ))
    assert corrected.status_code == 201
    second_batch = corrected.json()["batch"]
    assert second_batch["version"] == 2
    assert corrected.json()["superseded_batch_ids"] == [first_batch["id"]]

    old = client.get(f"/api/imports/{first_batch['id']}", headers=importer)
    assert old.json()["status"] == "superseded"

    other_source = submit(client, importer, source_key="其他村-2026-09")
    assert other_source.status_code == 201
    assert other_source.json()["batch"]["version"] == 1

    batches = client.get("/api/imports", headers=importer, params={"source_key": "幸福村-2026-09"})
    assert batches.json()["total"] == 2


def test_csv_and_json_rows_share_fingerprint(client, importer):
    csv_text = csv_body(f"张三,{CARD_1},男,1990-01-01,13800000001,幸福村1号,幸福村,,")
    first = submit(client, importer, content_csv=csv_text)
    assert first.status_code == 201
    equivalent = submit(client, importer, content_csv=None, rows=[
        {"name": "张三", "id_card": CARD_1, "gender": "男", "birth_date": "1990-01-01",
         "phone": "13800000001", "address": "幸福村1号", "village": "幸福村", "household_head": "", "household_head_id_card": ""},
    ])
    assert equivalent.status_code == 200
    assert equivalent.json()["replayed"] is True
    assert equivalent.json()["batch"]["id"] == first.json()["batch"]["id"]


def test_conflict_decision_and_confirm_flow(client, admin, importer, reviewer):
    existing = client.post("/residents", json={
        "name": "张三", "id_card": CARD_1, "gender": "男", "birth_date": "1990-01-01",
        "phone": "13800000000", "address": "旧地址1号", "village": "幸福村",
    })
    assert existing.status_code == 201
    resident_id = existing.json()["id"]

    response = submit(client, importer, content_csv=csv_body(
        f"张三,{CARD_1},男,1990-01-01,13800000001,新地址1号,幸福村,,",
        f"李四,{CARD_2},女,1992-02-02,,幸福村2号,幸福村,,",
        "坏行,bad-card,女,1992-02-02,,幸福村3号,幸福村,,",
    ))
    batch = response.json()["batch"]
    assert batch["counts"] == {"valid": 1, "invalid": 1, "conflict": 1, "written": 0, "skipped": 0, "pending_decision": 1}
    assert batch["confirmable"] is False

    rows = client.get(f"/api/imports/{batch['id']}/rows", headers=reviewer, params={"status": "conflict"})
    conflict_row = rows.json()["data"][0]
    assert conflict_row["existing"]["address"] == "旧地址1号"
    assert {item["field"] for item in conflict_row["diff"]} == {"phone", "address"}

    denied = client.post(f"/api/imports/{batch['id']}/confirm", headers=importer)
    assert denied.status_code == 403, "提交人无确认权限"

    premature = client.post(f"/api/imports/{batch['id']}/confirm", headers=reviewer)
    assert premature.status_code == 409
    assert premature.json()["error"]["context"]["row_numbers"] == [1]

    wrong_row = client.post(f"/api/imports/{batch['id']}/decisions", headers=reviewer,
                            json={"decisions": [{"row_number": 2, "decision": "skip"}]})
    assert wrong_row.status_code == 409, "非冲突行不能裁决"

    forbidden = client.post(f"/api/imports/{batch['id']}/decisions", headers=importer,
                            json={"decisions": [{"row_number": 1, "decision": "overwrite"}]})
    assert forbidden.status_code == 403

    decided = client.post(f"/api/imports/{batch['id']}/decisions", headers=reviewer,
                          json={"decisions": [{"row_number": 1, "decision": "overwrite"}]})
    assert decided.status_code == 200, decided.text
    assert decided.json()["confirmable"] is True

    decisions = client.get(f"/api/imports/{batch['id']}/rows", headers=reviewer, params={"decision": "overwrite"})
    assert decisions.json()["total"] == 1
    assert decisions.json()["data"][0]["decided_by"]["name"] == "import.reviewer"

    confirmed = client.post(f"/api/imports/{batch['id']}/confirm", headers=reviewer)
    assert confirmed.status_code == 200, confirmed.text
    outcome = confirmed.json()
    assert outcome["outcome"] == "confirmed"
    assert outcome["replayed"] is False
    result = outcome["batch"]["result"]
    assert (result["inserted"], result["updated"], result["skipped"], result["invalid"]) == (1, 1, 0, 1)
    assert outcome["batch"]["confirmed_by"]["name"] == "import.reviewer"

    resident = client.get(f"/residents/{resident_id}").json()
    assert resident["address"] == "新地址1号"
    assert resident["phone"] == "13800000001"

    writes = client.get(f"/api/imports/{batch['id']}/writes", headers=reviewer)
    assert writes.status_code == 200
    assert sorted(write["action"] for write in writes.json()["writes"]) == ["insert", "update"]

    replay = client.post(f"/api/imports/{batch['id']}/confirm", headers=reviewer)
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert client.get("/residents").json()["total"] == 2, "重复确认不得重复写入"


def test_confirm_requires_another_authorized_user(client, admin):
    response = submit(client, admin["headers"])
    batch = response.json()["batch"]
    same_user = client.post(f"/api/imports/{batch['id']}/confirm", headers=admin["headers"])
    assert same_user.status_code == 403
    assert "另一名" in same_user.json()["error"]["message"]


def test_new_resident_reflags_staged_rows(client, importer, reviewer):
    batch = submit(client, importer).json()["batch"]
    assert batch["counts"]["valid"] == 1

    created = client.post("/residents", json={
        "name": "张三", "id_card": CARD_1, "gender": "男", "birth_date": "1990-01-01",
        "address": "其他渠道录入", "village": "幸福村",
    })
    assert created.status_code == 201

    rows = client.get(f"/api/imports/{batch['id']}/rows", headers=importer)
    row = rows.json()["data"][0]
    assert row["status"] == "conflict"
    assert row["existing_resident_id"] == created.json()["id"]
    assert row["decision"] is None
    detail = client.get(f"/api/imports/{batch['id']}", headers=importer).json()
    assert detail["confirmable"] is False

    client.delete(f"/residents/{created.json()['id']}")
    restored = client.get(f"/api/imports/{batch['id']}/rows", headers=importer).json()["data"][0]
    assert restored["status"] == "valid"
    assert restored["existing_resident_id"] is None


def test_confirm_detects_concurrent_resident_change(client, importer, reviewer):
    batch = submit(client, importer).json()["batch"]

    from app.database import get_connection
    get_connection().execute(
        "INSERT INTO residents(name,id_card,gender,birth_date,phone,address,village,household_head) "
        "VALUES(?,?,?,?,?,?,?,?)",
        ("张三", CARD_1, "男", "1990-01-01", None, "并发录入", "幸福村", None),
    )

    attempt = client.post(f"/api/imports/{batch['id']}/confirm", headers=reviewer)
    assert attempt.status_code == 409
    context = attempt.json()["error"]["context"]
    assert context["refreshed_rows"][0]["status"] == "conflict"

    row = client.get(f"/api/imports/{batch['id']}/rows", headers=reviewer).json()["data"][0]
    assert row["status"] == "conflict"

    client.post(f"/api/imports/{batch['id']}/decisions", headers=reviewer,
                json={"decisions": [{"row_number": 1, "decision": "skip"}]})
    confirmed = client.post(f"/api/imports/{batch['id']}/confirm", headers=reviewer)
    assert confirmed.status_code == 200
    result = confirmed.json()["batch"]["result"]
    assert result["skipped"] == 1
    resident = client.get("/residents", params={"name": "张三"}).json()["data"][0]
    assert resident["address"] == "并发录入", "跳过的行不得覆盖正式数据"


def test_confirm_rolls_back_atomically(client, importer, reviewer, monkeypatch):
    batch = submit(client, importer, content_csv=csv_body(
        f"张三,{CARD_1},男,1990-01-01,,幸福村1号,幸福村,,",
        f"李四,{CARD_2},女,1992-02-02,,幸福村2号,幸福村,,",
    )).json()["batch"]

    from app.core.errors import ConflictError
    from app.repositories.imports import ImportRepository

    original = ImportRepository.mark_row_written
    calls = {"count": 0}

    def flaky(self, *args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 2:
            raise ConflictError("模拟写入失败")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(ImportRepository, "mark_row_written", flaky)
    failed = client.post(f"/api/imports/{batch['id']}/confirm", headers=reviewer)
    assert failed.status_code == 409
    assert client.get("/residents").json()["total"] == 0, "失败必须完整回滚"
    detail = client.get(f"/api/imports/{batch['id']}", headers=reviewer).json()
    assert detail["status"] == "staged"
    assert detail["counts"]["written"] == 0

    monkeypatch.undo()
    confirmed = client.post(f"/api/imports/{batch['id']}/confirm", headers=reviewer)
    assert confirmed.status_code == 200
    assert client.get("/residents").json()["total"] == 2


def test_confirm_resolves_household_head_name(client, importer, reviewer):
    batch = submit(client, importer, content_csv=csv_body(
        f"成员,{CARD_1},女,1990-01-01,,幸福村1号,幸福村,,{CARD_2}",
        f"户主甲,{CARD_2},男,1988-08-08,,幸福村1号,幸福村,,",
    )).json()["batch"]
    assert batch["counts"]["valid"] == 2
    confirmed = client.post(f"/api/imports/{batch['id']}/confirm", headers=reviewer)
    assert confirmed.status_code == 200
    member = client.get("/residents", params={"name": "成员"}).json()["data"][0]
    assert member["household_head"] == "户主甲"


def test_import_permissions(client, admin, importer, reviewer):
    outsider = make_user(client, admin, "import.outsider", [])
    assert client.post("/api/imports", headers=outsider, json={"source_key": "x", "rows": [{"name": "甲"}]}).status_code == 403
    assert client.get("/api/imports", headers=outsider).status_code == 403

    batch = submit(client, importer).json()["batch"]
    assert client.get("/api/imports", headers=importer).status_code == 200
    assert client.get(f"/api/imports/{batch['id']}", headers=reviewer).status_code == 200
    assert client.post("/api/imports", headers=reviewer, json={"source_key": "y", "rows": [{"name": "甲"}]}).status_code == 403


def test_csv_headers_are_normalized(client, importer):
    content = "﻿name, id_card , gender ,birth_date,address,village\n张三,110101199001011234,男,1990-01-01,幸福村1号,幸福村\n"
    response = submit(client, importer, content_csv=content)
    assert response.status_code == 201, response.text
    batch = response.json()["batch"]
    assert batch["counts"]["valid"] == 1
    assert batch["counts"]["invalid"] == 0


def test_malformed_csv_is_row_level_error(client, importer):
    content = CSV_HEADER + "\n张三,110101199001011234,男,1990-01-01,幸福村1号,幸福村\n"
    response = submit(client, importer, content_csv=content)
    assert response.status_code == 201
    batch = response.json()["batch"]
    assert batch["counts"]["invalid"] == 1
    row = client.get(f"/api/imports/{batch['id']}/rows", headers=importer).json()["data"][0]
    assert any("表头" in error["message"] for error in row["errors"])


def test_missing_csv_headers_rejected(client, importer):
    response = submit(client, importer, content_csv="name,gender\n张三,男\n")
    assert response.status_code == 422
    assert "缺少必需列" in response.json()["error"]["message"]


def test_superseded_batch_cannot_be_decided_or_confirmed(client, importer, reviewer):
    first = submit(client, importer).json()["batch"]
    submit(client, importer, content_csv=csv_body(f"张三,{CARD_2},男,1990-01-01,,幸福村1号,幸福村,,"))
    decided = client.post(f"/api/imports/{first['id']}/decisions", headers=reviewer,
                          json={"decisions": [{"row_number": 1, "decision": "skip"}]})
    assert decided.status_code == 409
    confirmed = client.post(f"/api/imports/{first['id']}/confirm", headers=reviewer)
    assert confirmed.status_code == 409


def test_missing_batch_returns_404(client, importer):
    assert client.get("/api/imports/9999", headers=importer).status_code == 404
    assert client.get("/api/imports/9999/rows", headers=importer).status_code == 404


def test_import_state_survives_restart(client, admin, importer, reviewer):
    batch = submit(client, importer).json()["batch"]

    from app.database import close_connection
    close_connection()
    from app.main import app

    with TestClient(app) as restarted:
        login = restarted.post("/api/auth/login", json={"username": "import.reviewer", "password": "Clerk!23456", "client_label": "tests"})
        headers = {"Authorization": f"Bearer {login.json()['token']}"}
        detail = restarted.get(f"/api/imports/{batch['id']}", headers=headers)
        assert detail.status_code == 200
        assert detail.json()["status"] == "staged"
        assert detail.json()["counts"]["valid"] == 1

        confirmed = restarted.post(f"/api/imports/{batch['id']}/confirm", headers=headers)
        assert confirmed.status_code == 200, confirmed.text
        writes = restarted.get(f"/api/imports/{batch['id']}/writes", headers=headers)
        assert len(writes.json()["writes"]) == 1
