from functools import lru_cache
from pathlib import Path

from agent_runtime.config.settings import Settings
from agent_runtime.core.agent.react import ReActLoop
from agent_runtime.core.context.budget import TokenBudget
from agent_runtime.core.context.manager import ContextManager
from agent_runtime.core.memory.manager import MemoryManager
from agent_runtime.core.skill.router import SkillRouter
from agent_runtime.core.tool.registry import ToolRegistry
from agent_runtime.core.trace.tracer import Tracer
from agent_runtime.infrastructure.tools.catalog import register_native_tools

_PROJECT_ROOT = Path(__file__).resolve().parents[3]


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


def get_memory_manager() -> MemoryManager:
    return MemoryManager()


def get_context_manager() -> ContextManager:
    return ContextManager(budget=TokenBudget())


def get_skill_router() -> SkillRouter:
    return SkillRouter()


def get_tracer() -> Tracer:
    return Tracer()


def get_llm():
    """真实 Provider。惰性 import：未安装 openai 时本模块仍可导入（测试/降级友好）。"""
    from agent_runtime.infrastructure.llm.openai_compatible import OpenAICompatibleProvider

    s = get_settings()
    return OpenAICompatibleProvider(
        api_key=s.llm_api_key, base_url=s.llm_base_url, model=s.llm_model
    )


def get_agent(llm=None) -> ReActLoop:
    s = get_settings()
    return ReActLoop(
        llm=llm or get_llm(),
        tool_registry=get_tool_registry(),
        context_manager=get_context_manager(),
        memory_manager=get_memory_manager(),
        skill_router=get_skill_router(),
        tracer=get_tracer(),
        max_steps=s.agent_max_steps,
        tool_timeout=float(s.agent_tool_timeout_seconds),
    )


def get_agent_dep() -> ReActLoop:
    """FastAPI 依赖包装：无参数签名，避免 get_agent 的 llm 形参被解析成 query param。"""
    return get_agent()
