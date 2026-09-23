"""摘要器装配：**一次 JUDGE_LLM 调用 + 调用方给的 prompt**（纯装配，零 import 期第三方依赖）。

为什么要抽出来：memory 的"任务结束整理"与 Phase 5 的"上下文压缩"都需要"用 JUDGE_LLM 把一段文本压短" ——
**prompt 不同、plumbing 相同**。抽成一处，避免两份 key/base_url 解析逻辑慢慢漂移
（Phase 4 的 memory 装配漂移就是这类问题）。

`provider_factory` 可注入：沙箱装不上 openai，注入假 provider 才能在无依赖环境里验证"怎么解析配置、
prompt 怎么格式化、回复怎么 strip"（与 `RedisShortTermMemory(client=...)`、
`OpenAICompatibleEmbedder(transport_factory=...)` 同一手法）。
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from ...core.llm.types import Message

Summarizer = Callable[[str], Awaitable[str]]


def build_summarizer(
    settings: Any, prompt: str, *, provider_factory: Any = None
) -> Summarizer | None:
    """按 JUDGE_LLM 配置构造摘要器；没有可用 key 就返回 None，由调用方决定怎么降级。

    `temperature=0.0`：摘要要的是稳定复现，不是创意。
    """
    api_key = settings.judge_llm_api_key or settings.llm_api_key
    if not api_key:
        return None

    if provider_factory is None:
        # 惰性 import：默认关时不该因为缺 openai 而影响装配
        from .openai_compatible import OpenAICompatibleProvider

        provider_factory = OpenAICompatibleProvider

    provider = provider_factory(
        api_key=api_key,
        base_url=settings.judge_llm_base_url or settings.llm_base_url,
        model=settings.judge_llm_model,
        temperature=0.0,
    )

    async def summarize(text: str) -> str:
        resp = await provider.chat(
            [Message(role="user", content=prompt.format(text=text))]
        )
        return (resp.content or "").strip()

    return summarize
