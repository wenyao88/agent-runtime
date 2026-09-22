"""三层记忆编排：级联召回、逐层标注、**失败一律不外抛**。

本模块的三条契约（都有测试钉死）：
  1. **级联与短路**：working → short_term → long_term，够 `top_k` 即停（不查更慢的持久层）；
  2. **任何一层抛异常都不外抛** —— `react.py` 结尾的 `await self.memory.store(...)` 没有任何 try 保护，
     接上 PG/Redis 后一次写库失败会把整轮任务炸掉。错误记进 `self.errors`（有上限：manager 是进程单例，
     不设上限会随任务数无限增长）；
  3. **consolidate 失败降级为原文截断存储** —— 宁可存粗糙的原文，也不要丢掉这次任务的记忆；
     截断**带标记**（本项目在 Demo 1 吃过"无标记截断把残缺当完整"的亏）。
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import replace

from .annotate import (
    SOURCE_LONG,
    SOURCE_SHORT,
    SOURCE_WORKING,
    dedupe,
    sort_entries,
    with_source,
)
from .base import MemoryEntry, MemoryQuery, normalize_session_id
from .working import WorkingMemory

MAX_ERRORS = 50
MAX_CONSOLIDATE_CHARS = 2000
TRUNCATED_MARK = "…（已截断）"

Summarizer = Callable[[str], Awaitable[str]]


class MemoryManager:
    def __init__(
        self,
        working=None,
        short_term=None,
        long_term=None,
        summarizer: Summarizer | None = None,
    ):
        self.working = working or WorkingMemory()
        self.short_term = short_term
        self.long_term = long_term
        self.summarizer = summarizer
        self.errors: list[str] = []

    # ---- internals ----
    def _record(self, message: str) -> None:
        self.errors.append(message)
        if len(self.errors) > MAX_ERRORS:
            del self.errors[:-MAX_ERRORS]

    async def _safe_query(self, layer, query: MemoryQuery, source: str) -> list[MemoryEntry]:
        if layer is None:
            return []
        try:
            entries = await layer.query(query)
        except Exception as e:  # noqa: BLE001 —— by design：记忆层失败不得影响任务
            self._record(f"{source} 查询失败：{type(e).__name__}: {e}")
            return []
        return with_source(list(entries or []), source)

    async def _safe_store(self, layer, source: str, entry: MemoryEntry) -> None:
        if layer is None:
            return
        try:
            await layer.store(entry)
        except Exception as e:  # noqa: BLE001 —— by design
            self._record(f"{source} 写入失败：{type(e).__name__}: {e}")

    # ---- public API ----
    async def store(self, entry: MemoryEntry) -> str:
        id_ = ""
        try:
            id_ = await self.working.store(entry)
        except Exception as e:  # noqa: BLE001 —— by design
            self._record(f"{SOURCE_WORKING} 写入失败：{type(e).__name__}: {e}")
        await self._persist(entry)
        return id_ or ""

    async def _summarize(self, text: str) -> str:
        """用注入的 summarizer 压成一条长期记忆；失败/空结果 → 降级为原文截断。"""
        if self.summarizer is None:
            return text
        try:
            produced = await self.summarizer(text)
        except Exception as e:  # noqa: BLE001 —— by design：摘要失败不丢记忆
            self._record(f"摘要失败，降级为原文：{type(e).__name__}: {e}")
            return text
        if isinstance(produced, str) and produced.strip():
            return produced.strip()
        return text

    async def _persist(self, entry: MemoryEntry) -> None:
        """写入**持久层**（short_term / long_term）。

        `store`（每轮任务结束）与 `consolidate`（显式整理）共用这一条路径 ——
        否则两处各写一遍会**双写**，或者摘要器永远不被调用（就成了死配置）。
        持久层只留摘要后的内容；**工作层留原文**（同会话内召回要的是原始细节）。
        持久内容一律截断并带标记：持久层不该存无界文本。
        """
        text = await self._summarize(entry.content or "")
        if len(text) > MAX_CONSOLIDATE_CHARS:
            text = text[:MAX_CONSOLIDATE_CHARS] + TRUNCATED_MARK
        persisted = replace(entry, content=text)  # 不改原对象：工作层那份要保留原文
        await self._safe_store(self.short_term, SOURCE_SHORT, persisted)
        await self._safe_store(self.long_term, SOURCE_LONG, persisted)

    async def recall(self, query: MemoryQuery) -> list[MemoryEntry]:
        merged = await self._safe_query(self.working, query, SOURCE_WORKING)
        if len(merged) < query.top_k:
            merged.extend(await self._safe_query(self.short_term, query, SOURCE_SHORT))
        if len(merged) < query.top_k:
            merged.extend(await self._safe_query(self.long_term, query, SOURCE_LONG))
        return sort_entries(dedupe(merged))[: query.top_k]

    async def consolidate(self, task_summary: str, session_id: str = "") -> None:
        """显式整理一段文本并写入持久层（供调用方 / Benchmark 使用）。"""
        text = (task_summary or "").strip()
        if not text:
            return
        entry = MemoryEntry(
            content=text,
            role="agent",
            metadata={"type": "summary", "session_id": normalize_session_id(session_id)},
        )
        await self._persist(entry)
