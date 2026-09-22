"""PG + pgvector 长时记忆（惰性 SQLAlchemy + 可注入 sessionmaker/embedder）。

运行时用**显式 SQL**（不是异步 ORM 查询）：可测试性更高（假会话能捕获 SQL 与参数），
也避免异步 lazy-load 的隐式 IO 坑。表结构的事实来源是 `infrastructure/db/models.py`，由 Alembic 对齐。

两条硬约定：
  * `query.embedding` 已给时**绝不重复调 embedding 接口** —— 按 token 计费，重复调用就是直接浪费钱；
  * 维度不匹配必须给**点名两个维度**的可读错误（换模型没迁移时最常见的故障），而不是让 PG 抛类型错误。
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from ...core.memory.base import BaseMemory, MemoryEntry, MemoryQuery, normalize_session_id
from ..db.postgres import DatabaseUnavailable, create_engine_and_sessionmaker, ping

DEFAULT_DIMENSION = 1024
DEFAULT_TABLE = "memory_entries"

__all__ = [
    "DatabaseUnavailable",
    "EmbeddingDimMismatch",
    "PostgresLongTermMemory",
]


class EmbeddingDimMismatch(Exception):
    """向量维度与表定义不一致（通常是换了 embedding 模型但没迁移）。"""


def _vector_literal(vector: list[float]) -> str:
    """把向量绑成 **pgvector 的文本字面量** `[0.1,0.2,…]`，而不是 Python list。

    为什么（Phase 4 审查标为最高未验证风险）：asyncpg 要注册 vector 编解码器（`register_vector`）才能直接收 list，
    而本仓库没有这个调用 —— 若它拒绝 list，`store`/`query` 会全部失败并被逐层 try 吞掉，
    表现为"长期记忆静默失效"。`CAST(:vec AS vector)` 本身就能吃文本字面量，
    所以绑字符串可以**彻底去掉这个隐式前提**，不依赖任何编解码器。
    """
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    """兼容 dict 行与 SQLAlchemy Row（`._mapping`）。"""
    if isinstance(row, dict):
        return row.get(key, default)
    mapping = getattr(row, "_mapping", None)
    if mapping is not None:
        return mapping.get(key, default)
    return getattr(row, key, default)


def _as_meta(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


class PostgresLongTermMemory(BaseMemory):
    def __init__(
        self,
        *,
        session_factory: Any = None,
        dsn: str = "",
        embedder: Any = None,
        dimension: int = DEFAULT_DIMENSION,
        table: str = DEFAULT_TABLE,
    ) -> None:
        self._session_factory = session_factory
        self._dsn = dsn
        self._embedder = embedder
        self._dimension = int(dimension)
        self._table = table
        self._engine: Any = None

    # ── 连接 ──

    async def connect(self) -> None:
        if self._session_factory is None:
            engine, session_factory = create_engine_and_sessionmaker(self._dsn)
            self._engine = engine
            self._session_factory = session_factory
        if self._engine is not None:
            await ping(self._engine)

    async def aclose(self) -> None:
        if self._engine is not None and hasattr(self._engine, "dispose"):
            await self._engine.dispose()

    # ── 向量 ──

    def _check_dim(self, vector: list[float], *, where: str) -> list[float]:
        if len(vector) != self._dimension:
            raise EmbeddingDimMismatch(
                f"{where} 的向量维度 {len(vector)} 与表定义 {self._dimension} 不一致："
                "请确认 EMBEDDING_MODEL / MEMORY_EMBEDDING_DIM 是否与迁移一致，"
                "换模型后需要重新迁移并重算历史向量"
            )
        return list(vector)

    async def _vector_for(self, content: str, *, where: str) -> list[float]:
        if self._embedder is None:
            raise EmbeddingDimMismatch(
                f"{where} 需要向量但未配置 embedder（EMBEDDING_API_KEY 是否缺失？）"
            )
        vectors = await self._embedder.embed([content])
        if not vectors:
            raise EmbeddingDimMismatch(f"{where} 的 embedding 调用没有返回向量")
        return self._check_dim(list(vectors[0]), where=where)

    # ── 语句构造 ──

    @staticmethod
    def _stmt(sql: str) -> Any:
        """把 SQL 包成 SQLAlchemy 语句；**缺 SQLAlchemy 时原样返回字符串**。

        为什么允许这个降级：本层接受**注入的 session**。沙箱里注入的是纯标准库假会话
        （它只对语句做 `str()` 并记录参数），于是"发了什么 SQL、怎么解释结果"能在装不上 SQLAlchemy
        的环境里被测到；真实运行必然装了 SQLAlchemy，走的是 `text()` 分支。
        """
        try:
            from sqlalchemy import text
        except ImportError:
            return sql
        return text(sql)

    # ── 读写 ──

    async def store(self, entry: MemoryEntry) -> str:
        if self._session_factory is None:
            await self.connect()
        if entry.embedding is not None:
            vector = self._check_dim(list(entry.embedding), where="memory.store")
        else:
            vector = await self._vector_for(entry.content, where="memory.store")

        sql = (
            f"INSERT INTO {self._table} (content, role, meta, embedding, session_id) "
            "VALUES (:content, :role, CAST(:meta AS jsonb), CAST(:vec AS vector), :session_id) "
            "RETURNING id"
        )
        params = {
            "content": entry.content,
            "role": entry.role,
            "meta": json.dumps(entry.metadata or {}, ensure_ascii=False, default=str),
            "vec": _vector_literal(vector),
            "session_id": normalize_session_id(entry.metadata.get("session_id")),
        }
        async with self._session_factory() as session:
            result = await session.execute(self._stmt(sql), params)
            new_id = result.scalar()
            await session.commit()
        return str(new_id or "")

    async def query(self, query: MemoryQuery) -> list[MemoryEntry]:
        if self._session_factory is None:
            await self.connect()
        if query.embedding is not None:
            vector = self._check_dim(list(query.embedding), where="memory.query")
        elif query.text:
            vector = await self._vector_for(query.text, where="memory.query")
        else:
            return []

        sql = (
            f"SELECT id, content, role, meta, created_at FROM {self._table} "
            "WHERE embedding IS NOT NULL "
            "ORDER BY embedding <=> CAST(:vec AS vector) "
            "LIMIT :k"
        )
        async with self._session_factory() as session:
            result = await session.execute(
                self._stmt(sql), {"vec": _vector_literal(vector), "k": int(query.top_k)}
            )
            rows = list(result.fetchall())

        entries: list[MemoryEntry] = []
        for row in rows:
            content = _row_get(row, "content")
            if not isinstance(content, str) or not content:
                continue
            created = _row_get(row, "created_at")
            entries.append(
                MemoryEntry(
                    content=content,
                    role=str(_row_get(row, "role") or "agent"),
                    metadata=_as_meta(_row_get(row, "meta")),
                    created_at=created if isinstance(created, datetime) else None,
                    id=str(_row_get(row, "id") or "") or None,
                )
            )
        return entries

    async def clear(self) -> None:
        if self._session_factory is None:
            await self.connect()
        async with self._session_factory() as session:
            await session.execute(self._stmt(f"DELETE FROM {self._table}"))
            await session.commit()
