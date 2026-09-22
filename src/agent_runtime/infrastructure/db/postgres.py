"""数据库引擎/会话工厂（惰性 SQLAlchemy）。

为何单独一层：`long_term.py` 只依赖"会话工厂"这个鸭子类型接口，
于是沙箱内可以用假会话把 SQL 与结果解释逻辑测完（本环境装不上 SQLAlchemy）。
"""
from __future__ import annotations

from typing import Any


class DatabaseUnavailable(Exception):
    """数据库不可用（缺依赖 / 连不上）。由装配层记录，不阻断启动与任务。"""


def create_engine_and_sessionmaker(dsn: str) -> tuple[Any, Any]:
    """返回 `(engine, sessionmaker)`；缺依赖时抛可读的 `DatabaseUnavailable`。"""
    if not dsn or not str(dsn).strip():
        raise DatabaseUnavailable("DATABASE_URL 为空：无法建立数据库连接")
    try:
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    except ImportError:
        raise DatabaseUnavailable(
            'sqlalchemy 未安装：请先 `pip install "sqlalchemy[asyncio]" asyncpg`'
        ) from None

    engine = create_async_engine(str(dsn), pool_pre_ping=True)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def ping(engine: Any) -> None:
    """探活：连不上时抛 `DatabaseUnavailable`（消息里**不含**连接串密码）。"""
    from sqlalchemy import text

    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except DatabaseUnavailable:
        raise
    except Exception as e:  # noqa: BLE001 —— 归一为"不可用"
        raise DatabaseUnavailable(f"数据库连接失败：{type(e).__name__}: {e}") from None
