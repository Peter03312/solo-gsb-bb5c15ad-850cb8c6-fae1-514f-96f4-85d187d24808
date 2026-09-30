"""输入校验边界。"""
from conftest import produce, register


def test_qty_accepts_string_and_number(client):
    r = register(client, "RM-S", 100)  # JSON 数字
    assert r.status_code == 201
    r = client.post("/api/v1/productions", json={
        "request_id": "r1", "output_batch_no": "FG-1", "output_qty": "5.5",
        "unit": "kg", "inputs": [{"batch_no": "RM-S", "used_qty": 5.5}],
    })
    assert r.status_code == 201, r.text
    assert r.json()["run"]["output_qty"] == "5.500"
    assert client.get("/api/v1/batches/RM-S").json()["remaining_qty"] == "94.500"


def test_zero_and_negative_qty_rejected(client):
    r = client.post("/api/v1/batches", json={"batch_no": "RM-0", "qty": "0", "unit": "kg"})
    assert r.status_code == 422
    r = client.post("/api/v1/batches", json={"batch_no": "RM-N", "qty": "-1", "unit": "kg"})
    assert r.status_code == 422


def test_too_many_decimal_places_rejected(client):
    r = register(client, "RM-X", "1.0001", expected=422)
    assert "decimal places" in r.text


def test_empty_inputs_rejected(client):
    register(client, "RM-A", "10")
    r = client.post("/api/v1/productions", json={
        "request_id": "r1", "output_batch_no": "FG-1", "output_qty": "1",
        "unit": "kg", "inputs": [],
    })
    assert r.status_code == 422


def test_blank_fields_rejected(client):
    r = client.post("/api/v1/batches", json={"batch_no": "   ", "qty": "1", "unit": "kg"})
    assert r.status_code == 422


def test_quarantine_empty_body_ok(client):
    register(client, "RM-A", "10")
    r = client.post("/api/v1/batches/RM-A/quarantine")
    assert r.status_code == 200
    assert r.json()["reason"] == "suspected contamination"


def test_404_body_shape(client):
    r = client.get("/api/v1/batches/NOPE")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "BATCH_NOT_FOUND"
