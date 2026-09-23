"""Trace 的三个端点（薄适配：逻辑在 `infrastructure/trace/service.py`）。

`/api/memories` 同一套路：**api 层只做参数解析与状态码**，语义全在被测过的 service 里。
`settings` 段（store 种类 + 装配期错误）与 `errors` 段（本次查询的错误）**故意分开**：
混在一起就分不清"存储配错了"和"这次查询刚好失败"。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from ...core.trace.store import DEFAULT_CAPACITY, TraceStore
from ...infrastructure.trace.service import events_view, trace_view, traces_view
from ..deps import get_trace_errors, get_trace_store

router = APIRouter(prefix="/api", tags=["traces"])


def _settings_view(store: TraceStore) -> dict:
    return {"store": type(store).__name__, "errors": get_trace_errors()}


@router.get("/traces")
async def list_traces(
    limit: int = Query(default=DEFAULT_CAPACITY, ge=0, le=200),
    store: TraceStore = Depends(get_trace_store),
) -> dict:
    """历史 trace 小结，最新在前（明细走 `/{trace_id}`）。"""
    return {**traces_view(store, limit=limit), "settings": _settings_view(store)}


@router.get("/traces/{trace_id}")
async def get_trace(
    trace_id: str,
    store: TraceStore = Depends(get_trace_store),
) -> dict:
    """单条 trace 详情（含 steps）；查不到 → 404 + 可读原因。"""
    body = trace_view(store, trace_id)
    if not body["available"]:
        raise HTTPException(status_code=404, detail=body["reason"])
    return body


@router.get("/traces/{trace_id}/events")
async def get_trace_events(
    trace_id: str,
    store: TraceStore = Depends(get_trace_store),
) -> dict:
    """原始事件日志（与 steps 是同一批事实的两种视图）。"""
    body = events_view(store, trace_id)
    if body["reason"]:
        raise HTTPException(status_code=404, detail=body["reason"])
    return body
