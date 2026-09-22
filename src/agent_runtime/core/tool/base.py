from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class PropertyDef:
    type: str = "string"
    description: str = ""
    enum: list[str] | None = None


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
                        k: {"type": v.type, "description": v.description}
                        for k, v in self.parameters.properties.items()
                    },
                    "required": self.parameters.required,
                },
            },
        }
