"""批次追溯服务 HTTP API。

统一错误体：
    {"error": {"code": "...", "message": "..."}}
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import service
from .db import ProductionInput, get_session, init_db
from .schemas import (
    BatchOut,
    LineageOut,
    ProduceRequest,
    QuarantineOut,
    RegisterBatchRequest,
    RunInputOut,
    RunOut,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Compose 首次启动：建库建表（幂等）
    init_db()
    yield


app = FastAPI(
    title="食品工厂批次追溯服务",
    version="1.0.0",
    lifespan=lifespan,
)


@app.exception_handler(service.ServiceError)
async def service_error_handler(request, exc: service.ServiceError):
    return JSONResponse(
        status_code=exc.http_status,
        content={"error": {"code": exc.code, "message": exc.message}},
    )


class HealthOut(BaseModel):
    status: str


@app.get("/health", response_model=HealthOut, tags=["meta"])
def health() -> HealthOut:
    return HealthOut(status="ok")


@app.post("/api/v1/batches", response_model=BatchOut, status_code=201, tags=["batches"])
def register(
    req: RegisterBatchRequest,
    session: Session = Depends(get_session),
) -> BatchOut:
    """登记一个原料批次（编号全局唯一，不可复用）。"""
    return BatchOut.model_validate(service.register_batch(session, req))


@app.get("/api/v1/batches/{batch_no}", response_model=BatchOut, tags=["batches"])
def get_balance(
    batch_no: str,
    session: Session = Depends(get_session),
) -> BatchOut:
    """查询批次余量与隔离状态。"""
    return BatchOut.model_validate(service.get_batch(session, batch_no))


class ProduceResponse(BaseModel):
    run: RunOut
    # 幂等重试时为 true：本次未重复扣减，返回的是首次提交的结果
    idempotent_replayed: bool


@app.post("/api/v1/productions", response_model=ProduceResponse, status_code=201, tags=["production"])
def produce(
    req: ProduceRequest,
    session: Session = Depends(get_session),
) -> ProduceResponse:
    """提交一笔生产：多个投入批次 + 各自用量 -> 一个产出批次。

    - 数量必须为正、单位一致、产出不超过投入总量
    - 来源不存在 / 用量无效 / 余量不足 / 批次已隔离 -> 整笔拒绝
    - 同 request_id 重试返回原结果；同 request_id 异内容 -> 409
    """
    run, created = service.produce(session, req)
    inputs = session.scalars(
        select(ProductionInput)
        .where(ProductionInput.run_id == run.id)
        .order_by(ProductionInput.input_batch_no)
    ).all()
    out = RunOut(
        run_id=run.id,
        output_batch_no=run.output_batch_no,
        output_qty=run.output_qty,
        unit=run.unit,
        inputs=[RunInputOut(batch_no=i.input_batch_no, used_qty=i.used_qty) for i in inputs],
        created_at=run.created_at,
    )
    return ProduceResponse(run=out, idempotent_replayed=not created)


@app.get(
    "/api/v1/batches/{batch_no}/lineage",
    response_model=LineageOut,
    tags=["genealogy"],
)
def lineage(
    batch_no: str,
    session: Session = Depends(get_session),
) -> LineageOut:
    """查询直接/全部上游（祖先）与下游（后代）谱系。"""
    return LineageOut.model_validate(service.lineage(session, batch_no))


class QuarantineRequest(BaseModel):
    reason: str | None = None


@app.post(
    "/api/v1/batches/{batch_no}/quarantine",
    response_model=QuarantineOut,
    tags=["quarantine"],
)
def quarantine(
    batch_no: str,
    body: QuarantineRequest | None = None,
    session: Session = Depends(get_session),
) -> QuarantineOut:
    """标记批次疑似污染；自动隔离其全部后代产出批次。已隔离者不能再投料。"""
    reason = body.reason if body is not None else None
    return QuarantineOut.model_validate(service.quarantine(session, batch_no, reason))
