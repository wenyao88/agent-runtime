"""MCP Tool → BaseTool 适配器。

两条契约：
  1. 工具名带 server 前缀（`fs__read_file`），避免与 Native Tool 撞名；
  2. schema 由 MCP 的 JSON Schema 翻译而来；遇到不支持的构造（oneOf/anyOf/allOf/$ref）
     降级为 object 并把原始构造保留进 description —— 不抛异常、不静默丢信息。
"""
from __future__ import annotations

import json
from typing import Any

from ...core.tool.base import BaseTool, PropertyDef, ToolResult, ToolSchema

PRIMITIVE_TYPES = {"string", "integer", "number", "boolean", "array", "object"}
UNSUPPORTED_KEYS = ("oneOf", "anyOf", "allOf", "$ref")


def _brief(raw: Any, limit: int = 200) -> str:
    try:
        text = json.dumps(raw, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(raw)
    return text if len(text) <= limit else text[:limit] + "…"


def _property_from_json(raw: Any, key: str) -> PropertyDef:
    if not isinstance(raw, dict):
        return PropertyDef(type="string", description=f"{key}: 无法解析的 schema 片段")

    found = [name for name in UNSUPPORTED_KEYS if name in raw]
    if found:
        return PropertyDef(
            type="object",
            description=f"{key}: 不支持的 schema 构造 {', '.join(found)}；原文={_brief(raw)}",
        )

    json_type = str(raw.get("type") or "string")
    if json_type not in PRIMITIVE_TYPES:
        json_type = "string"

    items = None
    if json_type == "array" and isinstance(raw.get("items"), dict):
        items = _property_from_json(raw["items"], f"{key}[]")

    enum = raw.get("enum")
    minimum = raw.get("minimum")
    maximum = raw.get("maximum")
    return PropertyDef(
        type=json_type,
        description=str(raw.get("description") or ""),
        enum=[str(v) for v in enum] if isinstance(enum, list) else None,
        items=items,
        default=raw.get("default"),
        minimum=minimum if isinstance(minimum, int) else None,
        maximum=maximum if isinstance(maximum, int) else None,
    )


class MCPToolAdapter(BaseTool):
    """把一个 MCP Tool 暴露成普通 BaseTool，Registry 与 ReAct Loop 都不感知来源。"""

    def __init__(self, client: Any, server: str, tool_info: dict[str, Any]) -> None:
        info = dict(tool_info or {})
        self._client = client
        self._server = server
        self._info = info
        raw_name = str(info.get("name") or "tool")
        self.name = f"{server}__{raw_name}"
        self.description = str(
            info.get("description") or f"MCP 工具 {raw_name}（来自 {server}）"
        )

    @property
    def parameters(self) -> ToolSchema:
        schema = self._info.get("inputSchema")
        if not isinstance(schema, dict):
            return ToolSchema()

        properties: dict[str, PropertyDef] = {}
        raw_props = schema.get("properties")
        if isinstance(raw_props, dict):
            for key, value in raw_props.items():
                properties[str(key)] = _property_from_json(value, str(key))

        required = [str(k) for k in (schema.get("required") or []) if isinstance(k, str)]
        return ToolSchema(
            type=str(schema.get("type") or "object"),
            properties=properties,
            required=required,
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        if self._client is None:
            return ToolResult(
                tool_name=self.name, success=False, text="Error: MCP 适配器未绑定 client"
            )
        return await self._client.call_tool(
            self._server, str(self._info.get("name") or ""), kwargs
        )
