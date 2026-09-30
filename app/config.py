"""运行时配置，全部可通过环境变量覆盖（见 README）。"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TRACE_", env_file=".env", extra="ignore")

    # SQLAlchemy 数据库地址。
    # Compose 下默认连接 postgres 服务；本地纯功能测试可用 sqlite+pysqlite:///./trace.db
    database_url: str = "postgresql+psycopg2://trace:trace@db:5432/trace"

    # HTTP 服务监听
    host: str = "0.0.0.0"
    port: int = 8080

    # 单条生产记录允许的最多投入批次数（防误传超大请求）
    max_inputs: int = 500


@lru_cache
def get_settings() -> Settings:
    return Settings()
