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
        from .deps import get_settings, get_tool_registry

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

    from .routes.chat import router as chat_router
    from .routes.tools import router as tools_router
    from .ws.agent import router as ws_router

    app.include_router(chat_router)
    app.include_router(tools_router)
    app.include_router(ws_router)

    return app


app = create_app()
