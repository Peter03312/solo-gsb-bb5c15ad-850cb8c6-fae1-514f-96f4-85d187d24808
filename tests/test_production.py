"""生产提交：余量扣减、整笔拒绝、幂等。"""
from conftest import produce, register


def test_production_deducts_balances_and_creates_output(client):
    register(client, "RM-A", "100")
    register(client, "RM-B", "50")
    r = produce(client, "r1", "FG-1", "80", [("RM-A", "60"), ("RM-B", "30")])
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["idempotent_replayed"] is False
    assert body["run"]["output_batch_no"] == "FG-1"
    assert {i["batch_no"]: i["used_qty"] for i in body["run"]["inputs"]} == {
        "RM-A": "60.000",
        "RM-B": "30.000",
    }
    assert client.get("/api/v1/batches/RM-A").json()["remaining_qty"] == "40.000"
    assert client.get("/api/v1/batches/RM-B").json()["remaining_qty"] == "20.000"
    assert client.get("/api/v1/batches/FG-1").json()["remaining_qty"] == "80.000"


def test_output_must_not_exceed_total_input(client):
    register(client, "RM-A", "50")
    r = produce(client, "r1", "FG-X", "60", [("RM-A", "50")], expected=422)
    assert r.json()["error"]["code"] == "OUTPUT_EXCEEDS_INPUT"
    # 整笔拒绝：余量不变
    assert client.get("/api/v1/batches/RM-A").json()["remaining_qty"] == "50.000"


def test_source_not_found_whole_reject(client):
    register(client, "RM-A", "50")
    r = produce(client, "r1", "FG-X", "10", [("RM-A", "5"), ("GHOST", "5")], expected=422)
    assert r.json()["error"]["code"] == "SOURCE_NOT_FOUND"
    assert client.get("/api/v1/batches/RM-A").json()["remaining_qty"] == "50.000"
    assert client.get("/api/v1/batches/FG-X").status_code == 404


def test_insufficient_balance_whole_reject(client):
    register(client, "RM-A", "10")
    register(client, "RM-B", "100")
    r = produce(client, "r1", "FG-X", "15", [("RM-A", "11"), ("RM-B", "4")], expected=422)
    assert r.json()["error"]["code"] == "INSUFFICIENT_BALANCE"
    # 两个批次余量都不得变化
    assert client.get("/api/v1/batches/RM-A").json()["remaining_qty"] == "10.000"
    assert client.get("/api/v1/batches/RM-B").json()["remaining_qty"] == "100.000"


def test_unit_mismatch_rejected(client):
    register(client, "RM-A", "10", unit="kg")
    register(client, "RM-B", "10", unit="L")
    r = produce(client, "r1", "FG-X", "5", [("RM-A", "5"), ("RM-B", "5")],
                unit="kg", expected=422)
    assert r.json()["error"]["code"] == "UNIT_MISMATCH"


def test_duplicate_input_in_request_rejected(client):
    register(client, "RM-A", "10")
    r = produce(client, "r1", "FG-X", "5", [("RM-A", "2"), ("RM-A", "3")], expected=422)
    assert "duplicate" in r.text


def test_output_batch_no_cannot_reuse(client):
    register(client, "RM-A", "10")
    produce(client, "r1", "FG-1", "5", [("RM-A", "5")])
    # 产出编号与原料/已有产出均不可复用；新 request_id 也不行
    r = produce(client, "r2", "FG-1", "3", [("RM-A", "3")], expected=409)
    assert r.json()["error"]["code"] == "BATCH_NO_EXISTS"
    r = produce(client, "r3", "RM-A", "1", [("FG-1", "1")], expected=409)
    assert r.json()["error"]["code"] == "BATCH_NO_EXISTS"


def test_idempotent_retry_returns_same_result(client):
    register(client, "RM-A", "10")
    r1 = produce(client, "req-1", "FG-1", "5", [("RM-A", "5")])
    r2 = produce(client, "req-1", "FG-1", "5", [("RM-A", "5")])
    assert r1.status_code == 201
    assert r2.status_code == 201
    assert r2.json()["idempotent_replayed"] is True
    assert r2.json()["run"] == r1.json()["run"]
    # 只扣了一次
    assert client.get("/api/v1/batches/RM-A").json()["remaining_qty"] == "5.000"


def test_same_request_id_different_content_conflicts(client):
    register(client, "RM-A", "10")
    register(client, "RM-B", "10")
    produce(client, "req-1", "FG-1", "5", [("RM-A", "5")])
    r = produce(client, "req-1", "FG-2", "5", [("RM-B", "5")], expected=409)
    assert r.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    # 冲突内容未产生任何效果
    assert client.get("/api/v1/batches/FG-2").status_code == 404
    assert client.get("/api/v1/batches/RM-B").json()["remaining_qty"] == "10.000"
