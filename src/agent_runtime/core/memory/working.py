from .base import BaseMemory, MemoryEntry, MemoryQuery


class WorkingMemory(BaseMemory):
    def __init__(self):
        self._store: dict[str, str] = {}
        self._entries: list[MemoryEntry] = []
        self._counter = 0

    async def store(self, entry: MemoryEntry) -> str:
        self._counter += 1
        entry.id = str(self._counter)
        self._entries.append(entry)
        if entry.metadata.get("key"):
            self._store[entry.metadata["key"]] = entry.content
        return entry.id

    async def query(self, query: MemoryQuery) -> list[MemoryEntry]:
        results = []
        for e in reversed(self._entries):
            if query.text and query.text.lower() in e.content.lower():
                results.append(e)
            if len(results) >= query.top_k:
                break
        return results

    async def clear(self) -> None:
        self._store.clear()
        self._entries.clear()

    def get(self, key: str) -> str | None:
        return self._store.get(key)

    def set(self, key: str, value: str) -> None:
        self._store[key] = value
