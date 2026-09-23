"""Trace 存储（纯逻辑，零第三方）。

**为什么要有这一层**：`Tracer` 原先只有一个 `_current_session`，跑完就没了 ——
Trace 页/`GET /api/traces` 无从查起。Store 是"跑完之后还能看"的唯一来源。

两个口径：
  * 列出用 `summary()`：**不**把逐条 steps/events 拖给列表接口（几十条 trace × 每条几十步会很笨重）；
  * 容量满时**淘汰最旧的**：内存环形，避免长跑时把进程吃满（默认 50 条，见 spec §4.3）。
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from .models import TraceEvent, TraceSession

DEFAULT_CAPACITY = 50


@runtime_checkable
class TraceStore(Protocol):
    """存储契约：`Tracer` 只依赖这四个方法（SQLite 实现见 infrastructure）。"""

    def save(self, session: TraceSession) -> None: ...

    def get(self, trace_id: str) -> TraceSession | None: ...

    def list(self, limit: int = DEFAULT_CAPACITY) -> list[dict]: ...

    def events(self, trace_id: str) -> list[TraceEvent]: ...


class InMemoryTraceStore:
    """进程内环形存储（默认实现：零配置、重启即丢，天花板写在 README）。

    `dict` 保持插入顺序，所以"最旧的"就是第一个键；重复 `save` 同一个 trace_id 只覆盖、不新增
    （同一个 trace 结束时可能再写一次，比如补 final_answer）。
    """

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        self._capacity = max(0, int(capacity or 0))
        self._sessions: dict[str, TraceSession] = {}

    @property
    def capacity(self) -> int:
        return self._capacity

    def save(self, session: TraceSession) -> None:
        if session is None or not getattr(session, "trace_id", ""):
            return
        trace_id = str(session.trace_id)
        self._sessions.pop(trace_id, None)  # 覆盖时也算"最近写入"
        self._sessions[trace_id] = session
        while len(self._sessions) > self._capacity:
            oldest = next(iter(self._sessions))
            self._sessions.pop(oldest, None)

    def get(self, trace_id: str) -> TraceSession | None:
        return self._sessions.get(str(trace_id or ""))

    def list(self, limit: int = DEFAULT_CAPACITY) -> list[dict]:
        size = max(0, int(limit if limit is not None else DEFAULT_CAPACITY))
        if size == 0:
            return []
        newest_first = reversed(list(self._sessions.values()))
        return [session.summary() for _, session in zip(range(size), newest_first)]

    def events(self, trace_id: str) -> list[TraceEvent]:
        session = self.get(trace_id)
        return list(session.events) if session is not None else []

    def clear(self) -> None:
        self._sessions.clear()

    def __len__(self) -> int:
        return len(self._sessions)
