from functools import lru_cache
from pathlib import Path

from agent_runtime.config.settings import Settings
from agent_runtime.core.agent.planner import TaskPlanner
from agent_runtime.core.agent.react import ReActLoop
from agent_runtime.core.context.budget import TokenBudget
from agent_runtime.core.context.manager import ContextManager
from agent_runtime.core.memory.manager import MemoryManager
from agent_runtime.core.skill.router import SkillRouter
from agent_runtime.core.tool.registry import ToolRegistry
from agent_runtime.core.trace.tracer import Tracer
from agent_runtime.infrastructure.skills.catalog import (
    register_skills_from_dir,
    resolve_skills_dir,
)
from agent_runtime.infrastructure.tools.catalog import register_native_tools

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_skill_errors: list[str] = []
_memory_errors: list[str] = []


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def get_tool_registry() -> ToolRegistry:
    """进程内共享的单一 registry。

    必须是单例：MCP 工具在 lifespan 里被发现并注入**同一个** registry；
    若每次调用都新建，/api/tools 与 Agent 就都看不到 MCP 工具。
    """
    settings = get_settings()
    registry = ToolRegistry()
    register_native_tools(
        registry,
        root=str(_PROJECT_ROOT),
        github_token=settings.github_token,
        web_search_provider=settings.web_search_provider,
        web_search_api_key=settings.web_search_api_key,
        http_timeout=float(settings.tool_http_timeout_seconds),
        max_chars=settings.tool_max_chars,
    )
    return registry


@lru_cache
def get_memory_manager() -> MemoryManager:
    """三层记忆装配（**默认全关**，spec D4）。

    必须是单例：Redis 客户端与 PG 连接池要复用，每次新建会漏连接。
    缺依赖 / 配置不足 → 该层**不构造**并把原因记进 `_memory_errors`（供 lifespan 暴露），**绝不阻断启动**。
    连接不在构造期做（避免启动阻塞）：层内部惰性 connect，失败由 `MemoryManager` 逐层捕获记账。
    """
    settings = get_settings()
    _memory_errors.clear()

    short_term = None
    if settings.memory_short_term_enabled:
        from agent_runtime.core.memory.short_term import ShortTermPolicy
        from agent_runtime.infrastructure.memory.short_term import RedisShortTermMemory

        short_term = RedisShortTermMemory(
            url=settings.redis_url,
            policy=ShortTermPolicy(
                ttl_seconds=settings.memory_short_term_ttl_seconds,
                max_items=settings.memory_short_term_max_items,
            ),
        )

    long_term = None
    if settings.memory_long_term_enabled:
        if not settings.embedding_api_key:
            _memory_errors.append(
                "MEMORY_LONG_TERM_ENABLED=true 但缺少 EMBEDDING_API_KEY：已跳过长期记忆层"
            )
        else:
            from agent_runtime.infrastructure.memory.embedding import (
                OpenAICompatibleEmbedder,
            )
            from agent_runtime.infrastructure.memory.long_term import PostgresLongTermMemory

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

    summarizer = _build_summarizer(settings) if settings.memory_consolidate_enabled else None
    return MemoryManager(short_term=short_term, long_term=long_term, summarizer=summarizer)


def _build_summarizer(settings: Settings):
    """任务结束时的整理用 **JUDGE_LLM**（spec D5）：省主模型上下文、可独立换模型。

    没有可用的 key 就返回 None —— 此时 `MemoryManager.consolidate` 会退化为"存原文截断"，
    宁可存粗糙原文也不丢记忆。**惰性 import**：默认关时不该因为缺 openai 而影响装配。
    """
    api_key = settings.judge_llm_api_key or settings.llm_api_key
    if not api_key:
        return None

    from agent_runtime.core.llm.types import Message
    from agent_runtime.infrastructure.llm.openai_compatible import OpenAICompatibleProvider

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


def get_memory_errors() -> list[str]:
    """记忆装配错误（缺依赖 / 缺 key）。只用于可见性，绝不影响启动。"""
    return list(_memory_errors)


def get_context_manager() -> ContextManager:
    settings = get_settings()
    return ContextManager(
        budget=TokenBudget(
            model_max_tokens=settings.llm_max_tokens,
            compaction_ratio=settings.agent_context_compaction_threshold,
        ),
        memory_max_chars=settings.memory_inject_max_chars,
    )


@lru_cache
def get_skill_router() -> SkillRouter:
    """进程内共享的单一 router。

    必须是单例（与 tool registry 同理）：技能在启动时加载一次，`/api/skills` 与 Agent
    必须看到**同一批**技能；每次新建就等于 Agent 永远看不到已加载的技能。
    加载错误记在模块级列表里，供 lifespan 放进 `app.state.skill_errors`。
    """
    settings = get_settings()
    router = SkillRouter()
    _skill_errors.clear()
    _skill_errors.extend(
        register_skills_from_dir(
            router, resolve_skills_dir(settings.skills_dir, str(_PROJECT_ROOT))
        )
    )
    return router


def get_skill_errors() -> list[str]:
    """技能加载错误（坏文件 / 目录缺失）。只用于可见性，绝不影响启动。"""
    return list(_skill_errors)


def get_tracer() -> Tracer:
    return Tracer()


def get_llm():
    """真实 Provider。惰性 import：未安装 openai 时本模块仍可导入（测试/降级友好）。"""
    from agent_runtime.infrastructure.llm.openai_compatible import OpenAICompatibleProvider

    s = get_settings()
    return OpenAICompatibleProvider(
        api_key=s.llm_api_key,
        base_url=s.llm_base_url,
        model=s.llm_model,
        temperature=s.llm_temperature,
    )


def get_agent(llm=None) -> ReActLoop:
    s = get_settings()
    provider = llm or get_llm()
    # 规划器默认关闭：它多花一次 LLM 调用，且只产出"参考计划"文本（不改变控制流）
    planner = TaskPlanner(llm=provider) if s.agent_task_planning_enabled else None
    return ReActLoop(
        llm=provider,
        tool_registry=get_tool_registry(),
        context_manager=get_context_manager(),
        memory_manager=get_memory_manager(),
        skill_router=get_skill_router(),
        planner=planner,
        skill_top_k=s.agent_skill_top_k,
        tracer=get_tracer(),
        max_steps=s.agent_max_steps,
        tool_timeout=float(s.agent_tool_timeout_seconds),
    )


def get_agent_dep() -> ReActLoop:
    """FastAPI 依赖包装：无参数签名，避免 get_agent 的 llm 形参被解析成 query param。"""
    return get_agent()
