from functools import lru_cache
from pathlib import Path

from agent_runtime.config.settings import Settings
from agent_runtime.core.agent.planner import TaskPlanner
from agent_runtime.core.agent.react import ReActLoop
from agent_runtime.core.context.manager import ContextManager
from agent_runtime.core.memory.manager import MemoryManager
from agent_runtime.core.skill.router import SkillRouter
from agent_runtime.core.tool.registry import ToolRegistry
from agent_runtime.core.trace.store import InMemoryTraceStore, TraceStore
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
_trace_errors: list[str] = []


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


@lru_cache
def get_trace_store() -> TraceStore:
    """进程单例 trace store（装配本体在 `infrastructure/trace/catalog.py`，**只有一处**）。

    必须是单例：每次请求新建一个 store 就等于每次都在看一张空表。
    装配失败（坏 `trace.db` / 路径不可用 / catalog 起不来）→ 降级为内存 store 并把原因记进
    `_trace_errors`（供 lifespan 暴露），**绝不阻断启动**（与 memory / context / MCP 同一态度）。
    """
    _trace_errors.clear()
    try:
        from agent_runtime.infrastructure.trace.catalog import build_trace_store

        store, errors = build_trace_store(get_settings(), str(_PROJECT_ROOT))
    except Exception as e:  # noqa: BLE001 —— 存储是可选能力，永不阻断启动
        # 这是"一处装配"（catalog）唯一被绕开的地方：catalog **自己**都 import 不进来时，
        # 已经没有别的地方可问了。默认内存 store 是零配置的那一档，行为与"没配 TRACE_STORE"一致。
        _trace_errors.append(f"trace 存储装配失败，已降级为内存：{type(e).__name__}: {e}")
        return InMemoryTraceStore()
    _trace_errors.extend(errors)
    return store


def get_trace_errors() -> list[str]:
    """trace 存储装配错误（坏文件 / 降级）。只用于可见性，绝不影响启动。"""
    return list(_trace_errors)


def get_tracer(source: str = "chat") -> Tracer:
    """新建一个 tracer（一次会话一个，事件队列不能串），但**共享同一个 store**。

    `source` 决定这条 trace 在 UI 里归到哪一类（`chat` / `benchmark`），必须由调用方给对：
    评测 agent 若走默认值，轨迹会混进聊天列表里，过滤就失去意义。
    """
    return Tracer(store=get_trace_store(), source=source)


_LAST_CONTEXT: ContextManager | None = None


def _remember_context(manager: ContextManager) -> None:
    global _LAST_CONTEXT
    _LAST_CONTEXT = manager


def get_last_context_manager() -> ContextManager | None:
    """最近一次**聊天会话**用的上下文管理器；一次都没聊过则为 `None`。

    `/api/context` 靠它看"当前上下文"。为什么不在路由里 `Depends(get_context_manager)`：
    那个函数每次新建（Phase 4 起的语义，本阶段不改），现造出来的永远没有消息 ——
    接口会回一份 "0 tokens" 的报告，看起来一切正常，比报错更误导。

    天花板（README 已记）：只反映**最近一次**会话（并发多会话时看到的是最后那一次）；
    评测 agent（`build_agent_for_settings`）不进这里，`available:false` 时原因会说明先聊一句。
    """
    return _LAST_CONTEXT


def _llm_for(settings: Settings):
    """按给定 settings 造 Provider。惰性 import：未安装 openai 时本模块仍可导入。"""
    from agent_runtime.infrastructure.llm.openai_compatible import OpenAICompatibleProvider

    return OpenAICompatibleProvider(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        temperature=settings.llm_temperature,
    )


def get_llm():
    """真实 Provider（进程单例配置）。"""
    return _llm_for(get_settings())


def build_agent_for_settings(settings: Settings) -> ReActLoop:
    """按**给定** settings 装配一个 agent（不缓存、不碰单例）。消融三组专用。

    为什么不能用 `get_agent()`：`get_settings` / `get_memory_manager` 是 `lru_cache` 单例，
    **一个进程只有一种配置** —— 三组要各装一次（只改开关、不改 `.env`），只能拿覆盖后的 settings 自己装。
    工具 registry 与技能 router 与这三组的自变量无关，仍共享单例（MCP 工具必须留在同一个 registry 里）。

    **每组只装一次、组内任务共用**：memory 组的 `session_scope=run` 要靠同一份记忆实例才成立；
    逐任务新建记忆实例等于把这一组测成空的。`ContextManager` 每次 `build()` 会清空消息，不会串任务。
    """
    from agent_runtime.infrastructure.context.catalog import build_context_manager
    from agent_runtime.infrastructure.memory.catalog import build_memory_manager

    memory, memory_errors = build_memory_manager(settings)
    context, context_errors = build_context_manager(settings)
    # 装配问题照样要可见（与 get_memory_manager/get_context_manager 同一份列表）
    _memory_errors.extend(memory_errors)
    _context_errors.extend(context_errors)

    provider = _llm_for(settings)
    planner = TaskPlanner(llm=provider) if settings.agent_task_planning_enabled else None
    return ReActLoop(
        llm=provider,
        tool_registry=get_tool_registry(),
        context_manager=context,
        memory_manager=memory,
        skill_router=get_skill_router(),
        planner=planner,
        skill_top_k=settings.agent_skill_top_k,
        recall_top_k=settings.memory_recall_top_k,
        tracer=get_tracer("benchmark"),
        max_steps=settings.agent_max_steps,
        tool_timeout=float(settings.agent_tool_timeout_seconds),
    )


def get_agent(llm=None, source: str = "chat") -> ReActLoop:
    s = get_settings()
    provider = llm or get_llm()
    # 规划器默认关闭：它多花一次 LLM 调用，且只产出"参考计划"文本（不改变控制流）
    planner = TaskPlanner(llm=provider) if s.agent_task_planning_enabled else None
    context = get_context_manager()
    if source == "chat":
        # 只有聊天会话进 `/api/context`（评测 agent 的上下文进去只会让人分不清看的是谁）
        _remember_context(context)
    return ReActLoop(
        llm=provider,
        tool_registry=get_tool_registry(),
        context_manager=context,
        memory_manager=get_memory_manager(),
        skill_router=get_skill_router(),
        planner=planner,
        skill_top_k=s.agent_skill_top_k,
        recall_top_k=s.memory_recall_top_k,
        tracer=get_tracer(source),
        max_steps=s.agent_max_steps,
        tool_timeout=float(s.agent_tool_timeout_seconds),
    )


def get_agent_dep() -> ReActLoop:
    """FastAPI 依赖包装：无参数签名，避免 get_agent 的 llm 形参被解析成 query param。"""
    return get_agent()


# ── Benchmark（Phase 6）──

_BENCHMARK_TASKS: set = set()
"""后台评测任务的强引用：`asyncio.create_task` 的返回值不持有就会被 GC 掉。"""


def _benchmark_paths() -> tuple[str, str]:
    from agent_runtime.infrastructure.benchmark.catalog import (
        resolve_runs_dir,
        resolve_tasks_file,
    )

    settings = get_settings()
    return (
        resolve_tasks_file(settings, str(_PROJECT_ROOT)),
        resolve_runs_dir(settings, str(_PROJECT_ROOT)),
    )


def list_benchmark_runs() -> list[dict]:
    from agent_runtime.infrastructure.benchmark.store import list_runs

    _, runs_dir = _benchmark_paths()
    return list_runs(runs_dir)


def load_benchmark_report(run_id: str):
    from agent_runtime.infrastructure.benchmark.store import load_report

    _, runs_dir = _benchmark_paths()
    return load_report(run_id, runs_dir)


def start_benchmark_run(
    *, provider: str = "mock", limit: int | None = None, judge: int = 0
) -> dict:
    """起一次后台评测，立即返回 run_id。任务集读不到时如实返回 `started: False` + 原因。"""
    import asyncio

    from agent_runtime.core.benchmark.dataset import load_tasks
    from agent_runtime.infrastructure.benchmark.catalog import (
        build_runner,
        real_agent_factory,
    )
    from agent_runtime.infrastructure.benchmark.service import (
        benchmark_config,
        new_run_id,
        run_and_save,
    )

    settings = get_settings()
    tasks_file, runs_dir = _benchmark_paths()
    tasks, errors = load_tasks(tasks_file)
    run_id = new_run_id(provider)
    if not tasks:
        return {
            "run_id": run_id,
            "started": False,
            "errors": errors or [f"任务集为空：{tasks_file}"],
        }

    # 启动校验（真实全量实测）：搜索配置错了就别开跑 —— 每条研究类任务都会白烧到 max_steps。
    # mock 不受影响（夹具不调用工具）。
    if provider != "mock":
        from agent_runtime.infrastructure.tools.catalog import search_config_errors

        fatal = search_config_errors(settings)
        if fatal:
            return {"run_id": run_id, "started": False, "errors": fatal}

    # 真实 provider 复用 API 自己的装配（api 可以 import 自己）；mock 用离线假 agent。
    # 注意必须经 `real_agent_factory` 包一层：`get_agent(llm=None)` 的第一个形参是 llm，
    # 直接传 `get_agent` 会让 runner 的 `factory(task)` 把 task 塞进 llm —— 见 catalog 里那段注释。
    # source="benchmark"：评测轨迹必须带自己的来源标签，否则会混进聊天 trace 列表。
    agent_factory = (
        None
        if provider == "mock"
        else real_agent_factory(lambda: get_agent(source="benchmark"))
    )
    runner, build_errors = build_runner(
        settings, provider, agent_factory=agent_factory
    )
    config = benchmark_config(
        provider, model="mock" if provider == "mock" else settings.llm_model, judge=judge
    )
    task = asyncio.create_task(
        run_and_save(
            runner,
            tasks,
            runs_dir=runs_dir,
            config=config,
            limit=limit,
            run_id=run_id,
        )
    )
    _BENCHMARK_TASKS.add(task)
    task.add_done_callback(_BENCHMARK_TASKS.discard)
    task.add_done_callback(_log_benchmark_task)
    return {"run_id": run_id, "started": True, "errors": list(errors) + list(build_errors)}


def _log_benchmark_task(task) -> None:
    """后台任务无人 await，异常默认只会变成一句 "never retrieved" —— 必须自己记下来（审查 M12）。"""
    import logging

    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        logging.getLogger(__name__).warning(
            "benchmark: 后台评测异常：%s: %s", type(error).__name__, error
        )
