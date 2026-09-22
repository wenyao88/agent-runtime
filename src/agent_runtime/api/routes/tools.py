"""工具目录 API：把 registry 里的工具（原生 + MCP）暴露给前端 Inspect 页。"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ...core.tool.registry import ToolRegistry
from ...infrastructure.tools.catalog import tool_catalog
from ..deps import get_tool_registry

router = APIRouter(prefix="/api", tags=["tools"])


@router.get("/tools")
async def list_tools(registry: ToolRegistry = Depends(get_tool_registry)) -> dict:
    tools = tool_catalog(registry)
    return {"count": len(tools), "tools": tools}
