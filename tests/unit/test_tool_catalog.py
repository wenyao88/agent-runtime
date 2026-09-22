"""工具装配契约：原生工具注册 + 工具目录（供 GET /api/tools 使用的纯逻辑）。

为什么这些逻辑放在 infrastructure 而不是 api/deps.py：
  deps.py 依赖 pydantic_settings（装不上），一旦把逻辑放那儿，沙箱内就无法测试。
  把「注册」与「列目录」做成纯标准库函数后，装配的正确性在本环境就能被验证。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from agent_runtime.core.tool.registry import ToolRegistry
from agent_runtime.infrastructure.tools.catalog import (
    NATIVE_TOOL_NAMES,
    register_native_tools,
    tool_catalog,
)

_ROOT = str(Path(__file__).resolve().parents[2])


def test_registers_all_native_tools() -> None:
    registry = ToolRegistry()
    names = register_native_tools(registry, root=_ROOT)
    assert set(names) == set(NATIVE_TOOL_NAMES)
    assert len(names) == len(NATIVE_TOOL_NAMES), "不得有重名"
    assert {t.name for t in registry.list_all()} == set(NATIVE_TOOL_NAMES)


def test_expected_tool_set_is_complete() -> None:
    assert set(NATIVE_TOOL_NAMES) == {
        "read_file",
        "github_get_repo",
        "github_list_dir",
        "github_read_file",
        "github_search_code",
        "web_search",
        "web_scrape",
        "pdf_read",
    }


def test_every_tool_has_a_usable_schema() -> None:
    registry = ToolRegistry()
    register_native_tools(registry, root=_ROOT)
    for tool in registry.list_all():
        schema = tool.to_openai_schema()
        assert schema["type"] == "function"
        fn = schema["function"]
        assert fn["name"] == tool.name
        assert fn["description"], f"{tool.name} 缺少 description（模型据此选择工具）"
        assert fn["parameters"]["type"] == "object"
        assert fn["parameters"]["properties"], f"{tool.name} 没有任何参数声明"
        assert fn["parameters"]["required"], f"{tool.name} 没有 required 参数"


def test_registration_is_idempotent() -> None:
    registry = ToolRegistry()
    register_native_tools(registry, root=_ROOT)
    register_native_tools(registry, root=_ROOT)
    assert len(registry.list_all()) == len(NATIVE_TOOL_NAMES), "重复注册不应产生重复工具"


def test_catalog_shape_is_stable_and_sorted() -> None:
    registry = ToolRegistry()
    register_native_tools(registry, root=_ROOT)
    catalog = tool_catalog(registry)
    assert [e["name"] for e in catalog] == sorted(NATIVE_TOOL_NAMES)
    for entry in catalog:
        assert set(entry) == {"name", "description", "parameters"}
        assert entry["parameters"]["type"] == "object"


def test_catalog_includes_mcp_tools_when_present() -> None:
    """MCP 工具注入同一个 registry 后，必须与原生工具在目录里形态一致。"""
    from agent_runtime.infrastructure.mcp.adapter import MCPToolAdapter

    registry = ToolRegistry()
    register_native_tools(registry, root=_ROOT)
    registry.register(
        MCPToolAdapter(
            client=None,
            server="fs",
            tool_info={
                "name": "read_file",
                "description": "MCP 提供的读文件",
                "inputSchema": {
                    "type": "object",
                    "properties": {"path": {"type": "string", "description": "路径"}},
                    "required": ["path"],
                },
            },
        )
    )
    catalog = tool_catalog(registry)
    assert len(catalog) == len(NATIVE_TOOL_NAMES) + 1
    entry = next(e for e in catalog if e["name"] == "fs__read_file")
    assert entry["parameters"]["required"] == ["path"], "MCP 工具的参数形态必须与原生一致"


def test_github_tools_share_the_configured_token() -> None:
    registry = ToolRegistry()
    register_native_tools(registry, root=_ROOT, github_token="ghp_x")
    tool = registry.get("github_search_code")
    assert tool is not None
    assert tool._token == "ghp_x", "token 应注入到所有 GitHub 工具"


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
