"""批次登记与余量查询。"""
from conftest import produce, register


def test_register_and_query(client):
    register(client, "RM-1", "100.000")
    r = client.get("/api/v1/batches/RM-1")
    assert r.status_code == 200
    body = r.json()
    assert body["initial_qty"] == "100.000"
    assert body["remaining_qty"] == "100.000"
    assert body["quarantined"] is False
    assert body["kind"] == "raw"


def test_batch_no_cannot_be_reused(client):
    register(client, "RM-1", "10")
    r = register(client, "RM-1", "20", expected=409)
    assert r.json()["error"]["code"] == "BATCH_NO_EXISTS"


def test_non_positive_qty_rejected(client):
    for bad in ("0", "-5"):
        r = register(client, "X-" + bad, bad, expected=422)
        assert r.json()  # 标准 pydantic 错误体
