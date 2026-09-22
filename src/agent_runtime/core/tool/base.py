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
    parameters: ToolSchema = field(default_factory=ToolSchema)

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
