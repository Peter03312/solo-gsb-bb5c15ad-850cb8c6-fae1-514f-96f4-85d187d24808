"""业务服务层：批次登记、生产提交（事务 + 行锁）、谱系、隔离。

所有校验失败抛出 ServiceError，由 API 层转成统一 JSON 错误；
异常即回滚，保证"整笔拒绝、不改变余量"。
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import get_settings
from .db import Batch, IdempotencyKey, ProductionInput, ProductionRun
from .schemas import ProduceRequest, RegisterBatchRequest

ZERO = Decimal("0")


class ServiceError(Exception):
    """可预期的业务错误。"""

    def __init__(self, code: str, message: str, http_status: int = 422):
        self.code = code
        self.message = message
        self.http_status = http_status
        super().__init__(message)


# ---------- 批次登记 ----------

def register_batch(session: Session, req: RegisterBatchRequest) -> Batch:
    """登记原料批次（批次编号全局唯一，不得复用）。"""
    existing = session.get(Batch, req.batch_no)
    if existing is not None:
        raise ServiceError(
            "BATCH_NO_EXISTS",
            f"batch number already exists: {req.batch_no}",
            http_status=409,
        )
    batch = Batch(
        batch_no=req.batch_no,
        kind="raw",
        unit=req.unit,
        initial_qty=req.qty,
        remaining_qty=req.qty,
        note=req.note,
    )
    session.add(batch)
    try:
        session.commit()
    except IntegrityError:  # 并发下另一请求抢先建号
        session.rollback()
        raise ServiceError(
            "BATCH_NO_EXISTS",
            f"batch number already exists: {req.batch_no}",
            http_status=409,
        )
    return batch


# ---------- 生产提交 ----------

def _content_hash(req: ProduceRequest) -> str:
    """规范化业务内容并哈希，用于识别"同键异内容"。"""
    payload = json.dumps(
        {
            "output_batch_no": req.output_batch_no,
            "output_qty": format(req.output_qty, "f"),
            "unit": req.unit,
            "inputs": [
                {"batch_no": i.batch_no, "used_qty": format(i.used_qty, "f")}
                for i in req.inputs
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def produce(session: Session, req: ProduceRequest) -> tuple[ProductionRun, bool]:
    """提交一笔生产记录。

    返回 (生产记录, 是否本次实际执行)；幂等重试时 created=False。
    全部操作在单个事务内，对投入批次排序后加行锁，避免并发超用与互锁。
    """
    content_hash = _content_hash(req)

    # 1) 幂等快速路径（未持锁）
    existing_key = session.get(IdempotencyKey, req.request_id)
    if existing_key is not None:
        if existing_key.request_hash != content_hash:
            raise ServiceError(
                "IDEMPOTENCY_CONFLICT",
                "same request_id was already used with different content",
                http_status=409,
            )
        run = session.get(ProductionRun, existing_key.run_id)
        return run, False

    # 2) 排序锁定全部投入批次（与并发提交的加锁顺序一致，杜绝互锁）
    input_nos = sorted({i.batch_no for i in req.inputs})
    rows = session.scalars(
        select(Batch).where(Batch.batch_no.in_(input_nos)).with_for_update()
    ).all()
    locked = {b.batch_no: b for b in rows}

    # 3) 业务校验 —— 任一不过则抛错回滚，余量分毫不动
    missing = [no for no in input_nos if no not in locked]
    if missing:
        raise ServiceError(
            "SOURCE_NOT_FOUND",
            f"input batch(es) not found: {', '.join(missing)}",
        )

    quarantined = [no for no in input_nos if locked[no].quarantined]
    if quarantined:
        raise ServiceError(
            "BATCH_QUARANTINED",
            f"quarantined batch(es) cannot be consumed: {', '.join(quarantined)}",
        )

    units = {locked[no].unit for no in input_nos}
    units.add(req.unit)
    if len(units) != 1:
        raise ServiceError(
            "UNIT_MISMATCH",
            "all input batches and the output must share one unit",
        )

    total_input = ZERO
    for item in req.inputs:
        batch = locked[item.batch_no]
        if batch.remaining_qty < item.used_qty:
            raise ServiceError(
                "INSUFFICIENT_BALANCE",
                f"insufficient balance of {item.batch_no}: "
                f"remaining={batch.remaining_qty}, requested={item.used_qty}",
            )
        total_input += item.used_qty

    if req.output_qty > total_input:
        raise ServiceError(
            "OUTPUT_EXCEEDS_INPUT",
            f"output qty {req.output_qty} exceeds total input {total_input}",
        )

    # 产出编号不得与已有批次（原料或产出）复用
    if session.get(Batch, req.output_batch_no) is not None:
        raise ServiceError(
            "BATCH_NO_EXISTS",
            f"output batch number already exists: {req.output_batch_no}",
            http_status=409,
        )

    # 4) 落库：产出批次 -> 生产记录 -> 扣减余量 -> 幂等键，同一事务提交
    output = Batch(
        batch_no=req.output_batch_no,
        kind="output",
        unit=req.unit,
        initial_qty=req.output_qty,
        remaining_qty=req.output_qty,
    )
    session.add(output)
    session.flush()  # 仅为建立外键，尚未提交，出错仍整体回滚

    run = ProductionRun(
        output_batch_no=req.output_batch_no,
        output_qty=req.output_qty,
        unit=req.unit,
    )
    session.add(run)
    session.flush()

    for item in req.inputs:
        # 原子条件扣减：数据库层保证并发下不会超用（余量不足时影响 0 行）。
        # PostgreSQL 上前面的 FOR UPDATE 已串行化，此语句是双保险；
        # SQLite 不支持行锁，该语句是并发正确性的最终保障。
        result = session.execute(
            update(Batch)
            .where(
                Batch.batch_no == item.batch_no,
                Batch.remaining_qty >= item.used_qty,
            )
            .values(remaining_qty=Batch.remaining_qty - item.used_qty)
        )
        if result.rowcount != 1:
            raise ServiceError(
                "INSUFFICIENT_BALANCE",
                f"insufficient balance of {item.batch_no}: requested={item.used_qty}",
            )
        session.add(
            ProductionInput(
                run_id=run.id,
                input_batch_no=item.batch_no,
                used_qty=item.used_qty,
            )
        )

    session.add(
        IdempotencyKey(
            key=req.request_id, request_hash=content_hash, run_id=run.id
        )
    )

    try:
        session.commit()
    except IntegrityError:
        # 并发下：幂等键已被另一请求插入，或产出编号已被占用
        session.rollback()
        prior = session.get(IdempotencyKey, req.request_id)
        if prior is not None:
            if prior.request_hash != content_hash:
                raise ServiceError(
                    "IDEMPOTENCY_CONFLICT",
                    "same request_id was already used with different content",
                    http_status=409,
                )
            return session.get(ProductionRun, prior.run_id), False
        # 其余唯一键冲突即编号复用
        raise ServiceError(
            "BATCH_NO_EXISTS",
            f"batch number already exists: {req.output_batch_no}",
            http_status=409,
        )

    # 重新加载，使返回值与数据库规整后的值（精度、时间戳）一致，保证重试结果逐字节相同
    session.refresh(run)
    return run, True


# ---------- 余量查询 ----------

def get_batch(session: Session, batch_no: str) -> Batch:
    batch = session.get(Batch, batch_no)
    if batch is None:
        raise ServiceError(
            "BATCH_NOT_FOUND", f"batch not found: {batch_no}", http_status=404
        )
    return batch


# ---------- 谱系 ----------

def _edges_from(session: Session, input_nos: list[str]) -> list:
    if not input_nos:
        return []
    return list(
        session.scalars(
            select(ProductionInput).where(ProductionInput.input_batch_no.in_(input_nos))
        ).all()
    )


def _edges_to(session: Session, output_nos: list[str]) -> list:
    if not output_nos:
        return []
    runs = list(
        session.scalars(
            select(ProductionRun.id).where(ProductionRun.output_batch_no.in_(output_nos))
        ).all()
    )
    if not runs:
        return []
    return list(
        session.scalars(
            select(ProductionInput).where(ProductionInput.run_id.in_(runs))
        ).all()
    )


def _walk_down(session: Session, start: str) -> tuple[set[str], set[str], dict[int, ProductionInput]]:
    """沿 input -> output 方向 BFS。

    返回 (第一层子节点, 全部后代, 沿途全部边)。
    """
    edges: dict[int, ProductionInput] = {}
    children: set[str] = set()
    seen: set[str] = set()
    frontier = [start]
    first = True
    while frontier:
        nxt: set[str] = set()
        for e in _edges_from(session, frontier):
            edges[e.id] = e
            out_no = session.get(ProductionRun, e.run_id).output_batch_no
            nxt.add(out_no)
            if first:
                children.add(out_no)
        nxt -= seen
        seen |= nxt
        frontier = list(nxt)
        first = False
    return children, seen, edges


def _walk_up(session: Session, start: str) -> tuple[set[str], set[str], dict[int, ProductionInput]]:
    """沿 output -> input 方向 BFS，返回 (第一层父节点, 全部祖先, 沿途全部边)。"""
    edges: dict[int, ProductionInput] = {}
    parents: set[str] = set()
    seen: set[str] = set()
    frontier = [start]
    first = True
    while frontier:
        nxt: set[str] = set()
        for e in _edges_to(session, frontier):
            edges[e.id] = e
            nxt.add(e.input_batch_no)
            if first:
                parents.add(e.input_batch_no)
        nxt -= seen
        seen |= nxt
        frontier = list(nxt)
        first = False
    return parents, seen, edges


def lineage(session: Session, batch_no: str) -> dict:
    """查询批次的直接/全部上下游及谱系边。方向：投入(input) -> 产出(output)。"""
    root = get_batch(session, batch_no)

    children, descendants, down_edges = _walk_down(session, batch_no)
    parents, ancestors, up_edges = _walk_up(session, batch_no)
    edges_all = {**up_edges, **down_edges}

    all_nos = {batch_no, *descendants, *ancestors}
    batches = {
        b.batch_no: b
        for b in session.scalars(select(Batch).where(Batch.batch_no.in_(all_nos))).all()
    }

    def node(no: str) -> dict:
        b = batches[no]
        return {
            "batch_no": b.batch_no,
            "kind": b.kind,
            "remaining_qty": b.remaining_qty,
            "quarantined": b.quarantined,
        }

    run_cache: dict[int, ProductionRun] = {}

    def run_of(run_id: int) -> ProductionRun:
        if run_id not in run_cache:
            run_cache[run_id] = session.get(ProductionRun, run_id)
        return run_cache[run_id]

    edges = [
        {
            "run_id": e.run_id,
            "input_batch_no": e.input_batch_no,
            "output_batch_no": run_of(e.run_id).output_batch_no,
            "used_qty": e.used_qty,
        }
        for e in edges_all.values()
    ]
    edges.sort(key=lambda e: (e["run_id"], e["input_batch_no"]))

    return {
        "batch": root,
        "parents": [node(no) for no in sorted(parents)],
        "ancestors": [node(no) for no in sorted(ancestors)],
        "children": [node(no) for no in sorted(children)],
        "descendants": [node(no) for no in sorted(descendants)],
        "edges": edges,
    }


# ---------- 污染隔离 ----------

def quarantine(session: Session, batch_no: str, reason: str | None) -> dict:
    """标记批次疑似污染，并隔离其全部后代产出批次（单事务）。

    混料时产出批次整体隔离；隔离批次不得再投料（produce 中强制）。
    """
    from .db import utcnow

    source = session.get(Batch, batch_no, with_for_update=True)
    if source is None:
        raise ServiceError(
            "BATCH_NOT_FOUND", f"batch not found: {batch_no}", http_status=404
        )

    # BFS 找出全部后代
    descendants: list[str] = []
    seen: set[str] = set()
    frontier = [batch_no]
    while frontier:
        edges = _edges_from(session, frontier)
        outs = {session.get(ProductionRun, e.run_id).output_batch_no for e in edges}
        next_frontier = [o for o in outs if o not in seen]
        seen.update(next_frontier)
        descendants.extend(next_frontier)
        frontier = next_frontier

    now = utcnow()
    full_reason = reason or "suspected contamination"
    if not source.quarantined:
        source.quarantined = True
        source.quarantine_reason = full_reason
        source.quarantined_at = now

    affected: list[str] = []
    for no in descendants:
        b = session.get(Batch, no, with_for_update=True)
        if b is not None and not b.quarantined:
            b.quarantined = True
            b.quarantine_reason = f"propagated from contaminated batch {batch_no}"
            b.quarantined_at = now
            affected.append(no)

    session.commit()
    return {
        "batch_no": batch_no,
        "quarantined": True,
        "reason": source.quarantine_reason,
        "affected_descendants": sorted(set(descendants)),
    }
