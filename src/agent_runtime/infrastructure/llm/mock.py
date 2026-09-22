"""脚本化 LLM：按预设序列返回响应；耗尽后永久重复最后一条。测试与离线演示用。"""
from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace

from ...core.llm.base import BaseLLMProvider
from ...core.llm.types import LLMResponse, LLMStreamChunk, Message


class MockLLMProvider(BaseLLMProvider):
    def __init__(self, script: list[LLMResponse]):
        if not script:
            raise ValueError("script must not be empty")
        self._script = list(script)
        self.calls = 0
        self.last_tools: list[dict] | None = None
        self.last_messages: list[Message] | None = None

    async def chat(self, messages: list[Message], tools: list[dict] | None = None) -> LLMResponse:
        self.calls += 1
        self.last_tools = tools
        self.last_messages = list(messages)
        if len(self._script) == 1:
            r = self._script[0]
        else:
            r = self._script.pop(0)
        return replace(r)

    async def stream(self, messages: list[Message], tools: list[dict] | None = None) -> AsyncIterator[LLMStreamChunk]:
        r = await self.chat(messages, tools)
        yield LLMStreamChunk(delta_content=r.content, delta_tool_call=None, finish_reason="stop")
