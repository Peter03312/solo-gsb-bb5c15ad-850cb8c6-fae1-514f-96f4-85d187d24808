"""并发提交：多个线程同时消耗同一批次，总扣减不得超过余量。"""
import threading

from app import db
from app.schemas import ProduceRequest
from app.service import ServiceError, produce as service_produce
from conftest import register


def test_concurrent_consumption_never_overuses(client):
    register(client, "RM-C", "100")

    n = 10
    per = 15  # 总量 150 > 100，恰好只有部分请求应成功
    results: list[bool] = []
    errors: list[str] = []
    lock = threading.Lock()

    def worker(i: int):
        session = db.SessionLocal()
        try:
            req = ProduceRequest(
                request_id=f"c-{i}",
                output_batch_no=f"FG-C{i}",
                output_qty=15,
                unit="kg",
                inputs=[{"batch_no": "RM-C", "used_qty": per}],
            )
            service_produce(session, req)
            with lock:
                results.append(True)
        except ServiceError as exc:
            with lock:
                results.append(False)
                errors.append(exc.code)
        finally:
            session.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    success = sum(results)
    assert success == 6  # 6 * 15 = 90；第 7 笔只剩 10，不足，拒绝
    assert all(e == "INSUFFICIENT_BALANCE" for e in errors)

    remaining = client.get("/api/v1/batches/RM-C").json()["remaining_qty"]
    assert remaining == "10.000"
