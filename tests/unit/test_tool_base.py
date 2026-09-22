"""BaseTool 契约测试：未显式声明 parameters 的工具也必须能产出合法 schema。

背景：BaseTool 是普通 ABC（不是 dataclass），早期版本把 `parameters` 写成
`field(default_factory=ToolSchema)`，于是类属性实际是一个 dataclasses.Field 对象，
任何未覆盖该属性的工具在 to_openai_schema() 时都会崩。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from agent_runtime.core.tool.base import BaseTool, PropertyDef, ToolResult, ToolSchema


class NoParamsTool(BaseTool):
    """合法场景：无参数工具（如 get_current_time），不声明 parameters。"""

    name = "no_params"
    description = "没有显式声明 parameters 的工具"

    async def execute(self, **kwargs) -> ToolResult:
        return ToolResult(tool_name=self.name, success=True, text="ok")


class WithParamsTool(BaseTool):
    """常规场景：显式声明 parameters。"""

    name = "with_params"
    description = "显式声明 parameters 的工具"
    parameters = ToolSchema(
        properties={"path": PropertyDef(type="string", description="路径")},
        required=["path"],
    )

    async def execute(self, path: str) -> ToolResult:
        return ToolResult(tool_name=self.name, success=True, text=path)


def test_default_parameters_is_a_tool_schema() -> None:
    tool = NoParamsTool()
    assert isinstance(tool.parameters, ToolSchema), f"实际类型 {type(tool.parameters)!r}"
    assert isinstance(tool.parameters.properties, dict)
    assert tool.parameters.required == []


def test_default_schema_not_shared_between_instances() -> None:
    a = NoParamsTool()
    a.parameters.properties["x"] = PropertyDef(type="string")
    b = NoParamsTool()
    assert b.parameters.properties == {}, "默认 schema 被跨实例共享（可变默认值陷阱）"


def test_to_openai_schema_without_explicit_parameters() -> None:
    schema = NoParamsTool().to_openai_schema()
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "no_params"
    assert schema["function"]["parameters"] == {
        "type": "object",
        "properties": {},
        "required": [],
    }


def test_to_openai_schema_with_explicit_parameters() -> None:
    params = WithParamsTool().to_openai_schema()["function"]["parameters"]
    assert params["type"] == "object"
    assert params["properties"]["path"] == {"type": "string", "description": "路径"}
    assert params["required"] == ["path"]


def test_base_tool_stays_abstract() -> None:
    try:
        BaseTool()  # type: ignore[abstract]
    except TypeError:
        return
    raise AssertionError("BaseTool 应保持抽象，不可直接实例化")


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
