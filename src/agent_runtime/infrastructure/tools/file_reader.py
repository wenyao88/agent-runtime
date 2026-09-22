"""读取工作区内文本文件。路径逃逸一律拒绝；输出截断 4000 字符。"""
from __future__ import annotations

import os

from ...core.tool.base import BaseTool, PropertyDef, ToolResult, ToolSchema

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
        p = os.path.abspath(os.path.join(self._root, path))
        if not (p == self._root or p.startswith(self._root + os.sep)):
            return ToolResult(tool_name=self.name, success=False,
                              text=f"Error: path escapes workspace root: {path!r}")
        if not os.path.isfile(p):
            return ToolResult(tool_name=self.name, success=False,
                              text=f"Error: file not found: {path!r}")
        try:
            text = open(p, encoding="utf-8", errors="replace").read(MAX_CHARS + 1)
        except OSError as e:
            return ToolResult(tool_name=self.name, success=False, text=f"Error: {e}")
        truncated = len(text) > MAX_CHARS
        out = text[:MAX_CHARS] + ("\n...[truncated]" if truncated else "")
        return ToolResult(tool_name=self.name, success=True, text=out,
                          data={"path": path, "chars": len(text), "truncated": truncated})
