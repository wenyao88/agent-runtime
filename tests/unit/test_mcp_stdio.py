"""stdio 服务循环契约：`run_stdio_server` + `StdioServerTransport`。

流是**可注入的**（StringIO / 队列 / 假件），所以整个 IO 循环可以在沙箱内验证：
逐行读 → `core.handle_line` → 有响应就写 stdout 并 flush；诊断只走 stderr；
EOF 干净返回 0；任何异常都不许让循环崩掉（这也是为什么"坏行"不会带走整个 server）。
"""
from __future__ import annotations

import asyncio
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from agent_runtime.core.mcp.server import MCPServerCore  # noqa: E402
from agent_runtime.core.tool.base import (  # noqa: E402
    BaseTool,
    PropertyDef,
    ToolResult,
    ToolSchema,
)
from agent_runtime.core.tool.registry import ToolRegistry  # noqa: E402
from agent_runtime.infrastructure.mcp.server import (  # noqa: E402
    StdioServerTransport,
    run_stdio_server,
)
from agent_runtime.infrastructure.tools.catalog import NATIVE_TOOL_NAMES  # noqa: E402


class EchoTool(BaseTool):
    name = "echo"
    description = "回显输入文本"
    parameters = ToolSchema(
        properties={"text": PropertyDef(type="string", description="要回显的文本")},
        required=["text"],
    )

    async def execute(self, text: str = "") -> ToolResult:
        return ToolResult(tool_name=self.name, success=True, text=f"echo: {text}")


class BoomTool(BaseTool):
    name = "boom"
    description = "永远抛异常"
    parameters = ToolSchema(properties={}, required=[])

    async def execute(self, **kwargs) -> ToolResult:
        raise ValueError("kaboom")


class MultiLineTool(BaseTool):
    name = "multi"
    description = "返回多行文本"
    parameters = ToolSchema(properties={}, required=[])

    async def execute(self, **kwargs) -> ToolResult:
        return ToolResult(tool_name=self.name, success=True, text="line1\nline2\nline3")


def _core(*tools: BaseTool) -> MCPServerCore:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return MCPServerCore(registry)


def _line(**payload) -> str:
    return json.dumps(payload, ensure_ascii=False) + "\n"


class FlushRecorder(io.StringIO):
    """记录 flush 次数：不 flush 的话真实 stdio 客户端会一直等（管道缓冲）。"""

    def __init__(self) -> None:
        super().__init__()
        self.flushes = 0

    def flush(self) -> None:  # type: ignore[override]
        self.flushes += 1
        super().flush()


class NoFlushStdout:
    """只有 write 的 stdout（最小面）：不能因为没有 flush 就崩。"""

    def __init__(self) -> None:
        self.written: list[str] = []

    def write(self, text: str) -> None:
        self.written.append(text)


class RaisingCore:
    """第一行抛异常，第二行正常 —— 用来证明循环"写 stderr + 继续"。"""

    def __init__(self) -> None:
        self.seen: list[str] = []

    async def handle_line(self, line: str) -> str | None:
        self.seen.append(line)
        if len(self.seen) == 1:
            raise RuntimeError("handle exploded")
        return json.dumps({"jsonrpc": "2.0", "id": 99, "result": {"ok": True}}) + "\n"


class AsyncLines:
    """readline() 返回 awaitable（asyncio 流的形态）：循环必须支持 await。"""

    def __init__(self, lines: list[str]) -> None:
        self._lines = list(lines)

    def readline(self):
        async def _next() -> str:
            return self._lines.pop(0) if self._lines else ""

        return _next()


class BrokenStdin:
    def readline(self) -> str:
        raise OSError("stdin is gone")


class BrokenStdout:
    def write(self, text: str) -> None:
        raise OSError("stdout is gone")

    def flush(self) -> None:
        pass


# ── 正常多行输入 ──


def test_loop_answers_each_request_and_skips_noise() -> None:
    stdin = io.StringIO(
        _line(jsonrpc="2.0", id=1, method="initialize")
        + "{this is not json\n"
        + "\n"
        + "   \n"
        + _line(jsonrpc="2.0", method="notifications/initialized")
        + _line(jsonrpc="2.0", id=2, method="tools/list")
        + _line(
            jsonrpc="2.0",
            id=3,
            method="tools/call",
            params={"name": "echo", "arguments": {"text": "hi"}},
        )
    )
    stdout = FlushRecorder()
    stderr = io.StringIO()

    code = asyncio.run(run_stdio_server(_core(EchoTool()), stdin, stdout, stderr))

    assert code == 0, "EOF 必须干净退出（0）"
    lines = stdout.getvalue().splitlines()
    assert len(lines) == 3, f"坏行/空行/通知都不该产生响应行，实际={lines}"
    payloads = [json.loads(line) for line in lines]
    assert [p["id"] for p in payloads] == [1, 2, 3], "id 必须与请求一一对应"
    assert all(p["jsonrpc"] == "2.0" for p in payloads)
    assert payloads[0]["result"]["protocolVersion"] == "2024-11-05"
    assert payloads[1]["result"]["tools"][0]["name"] == "echo"
    assert payloads[2]["result"]["content"][0]["text"] == "echo: hi"
    assert payloads[2]["result"]["isError"] is False
    assert stdout.flushes == 3, "每条响应都必须 flush，否则管道客户端会一直等"


def test_loop_writes_only_json_to_stdout() -> None:
    stdin = io.StringIO("garbage\n" + _line(jsonrpc="2.0", id=1, method="tools/list"))
    stdout = io.StringIO()
    stderr = io.StringIO()

    asyncio.run(run_stdio_server(_core(EchoTool()), stdin, stdout, stderr))

    for line in stdout.getvalue().splitlines():
        json.loads(line)  # 诊断绝不能混进 stdout（会污染协议帧）
    assert "garbage" not in stdout.getvalue()
    assert "garbage" not in stderr.getvalue(), "坏行没有 id：既不回响应，也不必当诊断刷屏"


def test_loop_tool_exception_becomes_is_error_not_crash() -> None:
    stdin = io.StringIO(
        _line(
            jsonrpc="2.0",
            id=1,
            method="tools/call",
            params={"name": "boom", "arguments": {}},
        )
        + _line(jsonrpc="2.0", id=2, method="tools/list")
    )
    stdout = io.StringIO()

    code = asyncio.run(run_stdio_server(_core(BoomTool(), EchoTool()), stdin, stdout))

    assert code == 0
    payloads = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert len(payloads) == 2, "工具抛异常之后循环必须继续服务下一条请求"
    assert payloads[0]["result"]["isError"] is True
    assert payloads[0]["result"]["content"][0]["text"] == "Error: ValueError: kaboom"
    assert payloads[1]["result"]["tools"]


# ── 异常韧性 ──


def test_loop_survives_handle_line_exception() -> None:
    core = RaisingCore()
    stdin = io.StringIO("first\nsecond\n")
    stdout = io.StringIO()
    stderr = io.StringIO()

    code = asyncio.run(run_stdio_server(core, stdin, stdout, stderr))

    assert code == 0, "处理单行出错不能让 server 崩掉"
    assert [line.strip() for line in core.seen] == ["first", "second"], "第一行出错后必须继续读第二行"
    payloads = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [p["id"] for p in payloads] == [99]
    assert "RuntimeError" in stderr.getvalue() and "handle exploded" in stderr.getvalue()


def test_loop_survives_stdin_read_error() -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()

    code = asyncio.run(run_stdio_server(_core(EchoTool()), BrokenStdin(), stdout, stderr))

    assert code == 0, "读 stdin 出错按 EOF 处理，不许抛穿"
    assert stdout.getvalue() == ""
    assert "stdin is gone" in stderr.getvalue()


def test_loop_keeps_multiline_tool_text_on_one_line() -> None:
    """工具返回多行文本时，响应仍必须是**一行**（JSON 内部转义换行）——
    否则客户端会把行内的换行当成两条协议帧。"""
    stdin = io.StringIO(
        _line(
            jsonrpc="2.0",
            id=1,
            method="tools/call",
            params={"name": "multi", "arguments": {}},
        )
    )
    stdout = io.StringIO()

    code = asyncio.run(run_stdio_server(_core(MultiLineTool()), stdin, stdout))

    assert code == 0
    raw = stdout.getvalue()
    assert raw.endswith("\n") and raw.count("\n") == 1, f"响应必须是单行：{raw!r}"
    payload = json.loads(raw)
    assert payload["result"]["content"][0]["text"] == "line1\nline2\nline3"


def test_loop_survives_stdout_write_failure() -> None:
    """stdout 断了（对端已退出）→ 记诊断并收摊，不能抛 traceback。"""
    stdin = io.StringIO(
        _line(jsonrpc="2.0", id=1, method="tools/list")
        + _line(jsonrpc="2.0", id=2, method="tools/list")
    )
    stderr = io.StringIO()

    code = asyncio.run(run_stdio_server(_core(EchoTool()), stdin, BrokenStdout(), stderr))

    assert code == 0
    assert "stdout is gone" in stderr.getvalue()


def test_loop_survives_stdout_without_flush() -> None:
    stdin = io.StringIO(_line(jsonrpc="2.0", id=1, method="tools/list"))
    stdout = NoFlushStdout()

    code = asyncio.run(run_stdio_server(_core(EchoTool()), stdin, stdout))

    assert code == 0
    assert json.loads("".join(stdout.written))["id"] == 1


def test_loop_without_stderr_still_works() -> None:
    stdin = io.StringIO("garbage\n" + _line(jsonrpc="2.0", id=1, method="tools/list"))
    stdout = io.StringIO()

    code = asyncio.run(run_stdio_server(_core(EchoTool()), stdin, stdout))

    assert code == 0
    assert json.loads(stdout.getvalue().strip())["id"] == 1


# ── 空输入 ──


def test_loop_on_immediate_eof_returns_zero() -> None:
    stdout = io.StringIO()
    assert asyncio.run(run_stdio_server(_core(EchoTool()), io.StringIO(""), stdout)) == 0
    assert stdout.getvalue() == ""


def test_loop_supports_awaitable_readline() -> None:
    """真实 asyncio 流的 readline 是 awaitable；进程内管道（闭环测试）也依赖这一点。"""
    stdin = AsyncLines([_line(jsonrpc="2.0", id=7, method="tools/list")])
    stdout = io.StringIO()

    code = asyncio.run(run_stdio_server(_core(EchoTool()), stdin, stdout))

    assert code == 0
    assert json.loads(stdout.getvalue().strip())["id"] == 7


# ── StdioServerTransport（给入口脚本用的真实流包装）──


def test_stdio_server_transport_wraps_real_streams() -> None:
    transport = StdioServerTransport()
    assert transport.stdin is sys.stdin
    assert transport.stdout is sys.stdout
    assert transport.stderr is sys.stderr


def test_stdio_server_transport_run_with_injected_streams() -> None:
    transport = StdioServerTransport(
        stdin=io.StringIO(_line(jsonrpc="2.0", id=5, method="tools/list")),
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )
    assert asyncio.run(transport.run(_core(EchoTool()))) == 0
    assert json.loads(transport.stdout.getvalue().strip())["id"] == 5


# ── 入口脚本 scripts/mcp_server.py（按路径 import，不能 spawn 子进程）──

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "scripts" / "mcp_server.py"
_README_SNIPPET = "自研单 Agent Runtime"
_NATIVE = NATIVE_TOOL_NAMES


def _load_script():
    import importlib.util

    spec = importlib.util.spec_from_file_location("mcp_server_script", _SCRIPT)
    assert spec and spec.loader, "无法加载入口脚本"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _transcript(*messages: dict) -> str:
    return "".join(json.dumps(m, ensure_ascii=False) + "\n" for m in messages)


def _message(**payload) -> dict:
    return {"jsonrpc": "2.0", **payload}


def _run_script(argv: list[str], transcript: str) -> tuple[int, str, str]:
    """把脚本的 sys.stdin/stdout 换成内存流后直接调 main（沙箱拒绝 spawn 子进程）。"""
    module = _load_script()
    saved_in, saved_out, saved_err = sys.stdin, sys.stdout, sys.stderr
    out, err = io.StringIO(), io.StringIO()
    sys.stdin, sys.stdout, sys.stderr = io.StringIO(transcript), out, err
    try:
        code = module.main(argv)
    finally:
        sys.stdin, sys.stdout, sys.stderr = saved_in, saved_out, saved_err
    return code, out.getvalue(), err.getvalue()


def test_script_serves_a_full_transcript_and_exits_zero() -> None:
    transcript = _transcript(
        _message(id=1, method="initialize", params={"protocolVersion": "2024-11-05"}),
        _message(method="notifications/initialized"),
        _message(id=2, method="tools/list"),
        _message(
            id=3,
            method="tools/call",
            params={"name": "read_file", "arguments": {"path": "README.md"}},
        ),
    )
    code, out, err = _run_script(
        ["--root", str(_ROOT), "--name", "scripted", "--version", "7.7.7"], transcript
    )

    assert code == 0, f"EOF 必须干净退出：{err}"
    payloads = [json.loads(line) for line in out.splitlines()]
    assert [p["id"] for p in payloads] == [1, 2, 3]
    assert payloads[0]["result"]["serverInfo"] == {"name": "scripted", "version": "7.7.7"}
    assert payloads[0]["result"]["protocolVersion"] == "2024-11-05"

    names = sorted(t["name"] for t in payloads[1]["result"]["tools"])
    assert names == sorted(_NATIVE), f"脚本必须装配和 API 同一套的 8 个原生工具：{names}"

    body = payloads[2]["result"]["content"][0]["text"]
    assert payloads[2]["result"]["isError"] is False, body
    assert _README_SNIPPET in body, "脚本必须真的把 README 内容读出来"
    assert not err, f"正常输入不该往 stderr 写东西：{err!r}"


def test_script_serves_tools_that_need_missing_dependencies() -> None:
    """沙箱缺 httpx / 无网络：web_search、pdf_read 仍然必须出现在 tools/list 里
    （缺依赖只影响**执行**，不影响启动）。"""
    transcript = _transcript(_message(id=1, method="tools/list"))
    _, out, err = _run_script(["--root", str(_ROOT)], transcript)
    names = {t["name"] for t in json.loads(out.strip())["result"]["tools"]}
    assert {"web_search", "web_scrape", "pdf_read"} <= names, names
    assert not err, err


def test_script_reports_unknown_tool_as_jsonrpc_error() -> None:
    transcript = _transcript(
        _message(id=9, method="tools/call", params={"name": "nope", "arguments": {}})
    )
    code, out, _ = _run_script(["--root", str(_ROOT)], transcript)
    assert code == 0
    payload = json.loads(out.strip())
    assert payload["id"] == 9
    assert payload["error"]["code"] == -32602
    assert "nope" in payload["error"]["message"]


def test_script_defaults_root_name_and_version() -> None:
    """不给 --root 时用项目根：README.md 依然读得到；--name/--version 有默认值。"""
    transcript = _transcript(
        _message(id=1, method="initialize"),
        _message(
            id=2,
            method="tools/call",
            params={"name": "read_file", "arguments": {"path": "README.md"}},
        ),
    )
    code, out, _ = _run_script([], transcript)
    assert code == 0
    payloads = [json.loads(line) for line in out.splitlines()]
    assert payloads[0]["result"]["serverInfo"] == {
        "name": "agent-runtime",
        "version": "0.1.0",
    }
    assert _README_SNIPPET in payloads[1]["result"]["content"][0]["text"]


def test_script_inserts_src_into_sys_path() -> None:
    """像 run_benchmark.py 一样把 <root>/src 插进 sys.path，才能直接 python scripts/mcp_server.py。"""
    before = list(sys.path)
    try:
        _run_script(["--root", str(_ROOT)], _transcript(_message(id=1, method="tools/list")))
        assert str(_ROOT / "src") in sys.path
    finally:
        sys.path[:] = before


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
