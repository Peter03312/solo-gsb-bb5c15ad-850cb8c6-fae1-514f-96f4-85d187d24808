"""测试夹具：每个用例独立的 SQLite 临时库 + TestClient。"""
import os
import tempfile

import pytest

# 必须在导入 app.* 之前设置
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["TRACE_DATABASE_URL"] = f"sqlite+pysqlite:///{_tmp.name}"

from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _database():
    db.init_db()
    yield
    db._engine.dispose()
    os.unlink(_tmp.name)


@pytest.fixture(autouse=True)
def _clean_tables():
    # 用例间清空数据（表本身保留），保证用例独立
    with db._engine.begin() as conn:
        for table in reversed(db.Base.metadata.sorted_tables):
            conn.execute(table.delete())
    yield


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def register(client, batch_no, qty, unit="kg", note=None, expected=201):
    payload = {"batch_no": batch_no, "qty": qty, "unit": unit}
    if note is not None:
        payload["note"] = note
    r = client.post("/api/v1/batches", json=payload)
    assert r.status_code == expected, r.text
    return r


def produce(client, request_id, output, out_qty, inputs, unit="kg", expected=None):
    payload = {
        "request_id": request_id,
        "output_batch_no": output,
        "output_qty": out_qty,
        "unit": unit,
        "inputs": [{"batch_no": b, "used_qty": q} for b, q in inputs],
    }
    r = client.post("/api/v1/productions", json=payload)
    if expected is not None:
        assert r.status_code == expected, r.text
    return r
