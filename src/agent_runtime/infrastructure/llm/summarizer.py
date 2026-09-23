"""摘要器装配：**一次 JUDGE_LLM 调用 + 调用方给的 prompt**（纯装配，零 import 期第三方依赖）。

为什么要抽出来：memory 的"任务结束整理"与 Phase 5 的"上下文压缩"都需要"用 JUDGE_LLM 把一段文本压短" ——
**prompt 不同、plumbing 相同**。抽成一处，避免两份 key/base_url 解析逻辑慢慢漂移
（Phase 4 的 memory 装配漂移就是这类问题）。

`provider_factory` 可注入：沙箱装不上 openai，注入假 provider 才能在无依赖环境里验证"怎么解析配置、
prompt 怎么格式化、回复怎么 strip"（与 `RedisShortTermMemory(client=...)`、
`OpenAICompatibleEmbedder(transport_factory=...)` 同一手法）。
"""
from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from ...core.llm.types import Message

Summarizer = Callable[[str], Awaitable[str]]


def _usage_tokens(response: Any) -> int:
    """provider 报的 `total_tokens`；没报就是 0 —— **不估算**（估出来的数字进成本表就是假账）。"""
    usage = getattr(response, "token_usage", None)
    try:
        return int(getattr(usage, "total_tokens", 0) or 0)
    except (TypeError, ValueError):
        return 0


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
        # 这次调用发生在**任务中途**：默认 60s 会让一步卡到 60 秒，跟随工具超时配置
        timeout=float(getattr(settings, "tool_http_timeout_seconds", 60.0)),
    )

    # 摘要调用是**额外**成本（消融要单独归因）：调用方读 `.stats` 拿增量，
    # 不必自己去掐表或翻 provider 的响应。
    stats = {"calls": 0, "total_tokens": 0, "total_ms": 0}

    async def summarize(text: str) -> str:
        started = time.perf_counter()
        try:
            resp = await provider.chat(
                [Message(role="user", content=prompt.format(text=text))]
            )
        except BaseException:
            # 墙钟是真花掉了（token 未知所以不记）：真实消融撞限流/超时时，
            # 摘要耗时不该被系统性少计（审查 M-8）
            stats["total_ms"] += int((time.perf_counter() - started) * 1000)
            raise
        stats["calls"] += 1
        stats["total_tokens"] += _usage_tokens(resp)
        stats["total_ms"] += int((time.perf_counter() - started) * 1000)
        return (resp.content or "").strip()

    summarize.stats = stats
    return summarize
