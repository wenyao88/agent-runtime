"""把"即将被压缩掉的对话"渲染成给 LLM 的摘要素材（纯函数，零第三方依赖）。

为什么单独一个模块：这是 SUMMARIZE 策略里唯一"喂什么给模型"的决策点 ——
它同时决定摘要质量与这次额外 LLM 调用的成本（老对话可能有几万 token）。

三条硬要求：
  1. **角色可辨**：`tool` 渲染成 `observation`、assistant 的 tool_calls 渲染成 `[调用 name(args)]`，
     这样模型才知道"工具返回了什么"以及"当初为什么调它"。
  2. **截断必须带标记**：单条超长、总量超限都要明说 —— Demo 1 吃过"无标记截断把残缺当完整"的亏。
  3. **总量超限时丢最旧的**（最近的历史对当前状态更有用），但保留部分仍按**时间正序**拼装。
"""
from __future__ import annotations

from ..llm.types import Message

SUMMARY_MARK = "[对话摘要 · 已压缩 {folded} 条早期消息]"
# 用来识别"这条消息是摘要"（`ContextManager` 靠它把摘要钉在头部，TRUNCATE 不得丢弃）
SUMMARY_MARK_PREFIX = "[对话摘要"

# 单条与总量的上限：摘要本身也是一次 LLM 调用，不设上限就是「用一次超大 prompt 换一点空间」
MAX_CHARS_PER_MESSAGE = 1200
MAX_TOTAL_CHARS = 24000

_TRUNCATED_MARK = "…（本条已截断 {omitted} 字符）"
_OMITTED_MARK = "…（更早的 {omitted} 条消息因长度上限未纳入摘要）"
_TOOL_LABEL = "observation"

SUMMARY_PROMPT = (
    "下面是一段 Agent 与工具交互的早期记录（可能已被截断）。把它压缩成一段简短摘要，"
    "供后续步骤当作背景参考。\n"
    "保留：任务目标与约束、已得出的结论、已做的决定、关键事实与文件/仓库路径、尚未完成的事项。\n"
    "丢弃：寒暄、重复尝试、已失败过程的细节、工具返回的大段原文。\n"
    "不得编造记录里没有的信息。只输出摘要正文，不要任何前后缀。\n\n"
    "{text}"
)


def _label(role: str) -> str:
    return _TOOL_LABEL if role == "tool" else (role or "unknown")


def _render_message(message: Message, max_chars: int) -> str:
    text = message.content or ""
    if len(text) > max_chars:
        omitted = len(text) - max_chars
        text = text[:max_chars] + _TRUNCATED_MARK.format(omitted=omitted)
    parts = [f"[{_label(message.role)}]"]
    if text:
        parts.append(text)
    for call in message.tool_calls or []:
        parts.append(f"[调用 {call.name}({call.arguments})]")
    return " ".join(parts)


def render_for_summary(
    messages: list[Message],
    *,
    max_chars_per_message: int = MAX_CHARS_PER_MESSAGE,
    max_total_chars: int = MAX_TOTAL_CHARS,
) -> str:
    """渲染待摘要的消息；没有任何消息时返回空串（调用方据此判定"没有素材"）。"""
    if not messages:
        return ""

    blocks = [_render_message(m, max_chars_per_message) for m in messages]
    kept: list[str] = []
    used = 0
    for block in reversed(blocks):  # 从最近的消息往回取
        if kept and used + len(block) + 1 > max_total_chars:
            break
        kept.append(block)
        used += len(block) + 1
    kept.reverse()  # 拼装时恢复时间正序

    body = "\n".join(kept)
    omitted = len(blocks) - len(kept)
    if omitted:
        return _OMITTED_MARK.format(omitted=omitted) + "\n" + body
    return body


def format_summary_message(text: str, folded: int) -> str:
    """把摘要正文包装成一条带标记的消息内容；正文为空时只留标记（不留空消息）。"""
    body = (text or "").strip()
    mark = SUMMARY_MARK.format(folded=folded)
    return f"{mark}\n{body}" if body else mark
