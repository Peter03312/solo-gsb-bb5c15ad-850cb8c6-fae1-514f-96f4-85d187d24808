"""请求 / 响应数据模型。结构性校验（非空、正数、去重）在此完成，
业务校验（存在性、余量、隔离、单位）在 service 层完成。"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    field_validator,
    model_validator,
)
from typing_extensions import Annotated

# 数量入参接受数字/字符串并转为 Decimal；出参统一序列化为字符串，杜绝 JSON 浮点误差
Qty = Annotated[
    Decimal,
    PlainSerializer(lambda v: format(v, "f") if v is not None else v, return_type=str),
]


def _limit_scale(v) -> Decimal:
    """统一为 Decimal 并限制最多 3 位小数，避免存储舍入与幂等哈希不一致。"""
    if isinstance(v, float):
        v = Decimal(str(v))
    elif not isinstance(v, Decimal):
        v = Decimal(v)
    if v.as_tuple().exponent < -3:
        raise ValueError("quantity may have at most 3 decimal places")
    return v


# 输入数量：先规范化/限精度，再由各字段的 Field(gt=0) 校验正数
PositiveQty = Annotated[Qty, BeforeValidator(_limit_scale)]


class RegisterBatchRequest(BaseModel):
    batch_no: str = Field(min_length=1, max_length=64, examples=["RM-20260930-01"])
    qty: PositiveQty = Field(gt=0, examples=["100.000"])
    unit: str = Field(min_length=1, max_length=16, examples=["kg"])
    note: str | None = Field(default=None, max_length=512)

    @field_validator("batch_no", "unit")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        return v


class InputItem(BaseModel):
    batch_no: str = Field(min_length=1, max_length=64)
    used_qty: PositiveQty = Field(gt=0)

    @field_validator("batch_no")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        return v


class ProduceRequest(BaseModel):
    # 调用方生成的请求标识，用于幂等
    request_id: str = Field(min_length=1, max_length=128)
    output_batch_no: str = Field(min_length=1, max_length=64, examples=["FG-001"])
    output_qty: PositiveQty = Field(gt=0, examples=["80.000"])
    unit: str = Field(min_length=1, max_length=16, examples=["kg"])
    inputs: list[InputItem] = Field(min_length=1)

    @field_validator("request_id", "output_batch_no", "unit")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        return v

    @model_validator(mode="after")
    def _unique_inputs(self) -> "ProduceRequest":
        seen: set[str] = set()
        for item in self.inputs:
            if item.batch_no in seen:
                raise ValueError(f"duplicate input batch: {item.batch_no}")
            seen.add(item.batch_no)
        # 延迟导入避免配置模块循环依赖
        from .config import get_settings

        limit = get_settings().max_inputs
        if len(self.inputs) > limit:
            raise ValueError(f"too many input batches: {len(self.inputs)} > {limit}")
        return self


# ---------- 响应 ----------

class BatchOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    batch_no: str
    kind: str
    unit: str
    initial_qty: Qty
    remaining_qty: Qty
    quarantined: bool
    quarantine_reason: str | None = None
    quarantined_at: datetime | None = None
    note: str | None = None
    created_at: datetime


class RunInputOut(BaseModel):
    batch_no: str
    used_qty: Qty


class RunOut(BaseModel):
    run_id: int
    output_batch_no: str
    output_qty: Qty
    unit: str
    inputs: list[RunInputOut]
    created_at: datetime


class LineageNode(BaseModel):
    batch_no: str
    kind: str
    remaining_qty: Qty
    quarantined: bool


class LineageEdge(BaseModel):
    run_id: int
    input_batch_no: str
    output_batch_no: str
    used_qty: Qty


class LineageOut(BaseModel):
    batch: BatchOut
    # 直接 / 全部上游（该批次由哪些批次产出）
    parents: list[LineageNode]
    ancestors: list[LineageNode]
    # 直接 / 全部下游（该批次又投入了哪些产出）
    children: list[LineageNode]
    descendants: list[LineageNode]
    edges: list[LineageEdge]


class QuarantineOut(BaseModel):
    batch_no: str
    quarantined: bool
    reason: str | None = None
    affected_descendants: list[str]
