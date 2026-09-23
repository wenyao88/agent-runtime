"""把本项目原生工具集当成一个 MCP Server 跑起来（stdio）。

    python scripts/mcp_server.py                      # 项目根为工作区，工具集与 API 同一套
    python scripts/mcp_server.py --root D:/work/repo --name my-tools

给 Claude Desktop 之类的 MCP 客户端用：客户端 spawn 本脚本，通过 stdin/stdout 上的
换行分隔 JSON-RPC 2.0 调 `tools/list` / `tools/call`。**协议实现是本仓库自研的**
（`agent_runtime.core.mcp.MCPServerCore` + `agent_runtime.infrastructure.mcp.server`），
不依赖官方 mcp SDK。

顶层只 import 标准库：缺 httpx / pydantic_settings 时也能启动（工具执行时才失败），
与 `scripts/run_benchmark.py` 同一约定。
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))


def _load_settings(root: str) -> object:
    """有完整依赖就用真 `Settings`；沙箱缺 pydantic_settings 时退回 env 兜底。

    兜底**不是**为了偷偷降级：字段名与 `Settings` 完全一致，缺字段会让搜索之类的能力
    在配置里"看起来配了、实际没生效" —— 所以这里显式把同一组字段都摆出来。
    """
    try:
        from agent_runtime.config.settings import Settings

        return Settings()
    except Exception:  # noqa: BLE001 —— 缺依赖不该影响 server 启动
        return types.SimpleNamespace(
            github_token=os.environ.get("GITHUB_TOKEN", ""),
            web_search_provider=os.environ.get("WEB_SEARCH_PROVIDER", "duckduckgo"),
            web_search_api_key=os.environ.get("WEB_SEARCH_API_KEY", ""),
            tool_http_timeout_seconds=float(os.environ.get("TOOL_HTTP_TIMEOUT_SECONDS", 20.0)),
            tool_max_chars=int(os.environ.get("TOOL_MAX_CHARS", 4000)),
        )


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="以 stdio 方式提供本项目的原生工具集（MCP Server）"
    )
    parser.add_argument("--root", default=str(_ROOT), help="工具工作区根目录（默认项目根）")
    parser.add_argument(
        "--name", default="agent-runtime", help="serverInfo.name（MCP 客户端可见）"
    )
    parser.add_argument(
        "--version", default="0.1.0", help="serverInfo.version（MCP 客户端可见）"
    )
    parser.add_argument("--github-token", default="", help="GitHub token（默认取配置/env）")
    parser.add_argument("--web-search-provider", default="", help="duckduckgo | tavily")
    parser.add_argument("--web-search-api-key", default="", help="tavily 需要的 key")
    parser.add_argument("--http-timeout", type=float, default=0.0, help="工具 HTTP 超时（秒）")
    parser.add_argument("--max-chars", type=int, default=0, help="单次工具输出上限（字符）")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    root = str(Path(args.root).resolve())
    if str(Path(root) / "src") not in sys.path:  # 允许 --root 指向另一份 checkout
        sys.path.insert(0, str(Path(root) / "src"))

    from agent_runtime.core.mcp.server import MCPServerCore
    from agent_runtime.core.tool.registry import ToolRegistry
    from agent_runtime.infrastructure.mcp.server import (
        StdioServerTransport,
        run_stdio_server,
    )
    from agent_runtime.infrastructure.tools.catalog import register_native_tools

    settings = _load_settings(root)
    registry = ToolRegistry()
    # 与 `api/deps.py:get_tool_registry()` 装配**同一套**工具：CLI 参数优先，其次配置/env。
    register_native_tools(
        registry,
        root=root,
        github_token=args.github_token or getattr(settings, "github_token", ""),
        web_search_provider=args.web_search_provider
        or getattr(settings, "web_search_provider", "duckduckgo"),
        web_search_api_key=args.web_search_api_key
        or getattr(settings, "web_search_api_key", ""),
        http_timeout=args.http_timeout
        or float(getattr(settings, "tool_http_timeout_seconds", 20.0)),
        max_chars=args.max_chars or int(getattr(settings, "tool_max_chars", 4000)),
    )

    core = MCPServerCore(registry, server_name=args.name, server_version=args.version)
    transport = StdioServerTransport()
    return asyncio.run(
        run_stdio_server(core, transport.stdin, transport.stdout, transport.stderr)
    )


if __name__ == "__main__":
    raise SystemExit(main())
