"""MCPClient / MCPToolAdapter 契约。

沙箱约束（已实测）：带管道的子进程被拒（PermissionError WinError 5），且没有 pip、
装不了官方 mcp SDK。因此：
  * 传输层是**可注入的鸭子类型**（start/request/notify/close），
    测试用进程内假 transport → 协议逻辑（initialize / tools/list / tools/call /
    schema 翻译 / 错误归一）在沙箱内全部可验证；
  * 真实 StdioTransport（subprocess + 逐行 JSON-RPC）在本环境会因 spawn 被拒而失败，
    这种情况必须变成**可读错误**而不是崩溃 —— 这条也在沙箱内实测。
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from agent_runtime.core.tool.registry import ToolRegistry
from agent_runtime.infrastructure.mcp.adapter import MCPToolAdapter
from agent_runtime.infrastructure.mcp.client import (
    MCPClient,
    MCPError,
    MCPServerConfig,
    StdioTransport,
    load_mcp_servers,
)

_BASE = Path(__file__).resolve().parents[2] / ".testtmp"
_SEQ = 0

TOOL_INFO = {
    "name": "read_file",
    "description": "读取文件",
    "inputSchema": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径"},
            "limit": {"type": "integer", "description": "行数", "default": 10, "minimum": 1},
            "mode": {"type": "string", "enum": ["text", "bytes"]},
            "paths": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["path"],
    },
}


def _new_root() -> str:
    global _SEQ
    _SEQ += 1
    p = _BASE / f"mcp{_SEQ:02d}"
    p.mkdir(parents=True, exist_ok=True)
    return str(p)


class FakeTransport:
    """进程内假 transport：实现与 StdioTransport 相同的鸭子类型契约。"""

    def __init__(self, tools=None, call_result=None, call_error=None, init_error=None):
        self.tools = list(tools if tools is not None else [TOOL_INFO])
        self.call_result = call_result
        self.call_error = call_error
        self.init_error = init_error
        self.started = False
        self.closed = False
        self.requests: list[tuple[str, object]] = []
        self.notifications: list[tuple[str, object]] = []

    async def start(self) -> None:
        if self.init_error:
            raise self.init_error
        self.started = True

    async def request(self, method: str, params=None):
        self.requests.append((method, params))
        if method == "initialize":
            return {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-server", "version": "0.1"},
            }
        if method == "tools/list":
            return {"tools": self.tools}
        if method == "tools/call":
            if self.call_error:
                raise self.call_error
            return self.call_result or {
                "content": [{"type": "text", "text": "file body"}],
                "isError": False,
            }
        raise MCPError(f"method not found: {method}")

    async def notify(self, method: str, params=None) -> None:
        self.notifications.append((method, params))

    async def close(self) -> None:
        self.closed = True

    @property
    def diagnostics(self) -> str:
        return ""


def _client(transport: FakeTransport, **kw) -> tuple[MCPClient, FakeTransport]:
    client = MCPClient(transport_factory=lambda cfg: transport, **kw)
    return client, transport


def _cfg(name: str = "fs", enabled: bool = True) -> MCPServerConfig:
    return MCPServerConfig(name=name, command="python", args=["-m", "fake"], enabled=enabled)


# ── 连接与初始化 ──


def test_connect_initializes_and_records_server() -> None:
    client, transport = _client(FakeTransport())
    asyncio.run(client.connect(_cfg("fs")))
    assert transport.started is True
    assert client.connected_servers == ["fs"]
    methods = [m for m, _ in transport.requests]
    assert methods[0] == "initialize"
    _, params = transport.requests[0]
    assert params["clientInfo"]["name"] == "agent-runtime"
    assert transport.notifications, "initialize 之后应发送 notifications/initialized"


def test_disabled_server_is_skipped() -> None:
    client, transport = _client(FakeTransport())
    asyncio.run(client.connect(_cfg("off", enabled=False)))
    assert transport.started is False
    assert client.connected_servers == []


def test_transport_start_failure_is_readable() -> None:
    client, _ = _client(FakeTransport(init_error=PermissionError("spawn denied")))
    asyncio.run(client.connect(_cfg("fs")))  # 不得抛异常
    assert client.connected_servers == []
    assert "fs" in client.last_errors[0]
    assert "spawn denied" in client.last_errors[0]


# ── 工具发现与注册 ──


def test_list_tools_returns_server_tools() -> None:
    client, _ = _client(FakeTransport())
    asyncio.run(client.connect(_cfg("fs")))
    tools = asyncio.run(client.list_tools("fs"))
    assert [t["name"] for t in tools] == ["read_file"]


def test_discover_tools_registers_prefixed_adapters() -> None:
    client, _ = _client(FakeTransport())
    asyncio.run(client.connect(_cfg("fs")))
    registry = ToolRegistry()
    names = asyncio.run(client.discover_tools(registry))
    assert names == ["fs__read_file"]
    assert {t.name for t in registry.list_all()} == {"fs__read_file"}
    assert registry.get("fs__read_file") is not None


def test_unknown_server_is_readable() -> None:
    client, _ = _client(FakeTransport())
    result = asyncio.run(client.call_tool("nope", "read_file", {}))
    assert result.success is False
    assert "nope" in result.text


# ── schema 翻译 ──


def test_adapter_translates_schema() -> None:
    adapter = MCPToolAdapter(client=None, server="fs", tool_info=TOOL_INFO)
    params = adapter.to_openai_schema()["function"]["parameters"]
    assert params["required"] == ["path"]
    assert params["properties"]["path"] == {"type": "string", "description": "文件路径"}
    assert params["properties"]["limit"] == {
        "type": "integer",
        "description": "行数",
        "default": 10,
        "minimum": 1,
    }
    assert params["properties"]["mode"]["enum"] == ["text", "bytes"]
    assert params["properties"]["paths"]["items"] == {"type": "string"}


def test_adapter_degrades_on_unsupported_constructs() -> None:
    info = {
        "name": "weird",
        "description": "用 oneOf/$ref 的 schema",
        "inputSchema": {
            "type": "object",
            "properties": {"x": {"oneOf": [{"type": "string"}, {"type": "integer"}]}},
            "required": ["x"],
        },
    }
    adapter = MCPToolAdapter(client=None, server="s", tool_info=info)
    params = adapter.to_openai_schema()["function"]["parameters"]
    assert params["properties"]["x"]["type"] == "object", "不支持的构造降级为 object"
    assert "oneOf" in params["properties"]["x"]["description"], "原始构造保留在描述里"


def test_adapter_description_and_name() -> None:
    adapter = MCPToolAdapter(client=None, server="fs", tool_info=TOOL_INFO)
    assert adapter.name == "fs__read_file"
    assert adapter.description == "读取文件"


# ── 调用工具 ──


def test_call_tool_success_returns_text() -> None:
    client, _ = _client(FakeTransport())
    asyncio.run(client.connect(_cfg("fs")))
    result = asyncio.run(client.call_tool("fs", "read_file", {"path": "a.txt"}))
    assert result.success is True
    assert "file body" in result.text
    assert result.data["content"][0]["text"] == "file body"


def test_call_tool_is_error_true_is_readable_failure() -> None:
    transport = FakeTransport(
        call_result={"content": [{"type": "text", "text": "boom"}], "isError": True}
    )
    client, _ = _client(transport)
    asyncio.run(client.connect(_cfg("fs")))
    result = asyncio.run(client.call_tool("fs", "read_file", {}))
    assert result.success is False
    assert "boom" in result.text


def test_call_tool_transport_error_is_readable_failure() -> None:
    client, _ = _client(FakeTransport(call_error=MCPError("server exploded")))
    asyncio.run(client.connect(_cfg("fs")))
    result = asyncio.run(client.call_tool("fs", "read_file", {}))
    assert result.success is False
    assert "server exploded" in result.text


def test_call_tool_truncates_long_text() -> None:
    long_text = "x" * 5000
    transport = FakeTransport(
        call_result={"content": [{"type": "text", "text": long_text}], "isError": False}
    )
    client, _ = _client(transport, max_chars=100)
    asyncio.run(client.connect(_cfg("fs")))
    result = asyncio.run(client.call_tool("fs", "read_file", {}))
    assert result.success is True
    assert result.metadata["truncated"] is True
    assert len(result.text) <= 100 + len("\n...[truncated]")


# ── 真实 stdio 传输：沙箱内 spawn 被拒时必须可读失败 ──


def test_real_stdio_transport_fails_readably_when_spawn_denied() -> None:
    cfg = MCPServerConfig(name="real", command=sys.executable, args=["-c", "print(1)"])
    client = MCPClient()  # 不注入，走真实 StdioTransport
    asyncio.run(client.connect(cfg))
    if client.connected_servers:
        print("     (本环境允许 spawn —— 跳过该分支)")
        asyncio.run(client.aclose())
        return
    assert client.last_errors, "spawn 失败必须被记录"
    assert any("real" in e for e in client.last_errors)
    assert isinstance(StdioTransport(cfg), StdioTransport)


# ── 配置文件加载 ──


def test_load_mcp_servers_from_json() -> None:
    root = _new_root()
    try:
        path = Path(root) / "mcp_servers.json"
        path.write_text(
            json.dumps(
                [
                    {"name": "fs", "transport": "stdio", "command": "npx",
                     "args": ["-y", "@modelcontextprotocol/server-filesystem", "."]},
                    {"name": "off", "command": "x", "enabled": False},
                ]
            ),
            encoding="utf-8",
        )
        servers = load_mcp_servers(str(path))
        assert [s.name for s in servers] == ["fs", "off"]
        assert servers[0].args[1] == "@modelcontextprotocol/server-filesystem"
        assert servers[1].enabled is False
        assert servers[0].enabled is True
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_load_mcp_servers_missing_or_broken_file_returns_empty() -> None:
    root = _new_root()
    try:
        assert load_mcp_servers(str(Path(root) / "nope.json")) == []
        broken = Path(root) / "broken.json"
        broken.write_text("{not json", encoding="utf-8")
        assert load_mcp_servers(str(broken)) == []
        wrong_shape = Path(root) / "shape.json"
        wrong_shape.write_text('{"name": "not-a-list"}', encoding="utf-8")
        assert load_mcp_servers(str(wrong_shape)) == []
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _run_all() -> None:
    tests = [
        v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)
    ]
    for t in tests:
        print(f"RUN  {t.__name__}")
        t()
        print(f"PASS {t.__name__}")
    print("ALL PASS")


if __name__ == "__main__":
    _run_all()
