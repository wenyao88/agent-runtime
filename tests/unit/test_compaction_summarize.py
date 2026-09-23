"""摘要素材构造契约（`core/context/summarize.py`）。

SUMMARIZE 策略要先把"即将被丢弃的那段对话"喂给 LLM。喂什么、怎么截断、怎么标注，
决定了摘要质量与成本 —— 也决定了 LLM 会不会把 `tool_call_id="call_abc"` 之类的协议噪声抄进摘要。

三条硬要求：
  1. **角色可辨**：模型必须能看出哪段是用户说的、哪段是工具返回（本项目一贯的"证据可追溯"）。
  2. **截断必须带标记**：单条超长、总量超限都要明说，否则模型会把残缺当完整（Demo 1 吃过这个亏）。
  3. **保留部分仍按时间正序**：总量超限时丢的是**最旧**的几条，但拼出来的文本顺序不能乱。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from agent_runtime.core.context.summarize import (  # noqa: E402
    MAX_CHARS_PER_MESSAGE,
    SUMMARY_MARK,
    format_summary_message,
    render_for_summary,
)
from agent_runtime.core.llm.types import FunctionCall, Message  # noqa: E402


def _call(name: str = "read_file", args: str = '{"path": "a.txt"}') -> FunctionCall:
    return FunctionCall(id="c1", name=name, arguments=args)


# ── 渲染 ──


def test_render_labels_each_role_and_keeps_content() -> None:
    text = render_for_summary(
        [
            Message(role="user", content="帮我看看仓库"),
            Message(role="assistant", content="先读文件"),
            Message(role="tool", content="文件内容在这里", tool_call_id="c1"),
        ]
    )
    assert "帮我看看仓库" in text
    assert "先读文件" in text
    assert "文件内容在这里" in text
    assert "[user]" in text and "[assistant]" in text
    assert "[observation]" in text, "tool 结果必须标成 observation，模型才知道那是工具返回而非用户说的"


def test_render_includes_tool_call_name_and_arguments() -> None:
    """不带 tool_calls 的话，模型只知道"工具返回了什么"，不知道"当初为什么调它"。"""
    text = render_for_summary(
        [Message(role="assistant", content="", tool_calls=[_call()])]
    )
    assert 'read_file({"path": "a.txt"})' in text
    assert text.strip().startswith("[assistant]")


def test_render_truncates_long_message_with_marker() -> None:
    body = "甲" * (MAX_CHARS_PER_MESSAGE + 500)
    text = render_for_summary([Message(role="user", content=body)])
    assert "甲" * MAX_CHARS_PER_MESSAGE in text
    assert text.count("甲") == MAX_CHARS_PER_MESSAGE
    assert "已截断" in text, "截断必须带标记"
    assert "500" in text, "要说明截掉多少字符"


def test_render_keeps_chronological_order_when_over_total_limit() -> None:
    """总量超限 → 丢最旧的，但留下来的必须仍按时间正序（乱序会让模型编出错误的因果）。"""
    old = Message(role="user", content="最早" + "A" * 4000)
    mid = Message(role="assistant", content="中间" + "B" * 4000)
    new = Message(role="assistant", content="最近" + "C" * 4000)
    text = render_for_summary(
        [old, mid, new], max_chars_per_message=100, max_total_chars=300
    )
    assert "最早" not in text, "总量超限时应丢弃最旧的消息"
    assert "中间" in text and "最近" in text
    assert text.index("中间") < text.index("最近"), "保留部分必须仍是时间正序"
    assert "未纳入摘要" in text, "丢了消息必须标注条数"
    assert "更早的 1 条" in text, "必须写清丢了几条"


def test_render_empty_input_is_empty_string() -> None:
    assert render_for_summary([]) == ""


# ── 摘要消息格式 ──


def test_format_summary_message_carries_mark_and_count() -> None:
    text = format_summary_message("用户要分析 fastapi/fastapi；已确认用 github_* 工具。", 3)
    assert text.startswith(SUMMARY_MARK.format(folded=3))
    assert "已压缩 3 条早期消息" in text
    assert "已确认用 github_* 工具" in text


def test_format_summary_message_strips_body() -> None:
    text = format_summary_message("  正文  \n", 1)
    assert text.endswith("正文")


def test_format_summary_message_with_blank_body_keeps_only_the_mark() -> None:
    """摘要为空时也要留下痕迹：一条空消息混进上下文比不插更糟。"""
    text = format_summary_message("   ", 2)
    assert text == SUMMARY_MARK.format(folded=2)


def test_summary_prompt_asks_for_the_right_things() -> None:
    """prompt 决定了摘要里留下什么。它在 core 里是常量，所以可以直接断言。"""
    from agent_runtime.core.context.summarize import SUMMARY_PROMPT

    for token in ("结论", "决定", "未完成", "只输出", "不得编造"):
        assert token in SUMMARY_PROMPT, f"摘要 prompt 必须要求「{token}」"


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
