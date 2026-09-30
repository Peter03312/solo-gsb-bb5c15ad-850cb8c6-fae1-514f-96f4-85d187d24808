"""谱系查询与污染隔离传播。"""
from conftest import produce, register


def _chain(client):
    # RM-1, RM-2 -> FG-1 ; FG-1 + RM-3 -> FG-2 ; FG-2 -> FG-3
    register(client, "RM-1", "100")
    register(client, "RM-2", "40")
    register(client, "RM-3", "100")
    produce(client, "r1", "FG-1", "90", [("RM-1", "70"), ("RM-2", "30")])
    produce(client, "r2", "FG-2", "100", [("FG-1", "50"), ("RM-3", "60")])
    produce(client, "r3", "FG-3", "80", [("FG-2", "80")])


def test_lineage_up_and_down(client):
    _chain(client)
    r = client.get("/api/v1/batches/FG-2/lineage")
    assert r.status_code == 200
    body = r.json()
    assert {p["batch_no"] for p in body["parents"]} == {"FG-1", "RM-3"}
    assert {a["batch_no"] for a in body["ancestors"]} == {"FG-1", "RM-1", "RM-2", "RM-3"}
    assert {c["batch_no"] for c in body["children"]} == {"FG-3"}
    assert {d["batch_no"] for d in body["descendants"]} == {"FG-3"}


def test_lineage_from_raw_to_all_descendants(client):
    _chain(client)
    body = client.get("/api/v1/batches/RM-1/lineage").json()
    assert {d["batch_no"] for d in body["descendants"]} == {"FG-1", "FG-2", "FG-3"}
    assert body["parents"] == []


def test_quarantine_propagates_to_all_descendants(client):
    _chain(client)
    r = client.post("/api/v1/batches/RM-2/quarantine", json={"reason": "foreign matter"})
    assert r.status_code == 200
    body = r.json()
    assert set(body["affected_descendants"]) == {"FG-1", "FG-2", "FG-3"}

    for no in ("RM-2", "FG-1", "FG-2", "FG-3"):
        b = client.get(f"/api/v1/batches/{no}").json()
        assert b["quarantined"] is True


def test_quarantined_batch_cannot_be_consumed(client):
    _chain(client)
    client.post("/api/v1/batches/RM-1/quarantine", json={})
    # 直接使用被隔离批次
    r = produce(client, "r-x", "FG-X", "5", [("RM-1", "5")], expected=422)
    assert r.json()["error"]["code"] == "BATCH_QUARANTINED"
    # 下游已整体隔离，同样不能投料
    r = produce(client, "r-y", "FG-Y", "5", [("FG-2", "5"), ("RM-3", "5")], expected=422)
    assert r.json()["error"]["code"] == "BATCH_QUARANTINED"
    # 干净批次余量未受影响
    assert client.get("/api/v1/batches/RM-3").json()["remaining_qty"] == "40.000"
    assert client.get("/api/v1/batches/FG-X").status_code == 404


def test_quarantine_unknown_batch_404(client):
    r = client.post("/api/v1/batches/NOPE/quarantine", json={})
    assert r.status_code == 404


def test_lineage_unknown_batch_404(client):
    assert client.get("/api/v1/batches/NOPE/lineage").status_code == 404
