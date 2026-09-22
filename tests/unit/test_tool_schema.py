"""ToolSchema 扩展契约：真实工具需要整数/数组/枚举/默认值，schema 必须能表达。

动机：Tool Argument Accuracy 是 Benchmark 指标之一，参数类型表达不清会直接掉分。
旧的 to_openai_schema() 只透出 type + description，整数边界、枚举、数组元素全部丢失。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from agent_runtime.core.tool.base import BaseTool, PropertyDef, ToolResult, ToolSchema


class SearchTool(BaseTool):
    """模拟 Phase 2 真实工具的典型参数形态。"""

    name = "search"
    description = "带完整参数类型的工具"
    parameters = ToolSchema(
        properties={
            "query": PropertyDef(type="string", description="搜索词"),
            "max_results": PropertyDef(
                type="integer", description="返回条数", default=5, minimum=1, maximum=20
            ),
            "language": PropertyDef(
                type="string", description="语言过滤", enum=["python", "go"]
            ),
            "tags": PropertyDef(
                type="array", description="标签", items=PropertyDef(type="string")
            ),
        },
        required=["query"],
    )

    async def execute(self, **kwargs) -> ToolResult:
        return ToolResult(tool_name=self.name, success=True, text="ok")


def _params() -> dict:
    return SearchTool().to_openai_schema()["function"]["parameters"]


def test_integer_bounds_and_default_are_emitted() -> None:
    assert _params()["properties"]["max_results"] == {
        "type": "integer",
        "description": "返回条数",
        "default": 5,
        "minimum": 1,
        "maximum": 20,
    }


def test_enum_is_emitted() -> None:
    assert _params()["properties"]["language"] == {
        "type": "string",
        "description": "语言过滤",
        "enum": ["python", "go"],
    }


def test_array_items_are_emitted() -> None:
    assert _params()["properties"]["tags"] == {
        "type": "array",
        "description": "标签",
        "items": {"type": "string"},
    }


def test_unset_optional_keys_are_omitted() -> None:
    assert _params()["properties"]["query"] == {"type": "string", "description": "搜索词"}


def test_empty_description_is_omitted() -> None:
    assert PropertyDef(type="string").to_json_schema() == {"type": "string"}


def test_top_level_shape_and_required_unchanged() -> None:
    params = _params()
    assert params["type"] == "object"
    assert params["required"] == ["query"]
    assert set(params["properties"]) == {"query", "max_results", "language", "tags"}


def test_property_def_defaults() -> None:
    p = PropertyDef()
    assert p.type == "string"
    assert p.description == ""
    assert p.enum is None
    assert p.items is None
    assert p.default is None
    assert p.minimum is None
    assert p.maximum is None


def test_schema_is_json_serializable() -> None:
    json.dumps(SearchTool().to_openai_schema())


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
