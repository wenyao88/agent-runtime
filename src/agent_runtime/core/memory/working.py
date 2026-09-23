from .base import BaseMemory, MemoryEntry, MemoryQuery


def _session_conflicts(query_session: object, entry_session: object) -> bool:
    """两侧**都**有会话且不同 → 冲突（该条目不参与召回）。

    任一侧没有会话时返回 `False`：不懂会话的调用方（早期测试、ad-hoc 键值用法）保持旧行为。
    """
    if not query_session or not entry_session:
        return False
    return str(entry_session) != str(query_session)


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
            # 会话隔离：上一条任务的记忆不该被下一条任务召回。
            # 这是 Phase 7 消融的前提 —— 否则 "+Memory" 组的效果来自偶发命中，数据不可解释。
            if _session_conflicts(query.session_id, e.metadata.get("session_id")):
                continue
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
