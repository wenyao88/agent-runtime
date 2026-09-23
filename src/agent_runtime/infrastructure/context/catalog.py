"""上下文装配（**只有一处**）：API 与 Demo 脚本共用。

与 `tools/catalog.py` / `skills/catalog.py` / `memory/catalog.py` 同一先例：deps 依赖 pydantic_settings
（沙箱装不上），逻辑一放那儿，"摘要器到底有没有接上、缺配置时是否只记错误"就都无法在沙箱内验证。

摘要器是**可选能力**（默认关）：它每次压缩都多花一次 LLM 调用，而且发生在**任务中途** ——
失败会影响正在跑的任务，所以缺 key / 缺依赖一律只记原因、绝不抛。
"""
from __future__ import annotations

from typing import Any

from ...core.context.budget import TokenBudget
from ...core.context.manager import ContextManager
from ...core.context.summarize import SUMMARY_PROMPT


def build_context_manager(
    settings: Any, *, provider_factory: Any = None
) -> tuple[ContextManager, list[str]]:
    """按 settings 装配 `ContextManager`，返回 `(manager, 错误列表)`。

    `provider_factory` 仅供测试注入（沙箱装不上 openai），生产路径传 None 即用真实 provider。
    """
    errors: list[str] = []

    summarizer = None
    if getattr(settings, "agent_compaction_summarize_enabled", False):
        try:
            from ..llm.summarizer import build_summarizer

            summarizer = build_summarizer(
                settings, SUMMARY_PROMPT, provider_factory=provider_factory
            )
        except Exception as e:  # noqa: BLE001 —— 摘要是可选能力：坏了就跳过并说明，不阻断启动
            errors.append(f"上下文摘要器未启用：{type(e).__name__}: {e}")
        else:
            if summarizer is None:
                errors.append(
                    "AGENT_COMPACTION_SUMMARIZE_ENABLED=true 但缺 JUDGE_LLM_API_KEY"
                    "（也没有 LLM_API_KEY）：压缩会降级为直接丢弃最旧消息"
                )

    manager = ContextManager(
        budget=TokenBudget(
            model_max_tokens=settings.llm_max_tokens,
            compaction_ratio=settings.agent_context_compaction_threshold,
        ),
        memory_max_chars=settings.memory_inject_max_chars,
        summarizer=summarizer,
    )
    return manager, errors
