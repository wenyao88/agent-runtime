"""OpenAI 兼容 Provider：覆盖硅基流动 / DeepSeek / Qwen / GLM 等 OpenAI 格式端点。"""
from __future__ import annotations

from collections.abc import AsyncIterator

from openai import AsyncOpenAI

from ...core.llm.base import BaseLLMProvider
from ...core.llm.types import (
    FunctionCall, LLMResponse, LLMStreamChunk, Message, TokenUsage,
)


def _to_openai_messages(messages: list[Message]) -> list[dict]:
    out: list[dict] = []
    for m in messages:
        d: dict = {"role": m.role}
        if m.content is not None:
            d["content"] = m.content
        if m.tool_calls:
            d["tool_calls"] = [
                {"id": t.id, "type": "function",
                 "function": {"name": t.name, "arguments": t.arguments or "{}"}}
                for t in m.tool_calls
            ]
        if m.tool_call_id:
            d["tool_call_id"] = m.tool_call_id
        out.append(d)
    return out


class OpenAICompatibleProvider(BaseLLMProvider):
    def __init__(self, api_key: str, base_url: str, model: str, timeout: float = 60.0):
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=timeout)
        self._model = model

    async def chat(self, messages: list[Message], tools: list[dict] | None = None) -> LLMResponse:
        kw: dict = {"model": self._model, "messages": _to_openai_messages(messages)}
        if tools:
            kw["tools"] = tools
        r = await self._client.chat.completions.create(**kw)
        choice = r.choices[0] if r.choices else None
        msg = getattr(choice, "message", None) if choice else None
        tool_calls = [
            FunctionCall(id=tc.id, name=tc.function.name, arguments=tc.function.arguments or "{}")
            for tc in (getattr(msg, "tool_calls", None) or [])
        ]
        u = getattr(r, "usage", None)
        usage = TokenUsage(
            prompt_tokens=getattr(u, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(u, "completion_tokens", 0) or 0,
            total_tokens=getattr(u, "total_tokens", 0) or 0,
        )
        return LLMResponse(
            content=(msg.content if msg else None),
            tool_calls=tool_calls,
            token_usage=usage,
            finish_reason=(getattr(choice, "finish_reason", None) or "stop"),
        )

    async def stream(self, messages: list[Message], tools: list[dict] | None = None) -> AsyncIterator[LLMStreamChunk]:
        kw: dict = {"model": self._model, "messages": _to_openai_messages(messages), "stream": True}
        if tools:
            kw["tools"] = tools
        s = await self._client.chat.completions.create(**kw)
        async for chunk in s:
            if not chunk.choices:
                continue
            ch = chunk.choices[0]
            delta = ch.delta
            dtc = None
            tcs = getattr(delta, "tool_calls", None)
            if tcs:
                t = tcs[0]
                fn = getattr(t, "function", None)
                dtc = FunctionCall(
                    id=getattr(t, "id", None),
                    name=(fn.name if fn and fn.name else ""),
                    arguments=(fn.arguments if fn and fn.arguments else ""),
                )
            yield LLMStreamChunk(
                delta_content=getattr(delta, "content", None),
                delta_tool_call=dtc,
                finish_reason=getattr(ch, "finish_reason", None),
            )
