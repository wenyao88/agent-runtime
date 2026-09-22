"""读取工作区内文本文件。路径逃逸一律拒绝；输出按上下文上限截断。"""
from __future__ import annotations

import os

from ...core.tool.base import BaseTool, PropertyDef, ToolResult, ToolSchema
from ._common import resolve_within, truncate_for_context

MAX_CHARS = 4000


class FileReaderTool(BaseTool):
    name = "read_file"
    description = "读取本地工作区内的文本文件内容。path 为工作区根目录的相对路径。"
    parameters = ToolSchema(
        properties={"path": PropertyDef(type="string", description="工作区内相对路径，如 docs/x.md")},
        required=["path"],
    )

    def __init__(self, root: str):
        self._root = os.path.abspath(root)

    async def execute(self, path: str) -> ToolResult:
        target = resolve_within(self._root, path)
        if target is None:
            return ToolResult(
                tool_name=self.name,
                success=False,
                text=f"Error: path escapes workspace root: {path!r}",
            )
        if not os.path.isfile(target):
            return ToolResult(
                tool_name=self.name, success=False, text=f"Error: file not found: {path!r}"
            )
        try:
            with open(target, encoding="utf-8", errors="replace") as fh:
                text = fh.read(MAX_CHARS + 1)
        except OSError as e:
            return ToolResult(tool_name=self.name, success=False, text=f"Error: {e}")

        out, truncated = truncate_for_context(text, MAX_CHARS)
        return ToolResult(
            tool_name=self.name,
            success=True,
            text=out,
            data={"path": path, "chars": len(text), "truncated": truncated},
        )
