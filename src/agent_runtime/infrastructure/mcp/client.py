"""MCP 客户端：连接 MCP Server、发现工具、调用工具。

协议实现说明（重要取舍）：
  沙箱没有 pip、装不了官方 mcp SDK，且**带管道的子进程被拒**（实测
  PermissionError WinError 5）。因此：
    * 传输层抽成鸭子类型（start/request/notify/close）并可注入 ——
      测试用进程内假 transport，协议逻辑（initialize / tools/list / tools/call /
      schema 翻译 / 错误归一）在沙箱内全部可验证；
    * 真实 StdioTransport 用标准库 asyncio 子进程 + 逐行 JSON-RPC 2.0 实现 ——
      MCP 的 stdio 线格式就是换行分隔的 JSON-RPC，所以**不需要 SDK** 也能对接真实 server；
    * spawn 被拒 / server 崩溃一律变成可读错误，绝不向上抛。

ponytail: 目前只实现 stdio 传输。streamable-http / SSE、进度通知、sampling 等
SDK 能力未做；升级路径是在同一 transport 契约下再加一个 HttpTransport，
或直接换成官方 mcp SDK 的 ClientSession。
"""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ...core.tool.base import ToolResult
from ..tools._common import truncate_for_context

PROTOCOL_VERSION = "2024-11-05"
DEFAULT_CLIENT_NAME = "agent-runtime"
DEFAULT_CLIENT_VERSION = "0.1.0"


class MCPError(Exception):
    """MCP 协议层/传输层错误（调用方一律转成可读 ToolResult）。"""


@dataclass
class MCPServerConfig:
    name: str
    transport: str = "stdio"
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    enabled: bool = True


class StdioTransport:
    """真实 stdio 传输：起子进程 + 逐行 JSON-RPC 2.0（MCP 的 stdio 线格式）。"""

    def __init__(self, cfg: MCPServerConfig, timeout: float = 20.0) -> None:
        self._cfg = cfg
        self._timeout = timeout
        self._proc: asyncio.subprocess.Process | None = None
        self._request_id = 0

    async def start(self) -> None:
        if not self._cfg.command:
            raise MCPError(f"MCP server {self._cfg.name!r} 未配置 command")
        env = dict(os.environ)
        env.update(self._cfg.env or {})
        self._proc = await asyncio.create_subprocess_exec(
            self._cfg.command,
            *self._cfg.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

    async def _write(self, payload: dict) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise MCPError("transport 尚未启动")
        proc.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        await proc.stdin.drain()

    async def request(self, method: str, params: Any = None) -> dict:
        proc = self._proc
        if proc is None or proc.stdout is None:
            raise MCPError("transport 尚未启动")
        self._request_id += 1
        await self._write(
            {
                "jsonrpc": "2.0",
                "id": self._request_id,
                "method": method,
                "params": params if params is not None else {},
            }
        )
        try:
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=self._timeout)
        except asyncio.TimeoutError:
            raise MCPError(f"{method} 超时（{self._timeout}s）") from None
        if not line:
            raise MCPError(f"{method} 失败：server 已关闭 stdout")
        try:
            message = json.loads(line.decode("utf-8", errors="replace"))
        except ValueError as e:
            raise MCPError(f"{method} 返回了非 JSON 内容：{e}") from None
        if isinstance(message, dict) and message.get("error"):
            error = message["error"] or {}
            raise MCPError(f"{method} 出错：{error.get('code')} {error.get('message')}")
        result = message.get("result") if isinstance(message, dict) else None
        return result if isinstance(result, dict) else {}

    async def notify(self, method: str, params: Any = None) -> None:
        await self._write(
            {"jsonrpc": "2.0", "method": method, "params": params if params is not None else {}}
        )

    async def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin is not None and not proc.stdin.is_closing():
                proc.stdin.close()
            if proc.returncode is None:
                proc.terminate()
        except (ProcessLookupError, RuntimeError):
            pass


class MCPClient:
    """管理多个 MCP Server：连接、发现工具、调用工具。"""

    def __init__(
        self,
        timeout: float = 20.0,
        max_chars: int = 4000,
        transport_factory: Callable[[MCPServerConfig], Any] | None = None,
        client_name: str = DEFAULT_CLIENT_NAME,
        client_version: str = DEFAULT_CLIENT_VERSION,
    ) -> None:
        self._timeout = timeout
        self._max_chars = max_chars
        self._factory = transport_factory or (
            lambda cfg: StdioTransport(cfg, timeout=self._timeout)
        )
        self._client_name = client_name
        self._client_version = client_version
        self._transports: dict[str, Any] = {}
        self._server_info: dict[str, dict] = {}
        self.last_errors: list[str] = []

    @property
    def connected_servers(self) -> list[str]:
        return list(self._transports)

    async def connect(self, cfg: MCPServerConfig) -> None:
        if not cfg.enabled:
            return
        transport: Any = None
        try:
            transport = self._factory(cfg)
            await transport.start()
            info = await transport.request(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {
                        "name": self._client_name,
                        "version": self._client_version,
                    },
                },
            )
            await transport.notify("notifications/initialized", {})
        except Exception as e:  # noqa: BLE001 —— 连接失败不阻断启动
            self.last_errors.append(f"{cfg.name}: {type(e).__name__}: {e}")
            if transport is not None:
                try:
                    await transport.close()
                except Exception:  # noqa: BLE001
                    pass
            return
        self._transports[cfg.name] = transport
        self._server_info[cfg.name] = info if isinstance(info, dict) else {}

    async def list_tools(self, server: str) -> list[dict]:
        transport = self._transports.get(server)
        if transport is None:
            raise MCPError(f"server 未连接：{server!r}")
        result = await transport.request("tools/list", {})
        tools = result.get("tools") if isinstance(result, dict) else None
        return [t for t in (tools or []) if isinstance(t, dict)]

    async def discover_tools(self, registry: Any, servers: list[str] | None = None) -> list[str]:
        """发现各 Server 的工具，包装成 MCPToolAdapter 注入 registry。"""
        from .adapter import MCPToolAdapter

        names: list[str] = []
        for server in servers if servers is not None else list(self._transports):
            try:
                tools = await self.list_tools(server)
            except Exception as e:  # noqa: BLE001
                self.last_errors.append(f"{server}: {type(e).__name__}: {e}")
                continue
            for info in tools:
                if not info.get("name"):
                    continue
                adapter = MCPToolAdapter(client=self, server=server, tool_info=info)
                registry.register(adapter)
                names.append(adapter.name)
        return names

    async def call_tool(self, server: str, tool: str, args: dict | None = None) -> ToolResult:
        tool_name = f"{server}__{tool}"
        transport = self._transports.get(server)
        if transport is None:
            return ToolResult(
                tool_name=tool_name,
                success=False,
                text=f"Error: MCP server 未连接：{server!r}",
            )
        try:
            result = await transport.request(
                "tools/call", {"name": tool, "arguments": dict(args or {})}
            )
        except Exception as e:  # noqa: BLE001 —— 一律转可读失败，绝不抛
            return ToolResult(
                tool_name=tool_name,
                success=False,
                text=f"Error: MCP 调用失败：{type(e).__name__}: {e}",
            )

        if not isinstance(result, dict):
            return ToolResult(
                tool_name=tool_name, success=False, text="Error: MCP 返回了非对象结果"
            )
        content = result.get("content") or []
        parts = [
            str(item.get("text") or "")
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        body = "\n".join(p for p in parts if p)
        is_error = bool(result.get("isError"))
        out, truncated = truncate_for_context(
            body or ("(empty result)" if not is_error else "tool reported an error"),
            self._max_chars,
        )
        return ToolResult(
            tool_name=tool_name,
            success=not is_error,
            text=out if not is_error else f"Error: {out}",
            data={"content": content, "server": server},
            metadata={"truncated": truncated, "server": server},
        )

    async def aclose(self) -> None:
        transports, self._transports = self._transports, {}
        for transport in transports.values():
            try:
                await transport.close()
            except Exception:  # noqa: BLE001
                pass


def load_mcp_servers(path: str) -> list[MCPServerConfig]:
    """从 JSON 文件读取 Server 列表；文件缺失/损坏/结构不对一律返回 []。"""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(raw, list):
        return []

    servers: list[MCPServerConfig] = []
    for item in raw:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        servers.append(
            MCPServerConfig(
                name=str(item["name"]),
                transport=str(item.get("transport") or "stdio"),
                command=str(item.get("command") or ""),
                args=[str(a) for a in (item.get("args") or [])],
                env={str(k): str(v) for k, v in (item.get("env") or {}).items()},
                enabled=bool(item.get("enabled", True)),
            )
        )
    return servers


async def bootstrap_mcp(
    registry: Any,
    servers_file: str,
    client: "MCPClient",
) -> tuple[list[str], list[str]]:
    """按配置连接所有启用的 MCP Server，并把它们的工具注入 registry。

    返回 (注入的工具名, 错误列表)。配置缺失/损坏或某个 server 起不来都**不抛异常**
    —— MCP 是可选能力，绝不能阻断启动。
    client 由调用方持有：lifespan 需要在关闭时 aclose() 它，否则子进程会泄漏。
    """
    servers = [cfg for cfg in load_mcp_servers(servers_file) if cfg.enabled]
    if not servers:
        return [], []
    for cfg in servers:
        await client.connect(cfg)
    names = await client.discover_tools(registry)
    return names, list(client.last_errors)
