"""`WorkingMemory` 的会话隔离契约（`core/memory/working.py`）。

背景（Phase 7 开跑前检查发现）：`WorkingMemory.query` 原来**无视 `query.session_id`**，对进程内所有历史
条目做子串匹配 → 消融的 "+Memory" 组要么没有记忆效果，要么靠**偶发命中**，数据不可解释。

规则（必须精确，否则会打破既有调用方）：

* 查询与条目**两侧都有会话且不同** → 跳过该条目；
* 任一侧没有会话 → 保持旧行为（不懂会话的调用方不受影响）；
* 同会话 → 正常召回。

另外：`get/set` 那套键值用法与 `clear()` 不能被这次改动破坏。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from agent_runtime.core.memory.base import MemoryEntry, MemoryQuery  # noqa: E402
from agent_runtime.core.memory.working import WorkingMemory  # noqa: E402


def _entry(content: str, session: str | None = None) -> MemoryEntry:
    metadata = {"session_id": session} if session is not None else {}
    return MemoryEntry(content=content, metadata=metadata)


async def _store_all(memory: WorkingMemory, entries: list[MemoryEntry]) -> list[str]:
    return [await memory.store(entry) for entry in entries]


# ── 会话隔离 ──


def test_same_session_entries_are_recalled() -> None:
    async def run():
        memory = WorkingMemory()
        await _store_all(memory, [_entry("分析 pallets/flask", session="s1")])
        return await memory.query(MemoryQuery(text="分析", session_id="s1"))

    found = asyncio.run(run())
    assert [e.content for e in found] == ["分析 pallets/flask"]


def test_another_session_is_not_recalled() -> None:
    """这就是 Phase 7 要修的那条：上一条任务的记忆不该被下一条召回。"""

    async def run():
        memory = WorkingMemory()
        await _store_all(memory, [_entry("分析 pallets/flask", session="task-a")])
        return await memory.query(MemoryQuery(text="分析", session_id="task-b"))

    assert asyncio.run(run()) == []


def test_entry_without_a_session_stays_visible() -> None:
    """任一侧没有会话 → 旧行为（早期调用方/ad-hoc 用法不受影响）。"""

    async def run():
        memory = WorkingMemory()
        await _store_all(memory, [_entry("没有会话的条目")])
        return await memory.query(MemoryQuery(text="条目", session_id="s1"))

    assert [e.content for e in asyncio.run(run())] == ["没有会话的条目"]


def test_query_without_a_session_sees_every_session() -> None:
    async def run():
        memory = WorkingMemory()
        await _store_all(
            memory,
            [_entry("共同关键词 A", session="s1"), _entry("共同关键词 B", session="s2")],
        )
        return await memory.query(MemoryQuery(text="共同关键词"))

    contents = sorted(e.content for e in asyncio.run(run()))
    assert contents == ["共同关键词 A", "共同关键词 B"]


def test_a_shared_run_session_sees_earlier_tasks() -> None:
    """消融的 memory 组用运行内共享会话 → 后面的任务能看到前面的记忆（这是要测的效应）。"""

    async def run():
        memory = WorkingMemory()
        await _store_all(memory, [_entry("第一条任务的结论", session="bench-run-1")])
        return await memory.query(MemoryQuery(text="结论", session_id="bench-run-1"))

    assert [e.content for e in asyncio.run(run())] == ["第一条任务的结论"]


# ── 既有语义不能被破坏 ──


def test_query_matches_substring_and_respects_top_k() -> None:
    async def run():
        memory = WorkingMemory()
        await _store_all(memory, [_entry("命中一"), _entry("命中二"), _entry("命中三")])
        return await memory.query(MemoryQuery(text="命中", top_k=2))

    found = asyncio.run(run())
    assert len(found) == 2, "top_k 必须生效"
    assert [e.content for e in found] == ["命中三", "命中二"], "最新的先返回"


def test_store_assigns_an_id_and_clear_empties() -> None:
    async def run():
        memory = WorkingMemory()
        ids = await _store_all(memory, [_entry("x"), _entry("y")])
        before = await memory.query(MemoryQuery(text="x"))
        await memory.clear()
        after = await memory.query(MemoryQuery(text="x"))
        return ids, before, after

    ids, before, after = asyncio.run(run())
    assert ids == ["1", "2"]
    assert len(before) == 1
    assert after == []


def test_key_value_usage_still_works() -> None:
    async def run():
        memory = WorkingMemory()
        await memory.store(_entry("值", session="s1"))
        entry = MemoryEntry(content="值", metadata={"key": "k"})
        await memory.store(entry)
        return memory.get("k")

    assert asyncio.run(run()) == "值"


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
