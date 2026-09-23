"""`/api/traces` 背后的纯逻辑契约（内存 store / 假 store 驱动，沙箱可测）。

为什么这段逻辑不放 `api/routes/traces.py`：路由依赖 fastapi + pydantic（沙箱装不上），
逻辑一放那儿就完全无法验证。与 memories / tools / skills 同一处理方式 —— api 层只做参数解析与序列化。

关键语义：
  * 列表只给小结（**不拖** steps/events）：几十条 trace × 每条几十步会很笨重；
  * 查不到 → `available: false` + 可读原因（路由据此回 404），**不抛**；
  * store 报错（磁盘坏 / DB 坏）→ 如实回报，接口不 500。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from agent_runtime.core.trace.models import TraceEvent, TraceSession, TraceStep
from agent_runtime.core.trace.store import InMemoryTraceStore
from agent_runtime.infrastructure.trace.service import (
    events_view,
    trace_view,
    traces_view,
)


def _session(trace_id: str, task: str, *, source: str = "chat", steps: int = 0) -> TraceSession:
    session = TraceSession(trace_id=trace_id, task=task, source=source, status="finished")
    for number in range(1, steps + 1):
        session.steps.append(TraceStep(step_number=number, thought=f"thought {number}"))
    session.events.append(TraceEvent(event_type="thought", step_number=1, data={"content": "x"}))
    return session


class RaisingStore:
    """任何一次访问都炸 —— 用来证明"存储坏了接口也不 500"。"""

    def __init__(self, message: str = "disk on fire") -> None:
        self.message = message

    def save(self, session) -> None:  # pragma: no cover - 契约完整性
        raise RuntimeError(self.message)

    def get(self, trace_id: str):
        raise RuntimeError(self.message)

    def list(self, limit: int = 50):
        raise RuntimeError(self.message)

    def events(self, trace_id: str):
        raise RuntimeError(self.message)


# ── 列表 ──


def test_list_returns_newest_first() -> None:
    store = InMemoryTraceStore(capacity=10)
    store.save(_session("aaa", "first"))
    store.save(_session("bbb", "second"))
    body = traces_view(store)
    assert body["count"] == 2, body
    assert [item["trace_id"] for item in body["traces"]] == ["bbb", "aaa"], body
    assert body["errors"] == [], body


def test_list_summaries_carry_no_heavy_detail() -> None:
    """列表必须是"小结"：带上 steps/events 就等于把整份 trace 拖给列表接口。"""
    store = InMemoryTraceStore(capacity=10)
    store.save(_session("aaa", "task", steps=3))
    item = traces_view(store)["traces"][0]
    assert "events" not in item, item
    assert "config" not in item, item
    assert item["steps"] == 3, item
    assert item["source"] == "chat", item


def test_list_respects_the_limit() -> None:
    store = InMemoryTraceStore(capacity=10)
    for index in range(5):
        store.save(_session(f"t{index}", f"task {index}"))
    assert traces_view(store, limit=2)["count"] == 2
    assert traces_view(store, limit=0)["count"] == 0


def test_list_reports_a_store_failure_instead_of_raising() -> None:
    body = traces_view(RaisingStore())
    assert body["count"] == 0, body
    assert body["traces"] == [], body
    assert body["errors"] and "disk on fire" in body["errors"][0], body


# ── 详情 ──


def test_a_known_trace_is_available_with_its_steps() -> None:
    store = InMemoryTraceStore(capacity=10)
    store.save(_session("aaa", "task", steps=2))
    body = trace_view(store, "aaa")
    assert body["available"] is True, body
    assert body["reason"] == "", body
    assert body["trace"]["trace_id"] == "aaa", body
    assert len(body["trace"]["steps"]) == 2, body
    assert body["trace"]["steps"][0]["thought"] == "thought 1", body


def test_a_missing_trace_is_reported_with_a_readable_reason() -> None:
    body = trace_view(InMemoryTraceStore(capacity=10), "nope")
    assert body["available"] is False, body
    assert body["trace"] is None, body
    assert "nope" in body["reason"], body


def test_a_blank_trace_id_is_rejected_with_a_reason() -> None:
    """空 id 不等于"某条 id 为空"的 trace —— 必须与"查不到"区分开，否则日志里查不出是哪种。"""
    body = trace_view(InMemoryTraceStore(capacity=10), "  ")
    assert body["available"] is False, body
    assert "trace_id" in body["reason"], body


def test_detail_reports_a_store_failure_instead_of_raising() -> None:
    body = trace_view(RaisingStore(), "aaa")
    assert body["available"] is False, body
    assert "disk on fire" in body["reason"], body


# ── 事件 ──


def test_events_are_returned_for_a_known_trace() -> None:
    store = InMemoryTraceStore(capacity=10)
    store.save(_session("aaa", "task", steps=1))
    body = events_view(store, "aaa")
    assert body["count"] == 1, body
    assert body["events"][0]["event_type"] == "thought", body
    assert body["reason"] == "", body


def test_events_view_distinguishes_missing_from_empty() -> None:
    """`count: 0` 有两种含义：这条 trace 没有事件，和这条 trace 根本不存在。"""
    store = InMemoryTraceStore(capacity=10)
    empty = _session("empty", "task")
    empty.events.clear()
    store.save(empty)
    known = events_view(store, "empty")
    assert known["count"] == 0 and known["reason"] == "", known
    missing = events_view(store, "nope")
    assert missing["count"] == 0 and "nope" in missing["reason"], missing


def test_events_report_a_store_failure_instead_of_raising() -> None:
    body = events_view(RaisingStore(), "aaa")
    assert body["count"] == 0, body
    assert "disk on fire" in body["reason"], body


def _run_all() -> None:
    failed = []
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"RUN  {name}")
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                print(f"FAIL {name}: {type(e).__name__}: {e}")
                failed.append(name)
            else:
                print(f"PASS {name}")
    if failed:
        raise SystemExit(f"FAILED: {', '.join(failed)}")
    print("ALL PASS")


if __name__ == "__main__":
    _run_all()
