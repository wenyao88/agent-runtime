"""`GET /api/context`：Inspector 的"上下文"一块（薄适配，逻辑在 `infrastructure/context/service.py`）。

数据源是**最近一次聊天会话**用的上下文管理器（见 `deps.get_last_context_manager` 的天花板说明）：
不能用 `Depends(get_context_manager)` 现造一个 —— 那个每次都是空的，接口会回一份
"0 tokens" 的报告，看起来一切正常却毫无意义。

拿不到时**HTTP 仍 200** + `available: false` + 原因：前端直接展示原因（与 memories 同一态度）。
"""
from __future__ import annotations

from fastapi import APIRouter

from ...infrastructure.context.service import context_view
from ..deps import get_context_errors, get_last_context_manager

router = APIRouter(prefix="/api", tags=["context"])


@router.get("/context")
async def get_context() -> dict:
    """当前上下文快照：分段 token / 预算与阈值 / 最近一次压缩。"""
    return context_view(get_last_context_manager(), errors=get_context_errors())
