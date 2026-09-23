from functools import lru_cache
from pathlib import Path

from agent_runtime.config.settings import Settings
from agent_runtime.core.agent.planner import TaskPlanner
from agent_runtime.core.agent.react import ReActLoop
from agent_runtime.core.context.manager import ContextManager
from agent_runtime.core.memory.manager import MemoryManager
from agent_runtime.core.skill.router import SkillRouter
from agent_runtime.core.tool.registry import ToolRegistry
from agent_runtime.core.trace.tracer import Tracer
from agent_runtime.infrastructure.context.catalog import build_context_manager
from agent_runtime.infrastructure.memory.catalog import build_memory_manager
from agent_runtime.infrastructure.skills.catalog import (
    register_skills_from_dir,
    resolve_skills_dir,
)
from agent_runtime.infrastructure.tools.catalog import register_native_tools

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_skill_errors: list[str] = []
_memory_errors: list[str] = []
_context_errors: list[str] = []


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
    装配本体在 `infrastructure/memory/catalog.py`：**只有一处**，API 与 `scripts/run_demo1_github.py` 共用
    （demo 曾自己拼一套没有持久层的空壳 manager，与这里漂移 → Redis 里一条都没有）。
    缺依赖 / 配置不足 → 该层**不构造**并把原因记进 `_memory_errors`（供 lifespan 暴露），**绝不阻断启动**。
    连接不在构造期做（避免启动阻塞）：层内部惰性 connect，失败由 `MemoryManager` 逐层捕获记账。
    """
    _memory_errors.clear()
    manager, errors = build_memory_manager(get_settings())
    _memory_errors.extend(errors)
    return manager


def get_memory_errors() -> list[str]:
    """记忆装配错误（缺依赖 / 缺 key）。只用于可见性，绝不影响启动。"""
    return list(_memory_errors)


def get_context_manager() -> ContextManager:
    """上下文装配（薄封装：本体在 `infrastructure/context/catalog.py`）。

    与 `scripts/run_demo1_github.py` **共用同一处装配** —— Phase 4 的 `memory_manager`
    就是因为脚本自己拼一套而漂移，结果 `--session-id` 传对了也读不到任何东西。
    缺 key / 缺依赖 → **不构造摘要器**并把原因记进 `_context_errors`（供 lifespan 暴露），绝不阻断启动。
    """
    _context_errors.clear()
    manager, errors = build_context_manager(get_settings())
    _context_errors.extend(errors)
    return manager


def get_context_errors() -> list[str]:
    """上下文装配错误（缺 key / 缺依赖）。只用于可见性，绝不影响启动。"""
    return list(_context_errors)


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
        recall_top_k=s.memory_recall_top_k,
        tracer=get_tracer(),
        max_steps=s.agent_max_steps,
        tool_timeout=float(s.agent_tool_timeout_seconds),
    )


def get_agent_dep() -> ReActLoop:
    """FastAPI 依赖包装：无参数签名，避免 get_agent 的 llm 形参被解析成 query param。"""
    return get_agent()
