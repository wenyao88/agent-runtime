from .base import BaseTool


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> BaseTool | None:
        return self._tools.get(name)

    def list_all(self) -> list[BaseTool]:
        return list(self._tools.values())

    def list_schemas(self) -> list[dict]:
        return [t.to_openai_schema() for t in self._tools.values()]

    def get_names(self) -> list[str]:
        return list(self._tools)
