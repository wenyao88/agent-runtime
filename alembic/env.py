"""Alembic 环境（async）。

连接串从 **`os.environ["DATABASE_URL"]`** 读，而不是走 app 的 `Settings`：
迁移要在"服务还没起来"时也能跑（`docker compose run api alembic upgrade head`），
不该因为 Settings 里其它字段缺失而失败；也避免把密码写进 `alembic.ini`。

`target_metadata` 指向 `infrastructure/db/models.py` 的 `Base.metadata` —— 这样
`alembic revision --autogenerate` 生成的迁移与模型不会各写一份、慢慢漂移。
"""
from __future__ import annotations

import asyncio
import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_runtime.infrastructure.db.models import Base  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    url = (os.environ.get("DATABASE_URL") or "").strip()
    if not url:
        raise RuntimeError(
            "DATABASE_URL 未设置：请先 `export DATABASE_URL=postgresql+asyncpg://...` 再执行迁移"
        )
    return url


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = _database_url()
    connectable = async_engine_from_config(
        configuration, prefix="sqlalchemy.", poolclass=pool.NullPool
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
