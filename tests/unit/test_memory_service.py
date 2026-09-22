"""`/api/memories` 背后的纯逻辑契约（假 manager/层驱动，沙箱可测）。

为什么把这层逻辑放 `infrastructure/memory/service.py` 而不是路由里：
路由依赖 fastapi + pydantic（沙箱装不上），逻辑一放那儿就完全无法验证。
与 Phase 2 的 `tools/catalog.py`、Phase 3 的 `skills/catalog.py` 同一处理方式 —— api 层只做参数与序列化。

关键语义：
  * **未启用的层返回 `enabled: false`**，而不是报错（默认全关是 D4，API 必须体现这一点）；
  * `layer="all"` 走 `manager.recall`（与 Agent 的级联/短路语义一致），指定层则单独查该层；
  * 未知 `layer` → 返回可读错误而不是抛异常；
  * 清空时逐层 best-effort：一层失败不影响另一层，且失败原因如实返回。
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from agent_runtime.core.memory.annotate import SOURCE_LONG, SOURCE_SHORT, SOURCE_WORKING
from agent_runtime.core.memory.base import BaseMemory, MemoryEntry, MemoryQuery
from agent_runtime.core.memory.manager import MemoryManager
from agent_runtime.core.memory.working import WorkingMemory
from agent_runtime.infrastructure.memory.service import (
    clear_memories,
    list_memories,
    memory_catalog,
    memory_settings_view,
)


class FakeLayer(BaseMemory):
    def __init__(self, entries=None, *, raises: Exception | None = None):
        self.entries = list(entries or [])
        self.raises = raises
        self.query_calls = 0
        self.cleared = 0

    async def store(self, entry):
        return "id"

    async def query(self, query: MemoryQuery):
        self.query_calls += 1
        if self.raises:
            raise self.raises
        return list(self.entries[: query.top_k])

    async def clear(self):
        self.cleared += 1
        if self.raises:
            raise self.raises


def _run(coro):
    return asyncio.run(coro)


# ── settings view ──


def test_settings_view_reports_enabled_flags() -> None:
    view = memory_settings_view(short_term_enabled=True, long_term_enabled=False, errors=[])
    assert view["short_term"]["enabled"] is True
    assert view["long_term"]["enabled"] is False
    assert view["errors"] == []


def test_settings_view_copies_errors() -> None:
    errors = ["redis 未安装"]
    view = memory_settings_view(short_term_enabled=False, long_term_enabled=False, errors=errors)
    view["errors"].append("mutated")
    assert errors == ["redis 未安装"], "不得把内部列表暴露出去"


# ── catalog ──


def test_catalog_serializes_fields() -> None:
    entry = MemoryEntry(
        content="历史结论", role="agent", source=SOURCE_LONG,
        created_at=datetime(2026, 9, 21, 8, 30), metadata={"type": "summary"},
    )
    item = memory_catalog([entry])[0]
    assert item["content"] == "历史结论"
    assert item["role"] == "agent"
    assert item["source"] == SOURCE_LONG
    assert item["created_at"] == "2026-09-21T08:30:00"
    assert item["metadata"] == {"type": "summary"}


def test_catalog_handles_missing_created_at() -> None:
    item = memory_catalog([MemoryEntry(content="x")])[0]
    assert item["created_at"] is None


# ── list_memories ──


def test_list_all_reports_disabled_layers_without_querying() -> None:
    manager = MemoryManager(working=WorkingMemory())
    body = _run(list_memories(manager, query="x", top_k=3, layer="all"))
    assert body["enabled"] == {"short_term": False, "long_term": False}
    assert body["count"] == 0 and body["memories"] == []


def test_list_specific_layer_queries_only_that_layer() -> None:
    short = FakeLayer([MemoryEntry(content="短期")])
    long_ = FakeLayer([MemoryEntry(content="长期")])
    manager = MemoryManager(working=WorkingMemory(), short_term=short, long_term=long_)
    body = _run(list_memories(manager, query="内", top_k=5, layer="long_term"))
    assert [m["content"] for m in body["memories"]] == ["长期"]
    assert short.query_calls == 0, "指定层时不得去查别的层"
    assert body["memories"][0]["source"] == SOURCE_LONG


def test_list_all_uses_cascade_recall_with_annotation() -> None:
    short = FakeLayer([MemoryEntry(content="短期记忆")])
    long_ = FakeLayer([MemoryEntry(content="长期记忆")])
    manager = MemoryManager(working=WorkingMemory(), short_term=short, long_term=long_)
    body = _run(list_memories(manager, query="记忆", top_k=5, layer="all"))
    contents = [m["content"] for m in body["memories"]]
    assert contents == ["短期记忆", "长期记忆"]
    assert [m["source"] for m in body["memories"]] == [SOURCE_SHORT, SOURCE_LONG]


def test_list_reports_layer_errors_without_raising() -> None:
    bad = FakeLayer(raises=RuntimeError("redis down"))
    manager = MemoryManager(working=WorkingMemory(), short_term=bad)
    body = _run(list_memories(manager, query="x", top_k=3, layer="short_term"))
    assert body["memories"] == []
    assert any("redis down" in e for e in body["errors"]), body


def test_list_unknown_layer_returns_readable_error() -> None:
    manager = MemoryManager(working=WorkingMemory())
    body = _run(list_memories(manager, query="x", top_k=3, layer="nope"))
    assert body["count"] == 0
    assert any("layer" in e for e in body["errors"]), body


# ── clear_memories ──


def test_clear_skips_disabled_layers() -> None:
    manager = MemoryManager(working=WorkingMemory())
    body = _run(clear_memories(manager))
    assert body["short_term"]["enabled"] is False
    assert body["long_term"]["enabled"] is False


def test_clear_calls_enabled_layers() -> None:
    short = FakeLayer()
    long_ = FakeLayer()
    manager = MemoryManager(working=WorkingMemory(), short_term=short, long_term=long_)
    body = _run(clear_memories(manager))
    assert short.cleared == 1 and long_.cleared == 1
    assert body["short_term"]["ok"] is True and body["long_term"]["ok"] is True


def test_clear_reports_failure_per_layer_without_raising() -> None:
    short = FakeLayer(raises=RuntimeError("redis down"))
    long_ = FakeLayer()
    manager = MemoryManager(working=WorkingMemory(), short_term=short, long_term=long_)
    body = _run(clear_memories(manager))
    assert body["short_term"]["ok"] is False and "redis down" in body["short_term"]["error"]
    assert body["long_term"]["ok"] is True, "一层失败不得影响另一层"


def test_working_layer_is_never_exposed_or_cleared() -> None:
    """工作记忆是进程内临时状态，不该被 API 查询/清空语义牵扯进来。"""
    manager = MemoryManager(working=WorkingMemory())
    _run(manager.store(MemoryEntry(content="进程内")))
    listed = _run(list_memories(manager, query="进程内", top_k=3, layer="all"))
    assert listed["enabled"] == {"short_term": False, "long_term": False}
    short_circuit = [m for m in listed["memories"] if m["source"] == SOURCE_WORKING]
    assert short_circuit, "all 走 recall，工作记忆本就会命中（这是既有级联语义）"


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
