"""数据库模型与会话管理。

数量一律使用 NUMERIC + Python Decimal，避免浮点误差。
生产与隔离操作全部在单个事务内完成，配合行级锁保证并发安全。
"""
from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    sessionmaker,
)

from .config import get_settings

# 数量精度：最多 18 位、小数 3 位（千克等场景足够）
QTY = Numeric(18, 3, asdecimal=True)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Batch(Base):
    __tablename__ = "batches"
    __table_args__ = (
        CheckConstraint("initial_qty > 0", name="ck_batch_initial_positive"),
        CheckConstraint("remaining_qty >= 0", name="ck_batch_remaining_nonneg"),
    )

    batch_no: Mapped[str] = mapped_column(String(64), primary_key=True)
    # raw = 登记入库的原料批次；output = 生产产出批次
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    unit: Mapped[str] = mapped_column(String(16), nullable=False)
    initial_qty: Mapped[Decimal] = mapped_column(QTY, nullable=False)
    remaining_qty: Mapped[Decimal] = mapped_column(QTY, nullable=False)

    # 隔离状态：疑似污染批次本体、以及混料波及的全部产出批次
    quarantined: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    quarantine_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    quarantined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    inputs: Mapped[list["ProductionInput"]] = relationship(back_populates="batch")


class ProductionRun(Base):
    """一次生产：多个投入批次 -> 一个产出批次。"""

    __tablename__ = "production_runs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    output_batch_no: Mapped[str] = mapped_column(
        String(64), ForeignKey("batches.batch_no"), nullable=False, unique=True
    )
    output_qty: Mapped[Decimal] = mapped_column(QTY, nullable=False)
    unit: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class ProductionInput(Base):
    """生产投入明细：某条生产记录消耗了哪个批次、多少量。"""

    __tablename__ = "production_inputs"
    __table_args__ = (
        UniqueConstraint("run_id", "input_batch_no", name="uq_run_input"),
        Index("ix_input_batch", "input_batch_no"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("production_runs.id", ondelete="CASCADE"), nullable=False
    )
    input_batch_no: Mapped[str] = mapped_column(
        String(64), ForeignKey("batches.batch_no"), nullable=False
    )
    used_qty: Mapped[Decimal] = mapped_column(QTY, nullable=False)

    batch: Mapped[Batch] = relationship(back_populates="inputs")


class IdempotencyKey(Base):
    """生产提交的幂等键。与余量变更在同一事务内提交，杜绝"键已存但业务未生效"。"""

    __tablename__ = "idempotency_keys"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[int] = mapped_column(ForeignKey("production_runs.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


_settings = get_settings()
_engine = create_engine(
    _settings.database_url,
    pool_pre_ping=True,
    future=True,
)

# SQLite（本地/测试）：开启 WAL 与较长 busy timeout，使并发写入排队而非立即报锁错，
# 配合 ck_batch_remaining_nonneg 约束，并发扣减同样不会超用。
if _engine.url.get_backend_name() == "sqlite":

    @event.listens_for(_engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, future=True)


def init_db(retries: int = 30, delay: float = 2.0) -> None:
    """建库建表（启动时调用；Compose 首次启动自动执行）。

    等待数据库容器就绪（最多 retries*delay 秒）；目标库缺失时自动创建。
    """
    import time

    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError

    is_pg = _engine.url.get_backend_name() == "postgresql"
    ready = False
    for attempt in range(1, retries + 1):
        try:
            with _engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            ready = True
            break
        except OperationalError:
            if is_pg:
                # 数据库本身可能尚未创建：连默认库 postgres 尝试 CREATE DATABASE
                url = _engine.url
                admin_engine = create_engine(
                    url.set(database="postgres"),
                    isolation_level="AUTOCOMMIT",
                    future=True,
                )
                try:
                    with admin_engine.connect() as conn:
                        exists = conn.execute(
                            text("SELECT 1 FROM pg_database WHERE datname = :name"),
                            {"name": url.database},
                        ).scalar()
                        if not exists:
                            conn.execute(text(f'CREATE DATABASE "{url.database}"'))
                        ready = True
                        break
                except OperationalError:
                    pass  # postgres 服务尚未就绪，稍后重试
                finally:
                    admin_engine.dispose()
            if attempt == retries:
                raise
            time.sleep(delay)

    if not ready:
        raise RuntimeError("database not ready")
    Base.metadata.create_all(_engine)


def get_session() -> Iterator:
    """FastAPI 依赖：请求级会话，异常回滚。"""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
