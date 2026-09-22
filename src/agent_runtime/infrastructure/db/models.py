"""SQLAlchemy 2.0 模型：`memory_entries`。

**本文件的用途是"schema 的单一事实来源"**：运行时查询走 `infrastructure/memory/long_term.py` 里的显式 SQL
（选它的理由：异步 ORM 的隐式 lazy-load 容易踩坑，而显式 SQL 能用假会话在沙箱内把 SQL 与结果解释测完），
但**表结构**由 Alembic 从这里的 `Base.metadata` 推导 / 对齐，避免"迁移文件与模型各写一份、慢慢漂移"。

维度固定 1024（spec D2）：换 embedding 模型必须同时改这里、改迁移、并重算历史向量。
"""
from __future__ import annotations

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

EMBEDDING_DIM = 1024


class Base(DeclarativeBase):
    pass


class MemoryEntryRow(Base):
    __tablename__ = "memory_entries"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False, server_default="agent")
    meta: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_memory_entries_created_at", created_at.desc()),
        Index("ix_memory_entries_session", "session_id"),
        Index(
            "ix_memory_entries_embedding",
            "embedding",
            postgresql_using="ivfflat",
            postgresql_ops={"embedding": "vector_cosine_ops"},
            postgresql_with={"lists": 100},
        ),
    )
