"""上下文压缩契约：SQUEEZE（就地压缩工具结果）/ TRUNCATE（丢弃最旧消息）+ 策略选择。

两条必须守住的不变量：
  1. **当前任务不能丢**（spec 的优先级 1：System Prompt + 当前任务是永不过期的）；
  2. **保留段不能以 role="tool" 开头** —— 孤立 tool 结果违反 OpenAI 的消息协议约束。

另外修一个既有语义 bug：旧 compact() 把 `messages_dropped` 填成了 token 差，
而该字段会进 Trace/Benchmark 报告，必须是**真实丢弃条数**。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from agent_runtime.core.context.budget import TokenBudget
from agent_runtime.core.context.compaction import CompactionStrategy, decide_strategy
from agent_runtime.core.context.manager import ContextManager
from agent_runtime.core.llm.types import FunctionCall, Message


def _tiny_budget() -> TokenBudget:
    """available=200, 触发阈值=160 —— 让压缩在测试里必然触发。"""
    return TokenBudget(model_max_tokens=200, reserved_output=0, safety_margin=1.0)


def _cm(keep_recent: int = 6, budget: TokenBudget | None = None) -> ContextManager:
    return ContextManager(budget=budget or _tiny_budget(), keep_recent=keep_recent)


async def _built(keep_recent: int = 6, budget: TokenBudget | None = None) -> ContextManager:
    cm = _cm(keep_recent=keep_recent, budget=budget)
    await cm.build(task="原始任务", system_prompt="system")
    return cm


def _roles(cm: ContextManager) -> list[str]:
    return [m.role for m in cm.get_messages()]


# ── 策略选择 ──


def test_decide_strategy_boundaries() -> None:
    assert decide_strategy(0.50) is CompactionStrategy.SQUEEZE
    assert decide_strategy(0.85) is CompactionStrategy.SQUEEZE
    assert decide_strategy(0.90) is CompactionStrategy.TRUNCATE
    assert decide_strategy(0.94) is CompactionStrategy.TRUNCATE
    assert decide_strategy(0.95) is CompactionStrategy.SUMMARIZE
    assert decide_strategy(0.99) is CompactionStrategy.SUMMARIZE


# ── SQUEEZE ──


def test_squeeze_compresses_tool_message_and_keeps_protocol_fields() -> None:
    async def run():
        cm = await _built()
        body = "A" * 500 + "MIDDLE" + "B" * 500
        cm.append(Message(role="assistant", content="调用工具",
                          tool_calls=[FunctionCall(id="c1", name="read_file", arguments="{}")]))
        cm.append(Message(role="tool", content=body, tool_call_id="c1"))
        before = cm.token_count()
        result = await cm.compact(strategy=CompactionStrategy.SQUEEZE)
        msg = cm.get_messages()[-1]
        return result, msg, before, cm.token_count()

    result, msg, before, after = asyncio.run(run())
    assert result.strategy is CompactionStrategy.SQUEEZE
    assert result.messages_squeezed == 1
    assert result.messages_dropped == 0
    assert msg.role == "tool"
    assert msg.tool_call_id == "c1", "压缩后必须保留 tool_call_id"
    assert "[SQUEEZED" in msg.content
    assert msg.content.startswith("A" * 50)
    assert msg.content.endswith("B" * 50)
    assert after < before, "压缩后 token 必须下降"


def test_squeeze_leaves_short_tool_message_untouched() -> None:
    async def run():
        cm = await _built()
        cm.append(Message(role="tool", content="short", tool_call_id="c1"))
        result = await cm.compact(strategy=CompactionStrategy.SQUEEZE)
        return result, cm.get_messages()[-1].content

    result, content = asyncio.run(run())
    assert content == "short"
    assert result.messages_squeezed == 0


def test_squeeze_does_not_touch_non_tool_messages() -> None:
    async def run():
        cm = await _built()
        big_user = Message(role="user", content="U" * 800)
        cm.append(big_user)
        cm.append(Message(role="tool", content="T" * 800, tool_call_id="c1"))
        await cm.compact(strategy=CompactionStrategy.SQUEEZE)
        msgs = cm.get_messages()
        return [m.content for m in msgs], msgs[-1].content

    contents, tool_content = asyncio.run(run())
    assert "U" * 800 in contents, "非 tool 消息不得被改动"
    assert "[SQUEEZED" in tool_content


# ── TRUNCATE ──


def test_truncate_keeps_system_task_and_recent() -> None:
    async def run():
        cm = await _built(keep_recent=2)
        cm.append(Message(role="assistant", content="X" * 3000))
        cm.append(Message(role="user", content="最近1"))
        cm.append(Message(role="assistant", content="最近2"))
        assert cm.should_compact() is True
        result = await cm.compact(strategy=CompactionStrategy.TRUNCATE)
        return result, cm.get_messages(), cm.should_compact()

    result, messages, still_over = asyncio.run(run())
    contents = [m.content for m in messages]
    assert messages[0].role == "system", "system 必须保留"
    assert "原始任务" in contents, "当前任务不得被丢弃（spec 优先级 1）"
    assert "最近1" in contents and "最近2" in contents
    assert "X" * 100 not in contents, "最旧的大消息应被丢弃"
    assert result.messages_dropped == 1, "必须是真实丢弃条数，而不是 token 差"
    assert still_over is False


def test_truncate_never_leaves_orphan_tool_message() -> None:
    async def run():
        cm = await _built(keep_recent=3)
        cm.append(Message(role="assistant", content="a1",
                          tool_calls=[FunctionCall(id="c1", name="t", arguments="{}")]))
        cm.append(Message(role="tool", content="r1", tool_call_id="c1"))
        cm.append(Message(role="assistant", content="a2",
                          tool_calls=[FunctionCall(id="c2", name="t", arguments="{}")]))
        cm.append(Message(role="tool", content="r2", tool_call_id="c2"))
        await cm.compact(strategy=CompactionStrategy.TRUNCATE)
        return cm.get_messages()

    messages = asyncio.run(run())
    assert messages[0].role == "system"
    assert messages[2].role != "tool", "保留段不得以孤立的 tool 结果开头"


# ── 自动策略选择 ──


def test_auto_strategy_degrades_summarize_to_truncate() -> None:
    async def run():
        cm = await _built(keep_recent=2)
        cm.append(Message(role="assistant", content="Z" * 4000))
        cm.append(Message(role="user", content="近1"))
        cm.append(Message(role="assistant", content="近2"))
        result = await cm.compact()  # 不传策略 → 按 ratio 自动选
        return result, cm.should_compact()

    result, still_over = asyncio.run(run())
    assert result.strategy is CompactionStrategy.TRUNCATE
    assert result.degraded_from is CompactionStrategy.SUMMARIZE, "SUMMARIZE 在 Phase 5 前降级，且必须如实记录"
    assert still_over is False


def test_explicit_strategy_overrides_auto_selection() -> None:
    async def run():
        cm = await _built(keep_recent=2)
        cm.append(Message(role="tool", content="Q" * 4000, tool_call_id="c1"))
        result = await cm.compact(strategy=CompactionStrategy.SQUEEZE)
        return result, cm.get_messages(), cm.token_count()

    result, messages, _ = asyncio.run(run())
    assert result.strategy is CompactionStrategy.SQUEEZE
    assert len(messages) >= 3, "显式 SQUEEZE 不应丢弃消息"
    assert result.messages_dropped == 0


def test_result_token_counters_are_consistent() -> None:
    async def run():
        cm = await _built(keep_recent=2)
        cm.append(Message(role="tool", content="W" * 2000, tool_call_id="c1"))
        result = await cm.compact(strategy=CompactionStrategy.SQUEEZE)
        return result, cm.token_count()

    result, after = asyncio.run(run())
    assert result.tokens_after == after
    assert result.tokens_before >= result.tokens_after


def test_auto_squeeze_escalates_to_truncate_when_still_over_budget() -> None:
    """自动模式下 SQUEEZE 压不到阈值以下时，必须继续升级为 TRUNCATE。

    否则 ReActLoop 每步都会再调一次 compact()，SQUEEZE 已是空操作 → 空转且 token 继续涨，
    最终上下文溢出。这里用"非工具大消息 + 可压缩的小工具消息"精确落进 SQUEEZE 档。
    """

    async def run():
        budget = TokenBudget(model_max_tokens=1_000_000, reserved_output=0, safety_margin=1.0)
        cm = ContextManager(budget=budget, keep_recent=1)
        await cm.build(task="任务", system_prompt="system")
        cm.append(Message(role="assistant", content="A" * 20000))  # 非 tool：SQUEEZE 动不了
        cm.append(Message(role="tool", content="T" * 800, tool_call_id="c1"))
        # 把预算调到「占用 ≈ 85%」：落在 SQUEEZE 档（0.8~0.9）且已越过触发阈值
        budget.model_max_tokens = max(1, int(cm.token_count() / 0.85))
        assert cm.should_compact() is True
        result = await cm.compact()  # 自动选择
        return result, cm.should_compact()

    result, still_over = asyncio.run(run())
    assert result.messages_squeezed >= 1, "应先在 SQUEEZE 档尝试压缩"
    assert result.strategy is CompactionStrategy.TRUNCATE, "SQUEEZE 不够用时必须升级"
    assert still_over is False


def _run_all() -> None:
    tests = [
        v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)
    ]
    for t in tests:
        print(f"RUN  {t.__name__}")
        t()
        print(f"PASS {t.__name__}")
    print("ALL PASS")


if __name__ == "__main__":
    _run_all()
