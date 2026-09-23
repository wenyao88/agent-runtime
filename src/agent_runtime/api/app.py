from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """启动时发现 MCP 工具，注入共享 registry。

        MCP 是可选能力：配置缺失 / 损坏 / 某个 server 起不来，都只记录错误，
        **绝不阻断启动** —— 否则一个坏配置就会让整个服务起不来。
        """
        from ..infrastructure.mcp.client import MCPClient, bootstrap_mcp
        from .deps import (
            get_context_errors,
            get_context_manager,
            get_memory_errors,
            get_memory_manager,
            get_settings,
            get_skill_errors,
            get_skill_router,
            get_tool_registry,
            get_trace_errors,
            get_trace_store,
        )

        settings = get_settings()
        mcp = MCPClient(
            timeout=float(settings.tool_http_timeout_seconds),
            max_chars=settings.tool_max_chars,
        )
        try:
            names, errors = await bootstrap_mcp(
                get_tool_registry(), settings.mcp_servers_file, mcp
            )
        except Exception as e:  # noqa: BLE001 —— 发现阶段的兜底，永不阻断启动
            names, errors = [], [f"mcp bootstrap 异常：{type(e).__name__}: {e}"]

        app.state.mcp_client = mcp
        app.state.mcp_tool_names = names
        app.state.mcp_errors = errors

        # 技能：启动时加载一次（单例，与 Agent 共享同一批）。坏文件只记错误，不阻断启动。
        try:
            get_skill_router()
            app.state.skill_errors = get_skill_errors()
        except Exception as e:  # noqa: BLE001 —— 技能是可选能力，永不阻断启动
            app.state.skill_errors = [f"skill 加载异常：{type(e).__name__}: {e}"]

        # 记忆：只做装配（不连接，避免启动阻塞）。缺依赖/缺 key 只记错误，不阻断启动。
        try:
            get_memory_manager()
            app.state.memory_errors = get_memory_errors()
        except Exception as e:  # noqa: BLE001 —— 记忆是可选能力，永不阻断启动
            app.state.memory_errors = [f"memory 装配异常：{type(e).__name__}: {e}"]

        # 上下文：装配压缩摘要器（默认关）。注意 `deps.get_context_manager()` **没有缓存**，
        # 每次调用都会新建（与 Phase 4 行为一致，本阶段不改生命周期语义）；缺 key/缺依赖只记错误。
        try:
            get_context_manager()
            app.state.context_errors = get_context_errors()
        except Exception as e:  # noqa: BLE001 —— 摘要是可选能力，永不阻断启动
            app.state.context_errors = [f"context 装配异常：{type(e).__name__}: {e}"]

        # trace 存储：默认内存环形，配 TRACE_STORE=sqlite 才落盘。坏文件/路径不可用只记错误 +
        # 降级为内存，**不阻断启动**（本次重启丢历史，但服务照常可用，原因见 /api/traces 的 settings 段）。
        try:
            get_trace_store()
            app.state.trace_errors = get_trace_errors()
        except Exception as e:  # noqa: BLE001 —— 存储是可选能力，永不阻断启动
            app.state.trace_errors = [f"trace 存储装配异常：{type(e).__name__}: {e}"]

        try:
            yield
        finally:
            await mcp.aclose()

    app = FastAPI(title="Agent Runtime", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    from .routes.benchmarks import router as benchmarks_router
    from .routes.chat import router as chat_router
    from .routes.context import router as context_router
    from .routes.memories import router as memories_router
    from .routes.skills import router as skills_router
    from .routes.tools import router as tools_router
    from .routes.traces import router as traces_router
    from .ws.agent import router as ws_router

    app.include_router(chat_router)
    app.include_router(tools_router)
    app.include_router(skills_router)
    app.include_router(memories_router)
    app.include_router(benchmarks_router)
    app.include_router(traces_router)
    app.include_router(context_router)
    app.include_router(ws_router)

    return app


app = create_app()
