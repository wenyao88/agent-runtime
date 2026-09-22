from .base import MemoryEntry, MemoryQuery
from .working import WorkingMemory


class MemoryManager:
    def __init__(
        self,
        working: WorkingMemory | None = None,
        short_term=None,
        long_term=None,
    ):
        self.working = working or WorkingMemory()
        self.short_term = short_term
        self.long_term = long_term

    async def store(self, entry: MemoryEntry) -> str:
        id_ = await self.working.store(entry)
        if self.short_term:
            await self.short_term.store(entry)
        if self.long_term:
            await self.long_term.store(entry)
        return id_

    async def recall(self, query: MemoryQuery) -> list[MemoryEntry]:
        results = await self.working.query(query)
        if len(results) >= query.top_k:
            return results[: query.top_k]
        if self.short_term:
            results.extend(await self.short_term.query(query))
        if len(results) >= query.top_k:
            return results[: query.top_k]
        if self.long_term:
            results.extend(await self.long_term.query(query))
        return results[: query.top_k]

    async def consolidate(self, task_summary: str) -> None:
        entry = MemoryEntry(content=task_summary, role="agent", metadata={"type": "summary"})
        if self.short_term:
            await self.short_term.store(entry)
        if self.long_term:
            await self.long_term.store(entry)
