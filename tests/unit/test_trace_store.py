"""Trace 存储契约（`core/trace/store.py`，纯逻辑）。

真实缺口（Phase 8 开跑前检查）：`Tracer` 只把会话留在内存里的 `_current_session`，**没有历史**，
UI 无从查起；而且 `record_*` 从不累计 `session.steps`。这里钉住存储侧：

  1. `save` 之后 `get` 能拿回**完整**的会话（steps 与 events 都在，且能 JSON 往返）；
  2. 容量满时**淘汰最旧的**（环形），`list()` **最新在前**；
  3. `list()` 只给小结（不带逐条明细）—— 列表接口不该把整份 trace 拖出来；
  4. 坏输入（不存在的 id、垃圾 limit）不抛。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from agent_runtime.core.llm.types import TokenUsage  # noqa: E402
from agent_runtime.core.trace.models import (  # noqa: E402
    TraceEvent,
    TraceSession,
    TraceStep,
)
from agent_runtime.core.trace.store import InMemoryTraceStore  # noqa: E402


def _session(trace_id: str = "t1", *, source: str = "chat") -> TraceSession:
    return TraceSession(
        trace_id=trace_id,
        task=f"任务 {trace_id}",
        config={"max_steps": 15},
        steps=[
            TraceStep(
                step_number=1,
                thought="先读文件",
                tool_call={"tool_name": "read_file", "args": {"path": "README.md"}},
                tool_result={"success": True, "chars": 42},
                token_usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
                latency_ms=120,
            )
        ],
        events=[
            TraceEvent(event_type="thought", step_number=1, data={"content": "先读文件"}),
            TraceEvent(event_type="final_answer", step_number=0, data={"content": "答案"}),
        ],
        final_answer="答案",
        total_tokens=TokenUsage(
            prompt_tokens=10, completion_tokens=5, total_tokens=15
        ),
        total_latency_ms=200,
        status="finished",
        source=source,
    )


# ── 往返 ──


def test_save_then_get_round_trips_the_whole_session() -> None:
    store = InMemoryTraceStore()
    session = _session()
    store.save(session)

    again = store.get("t1")
    assert again is not None
    assert again.trace_id == "t1"
    assert again.source == "chat"
    assert again.status == "finished"
    assert again.final_answer == "答案"
    assert again.config["max_steps"] == 15
    assert again.total_tokens.total_tokens == 15
    assert len(again.steps) == 1
    step = again.steps[0]
    assert step.thought == "先读文件"
    assert step.tool_call["tool_name"] == "read_file"
    assert step.latency_ms == 120
    assert [e.event_type for e in again.events] == ["thought", "final_answer"]


def test_get_missing_trace_returns_none() -> None:
    assert InMemoryTraceStore().get("nope") is None


def test_sessions_and_events_survive_json() -> None:
    """落盘/出 API 都要经过 JSON：`to_dict`/`from_dict` 必须无损。"""
    session = _session()
    payload = json.dumps(session.to_dict(), ensure_ascii=False)
    again = TraceSession.from_dict(json.loads(payload))
    assert again.trace_id == session.trace_id
    assert again.started_at == session.started_at
    assert again.finished_at == session.finished_at
    assert again.total_tokens.total_tokens == 15
    assert again.steps[0].tool_call == session.steps[0].tool_call
    assert again.events[0].data == {"content": "先读文件"}
    assert again.source == "chat"


def test_events_are_returned_for_a_trace() -> None:
    store = InMemoryTraceStore()
    store.save(_session("t1"))
    events = store.events("t1")
    assert [e.event_type for e in events] == ["thought", "final_answer"]
    assert store.events("missing") == []


# ── 列表与容量 ──


def test_list_is_newest_first_and_only_summaries() -> None:
    store = InMemoryTraceStore()
    store.save(_session("old"))
    store.save(_session("new", source="benchmark"))

    summaries = store.list()
    assert [s["trace_id"] for s in summaries] == ["new", "old"], "最新在前"
    newest = summaries[0]
    assert newest["source"] == "benchmark"
    assert newest["steps"] == 1, "小结里带步数（列表要显示）"
    assert newest["total_tokens"] == 15
    assert "events" not in newest and "steps_detail" not in newest, "小结不带明细"


def test_list_honours_the_limit() -> None:
    store = InMemoryTraceStore()
    for index in range(5):
        store.save(_session(f"t{index}"))
    assert [s["trace_id"] for s in store.list(limit=2)] == ["t4", "t3"]
    assert store.list(limit=0) == []


def test_capacity_evicts_the_oldest_session() -> None:
    store = InMemoryTraceStore(capacity=2)
    store.save(_session("t1"))
    store.save(_session("t2"))
    store.save(_session("t3"))

    assert store.get("t1") is None, "最旧的被淘汰（环形）"
    assert store.get("t2") is not None and store.get("t3") is not None
    assert [s["trace_id"] for s in store.list()] == ["t3", "t2"]


def test_saving_the_same_trace_id_twice_overwrites_it() -> None:
    """同一个 trace 结束时再存一次（例如补写 final_answer）不该变成两条。"""
    store = InMemoryTraceStore()
    store.save(_session("t1"))
    updated = _session("t1")
    updated.final_answer = "改过的答案"
    store.save(updated)

    assert len(store.list()) == 1
    assert store.get("t1").final_answer == "改过的答案"  # type: ignore[union-attr]


def test_a_bad_limit_or_capacity_does_not_raise() -> None:
    store = InMemoryTraceStore(capacity=0)  # 容量 0 = 不保留（不抛）
    store.save(_session("t1"))
    assert store.list() == []
    assert InMemoryTraceStore().list(limit=-5) == []


def _run_all() -> None:
    failed: list[str] = []
    tests = [
        v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)
    ]
    for t in tests:
        print(f"RUN  {t.__name__}")
        try:
            t()
        except Exception as e:  # noqa: BLE001
            print(f"FAIL {t.__name__}: {type(e).__name__}: {e}")
            failed.append(t.__name__)
        else:
            print(f"PASS {t.__name__}")
    if failed:
        raise SystemExit(f"FAILED: {', '.join(failed)}")
    print("ALL PASS")


if __name__ == "__main__":
    _run_all()
