"""闭环：**我们自己的 MCP 客户端** ↔ **我们自己的 MCP Server**（进程内 stdio 管道）。

这是 Phase 8 最强的一条证据：不是"分别测通了两半"，而是把仓库里**已有的** `MCPClient`
通过一条真实的换行分隔 JSON-RPC 管道接到自研 `MCPServerCore` + `run_stdio_server` 上，
走完整链路：

    MCPClient → StdioTransport 契约 → run_stdio_server → handle_line → handle
             → ToolRegistry → 原生工具 → ToolResult → content/isError → MCPClient

沙箱约束：**不 spawn 子进程**（带管道会被拒），改用 `asyncio.Queue` 驱动的进程内双向管道；
管道两端的行格式、id 匹配、EOF 语义都与真实 stdio 一致，所以除"真实进程边界"之外全部被覆盖。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from agent_runtime.core.mcp.server import MCPServerCore  # noqa: E402
from agent_runtime.core.tool.registry import ToolRegistry  # noqa: E402
from agent_runtime.infrastructure.mcp.client import (  # noqa: E402
    MCPClient,
    MCPError,
    MCPServerConfig,
)
from agent_runtime.infrastructure.mcp.server import run_stdio_server  # noqa: E402
from agent_runtime.infrastructure.tools.catalog import (  # noqa: E402
    NATIVE_TOOL_NAMES,
    register_native_tools,
)

ROOT = Path(__file__).resolve().parents[2]
README = ROOT / "README.md"
# README 开头的一句（截断只影响尾部，所以断言必须落在开头）
README_SNIPPET = "自研单 Agent Runtime"


class _QueueStdin:
    """server 侧的 stdin：readline() 返回 awaitable（真实 asyncio 流的形态）。"""

    def __init__(self, queue: asyncio.Queue) -> None:
        self._queue = queue

    def readline(self):
        return self._queue.get()


class _QueueStdout:
    """server 侧的 stdout：write() 把整行推进队列；flush() 在队列上等价于"已落地"。"""

    def __init__(self, queue: asyncio.Queue) -> None:
        self._queue = queue
        self.flushes = 0

    def write(self, text: str) -> None:
        self._queue.put_nowait(text)

    def flush(self) -> None:
        self.flushes += 1


class InProcessPipeTransport:
    """客户端侧 transport：实现与 `StdioTransport` 相同的鸭子类型契约。

    `request` 与真实实现一样**按 id 匹配**响应，并跳过通知/非 JSON 行 —— 否则这条闭环就
    证明不了"客户端真的能对上自研 server 的响应"。
    """

    def __init__(self, core: MCPServerCore, timeout: float = 5.0) -> None:
        self._core = core
        self._timeout = timeout
        self._to_server: asyncio.Queue = asyncio.Queue()
        self._from_server: asyncio.Queue = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._request_id = 0
        self.requests_sent: list[str] = []
        self.notifications_sent: list[str] = []
        self.exit_code: int | None = None
        self.server_stdout = _QueueStdout(self._from_server)

    async def start(self) -> None:
        self._task = asyncio.create_task(
            run_stdio_server(
                self._core,
                _QueueStdin(self._to_server),
                self.server_stdout,
                stderr=None,
            )
        )

    async def _write(self, payload: dict) -> None:
        self._to_server.put_nowait(json.dumps(payload, ensure_ascii=False) + "\n")

    async def request(self, method: str, params=None) -> dict:
        self._request_id += 1
        request_id = self._request_id
        self.requests_sent.append(method)
        await self._write(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params if params is not None else {},
            }
        )
        while True:
            line = await asyncio.wait_for(self._from_server.get(), timeout=self._timeout)
            try:
                message = json.loads(line)
            except ValueError:
                continue  # server 打到 stdout 的日志行 → 跳过
            if not isinstance(message, dict) or "id" not in message:
                continue  # 通知 → 跳过
            if message["id"] != request_id:
                continue  # 别把上一次的响应当成本次结果
            if message.get("error"):
                error = message["error"] or {}
                raise MCPError(f"{method} 出错：{error.get('code')} {error.get('message')}")
            result = message.get("result")
            return result if isinstance(result, dict) else {}

    async def notify(self, method: str, params=None) -> None:
        self.notifications_sent.append(method)
        await self._write(
            {"jsonrpc": "2.0", "method": method, "params": params if params is not None else {}}
        )

    async def close(self) -> None:
        self._to_server.put_nowait("")  # EOF：真实实现是关掉子进程 stdin
        if self._task is not None:
            self.exit_code = await asyncio.wait_for(self._task, timeout=self._timeout)
            self._task = None


def _build(root: str = str(ROOT)) -> tuple[ToolRegistry, MCPServerCore]:
    """装配**和 API 同一套**原生工具（缺 httpx 等依赖不影响装配，工具执行时才失败）。"""
    registry = ToolRegistry()
    register_native_tools(registry, root=root)
    return registry, MCPServerCore(registry, server_name="self", server_version="0.1.0")


async def _connected(root: str = str(ROOT)):
    registry, core = _build(root)
    transport = InProcessPipeTransport(core)
    client = MCPClient(transport_factory=lambda cfg: transport)
    await client.connect(MCPServerConfig(name="self", transport="stdio", command="x"))
    return client, registry, core, transport


def _roundtrip(step):
    """在一条新的进程内管道上跑 step(client, registry, core, transport)，跑完关掉。"""

    async def _main():
        client, registry, core, transport = await _connected()
        try:
            return await step(client, registry, core, transport)
        finally:
            await client.aclose()

    return asyncio.run(_main())


# ── 握手 ──


def test_client_connects_to_self_built_server() -> None:
    async def step(client, registry, core, transport):
        return client.connected_servers, list(client.last_errors), transport.requests_sent

    servers, errors, sent = _roundtrip(step)
    assert servers == ["self"], f"自带 server 必须连接成功：{errors}"
    assert errors == [], errors
    assert sent[0] == "initialize"


def test_initialize_result_is_accepted_by_the_client() -> None:
    """客户端只把 initialize 的 dict 结果存起来；协议版本必须与客户端一致。"""

    async def step(client, registry, core, transport):
        # 只读地看一眼客户端记录下来的 server info（MCPClient 没有公开访问器）
        return client._server_info["self"]

    info = _roundtrip(step)
    assert info["protocolVersion"] == "2024-11-05"
    assert info["serverInfo"]["name"] == "self"
    assert info["capabilities"]["tools"] == {"listChanged": False}


# ── 工具发现：8 个原生工具 ──


def test_discover_tools_finds_all_eight_native_tools() -> None:
    async def step(client, registry, core, transport):
        names = await client.discover_tools(registry)
        return names, sorted(t.name for t in registry.list_all())

    names, all_names = _roundtrip(step)
    expected = sorted(f"self__{name}" for name in NATIVE_TOOL_NAMES)
    print(f"\n[roundtrip] 通过 MCP 发现 {len(names)} 个工具：{sorted(names)}")
    assert len(NATIVE_TOOL_NAMES) == 8
    assert sorted(names) == expected, f"必须发现全部 8 个原生工具，实际={sorted(names)}"
    for name in NATIVE_TOOL_NAMES:
        assert f"self__{name}" in all_names, f"{name} 没有被注入 registry"


def test_discovered_tool_schema_survives_the_roundtrip() -> None:
    """inputSchema 从 registry 出去、经 MCP 回来，参数契约不能变形。"""

    async def step(client, registry, core, transport):
        await client.discover_tools(registry)
        return registry.get("self__read_file").parameters

    params = _roundtrip(step)
    assert params.required == ["path"]
    assert params.properties["path"].type == "string"


# ── 真实执行：read_file 必须读到 README 的内容 ──


def test_call_tool_reads_real_readme_content_over_mcp() -> None:
    assert README.is_file(), f"前置条件：{README} 必须存在"

    async def step(client, registry, core, transport):
        result = await client.call_tool("self", "read_file", {"path": "README.md"})
        return result, transport

    result, transport = _roundtrip(step)
    print(
        f"[roundtrip] read_file success={result.success} 读到 {len(result.text)} 字符；"
        f"首行={result.text.splitlines()[0][:40]!r}"
    )
    assert result.success is True, result.text
    assert README_SNIPPET in result.text, "读到的必须是 README 的真实内容，不能是空串/占位"
    assert len(result.text) > 100, f"只读到 {len(result.text)} 字符，不像真实文件内容"
    assert result.data["server"] == "self"


def test_registry_adapter_call_also_works_end_to_end() -> None:
    """走 ReAct Loop 会走的那条路：registry 里的 MCP 适配器 → client → server → 原生工具。"""

    async def step(client, registry, core, transport):
        await client.discover_tools(registry)
        adapter = registry.get("self__read_file")
        return await adapter.execute(path="README.md")

    result = _roundtrip(step)
    assert result.success is True, result.text
    assert README_SNIPPET in result.text


def test_missing_file_is_a_readable_failure_not_an_exception() -> None:
    async def step(client, registry, core, transport):
        return await client.call_tool("self", "read_file", {"path": "definitely/not/here.txt"})

    result = _roundtrip(step)
    assert result.success is False
    assert "definitely/not/here.txt" in result.text


# ── 失败路径：未知工具 ──


def test_unknown_tool_is_a_readable_failure() -> None:
    async def step(client, registry, core, transport):
        return await client.call_tool("self", "no_such_tool", {})

    result = _roundtrip(step)
    print(f"[roundtrip] 未知工具 → success={result.success}；text={result.text!r}")
    assert result.success is False, "未知工具不能抛，也不能被当成成功"
    assert result.text.startswith("Error:")
    assert "no_such_tool" in result.text and "-32602" in result.text


def test_tool_exception_is_a_readable_failure() -> None:
    """工具执行时缺依赖（httpx）等异常，必须变成 isError，而不是打断整条链路。"""

    async def step(client, registry, core, transport):
        # web_search 在本沙箱缺 httpx / 无网络：执行必然失败，但链路必须活着
        first = await client.call_tool("self", "web_search", {"query": "x"})
        second = await client.call_tool("self", "read_file", {"path": "README.md"})
        return first, second

    first, second = _roundtrip(step)
    assert first.success is False, first.text
    assert second.success is True, "一次工具失败不能带崩后面的调用"
    assert README_SNIPPET in second.text


# ── EOF 与关闭 ──


def test_client_matches_by_id_and_skips_a_stale_response() -> None:
    """id 匹配必须真的生效：先塞一条"上一次请求"的迟到响应，客户端不能把它当本次结果。

    没有这条测试，`if message["id"] != request_id: continue` 就是死代码 ——
    因为正常流程里请求是串行的，永远不会有错位的响应。
    """
    stale_id = 1  # connect() 已经用掉 id=1（initialize），下一条请求是 id=2

    async def step(client, registry, core, transport):
        transport._from_server.put_nowait(
            json.dumps({"jsonrpc": "2.0", "id": stale_id, "result": {"stale": True}}) + "\n"
        )
        return await transport.request("tools/list")

    result = _roundtrip(step)
    assert "stale" not in result, "迟到响应（id 不匹配）被当成了本次结果"
    assert len(result["tools"]) == len(NATIVE_TOOL_NAMES)


def test_server_loop_exits_cleanly_on_client_close() -> None:
    async def step(client, registry, core, transport):
        await client.discover_tools(registry)
        await client.call_tool("self", "read_file", {"path": "README.md"})
        return transport

    transport = _roundtrip(step)
    print(f"[roundtrip] 客户端关闭后 server 循环退出码={transport.exit_code}")
    assert transport.exit_code == 0, "客户端关闭 stdin → server 必须干净退出（0）"
    assert transport.notifications_sent == ["notifications/initialized"]
    assert transport.server_stdout.flushes >= 3, "每条响应都要 flush"


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
