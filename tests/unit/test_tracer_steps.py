"""Tracer 契约（`core/trace/tracer.py`，Phase 8 补齐）。

开跑前检查发现的缺口：`record_*` 只往事件队列扔事件，**从不累计 `session.steps`** ——
`TraceSession.steps` 永远是空的，Trace 页的"步骤树"等于没数据。这里钉住：

  1. 同一个 `step_number` 的 thought / tool_call / tool_result **合并进同一条 `TraceStep`**
     （一轮里可能一次发多个工具调用，所以 tool_call/tool_result 是**列表式累加**在同一 step 上）；
  2. `source` 标记跟着 session 走（chat / benchmark），UI 要能按来源过滤；
  3. `end_session` 把会话写进 store（可选）；**没有 store 时旧行为完全不变**（既有调用方零改动）；
  4. `record_*` 仍然推事件（WS 的实时流不能因为这次改动断掉）。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from agent_runtime.core.agent.base import AgentResult  # noqa: E402
from agent_runtime.core.llm.types import TokenUsage  # noqa: E402
from agent_runtime.core.tool.base import ToolResult  # noqa: E402
from agent_runtime.core.trace.store import InMemoryTraceStore  # noqa: E402
from agent_runtime.core.trace.tracer import Tracer  # noqa: E402


def _result(answer: str = "答案", *, tokens: int = 42, latency: int = 300) -> AgentResult:
    return AgentResult(
        task="t",
        final_answer=answer,
        total_tokens=TokenUsage(prompt_tokens=20, completion_tokens=22, total_tokens=tokens),
        total_latency_ms=latency,
    )


# ── steps 累计（本次修复的核心） ──


def test_records_are_merged_into_one_step() -> None:
    async def run():
        tracer = Tracer()
        session = await tracer.start_session("读文件")
        tracer.record_thought(1, "先看看 README")
        tracer.record_tool_call(1, "read_file", {"path": "README.md"})
        tracer.record_tool_result(
            1, ToolResult(tool_name="read_file", success=True, text="内容", latency_ms=88)
        )
        return session

    session = asyncio.run(run())
    assert len(session.steps) == 1, "同一个 step_number 只产生一条 TraceStep"
    step = session.steps[0]
    assert step.step_number == 1
    assert step.thought == "先看看 README"
    assert step.tool_call == {"tool_name": "read_file", "args": {"path": "README.md"}}
    assert step.tool_result is not None
    assert step.tool_result["success"] is True
    assert step.tool_result["chars"] == len("内容")
    assert step.latency_ms == 88


def test_multiple_tool_calls_in_one_round_are_all_kept() -> None:
    """一轮里发多个工具调用是真实全量的常态（`steps` 比 `rounds` 大一倍就是这个原因）。"""

    async def run():
        tracer = Tracer()
        session = await tracer.start_session("读两个文件")
        tracer.record_thought(1, "一次读两个")
        tracer.record_tool_call(1, "read_file", {"path": "a.txt"})
        tracer.record_tool_result(1, ToolResult(tool_name="read_file", success=True, text="A"))
        tracer.record_tool_call(1, "read_file", {"path": "b.txt"})
        tracer.record_tool_result(1, ToolResult(tool_name="read_file", success=False, text="boom"))
        return session

    session = asyncio.run(run())
    assert len(session.steps) == 1
    calls = session.steps[0].tool_call
    results = session.steps[0].tool_result
    assert isinstance(calls, list) and len(calls) == 2, calls
    assert isinstance(results, list) and len(results) == 2, results
    assert results[1]["success"] is False
    assert "boom" in results[1]["text"]


def test_steps_are_ordered_by_step_number() -> None:
    async def run():
        tracer = Tracer()
        session = await tracer.start_session("两步")
        tracer.record_thought(2, "第二步")
        tracer.record_thought(1, "第一步")
        return session

    session = asyncio.run(run())
    assert [s.step_number for s in session.steps] == [1, 2], "即使乱序调用也要按步号排好"


def test_the_event_log_is_still_recorded() -> None:
    """WS 的实时流靠事件队列；改动不能把它弄丢。"""

    async def run():
        tracer = Tracer()
        session = await tracer.start_session("t")
        tracer.record_thought(1, "想")
        tracer.record_tool_call(1, "read_file", {})
        tracer.record_tool_result(1, ToolResult(tool_name="read_file", success=True, text="x"))
        tracer.record_compaction(1000, 400, "squeeze")
        tracer.record_final_answer("答案")
        tracer.record_error(1, "ValueError", "坏了")
        return session

    session = asyncio.run(run())
    kinds = [e.event_type for e in session.events]
    assert kinds == [
        "thought",
        "tool_call",
        "tool_result",
        "compaction",
        "final_answer",
        "error",
    ], kinds
    assert session.final_answer == "答案"
    assert session.events[3].data["strategy"] == "squeeze"


def test_source_is_carried_by_the_session() -> None:
    async def run():
        tracer = Tracer(source="benchmark")
        return await tracer.start_session("评测任务")

    session = asyncio.run(run())
    assert session.source == "benchmark"
    assert Tracer()._source == "chat", "默认来源是 chat"


# ── 落库 ──


def test_end_session_saves_into_the_store() -> None:
    async def run():
        store = InMemoryTraceStore()
        tracer = Tracer(store=store, source="benchmark")
        await tracer.start_session("评测任务 tr-001")
        tracer.record_thought(1, "想")
        result = await tracer.end_session(_result())
        return store, result

    store, result = asyncio.run(run())
    assert result.status == "finished"
    assert result.finished_at is not None
    assert result.total_tokens.total_tokens == 42
    assert result.total_latency_ms == 300

    saved = store.get(result.trace_id)
    assert saved is not None, "end_session 必须把会话写进 store"
    assert saved.source == "benchmark"
    assert len(saved.steps) == 1
    assert [s["trace_id"] for s in store.list()] == [result.trace_id]


def test_end_without_a_store_keeps_the_old_behaviour() -> None:
    """既有调用方（demo / smoke / `get_tracer()`）不传 store，行为必须一模一样。"""

    async def run():
        tracer = Tracer()
        await tracer.start_session("t")
        return await tracer.end_session(_result("老答案"))

    session = asyncio.run(run())
    assert session.final_answer == "老答案"
    assert session.status == "finished"


def test_a_broken_store_does_not_break_the_run() -> None:
    """存储是可选能力：写不进去也不能让任务失败（错误要有出口，但不阻断）。"""

    class _BrokenStore:
        def save(self, session):  # noqa: ANN001
            raise OSError("磁盘满了")

    async def run():
        tracer = Tracer(store=_BrokenStore())
        await tracer.start_session("t")
        return await tracer.end_session(_result())

    session = asyncio.run(run())
    assert session.status == "finished", "落库失败不影响会话本身"
    tracer = Tracer(store=_BrokenStore())
    asyncio.run(tracer.start_session("t2"))
    asyncio.run(tracer.end_session(_result()))
    assert tracer.last_store_error and "磁盘满了" in tracer.last_store_error


def test_stream_events_yields_recorded_events() -> None:
    async def run():
        tracer = Tracer()
        await tracer.start_session("t")
        tracer.record_thought(1, "想")
        stream = tracer.stream_events()
        return await asyncio.wait_for(stream.__anext__(), timeout=1.0)

    event = asyncio.run(run())
    assert event.event_type == "thought"
    assert event.data["content"] == "想"


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
