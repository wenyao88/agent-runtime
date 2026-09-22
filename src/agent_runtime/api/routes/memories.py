"""记忆 API：让"Agent 记了什么"可被外部看见与清理。

两层错误来源，**故意分开**：
  * `settings.errors`：**装配期**错误（缺依赖 / 缺 key），进程级、启动时就定了；
  * `errors`：**本次查询**的错误（某层查询失败）。
混在一起会让人分不清"配置有问题"和"这次查询刚好失败"。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from ...core.memory.manager import MemoryManager
from ...infrastructure.memory.service import (
    LAYER_ALL,
    clear_memories,
    list_memories,
    memory_settings_view,
)
from ..deps import get_memory_errors, get_memory_manager

router = APIRouter(prefix="/api", tags=["memories"])


def _settings_view(manager: MemoryManager) -> dict:
    return memory_settings_view(
        short_term_enabled=getattr(manager, "short_term", None) is not None,
        long_term_enabled=getattr(manager, "long_term", None) is not None,
        errors=get_memory_errors(),
    )


@router.get("/memories")
async def get_memories(
    query: str = "",
    top_k: int = Query(default=5, ge=1, le=50),
    layer: str = LAYER_ALL,
    manager: MemoryManager = Depends(get_memory_manager),
) -> dict:
    body = await list_memories(manager, query=query, top_k=top_k, layer=layer)
    return {"settings": _settings_view(manager), **body}


@router.delete("/memories")
async def delete_memories(
    manager: MemoryManager = Depends(get_memory_manager),
) -> dict:
    result = await clear_memories(manager)
    return {"settings": _settings_view(manager), "cleared": result}
