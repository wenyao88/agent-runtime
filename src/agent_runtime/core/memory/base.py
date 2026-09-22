from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class MemoryEntry:
    content: str
    role: str = "agent"
    metadata: dict = field(default_factory=dict)
    embedding: list[float] | None = None
    id: str | None = None


@dataclass
class MemoryQuery:
    text: str | None = None
    embedding: list[float] | None = None
    top_k: int = 5
    metadata_filters: dict | None = None


class BaseMemory(ABC):
    @abstractmethod
    async def store(self, entry: MemoryEntry) -> str: ...
    @abstractmethod
    async def query(self, query: MemoryQuery) -> list[MemoryEntry]: ...
    @abstractmethod
    async def clear(self) -> None: ...
