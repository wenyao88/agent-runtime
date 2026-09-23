"""`/api/traces` 背后的逻辑（纯逻辑，零第三方依赖 → 沙箱可测）。

放这里的理由与 `memory/service.py`、`tools/catalog.py` 一致：路由依赖 fastapi，
逻辑一放路由里就完全无法验证。**api 层只做参数解析与序列化。**

两条口径：
  * 列表只出小结（`TraceSession.summary()`，**不带** steps/events）—— 几十条 trace 全量明细很笨重；
  * 查不到 / store 坏了 → 返回可读 `reason`（路由据此回 404 或如实展示），**绝不抛**。
"""
from __future__ import annotations

from typing import Any

from ...core.trace.models import TraceSession
from ...core.trace.store import DEFAULT_CAPACITY, TraceStore


def traces_view(store: TraceStore, *, limit: int = DEFAULT_CAPACITY) -> dict[str, Any]:
    """列表：最新在前的小结 + 条数。store 坏了 → `errors` 里说明，仍回 200。"""
    try:
        items = list(store.list(limit))
    except Exception as e:  # noqa: BLE001 —— 对外接口不抛，如实回报
        return {
            "count": 0,
            "traces": [],
            "errors": [f"列出 trace 失败：{type(e).__name__}: {e}"],
        }
    return {"count": len(items), "traces": items, "errors": []}


def _resolve(store: TraceStore, trace_id: str) -> tuple[TraceSession | None, str]:
    """按 id 取会话；拿不到就返回 `(None, 可读原因)`。空 id 与"查不到"必须区分（排查时不是一回事）。"""
    wanted = str(trace_id or "").strip()
    if not wanted:
        return None, "trace_id 为空：请给出要查的 trace（GET /api/traces 可列出全部）"
    try:
        session = store.get(wanted)
    except Exception as e:  # noqa: BLE001
        return None, f"读取 trace 失败：{type(e).__name__}: {e}"
    if session is None:
        return None, f"没有这条 trace：{wanted}（默认内存存储重启即丢，容量满会淘汰最旧的）"
    return session, ""


def trace_view(store: TraceStore, trace_id: str) -> dict[str, Any]:
    """详情（含 steps/config）：`available: false` + `reason` 时路由回 404。"""
    session, reason = _resolve(store, trace_id)
    if session is None:
        return {"available": False, "trace": None, "reason": reason}
    return {"available": True, "trace": session.to_dict(), "reason": ""}


def events_view(store: TraceStore, trace_id: str) -> dict[str, Any]:
    """原始事件日志。

    先确认 trace 存在再看事件：否则"这条 trace 没有事件"和"这条 trace 不存在"都会回 `count: 0`，
    排查时就分不清是数据少还是 id 写错了。
    """
    session, reason = _resolve(store, trace_id)
    if session is None:
        return {"count": 0, "events": [], "reason": reason}
    try:
        events = list(store.events(session.trace_id))
    except Exception as e:  # noqa: BLE001
        return {"count": 0, "events": [], "reason": f"读取事件失败：{type(e).__name__}: {e}"}
    payload = [event.to_dict() for event in events]
    return {"count": len(payload), "events": payload, "reason": ""}
