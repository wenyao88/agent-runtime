from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from .types import LLMResponse, LLMStreamChunk, Message


class BaseLLMProvider(ABC):
    @abstractmethod
    async def chat(
        self, messages: list[Message], tools: list[dict] | None = None
    ) -> LLMResponse: ...

    @abstractmethod
    async def stream(
        self, messages: list[Message], tools: list[dict] | None = None
    ) -> AsyncIterator[LLMStreamChunk]: ...
