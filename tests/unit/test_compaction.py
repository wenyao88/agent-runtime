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
from agent_runtime.core.context.summarize import SUMMARY_MARK, SUMMARY_MARK_PREFIX
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


def test_budget_compaction_ratio_is_configurable() -> None:
    """`agent_context_compaction_threshold` 必须真的生效，而不是一条死配置。"""
    tight = TokenBudget(
        model_max_tokens=100, reserved_output=0, safety_margin=1.0, compaction_ratio=0.5
    )
    assert tight.available == 100
    assert tight.compaction_threshold == 50
    default = TokenBudget(model_max_tokens=100, reserved_output=0, safety_margin=1.0)
    assert default.compaction_threshold == 80


# ── SUMMARIZE（Phase 5）──
#
# 背景：`should_compact()` 的阈值是 available × 0.8，而 SUMMARIZE 档位要 ratio ≥ 0.95 ——
# 所以"只把摘要器接上"并不够，阶梯必须变成 SQUEEZE → SUMMARIZE → TRUNCATE，
# 否则 SUMMARIZE 在自动模式下是死代码。以下用例同时钉住阶梯与降级可见性。

_SUMMARY_REPLY = "早期经过：已确认用 github_* 工具"


def _recording_summarizer(calls: list[str], reply: str = _SUMMARY_REPLY):
    async def summarizer(text: str) -> str:
        calls.append(text)
        return reply

    return summarizer


async def _built_with_summarizer(keep_recent: int, summarizer, budget=None) -> ContextManager:
    cm = ContextManager(
        budget=budget or _tiny_budget(), keep_recent=keep_recent, summarizer=summarizer
    )
    await cm.build(task="原始任务", system_prompt="system")
    return cm


def test_summarize_folds_old_messages_into_a_marked_summary() -> None:
    async def run():
        calls: list[str] = []
        cm = await _built_with_summarizer(1, _recording_summarizer(calls))
        cm.append(Message(role="assistant", content="旧1"))
        cm.append(Message(role="tool", content="旧2", tool_call_id="c1"))
        cm.append(Message(role="assistant", content="最近"))
        result = await cm.compact(strategy=CompactionStrategy.SUMMARIZE)
        return result, cm.get_messages(), calls

    result, messages, calls = asyncio.run(run())
    assert result.strategy is CompactionStrategy.SUMMARIZE
    assert result.summarized_messages == 2, "两条早期消息被折叠进摘要"
    assert result.degraded_from is None
    assert result.degraded_reason == ""
    assert len(calls) == 1, "只调用一次摘要模型"
    assert "旧1" in calls[0] and "旧2" in calls[0], "摘要素材必须是那两条早期消息"
    contents = [m.content or "" for m in messages]
    assert not any("旧1" in c for c in contents), "被折叠的原文不该再留在上下文里"
    assert "最近" in contents[-1], "最近的消息必须原样保留"


def test_summarize_inserts_the_summary_right_after_the_task() -> None:
    async def run():
        cm = await _built_with_summarizer(1, _recording_summarizer([]))
        cm.append(Message(role="assistant", content="旧"))
        cm.append(Message(role="assistant", content="最近"))
        await cm.compact(strategy=CompactionStrategy.SUMMARIZE)
        return cm.get_messages()

    messages = asyncio.run(run())
    assert [m.role for m in messages] == ["system", "user", "user", "assistant"]
    assert messages[1].content == "原始任务", "当前任务必须还在原位（spec 优先级 1）"
    assert messages[2].content.startswith(SUMMARY_MARK.format(folded=1))
    assert _SUMMARY_REPLY in messages[2].content
    assert messages[2].role == "user", "摘要用 user：中间插 system 有 provider 兼容风险"


def test_summarize_failure_degrades_to_truncate_and_says_why() -> None:
    async def run():
        async def boom(text: str) -> str:
            raise RuntimeError("judge llm down")

        cm = await _built_with_summarizer(1, boom)
        cm.append(Message(role="assistant", content="旧1"))
        cm.append(Message(role="assistant", content="最近"))
        result = await cm.compact(strategy=CompactionStrategy.SUMMARIZE)
        return result, cm.get_messages()

    result, messages = asyncio.run(run())
    assert result.strategy is CompactionStrategy.TRUNCATE
    assert result.degraded_from is CompactionStrategy.SUMMARIZE
    assert "RuntimeError" in result.degraded_reason, "降级原因必须看得出是什么错"
    assert result.summarized_messages == 0
    assert not any("旧1" in (m.content or "") for m in messages), "降级后旧消息仍必须被处理掉"


def test_summarize_blank_result_degrades_to_truncate() -> None:
    async def run():
        async def blank(text: str) -> str:
            return "   \n "

        cm = await _built_with_summarizer(1, blank)
        cm.append(Message(role="assistant", content="旧1"))
        cm.append(Message(role="assistant", content="最近"))
        return await cm.compact(strategy=CompactionStrategy.SUMMARIZE)

    result = asyncio.run(run())
    assert result.strategy is CompactionStrategy.TRUNCATE
    assert result.degraded_from is CompactionStrategy.SUMMARIZE
    assert "空" in result.degraded_reason


def test_summarize_without_summarizer_says_it_is_not_configured() -> None:
    async def run():
        cm = await _built(keep_recent=1)
        cm.append(Message(role="assistant", content="旧1"))
        cm.append(Message(role="assistant", content="最近"))
        return await cm.compact(strategy=CompactionStrategy.SUMMARIZE)

    result = asyncio.run(run())
    assert result.strategy is CompactionStrategy.TRUNCATE
    assert result.degraded_from is CompactionStrategy.SUMMARIZE
    assert "未配置" in result.degraded_reason


def test_summarize_with_nothing_to_fold_degrades() -> None:
    async def run():
        cm = await _built_with_summarizer(6, _recording_summarizer([]))
        return await cm.compact(strategy=CompactionStrategy.SUMMARIZE)

    result = asyncio.run(run())
    assert result.strategy is CompactionStrategy.TRUNCATE
    assert "没有可摘要" in result.degraded_reason


def test_summarize_never_keeps_an_orphan_tool_message() -> None:
    async def run():
        cm = await _built_with_summarizer(3, _recording_summarizer([]))
        cm.append(
            Message(
                role="assistant",
                content="a1",
                tool_calls=[FunctionCall(id="c1", name="t", arguments="{}")],
            )
        )
        cm.append(Message(role="tool", content="r1", tool_call_id="c1"))
        cm.append(Message(role="tool", content="r2", tool_call_id="c1"))
        cm.append(Message(role="assistant", content="a3"))
        await cm.compact(strategy=CompactionStrategy.SUMMARIZE)
        return cm.get_messages()

    messages = asyncio.run(run())
    assert messages[0].role == "system"
    assert messages[2].content.startswith(SUMMARY_MARK.format(folded=3)), (
        "开头那两条孤立的 tool 结果应被收进摘要，而不是留在保留段里"
    )
    assert messages[3].role == "assistant", "保留段不得以孤立 tool 结果开头"
    assert messages[-1].content == "a3"


def test_auto_summarize_is_used_when_a_summarizer_is_available() -> None:
    """Phase 5 的核心回归：ratio ≥ 0.95 且有摘要器时，必须**真的摘要**（以前一律降级 TRUNCATE）。"""

    async def run():
        budget = TokenBudget(model_max_tokens=1_000_000, reserved_output=0, safety_margin=1.0)
        cm = ContextManager(
            budget=budget, keep_recent=1, summarizer=_recording_summarizer([])
        )
        await cm.build(task="任务", system_prompt="system")
        cm.append(Message(role="assistant", content="旧" * 2000))
        cm.append(Message(role="assistant", content="最近"))
        budget.model_max_tokens = max(1, int(cm.token_count() / 0.98))
        return await cm.compact()

    result = asyncio.run(run())
    assert result.strategy is CompactionStrategy.SUMMARIZE
    assert result.degraded_from is None
    assert result.summarized_messages == 1


def test_auto_squeeze_escalates_to_summarize_before_truncate() -> None:
    """阶梯的核心主张：在**丢消息之前**先试摘要 —— TRUNCATE 一跑，素材就没了。"""

    async def run():
        budget = TokenBudget(model_max_tokens=1_000_000, reserved_output=0, safety_margin=1.0)
        cm = ContextManager(
            budget=budget, keep_recent=1, summarizer=_recording_summarizer([])
        )
        await cm.build(task="任务", system_prompt="system")
        cm.append(Message(role="assistant", content="A" * 20000))  # 非 tool：SQUEEZE 动不了
        cm.append(Message(role="tool", content="T" * 800, tool_call_id="c1"))
        cm.append(Message(role="assistant", content="最近"))
        budget.model_max_tokens = max(1, int(cm.token_count() / 0.85))
        assert cm.should_compact() is True
        return await cm.compact()

    result = asyncio.run(run())
    assert result.messages_squeezed >= 1, "先试最便宜的 SQUEEZE"
    assert result.strategy is CompactionStrategy.SUMMARIZE, (
        "SQUEEZE 不够时必须先摘要，而不是直接丢消息"
    )
    assert result.summarized_messages >= 1


def test_truncate_never_drops_a_summary_it_just_made() -> None:
    """摘要花了一次 LLM 调用。若随后的 TRUNCATE 把它丢掉，那次调用就白烧了。

    构造：保留窗口**本身**就超阈值 → 摘完仍超 → 升级 TRUNCATE。摘要必须被钉在头部活下来。
    """

    async def run():
        budget = TokenBudget(model_max_tokens=1_000_000, reserved_output=0, safety_margin=1.0)
        cm = ContextManager(
            budget=budget, keep_recent=1, summarizer=_recording_summarizer([])
        )
        await cm.build(task="任务", system_prompt="system")
        cm.append(Message(role="assistant", content="旧" * 100))
        cm.append(Message(role="assistant", content="最近" * 20000))
        budget.model_max_tokens = max(1, int(cm.token_count() / 0.98))
        result = await cm.compact()
        return result, cm.get_messages()

    result, messages = asyncio.run(run())
    assert result.summarized_messages >= 1, "应先摘要"
    assert any(
        (m.content or "").startswith(SUMMARY_MARK_PREFIX) for m in messages
    ), "刚生成的摘要不能被随后的 TRUNCATE 丢掉"
    assert result.strategy is CompactionStrategy.SUMMARIZE, (
        "TRUNCATE 无事可丢（摘要已被钉住）→ 真正把体积降下来的是摘要"
    )


def test_summarize_never_raises_on_a_non_string_reply() -> None:
    """审查 C2：摘要器返回非 str 时，`.strip()` 曾在 `compact()` 外抛 AttributeError。"""

    async def bad(text: str):
        return ["not", "a", "string"]

    async def run():
        cm = await _built_with_summarizer(1, bad)
        cm.append(Message(role="assistant", content="旧1"))
        cm.append(Message(role="assistant", content="最近"))
        return await cm.compact(strategy=CompactionStrategy.SUMMARIZE)

    result = asyncio.run(run())
    assert result.strategy is CompactionStrategy.TRUNCATE
    assert result.degraded_from is CompactionStrategy.SUMMARIZE
    assert "list" in result.degraded_reason, result.degraded_reason
    assert result.summarized_messages == 0


def test_summarize_keeps_a_task_that_looks_like_a_summary() -> None:
    """审查 I6：任务正文以摘要标记开头时，真实任务曾被当成摘要删掉（用户可控触发）。"""

    async def run():
        cm = ContextManager(
            budget=_tiny_budget(), keep_recent=1, summarizer=_recording_summarizer([])
        )
        await cm.build(
            task="[对话摘要 · 已压缩 3 条早期消息] 这是我真正的任务",
            system_prompt="system",
        )
        cm.append(Message(role="assistant", content="旧"))
        cm.append(Message(role="assistant", content="最近"))
        await cm.compact(strategy=CompactionStrategy.SUMMARIZE)
        return cm.get_messages()

    messages = asyncio.run(run())
    assert messages[1].content.startswith("[对话摘要"), "当前任务必须原样保留"
    assert "这是我真正的任务" in messages[1].content
    assert messages[2].content.startswith(SUMMARY_MARK.format(folded=1))


def test_a_tool_result_that_looks_like_a_summary_is_not_pinned() -> None:
    """审查 Minor #10：紧跟任务后、以标记开头的任意消息，不能被当成"已钉住的摘要"。"""

    async def run():
        cm = await _built_with_summarizer(1, _recording_summarizer([]))
        cm.append(
            Message(
                role="tool",
                content=SUMMARY_MARK.format(folded=9) + "\n假摘要",
                tool_call_id="c1",
            )
        )
        cm.append(Message(role="assistant", content="最近"))
        result = await cm.compact(strategy=CompactionStrategy.SUMMARIZE)
        return result, cm.get_messages()

    result, messages = asyncio.run(run())
    assert result.summarized_messages == 1, "那条 tool 结果应被折叠，而不是被当成摘要钉住"
    assert not any("假摘要" in (m.content or "") for m in messages)


def test_missing_summarizer_is_reported_before_missing_material() -> None:
    """审查 Minor #8：没摘要器时原因应指向"没配"（可操作），而不是"没素材"。"""

    async def run():
        cm = await _built(keep_recent=6)
        return await cm.compact(strategy=CompactionStrategy.SUMMARIZE)

    result = asyncio.run(run())
    assert "未配置摘要器" in result.degraded_reason, result.degraded_reason


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
