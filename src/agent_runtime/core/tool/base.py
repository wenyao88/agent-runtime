from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class PropertyDef:
    type: str = "string"
    description: str = ""
    enum: list[str] | None = None
    items: PropertyDef | None = None  # type="array" 时的元素定义
    default: Any = None
    minimum: int | None = None
    maximum: int | None = None

    def to_json_schema(self) -> dict:
        """只输出已设置的键（None 与空 description 不落盘），保持生成结果干净。"""
        out: dict = {"type": self.type}
        if self.description:
            out["description"] = self.description
        if self.enum:
            out["enum"] = list(self.enum)
        if self.items is not None:
            out["items"] = self.items.to_json_schema()
        if self.default is not None:
            out["default"] = self.default
        if self.minimum is not None:
            out["minimum"] = self.minimum
        if self.maximum is not None:
            out["maximum"] = self.maximum
        return out


@dataclass
class ToolSchema:
    type: str = "object"
    properties: dict[str, PropertyDef] = field(default_factory=dict)
    required: list[str] = field(default_factory=list)


@dataclass
class ToolResult:
    tool_name: str
    success: bool
    text: str = ""
    data: Any = None
    error: str | None = None
    latency_ms: int = 0
    metadata: dict = field(default_factory=dict)


class BaseTool(ABC):
    name: str = ""
    description: str = ""

    # 默认空 schema。这里必须是真正的 ToolSchema 实例：BaseTool 不是 dataclass，
    # 用 field(default_factory=...) 只会留下一个 dataclasses.Field 对象，让未声明
    # parameters 的工具在 to_openai_schema() 时崩掉。
    # ponytail: 每次访问新建实例（避免可变默认值跨实例共享）；天花板是默认 schema 上的
    # 原地修改不会保留 —— 需要可变 schema 的工具应像 FileReaderTool 那样显式声明
    # parameters 类属性（子类类属性会遮蔽本 property）。
    @property
    def parameters(self) -> ToolSchema:
        return ToolSchema()

    @abstractmethod
    async def execute(self, **kwargs) -> ToolResult: ...

    def to_openai_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": self.parameters.type,
                    "properties": {
                        k: v.to_json_schema() for k, v in self.parameters.properties.items()
                    },
                    "required": self.parameters.required,
                },
            },
        }
