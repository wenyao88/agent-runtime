"""MCPServerCore 契约：把 ToolRegistry 暴露成 MCP Server（纯协议、零 IO）。

对称性要求：本文件断言的行为必须与 `infrastructure/mcp/client.py` 的**客户端**实现一致
（PROTOCOL_VERSION=2024-11-05、换行分隔 JSON-RPC 2.0、initialize / notifications/initialized /
tools/list / tools/call、结果 content:[{type:"text",text}] + isError）。

沙箱约束：core/** 零第三方依赖、且不得 import infrastructure/api —— 该约束在文件末尾
用源码扫描实测，而不是靠约定。
"""
from __future__ import annotations

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
    parameters = ToolSchema(properties={"text": PropertyDef(type="string")}, required=[])

    async def execute(self, **kwargs) -> ToolResult:
        raise ValueError("kaboom")


class FailTool(BaseTool):
    name = "fail"
    description = "返回 success=False"
    parameters = ToolSchema(properties={}, required=[])

    async def execute(self, **kwargs) -> ToolResult:
        return ToolResult(tool_name=self.name, success=False, text="Error: nope")


def _registry(*tools: BaseTool) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return registry


def _core(*tools: BaseTool, **kw) -> MCPServerCore:
    return MCPServerCore(_registry(*tools), **kw)


async def _call(core: MCPServerCore, message) -> dict | None:
    return await core.handle(message)


def _run(coro):
    import asyncio

    return asyncio.run(coro)


# ── initialize ──


def test_initialize_returns_capabilities_and_server_info() -> None:
    core = _core(EchoTool(), server_name="my-server", server_version="9.9.9")
    response = _run(core.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}))
    assert response["jsonrpc"] == "2.0"
    assert response["id"] == 1
    result = response["result"]
    assert result["protocolVersion"] == "2024-11-05"
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    assert result["serverInfo"] == {"name": "my-server", "version": "9.9.9"}


def test_initialize_defaults_and_custom_protocol_version() -> None:
    core = _core(EchoTool())
    result = _run(core.handle({"id": 1, "method": "initialize", "params": {}}))["result"]
    assert result["protocolVersion"] == "2024-11-05"
    assert result["serverInfo"] == {"name": "agent-runtime", "version": "0.1.0"}

    core2 = _core(EchoTool(), protocol_version="2025-01-01")
    result2 = _run(core2.handle({"id": 1, "method": "initialize"}))["result"]
    assert result2["protocolVersion"] == "2025-01-01"


def test_initialize_echoes_string_id_verbatim() -> None:
    core = _core(EchoTool())
    response = _run(core.handle({"jsonrpc": "2.0", "id": "abc-1", "method": "initialize"}))
    assert response["id"] == "abc-1"


# ── 通知：一律不回响应 ──


def test_notifications_initialized_returns_none() -> None:
    core = _core(EchoTool())
    assert _run(
        core.handle({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
    ) is None


def test_any_notification_without_id_returns_none() -> None:
    core = _core(EchoTool())
    for method in ("notifications/cancelled", "notifications/progress", "some/unknown"):
        assert _run(core.handle({"jsonrpc": "2.0", "method": method})) is None, method


# ── tools/list ──


def test_tools_list_exposes_name_description_and_input_schema() -> None:
    core = _core(EchoTool())
    result = _run(core.handle({"id": 2, "method": "tools/list", "params": {}}))["result"]
    assert len(result["tools"]) == 1
    tool = result["tools"][0]
    assert tool["name"] == "echo"
    assert tool["description"] == "回显输入文本"
    assert tool["inputSchema"] == {
        "type": "object",
        "properties": {"text": {"type": "string", "description": "要回显的文本"}},
        "required": ["text"],
    }


def test_tools_list_input_schema_matches_openai_parameters() -> None:
    """MCP 的 inputSchema 必须就是 to_openai_schema()["function"]["parameters"]，不能是转写。"""
    tool = EchoTool()
    core = _core(tool)
    result = _run(core.handle({"id": 1, "method": "tools/list"}))["result"]
    assert result["tools"][0]["inputSchema"] == tool.to_openai_schema()["function"]["parameters"]


def test_tools_list_without_registry_is_empty_not_crash() -> None:
    core = MCPServerCore(None)
    result = _run(core.handle({"id": 1, "method": "tools/list"}))["result"]
    assert result == {"tools": []}


def test_tools_list_with_empty_registry_is_empty() -> None:
    core = MCPServerCore(ToolRegistry())
    result = _run(core.handle({"id": 1, "method": "tools/list"}))["result"]
    assert result == {"tools": []}


# ── tools/call ──


def test_tools_call_success_returns_text_content() -> None:
    core = _core(EchoTool())
    response = _run(
        core.handle(
            {
                "id": 3,
                "method": "tools/call",
                "params": {"name": "echo", "arguments": {"text": "hi"}},
            }
        )
    )
    assert response["result"] == {
        "content": [{"type": "text", "text": "echo: hi"}],
        "isError": False,
    }


def test_tools_call_failure_result_becomes_is_error() -> None:
    core = _core(FailTool())
    result = _run(
        core.handle({"id": 1, "method": "tools/call", "params": {"name": "fail", "arguments": {}}})
    )["result"]
    assert result["isError"] is True
    assert "nope" in result["content"][0]["text"]


def test_tools_call_missing_arguments_defaults_to_empty() -> None:
    core = _core(FailTool())
    response = _run(core.handle({"id": 1, "method": "tools/call", "params": {"name": "fail"}}))
    assert response["result"]["isError"] is True


def test_tools_call_tool_exception_never_escapes() -> None:
    core = _core(BoomTool())
    result = _run(
        core.handle({"id": 1, "method": "tools/call", "params": {"name": "boom", "arguments": {}}})
    )["result"]
    assert result["isError"] is True
    assert result["content"][0]["text"] == "Error: ValueError: kaboom"


def test_tools_call_unknown_tool_is_invalid_params_error() -> None:
    core = _core(EchoTool())
    response = _run(
        core.handle({"id": 4, "method": "tools/call", "params": {"name": "nope", "arguments": {}}})
    )
    assert response["id"] == 4
    assert response["error"]["code"] == -32602
    assert "nope" in response["error"]["message"]
    assert "result" not in response


def test_tools_call_non_object_arguments_is_invalid_params_error() -> None:
    core = _core(EchoTool())
    for bad in ("text", 5, ["text"], None):
        response = _run(
            core.handle(
                {"id": 5, "method": "tools/call", "params": {"name": "echo", "arguments": bad}}
            )
        )
        assert response["error"]["code"] == -32602, bad


def test_tools_call_missing_name_is_invalid_params_error() -> None:
    core = _core(EchoTool())
    response = _run(core.handle({"id": 6, "method": "tools/call", "params": {"arguments": {}}}))
    assert response["error"]["code"] == -32602


def test_tools_call_on_empty_registry_is_invalid_params_error() -> None:
    core = MCPServerCore(None)
    response = _run(
        core.handle({"id": 7, "method": "tools/call", "params": {"name": "x", "arguments": {}}})
    )
    assert response["error"]["code"] == -32602


# ── 未知 method ──


def test_unknown_method_is_method_not_found_error() -> None:
    core = _core(EchoTool())
    response = _run(core.handle({"id": 8, "method": "resources/list", "params": {}}))
    assert response["id"] == 8
    assert response["error"]["code"] == -32601
    assert "resources/list" in response["error"]["message"]


# ── 畸形输入：绝不抛 ──


def test_handle_malformed_messages_never_raise() -> None:
    core = _core(EchoTool())
    assert _run(core.handle(None)) is None
    assert _run(core.handle("not a dict")) is None
    assert _run(core.handle([1, 2, 3])) is None


def test_handle_missing_or_empty_method_never_raises() -> None:
    """缺 method / method 为空串：有 id 就回 -32600（invalid request），没 id 就当通知。"""
    core = _core(EchoTool())
    response = _run(core.handle({"id": 9, "params": {}}))
    assert response is not None and response["id"] == 9
    assert response["error"]["code"] == -32600
    assert _run(core.handle({"method": ""})) is None
    assert _run(core.handle({"id": 12, "method": ""}))["error"]["code"] == -32600
    assert _run(core.handle({"id": 13, "method": 42}))["error"]["code"] == -32600


def test_handle_unexpected_params_shape_never_raises() -> None:
    core = _core(EchoTool())
    for params in (None, "x", 5, []):
        response = _run(core.handle({"id": 10, "method": "tools/call", "params": params}))
        assert response["error"]["code"] == -32602, params


# ── handle_line：线格式 ──


def test_handle_line_returns_single_trailing_newline_json() -> None:
    core = _core(EchoTool())
    out = _run(core.handle_line('{"jsonrpc":"2.0","id":1,"method":"tools/list"}'))
    assert isinstance(out, str)
    assert out.endswith("\n")
    assert not out.endswith("\n\n"), "响应行结尾只能有一个 \\n"
    assert out.count("\n") == 1
    payload = json.loads(out)
    assert payload["id"] == 1
    assert payload["result"]["tools"][0]["name"] == "echo"


def test_handle_line_blank_lines_return_none() -> None:
    core = _core(EchoTool())
    for blank in ("", "   ", "\n", "\t\r\n"):
        assert _run(core.handle_line(blank)) is None, repr(blank)


def test_handle_line_notification_returns_none() -> None:
    core = _core(EchoTool())
    assert _run(core.handle_line('{"jsonrpc":"2.0","method":"notifications/initialized"}')) is None


def test_handle_line_broken_json_with_id_returns_error_with_that_id() -> None:
    core = _core(EchoTool())
    out = _run(core.handle_line('{"jsonrpc":"2.0","id":5,"method":"tools/list"'))
    assert out is not None, "行内含 id 时必须回一条 error 响应"
    payload = json.loads(out)
    assert payload["id"] == 5
    assert payload["error"]["code"] == -32700
    assert out.count("\n") == 1


def test_handle_line_broken_json_without_id_returns_none() -> None:
    core = _core(EchoTool())
    for broken in ("{not json", "hello world", "[[["):
        assert _run(core.handle_line(broken)) is None, broken
    assert (
        _run(core.handle_line('{"method":"tools/list"')) is None
    ), "坏 JSON 里没有 id 时不能猜一个 id 出来"


def test_handle_line_broken_json_ignores_nested_ids() -> None:
    """只认**顶层** id。嵌套 id（比如 params 里的）不是这条消息的请求 id ——
    拿它当 id 回错误响应，会把错误错配到客户端另一次在途请求上，比不回响应更糟。"""
    core = _core(EchoTool())
    for broken in (
        '{"method":"x","params":{"id":9}}',
        '{"params":{"id":9},"method":"tools/list"',
        '{"jsonrpc":"2.0","params":{"nested":{"id":9}}}',
    ):
        assert _run(core.handle_line(broken)) is None, broken


def test_handle_line_broken_json_ignores_id_lookalike_inside_a_string_value() -> None:
    """字符串值里出现的 `id":9` 不是键，不能当 id —— 那同样是错误响应错配。"""
    core = _core(EchoTool())
    assert _run(core.handle_line('{"a":"id":9","method":"x"')) is None


def test_handle_line_broken_json_finds_top_level_id_after_nested_one() -> None:
    """嵌套 id 要被跳过，但扫描必须继续 —— 后面那个顶层 id 才是真正的请求 id。"""
    core = _core(EchoTool())
    out = _run(core.handle_line('{"params":{"id":9},"id":7,"method":"tools/list"'))
    assert out is not None
    assert json.loads(out)["id"] == 7


def test_handle_line_broken_json_with_null_id_uses_null() -> None:
    """`"id": null` 是合法的 JSON-RPC id，不能装看不见（那会让客户端永远等不到响应）。"""
    core = _core(EchoTool())
    out = _run(core.handle_line('{"jsonrpc":"2.0","id":null,"method":"tools/list"'))
    assert out is not None
    payload = json.loads(out)
    assert payload["id"] is None
    assert payload["error"]["code"] == -32700


def test_handle_line_accepts_bytes_lines() -> None:
    """真实 stdio 写过来的是 utf-8 字节；线格式层不该假设调用方已经解码。"""
    core = _core(EchoTool())
    out = _run(core.handle_line(b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}'))
    assert json.loads(out)["id"] == 1
    assert _run(core.handle_line(b"")) is None


def test_handle_empty_dict_returns_none() -> None:
    assert _run(_core(EchoTool()).handle({})) is None


def test_handle_line_non_object_json_returns_none() -> None:
    core = _core(EchoTool())
    for payload in ("[1,2]", '"str"', "42", "null"):
        assert _run(core.handle_line(payload)) is None, payload


def test_handle_line_internal_error_is_reported_not_raised() -> None:
    class ExplodingRegistry:
        def list_all(self):
            raise RuntimeError("registry exploded")

    core = MCPServerCore(ExplodingRegistry())
    out = _run(core.handle_line('{"id":11,"method":"tools/list"}'))
    payload = json.loads(out)
    assert payload["id"] == 11
    assert payload["error"]["code"] == -32603


# ── 结构约束（core 不得依赖第三方 / IO 层 / API 层）──
# 已移到 `test_core_layering.py`：那条约束属于整个 `core/**`，写在 MCP 的测试文件里就只能扫
# `core/mcp/**` —— 名字写着"core 不得依赖第三方"，实际覆盖一个子目录（审查 M4）。


# ── 独立运行（双模式：python tests/unit/x.py 末行 ALL PASS）──


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
