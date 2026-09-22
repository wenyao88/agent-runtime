"""`MemoryManager` 与记忆注入的契约（Phase 4）。

本文件钉死三件事：
  1. **级联与短路**语义不变：working → short_term → long_term，够数即停；
  2. **任一层抛异常都不外抛**（`react.py` 结尾的 `await memory.store(...)` 没有任何 try 保护，
     接上 PG/Redis 后一次写库失败会把整轮任务炸掉 —— 这是本任务顺带修掉的真实脆弱点）；
  3. 注入 System Prompt 的记忆行带**来源与时间**标注、超长截断**带标记**。

沙箱适配：全部用纯标准库假层，不碰 Redis/PG。
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from agent_runtime.core.context.budget import TokenBudget
from agent_runtime.core.context.manager import ContextManager
from agent_runtime.core.memory.annotate import SOURCE_LONG, SOURCE_SHORT, SOURCE_WORKING
from agent_runtime.core.memory.base import (
    BaseMemory,
    MemoryEntry,
    MemoryQuery,
    normalize_session_id,
)
from agent_runtime.core.memory.manager import MemoryManager
from agent_runtime.core.memory.working import WorkingMemory


class FakeLayer(BaseMemory):
    """可控假层：可预设返回条目、可强制抛异常、可数调用次数。"""

    def __init__(self, entries: list[MemoryEntry] | None = None, *, raises: Exception | None = None):
        self.entries = list(entries or [])
        self.raises = raises
        self.stored: list[MemoryEntry] = []
        self.query_calls = 0
        self.store_calls = 0

    async def store(self, entry: MemoryEntry) -> str:
        self.store_calls += 1
        if self.raises:
            raise self.raises
        self.stored.append(entry)
        return "fake-id"

    async def query(self, query: MemoryQuery) -> list[MemoryEntry]:
        self.query_calls += 1
        if self.raises:
            raise self.raises
        return list(self.entries)

    async def clear(self) -> None:
        self.entries.clear()
        self.stored.clear()


def _entry(content: str, when: datetime | None = None) -> MemoryEntry:
    return MemoryEntry(content=content, created_at=when)


def _run(coro):
    return asyncio.run(coro)


# ── session_id 归一 ──


def test_normalize_session_id() -> None:
    assert normalize_session_id(None) == "default"
    assert normalize_session_id("") == "default"
    assert normalize_session_id("   ") == "default"
    assert normalize_session_id(" s1 ") == "s1"


# ── store ──


def test_store_writes_all_configured_layers() -> None:
    working, short, long_ = WorkingMemory(), FakeLayer(), FakeLayer()
    mgr = MemoryManager(working=working, short_term=short, long_term=long_)
    _run(mgr.store(MemoryEntry(content="x")))
    assert len(working._entries) == 1
    assert short.store_calls == 1 and long_.store_calls == 1


def test_store_survives_a_failing_persistent_layer() -> None:
    """一次写库失败不得把整轮任务炸掉（react.py 调用点没有 try 保护）。"""
    short = FakeLayer(raises=RuntimeError("redis down"))
    long_ = FakeLayer()
    mgr = MemoryManager(working=WorkingMemory(), short_term=short, long_term=long_)
    result = _run(mgr.store(MemoryEntry(content="x")))  # 不得抛
    assert isinstance(result, str)
    assert long_.store_calls == 1, "一层失败不应阻止另一层写入"
    assert any("short_term" in e and "redis down" in e for e in mgr.errors), mgr.errors


# ── recall ──


def test_recall_short_circuits_when_working_is_enough() -> None:
    working = WorkingMemory()
    _run(working.store(MemoryEntry(content="命中内容")))
    short, long_ = FakeLayer(), FakeLayer()
    mgr = MemoryManager(working=working, short_term=short, long_term=long_)
    out = _run(mgr.recall(MemoryQuery(text="命中", top_k=1)))
    assert len(out) == 1
    assert short.query_calls == 0 and long_.query_calls == 0, "够数即短路，不该再查持久层"


def test_recall_cascades_when_working_is_insufficient() -> None:
    short = FakeLayer([_entry("短期记忆")])
    long_ = FakeLayer([_entry("长期记忆")])
    mgr = MemoryManager(working=WorkingMemory(), short_term=short, long_term=long_)
    out = _run(mgr.recall(MemoryQuery(text="记忆", top_k=5)))
    assert {e.content for e in out} == {"短期记忆", "长期记忆"}
    assert short.query_calls == 1 and long_.query_calls == 1


def test_recall_annotates_sources() -> None:
    short = FakeLayer([_entry("短期")])
    mgr = MemoryManager(working=WorkingMemory(), short_term=short)
    out = _run(mgr.recall(MemoryQuery(text="短期", top_k=5)))
    assert [e.source for e in out] == [SOURCE_SHORT]


def test_recall_puts_working_first() -> None:
    working = WorkingMemory()
    _run(working.store(MemoryEntry(content="工作记忆")))
    long_ = FakeLayer([_entry("长期记忆", when=datetime(2026, 9, 21))])
    mgr = MemoryManager(working=working, long_term=long_)
    out = _run(mgr.recall(MemoryQuery(text="记忆", top_k=5)))
    assert [e.source for e in out] == [SOURCE_WORKING, SOURCE_LONG]


def test_recall_dedupes_across_layers() -> None:
    short = FakeLayer([_entry("同一段内容")])
    long_ = FakeLayer([_entry("同一段内容")])
    mgr = MemoryManager(working=WorkingMemory(), short_term=short, long_term=long_)
    out = _run(mgr.recall(MemoryQuery(text="内容", top_k=5)))
    assert len(out) == 1, "跨层重复内容只注入一次"


def test_recall_respects_top_k() -> None:
    long_ = FakeLayer([_entry(f"m{i}") for i in range(5)])
    mgr = MemoryManager(working=WorkingMemory(), long_term=long_)
    assert len(_run(mgr.recall(MemoryQuery(text="m", top_k=2)))) == 2


def test_recall_survives_a_failing_layer() -> None:
    short = FakeLayer(raises=RuntimeError("redis down"))
    long_ = FakeLayer([_entry("长期记忆")])
    mgr = MemoryManager(working=WorkingMemory(), short_term=short, long_term=long_)
    out = _run(mgr.recall(MemoryQuery(text="记忆", top_k=5)))  # 不得抛
    assert [e.content for e in out] == ["长期记忆"], "一层坏掉不应影响其它层召回"
    assert any("short_term" in e and "查询失败" in e for e in mgr.errors), mgr.errors


def test_errors_are_bounded() -> None:
    """manager 是进程单例：错误列表不能随任务数无限增长。"""
    bad = FakeLayer(raises=RuntimeError("boom"))
    mgr = MemoryManager(working=WorkingMemory(), long_term=bad)
    for _ in range(80):
        _run(mgr.recall(MemoryQuery(text="x", top_k=1)))
    assert len(mgr.errors) <= 50, len(mgr.errors)


# ── consolidate ──


def test_consolidate_uses_summarizer() -> None:
    async def summarizer(text: str) -> str:
        return f"摘要({text})"

    long_ = FakeLayer()
    mgr = MemoryManager(working=WorkingMemory(), long_term=long_, summarizer=summarizer)
    _run(mgr.consolidate("原始任务总结", session_id="s1"))
    assert long_.stored[0].content == "摘要(原始任务总结)"
    assert long_.stored[0].metadata["session_id"] == "s1"
    assert long_.stored[0].metadata["type"] == "summary"


def test_consolidate_falls_back_to_raw_when_summarizer_fails() -> None:
    async def summarizer(text: str) -> str:
        raise RuntimeError("judge llm down")

    long_ = FakeLayer()
    mgr = MemoryManager(working=WorkingMemory(), long_term=long_, summarizer=summarizer)
    _run(mgr.consolidate("原始任务总结"))  # 不得抛
    assert long_.stored[0].content == "原始任务总结", "摘要失败要降级为原文，而不是丢记忆"
    assert any("摘要失败" in e for e in mgr.errors), mgr.errors


def test_consolidate_without_summarizer_stores_raw() -> None:
    long_ = FakeLayer()
    mgr = MemoryManager(working=WorkingMemory(), long_term=long_)
    _run(mgr.consolidate("原文"))
    assert long_.stored[0].content == "原文"


def test_consolidate_marks_truncation_of_overlong_raw() -> None:
    long_ = FakeLayer()
    mgr = MemoryManager(working=WorkingMemory(), long_term=long_)
    _run(mgr.consolidate("字" * 5000))
    stored = long_.stored[0].content
    assert len(stored) < 5000
    assert "截断" in stored, "截断必须带标记"


def test_consolidate_empty_text_is_noop() -> None:
    long_ = FakeLayer()
    mgr = MemoryManager(working=WorkingMemory(), long_term=long_)
    _run(mgr.consolidate("   "))
    assert long_.store_calls == 0


# ── 注入渲染（ContextManager）──


def _system_prompt(cm: ContextManager) -> str:
    return cm.get_messages()[0].content or ""


def test_build_renders_memory_with_source_annotation() -> None:
    cm = ContextManager(budget=TokenBudget())
    entry = MemoryEntry(content="历史结论", source=SOURCE_LONG, created_at=datetime(2026, 9, 21))
    _run(cm.build(task="任务", memory_entries=[entry], system_prompt="SYS"))
    prompt = _system_prompt(cm)
    assert "Relevant Memories:" in prompt
    assert "- [long_term · 2026-09-21] 历史结论" in prompt


def test_build_memory_truncation_is_marked() -> None:
    cm = ContextManager(budget=TokenBudget(), memory_max_chars=10)
    entry = MemoryEntry(content="z" * 100, source=SOURCE_WORKING)
    _run(cm.build(task="任务", memory_entries=[entry], system_prompt="SYS"))
    assert "…" in _system_prompt(cm), "截断必须带标记"


def test_build_respects_configured_memory_max_chars() -> None:
    short_cm = ContextManager(budget=TokenBudget(), memory_max_chars=5)
    long_cm = ContextManager(budget=TokenBudget(), memory_max_chars=500)
    entry = MemoryEntry(content="y" * 50)
    _run(short_cm.build(task="t", memory_entries=[entry], system_prompt="SYS"))
    _run(long_cm.build(task="t", memory_entries=[entry], system_prompt="SYS"))
    assert len(_system_prompt(short_cm)) < len(_system_prompt(long_cm))


def test_build_without_memories_has_no_memory_section() -> None:
    cm = ContextManager(budget=TokenBudget())
    _run(cm.build(task="任务", system_prompt="SYS"))
    assert "Relevant Memories" not in _system_prompt(cm)


def test_store_summarizes_persistent_layers_but_keeps_working_raw() -> None:
    """接上 summarizer 后（D5），持久层存摘要、**工作层留原文** —— 同会话召回需要原始细节。

    这条也是"摘要器不接进主链路就是死配置"的回归：ReActLoop 结束走的是 store。
    """

    async def summarizer(text: str) -> str:
        return f"摘要({text})"

    working = WorkingMemory()
    long_ = FakeLayer()
    mgr = MemoryManager(working=working, long_term=long_, summarizer=summarizer)
    _run(mgr.store(MemoryEntry(content="原始长文本", metadata={"type": "task_summary"})))
    assert long_.stored[0].content == "摘要(原始长文本)"
    assert working._entries[0].content == "原始长文本", "工作层必须保留原文"


def test_store_without_summarizer_persists_raw_text() -> None:
    long_ = FakeLayer()
    mgr = MemoryManager(working=WorkingMemory(), long_term=long_)
    _run(mgr.store(MemoryEntry(content="原文")))
    assert long_.stored[0].content == "原文"


def _run_all() -> None:
    failed = []
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"RUN  {name}")
            try:
                fn()
            except Exception as e:  # noqa: BLE001 —— 双模式跑法：单条失败不中断其余
                print(f"FAIL {name}: {type(e).__name__}: {e}")
                failed.append(name)
            else:
                print(f"PASS {name}")
    if failed:
        raise SystemExit(f"FAILED: {', '.join(failed)}")
    print("ALL PASS")


if __name__ == "__main__":
    _run_all()
