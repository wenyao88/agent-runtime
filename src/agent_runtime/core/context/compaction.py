"""上下文压缩：策略枚举、策略选择、结果模型，以及单条工具结果的压缩函数。

分档（ratio = 当前 token / 可用预算）：
    < 0.90 → SQUEEZE    就地压缩工具结果：协议字段无损、信息损失小、收益也小
    < 0.95 → TRUNCATE   丢弃最旧消息：保留 system + 当前任务 + 最近若干条
    >=0.95 → SUMMARIZE  需要额外 LLM 调用（Phase 5）；此前由 ContextManager 降级为 TRUNCATE
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

SQUEEZE_AT = 0.90
SUMMARIZE_AT = 0.95

SQUEEZE_HEAD = 200
SQUEEZE_TAIL = 100
SQUEEZE_MIN_CHARS = 400  # 短于这个长度不值得压缩，收益抵不过标记本身的噪声


class CompactionStrategy(str, Enum):
    SQUEEZE = "squeeze"
    TRUNCATE = "truncate"
    SUMMARIZE = "summarize"


@dataclass
class CompactionResult:
    strategy: CompactionStrategy
    tokens_before: int
    tokens_after: int
    messages_dropped: int = 0
    messages_squeezed: int = 0
    summarized_messages: int = 0
    """被折叠进摘要的消息条数；>0 表示**摘要确实发生了**（与 `degraded_from` 是两件事）。"""

    degraded_from: CompactionStrategy | None = None
    """想用某种策略但没用成（例如想摘要却没有摘要器）。"""

    degraded_reason: str = ""
    """人可读的降级原因。**必须有人读它** —— 静默降级是本项目反复踩的坑。"""

    noop: bool = False
    """本轮什么都没改变（保留窗口本身已超预算，且没有可摘要的早期消息）。

    必须与"降级"分开：没事可做 ≠ 想做什么没做成。否则 Trace/Benchmark 会把每一次
    "压不动"都统计成一次降级，长任务的统计数字就全错了。"""

    summarizer_tokens: int = 0
    """**本次**摘要那次 LLM 调用烧掉的 token（不是会话累计）。

    0 的含义有两种，都记在 `degraded_reason` 或"摘要器无 stats"里：没摘要 / provider 没报 usage。
    摘要器不报 usage 时宁可记 0，也不估算 —— 估算值进了成本表就是假账。"""

    summarizer_ms: int = 0
    """**本次**摘要调用的墙钟耗时（毫秒），来源同 `summarizer_tokens`。"""

    @property
    def saved_tokens(self) -> int:
        return max(0, self.tokens_before - self.tokens_after)


def decide_strategy(ratio: float) -> CompactionStrategy:
    """按预算占用比例选择压缩策略。"""
    if ratio >= SUMMARIZE_AT:
        return CompactionStrategy.SUMMARIZE
    if ratio >= SQUEEZE_AT:
        return CompactionStrategy.TRUNCATE
    return CompactionStrategy.SQUEEZE


def squeeze_text(
    text: str, head: int = SQUEEZE_HEAD, tail: int = SQUEEZE_TAIL
) -> str:
    """把过长的工具结果压成「头 + 省略标记 + 尾」；短文本原样返回。"""
    if len(text) <= max(SQUEEZE_MIN_CHARS, head + tail):
        return text
    omitted = len(text) - head - tail
    return f"{text[:head]}\n...[SQUEEZED {omitted} chars]...\n{text[-tail:]}"
