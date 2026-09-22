from .base import BaseMemory, MemoryEntry, MemoryQuery


class ShortTermMemory(BaseMemory):
    async def store(self, entry: MemoryEntry) -> str:
        return "stub"
    async def query(self, query: MemoryQuery) -> list[MemoryEntry]:
        return []
    async def clear(self) -> None:
        pass
