from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class MemoryEntry:
    content: str
    role: str = "agent"
    metadata: dict = field(default_factory=dict)
    embedding: list[float] | None = None
    id: str | None = None
    # `source` / `created_at` 是**查询期**语义：同一条记忆在不同层来源不同，
    # 由各层在返回时填充，再经 annotate 归一（Phase 4）。
    source: str = ""
    created_at: datetime | None = None


@dataclass
class MemoryQuery:
    text: str | None = None
    embedding: list[float] | None = None
    top_k: int = 5
    metadata_filters: dict | None = None
    session_id: str | None = None


class BaseMemory(ABC):
    @abstractmethod
    async def store(self, entry: MemoryEntry) -> str: ...
    @abstractmethod
    async def query(self, query: MemoryQuery) -> list[MemoryEntry]: ...
    @abstractmethod
    async def clear(self) -> None: ...


DEFAULT_SESSION_ID = "default"


def normalize_session_id(value: str | None) -> str:
    """会话标识归一：空串/纯空白/None 一律落到 `default`。

    统一在这里归一，避免"有的地方判 None、有的地方判空串"导致同一会话被拆成两个键。
    """
    return (value or "").strip() or DEFAULT_SESSION_ID
