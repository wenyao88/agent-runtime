"""`ContextManager.snapshot()` 契约（Phase 8：让 Inspector 的"上下文"一块有数据）。

要回答的问题很具体：**"现在这轮任务，token 花在哪了？离压缩阈值还有多远？上一次压缩干了什么？"**
所以快照必须给出分段分布（system / 记忆注入 / 当前任务 / 其余消息）、预算与阈值、以及最近一次压缩结果。

口径：
  * 记忆是**拼进 system 文本**的，所以要在 `build()` 时把它单独记下来，否则"记忆占了 120 token"这种话说不出来；
  * `None` ≠ `0`：没有消息时各段是 0（真值），而 `last_compaction` 是 `None`（**没发生过**）；
  * 快照**只读**：不许改动消息、不许触发压缩。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from agent_runtime.core.context.budget import TokenBudget  # noqa: E402
from agent_runtime.core.context.compaction import CompactionStrategy  # noqa: E402
from agent_runtime.core.context.manager import ContextManager  # noqa: E402
from agent_runtime.core.llm.types import Message  # noqa: E402


def _budget() -> TokenBudget:
    # 小预算便于断言阈值：available = (10000-4096)*0.9 = 5313.6 → 5313；threshold = 4250
    return TokenBudget(model_max_tokens=10000, reserved_output=4096, compaction_ratio=0.8)


def test_snapshot_reports_budget_and_ratio() -> None:
    async def run():
        cm = ContextManager(budget=_budget(), keep_recent=1)
        await cm.build(task="做事", system_prompt="SYSTEM")
        return cm.snapshot()

    snap = asyncio.run(run())
    assert snap["budget"]["available"] == 5313
    assert snap["budget"]["threshold"] == int(5313 * 0.8)
    assert snap["used_tokens"] > 0
    assert 0 < snap["ratio"] < 1
    assert snap["messages"] == 2, "system + 当前任务"


def test_snapshot_splits_system_memory_task_and_messages() -> None:
    class _Entry:
        def __init__(self, content: str) -> None:
            self.content = content
            self.role = "agent"
            self.source = "long_term"
            self.created_at = None
            self.metadata: dict = {}

    async def run():
        cm = ContextManager(budget=_budget(), keep_recent=2)
        await cm.build(
            task="调研 pgvector",
            system_prompt="SYSTEM",
            memory_entries=[_Entry("上次结论：pgvector 够用"), _Entry("另一条记忆")],
        )
        cm.append(Message(role="assistant", content="中间"))
        cm.append(Message(role="tool", content="工具结果", tool_call_id="c1"))
        cm.append(Message(role="assistant", content="最近"))
        return cm.snapshot()

    snap = asyncio.run(run())
    names = [section["name"] for section in snap["sections"]]
    assert names == ["system", "memory", "task", "messages"], names
    by_name = {section["name"]: section for section in snap["sections"]}
    assert by_name["memory"]["tokens"] > 0, "记忆注入必须单独成段"
    assert by_name["task"]["chars"] == len("调研 pgvector")
    assert by_name["messages"]["chars"] == len("中间") + len("工具结果") + len("最近")
    assert sum(section["tokens"] for section in snap["sections"]) == snap["used_tokens"]


def test_snapshot_without_memory_reports_a_zero_memory_block() -> None:
    async def run():
        cm = ContextManager(budget=_budget())
        await cm.build(task="做事", system_prompt="SYSTEM")
        return cm.snapshot()

    snap = asyncio.run(run())
    memory = next(s for s in snap["sections"] if s["name"] == "memory")
    assert memory["tokens"] == 0 and memory["chars"] == 0
    assert snap["last_compaction"] is None, "没压过就是 None（不是假装压过）"


def test_snapshot_carries_the_last_compaction() -> None:
    async def run():
        cm = ContextManager(budget=_budget(), keep_recent=1)
        await cm.build(task="做事", system_prompt="SYSTEM")
        cm.append(Message(role="assistant", content="旧1"))
        cm.append(Message(role="assistant", content="旧2"))
        cm.append(Message(role="assistant", content="最近"))
        await cm.compact(strategy=CompactionStrategy.TRUNCATE)
        return cm.snapshot()

    snap = asyncio.run(run())
    last = snap["last_compaction"]
    assert last is not None
    assert last["strategy"] == "truncate"
    assert last["before"] >= last["after"]
    assert last["noop"] is False, "真丢了消息就不是 noop"


def test_snapshot_is_read_only() -> None:
    async def run():
        cm = ContextManager(budget=_budget(), keep_recent=1)
        await cm.build(task="做事", system_prompt="SYSTEM")
        cm.append(Message(role="assistant", content="旧"))
        before = [(m.role, m.content) for m in cm.get_messages()]
        snap = cm.snapshot()
        after = [(m.role, m.content) for m in cm.get_messages()]
        return before, after, snap

    before, after, snap = asyncio.run(run())
    assert before == after, "快照绝不能改上下文"
    assert snap["used_tokens"] > 0


def test_a_rebuilt_context_forgets_the_previous_compaction() -> None:
    """`build()` 开始新一轮任务：上一次的压缩结果不能继续挂着（否则 Inspector 会误导）。"""

    async def run():
        cm = ContextManager(budget=_budget(), keep_recent=1)
        await cm.build(task="第一轮", system_prompt="SYSTEM")
        cm.append(Message(role="assistant", content="旧"))
        cm.append(Message(role="assistant", content="最近"))
        await cm.compact(strategy=CompactionStrategy.TRUNCATE)
        await cm.build(task="第二轮", system_prompt="SYSTEM")
        return cm.snapshot()

    snap = asyncio.run(run())
    assert snap["last_compaction"] is None
    assert snap["used_tokens"] > 0


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
