"""记忆层装配（**只有一处**）：API 与 Demo 脚本共用。

为什么放在 infrastructure 而不是 `api/deps.py`：与 `skills/catalog.py` / `tools/catalog.py` 同理，
而且这是本机实测踩出来的教训 —— `scripts/run_demo1_github.py` 曾自己拼一套
`MemoryManager(working=WorkingMemory())`，与 API 的装配漂移：demo 用 `--session-id my-test`
跑完，Redis 里 `session:my-test:*` 一条都没有、召回永远为空（`session_id` 本身是传对了的）。
装配只要存在两处，就一定会漂移。

三条不变量（有测试钉死）：
  1. **默认全关**（spec D4）：`MEMORY_*_ENABLED` 不开就绝不建层；
  2. 建不了（缺 key / 缺依赖 / 策略非法）→ 跳过该层 + 写进错误列表，**绝不抛、绝不阻断启动**；
  3. 连接不在构造期做（避免启动阻塞）：层内部惰性 connect，失败由 `MemoryManager` 逐层捕获记账。
"""
from __future__ import annotations

from typing import Any

from ...core.memory.manager import MemoryManager


def build_memory_manager(settings: Any) -> tuple[MemoryManager, list[str]]:
    """按 settings 装配三层记忆，返回 `(manager, 错误列表)`。

    `settings` 是鸭子类型（沙箱里装不上 pydantic_settings），所以这里只用属性、不做类型检查。
    """
    errors: list[str] = []

    short_term = None
    if settings.memory_short_term_enabled:
        try:
            from ...core.memory.short_term import ShortTermPolicy
            from .short_term import RedisShortTermMemory

            short_term = RedisShortTermMemory(
                url=settings.redis_url,
                policy=ShortTermPolicy(
                    ttl_seconds=settings.memory_short_term_ttl_seconds,
                    max_items=settings.memory_short_term_max_items,
                ),
            )
        except Exception as e:  # noqa: BLE001 —— 记忆是可选能力：坏了就跳过该层并说明，不阻断启动
            errors.append(f"短时记忆层未启用：{type(e).__name__}: {e}")

    long_term = None
    if settings.memory_long_term_enabled:
        if not settings.embedding_api_key:
            errors.append(
                "MEMORY_LONG_TERM_ENABLED=true 但缺少 EMBEDDING_API_KEY：已跳过长期记忆层"
            )
        else:
            try:
                from .embedding import OpenAICompatibleEmbedder
                from .long_term import PostgresLongTermMemory

                long_term = PostgresLongTermMemory(
                    dsn=settings.database_url,
                    embedder=OpenAICompatibleEmbedder(
                        api_key=settings.embedding_api_key,
                        base_url=settings.embedding_base_url,
                        model=settings.embedding_model,
                        timeout=float(settings.tool_http_timeout_seconds),
                    ),
                    dimension=settings.memory_embedding_dim,
                )
            except Exception as e:  # noqa: BLE001
                errors.append(f"长期记忆层未启用：{type(e).__name__}: {e}")

    summarizer = None
    if settings.memory_consolidate_enabled:
        try:
            summarizer = build_summarizer(settings)
        except Exception as e:  # noqa: BLE001 —— 缺 openai 等不该让整个进程起不来
            errors.append(f"记忆整理（consolidate）未启用：{type(e).__name__}: {e}")
    if summarizer is not None and short_term is None and long_term is None:
        errors.append(
            "MEMORY_CONSOLIDATE_ENABLED=true 但两个持久层都未启用：不会调用摘要模型（避免白烧 LLM 调用）"
        )
    return MemoryManager(short_term=short_term, long_term=long_term, summarizer=summarizer), errors


def build_summarizer(settings: Any):
    """任务结束时的整理用 **JUDGE_LLM**（spec D5）：省主模型上下文、可独立换模型。

    没有可用的 key 就返回 None —— 此时 `MemoryManager.consolidate` 会退化为"存原文截断"，
    宁可存粗糙原文也不丢记忆。**惰性 import**：默认关时不该因为缺 openai 而影响装配。
    """
    api_key = settings.judge_llm_api_key or settings.llm_api_key
    if not api_key:
        return None

    from ...core.llm.types import Message
    from ..llm.openai_compatible import OpenAICompatibleProvider

    provider = OpenAICompatibleProvider(
        api_key=api_key,
        base_url=settings.judge_llm_base_url or settings.llm_base_url,
        model=settings.judge_llm_model,
        temperature=0.0,
    )
    prompt = (
        "把下面这段任务记录压缩成一条可复用的长期记忆（保留结论与关键事实，去掉过程细节），"
        "只输出压缩后的内容，不要任何前后缀：\n\n{text}"
    )

    async def summarize(text: str) -> str:
        resp = await provider.chat([Message(role="user", content=prompt.format(text=text))])
        return (resp.content or "").strip()

    return summarize
