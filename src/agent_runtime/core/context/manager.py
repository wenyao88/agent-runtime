"""上下文管理：组装、追加、token 计量、按策略压缩。

压缩的四条不变量（都有测试钉死）：
  1. system prompt 与**当前任务**永不丢弃（spec 优先级 1：任务丢了 Agent 就失去目标）；
  2. 保留段不得以 `role="tool"` 开头 —— 孤立的 tool 结果违反 OpenAI 消息协议约束；
  3. 摘要消息一旦生成就**钉在头部**，后续 TRUNCATE 不得丢弃它（那次 LLM 调用不能白烧）；
  4. 显式传入的 strategy 优先于自动选择；摘要器缺省/失败 → 降级为 TRUNCATE 并如实记录
     `degraded_from` + `degraded_reason`（不假装做过摘要）。

压缩阶梯（自动模式，Phase 5）：

    ratio >= 0.95 → SUMMARIZE
    ratio <  0.95 → SQUEEZE，仍超阈值 → SUMMARIZE（有摘要器时）→ 仍超阈值 → TRUNCATE

顺序的理由：**必须在下手丢消息之前决定要不要摘要** —— TRUNCATE 跑完，素材就没了。
（注：`should_compact()` 的阈值是 available × 0.8，而 SUMMARIZE 档位要 ≥ 0.95，
所以只把摘要器接上而不改阶梯的话，SUMMARIZE 在自动模式下是死代码。）
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable

try:
    import tiktoken

    _ENC = tiktoken.get_encoding("cl100k_base")

    def _count(text: str) -> int:
        return len(_ENC.encode(text))

except ImportError:  # 无 tiktoken 时退化为字符数估算，保证核心链路可用

    def _count(text: str) -> int:
        return max(1, len(text) // 4)


from ..llm.types import Message
from ..memory.annotate import MAX_DEFAULT_CHARS, format_line
from .budget import TokenBudget
from .compaction import (
    CompactionResult,
    CompactionStrategy,
    decide_strategy,
    squeeze_text,
)
from .summarize import (
    SUMMARY_PROMPT,
    format_summary_message,
    render_for_summary,
)

Summarizer = Callable[[str], Awaitable[str]]


def _int_stat(stats: dict, key: str) -> int:
    try:
        return int(stats.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0


def _compaction_view(result: CompactionResult | None) -> dict | None:
    """把最近一次压缩结果压成快照里的一个小对象；没压过就是 `None`。"""
    if result is None:
        return None
    return {
        "strategy": result.strategy.value,
        "before": result.tokens_before,
        "after": result.tokens_after,
        "saved_tokens": result.saved_tokens,
        "messages_dropped": result.messages_dropped,
        "messages_squeezed": result.messages_squeezed,
        "summarized": result.summarized_messages,
        "noop": result.noop,
        "degraded_from": (
            result.degraded_from.value if result.degraded_from is not None else None
        ),
        "degraded_reason": result.degraded_reason,
        "summarizer_tokens": result.summarizer_tokens,
        "summarizer_ms": result.summarizer_ms,
    }


class ContextManager:
    def __init__(
        self,
        budget: TokenBudget | None = None,
        keep_recent: int = 6,
        memory_max_chars: int = MAX_DEFAULT_CHARS,
        summarizer: Summarizer | None = None,
    ):
        self._budget = budget or TokenBudget()
        self._keep_recent = max(0, keep_recent)
        self._memory_max_chars = memory_max_chars
        self._summarizer = summarizer
        # 已生成的那条摘要（**按对象身份**认得，不按内容前缀 —— 任务或工具结果也可能
        # 以那个标记开头，按前缀认会把真实内容当摘要删掉）
        self._pinned_summary: Message | None = None
        self._messages: list[Message] = []
        # Inspector 的"上下文"一块要看得见 token 花在哪：记忆注入是**拼进 system 文本**的，
        # 所以在 build 时单独留一份原文，否则快照里分不出"system 本体"与"记忆"
        self._memory_text = ""
        self._last_compaction: CompactionResult | None = None

    async def build(
        self,
        task: str,
        tools: list[dict] | None = None,
        memory_entries: list | None = None,
        system_prompt: str | None = None,
    ) -> None:
        self._messages = []
        self._pinned_summary = None
        # 新一轮任务：上一轮的压缩结果不再代表"当前状态"（快照里不能继续挂着它）
        self._last_compaction = None
        self._memory_text = ""
        sys_text = system_prompt or "You are a helpful AI assistant with access to tools."
        if memory_entries:
            # 记忆行带 [来源 · 日期] 标注：模型必须能区分"历史记忆"与"本轮工具结果"。
            # 截断长度由构造参数决定（来自 MEMORY_INJECT_MAX_CHARS），截断必带省略号。
            self._memory_text = "\n".join(
                format_line(e, self._memory_max_chars) for e in memory_entries
            )
            sys_text += "\n\nRelevant Memories:\n" + self._memory_text
        self._messages.append(Message(role="system", content=sys_text))
        self._messages.append(Message(role="user", content=task))

    def append(self, message: Message) -> None:
        self._messages.append(message)

    def token_count(self) -> int:
        text = "".join(m.content or "" for m in self._messages)
        return _count(text)

    def token_ratio(self) -> float:
        """当前占用 / 可用预算，用于 decide_strategy。"""
        return self.token_count() / max(1, self._budget.available)

    def get_messages(self) -> list[Message]:
        return list(self._messages)

    def should_compact(self) -> bool:
        return self.token_count() > self._budget.compaction_threshold

    def snapshot(self) -> dict:
        """当前上下文的**只读**快照：分段 token + 预算/阈值 + 最近一次压缩（Inspector 用）。

        分段口径：记忆被拼进 system 文本，所以"system 本体"要从 system 里**减去**记忆那段，
        四个段加起来必须等于 `used_tokens`（有测试钉住）。`last_compaction=None` = 这一轮还没压过
        （`None` ≠ "压了一次但什么都没变" —— 后者是 `noop=True` 的 CompactionResult）。
        """
        segments: list[tuple[str, str]] = []
        system_text = self._messages[0].content if self._messages else ""
        if self._memory_text and self._memory_text in (system_text or ""):
            # 连它前面那个 "\n\nRelevant Memories:\n" 一起算进记忆，否则差额会漏到 system 里
            marker = "\n\nRelevant Memories:\n"
            base = (system_text or "").split(marker, 1)[0]
            segments.append(("system", base))
            segments.append(("memory", marker + self._memory_text))
        else:
            segments.append(("system", system_text or ""))
            segments.append(("memory", ""))

        rest = self._messages[1:] if len(self._messages) > 1 else []
        if rest and rest[0].role == "user":
            segments.append(("task", rest[0].content or ""))
            rest = rest[1:]
        else:
            segments.append(("task", ""))
        segments.append(("messages", "".join(m.content or "" for m in rest)))

        sections = [
            # 空段记 0 而不是 `_count("")` 的兜底 1：分布图里"这段没内容"必须真是 0
            {"name": name, "tokens": _count(text) if text else 0, "chars": len(text)}
            for name, text in segments
        ]
        used = sum(section["tokens"] for section in sections)
        return {
            "budget": {
                "model_max_tokens": self._budget.model_max_tokens,
                "reserved_output": self._budget.reserved_output,
                "available": self._budget.available,
                "threshold": self._budget.compaction_threshold,
            },
            "used_tokens": used,
            "ratio": used / max(1, self._budget.available),
            "sections": sections,
            "messages": len(self._messages),
            "last_compaction": _compaction_view(self._last_compaction),
        }

    async def compact(self, strategy: CompactionStrategy | None = None) -> CompactionResult:
        before = self.token_count()
        if strategy is not None:
            chosen = strategy
        elif decide_strategy(self.token_ratio()) is CompactionStrategy.SUMMARIZE:
            chosen = CompactionStrategy.SUMMARIZE
        else:
            # 自动模式**永不**直接丢消息：先试免费的 SQUEEZE，不够再摘要，最后才 TRUNCATE。
            # （`decide_strategy` 在 0.90~0.95 档返回 TRUNCATE，若照它执行，摘要连试都不会试 —— 审查 I3。）
            chosen = CompactionStrategy.SQUEEZE
        result = CompactionResult(strategy=chosen, tokens_before=before, tokens_after=before)

        if chosen is CompactionStrategy.SQUEEZE:
            result.messages_squeezed = self._squeeze()
            if strategy is not None or not self.should_compact():
                # 显式要求 SQUEEZE，或自动模式下压完已经不超了。
                return self._finish(result)

        # 显式 TRUNCATE/SUMMARIZE，或自动模式下 SQUEEZE 没压下去：
        # **先试摘要，再丢消息** —— TRUNCATE 一跑，要摘要的素材就没了。
        degraded = False
        if chosen is not CompactionStrategy.TRUNCATE:
            ok = await self._try_summarize(result)
            if ok:
                result.strategy = CompactionStrategy.SUMMARIZE
                if not self.should_compact():
                    return self._finish(result)
            elif ok is False:
                result.degraded_from = CompactionStrategy.SUMMARIZE
                degraded = True

        dropped = self._truncate()
        result.messages_dropped = dropped
        if degraded or dropped:
            # 摘要没做成、或摘要后仍超阈值又丢了一批 → 真正把体积降下来的是 TRUNCATE
            result.strategy = CompactionStrategy.TRUNCATE
        return self._finish(result)

    def _finish(self, result: CompactionResult) -> CompactionResult:
        """统一收尾：算最终 token 与 `noop`。

        `noop` 必须覆盖**所有**返回路径，否则同一个"什么都没做"会因走的分支不同而报得不一样。
        """
        result.tokens_after = self.token_count()
        result.noop = not (
            result.messages_squeezed
            or result.messages_dropped
            or result.summarized_messages
        )
        # 所有返回路径都经过这里，所以快照里的"最近一次压缩"一定是最新的那次
        self._last_compaction = result
        return result

    # ── 内部策略实现 ──

    def _split_old(self) -> tuple[list[Message], list[Message], list[Message]]:
        """切成 `(head, old, keep)`。

        * `head` = system + 当前任务 + **已生成的摘要**（钉住，TRUNCATE 不得丢弃）
        * `keep` = 最近 `keep_recent` 条，且不得以孤立的 tool 结果开头
        * `old`  = 两者之间（含被剔除的孤立 tool 结果）—— 摘要 / 丢弃的唯一候选
        """
        messages = self._messages
        if not messages:
            return [], [], []

        head = [messages[0]]
        rest_start = 1
        if len(messages) > 1 and messages[1].role == "user":
            # 当前任务与 system 同为"永不过期"（spec 优先级 1）
            head.append(messages[1])
            rest_start = 2
        rest = messages[rest_start:]
        if rest and rest[0] is self._pinned_summary:
            head.append(rest[0])
            rest = rest[1:]

        keep = list(rest[-self._keep_recent :]) if self._keep_recent else []
        # 不变量：保留段不能以 tool 结果开头（否则就是没有对应 assistant tool_calls 的孤儿消息）
        while keep and keep[0].role == "tool" and len(keep) < len(rest):
            keep.pop(0)
        old = rest[: len(rest) - len(keep)]
        return head, old, keep

    async def _try_summarize(self, result: CompactionResult) -> bool | None:
        """把"保留段之外"的早期消息折叠成一条带标记的摘要。**绝不外抛**。

        返回：`True` 已摘要 / `False` 想摘要但失败了（调用方据此记降级）/
        `None` **没有可做的事**（没有早期消息 —— 这不是降级，别统计成"想摘要没做成"）。
        """
        if self._summarizer is None:
            # 先报"没配"再报"没素材"：前者更可操作（用户知道该补什么配置）
            result.degraded_reason = (
                "未配置摘要器（AGENT_COMPACTION_SUMMARIZE_ENABLED 未打开，或缺 JUDGE_LLM key）"
            )
            return False

        head, old, keep = self._split_old()
        if not old:
            result.degraded_reason = "没有可摘要的早期消息"
            return None

        # 上一轮的摘要要一并喂进去，否则跨多轮压缩会一层层丢信息
        previous = self._pinned_summary
        material = ([previous] if previous else []) + old
        # 摘要器的累计成本快照：调用前后各读一次，差值才是**这一次**的钱
        stats = getattr(self._summarizer, "stats", None)
        before_stats = dict(stats) if isinstance(stats, dict) else None
        try:
            raw = await self._summarizer(
                SUMMARY_PROMPT.format(text=render_for_summary(material))
            )
        except Exception as e:  # noqa: BLE001 —— 摘要失败绝不能让整轮任务炸掉
            self._collect_cost(result, stats, before_stats)
            result.degraded_reason = f"摘要失败，降级为丢弃：{type(e).__name__}: {e}"
            return False
        self._collect_cost(result, stats, before_stats)

        if not isinstance(raw, str):
            # 不把非字符串"字符串化"当摘要：那等于往上下文里塞垃圾
            result.degraded_reason = (
                f"摘要器返回了非字符串内容（{type(raw).__name__}），已忽略"
            )
            return False
        text = raw.strip()
        if not text:
            result.degraded_reason = "摘要器返回空内容"
            return False

        summary_message = Message(
            role="user", content=format_summary_message(text, len(old))
        )
        head = [m for m in head if m is not self._pinned_summary]
        self._pinned_summary = summary_message
        head.append(summary_message)
        self._messages = head + keep
        result.summarized_messages = len(old)
        return True

    @staticmethod
    def _collect_cost(
        result: CompactionResult, stats: object, before_stats: dict | None
    ) -> None:
        """把摘要器自报的成本**增量**记到这次压缩上。

        必须是增量：同一条会话会压很多次，报累计值会让汇总指标把同一笔钱重复相加。
        摘要器不带 `.stats`（第三方注入的 callable）时只能留 0 —— 不估算、也不算降级。
        """
        if not isinstance(stats, dict) or not isinstance(before_stats, dict):
            return
        result.summarizer_tokens = max(
            0, _int_stat(stats, "total_tokens") - _int_stat(before_stats, "total_tokens")
        )
        result.summarizer_ms = max(
            0, _int_stat(stats, "total_ms") - _int_stat(before_stats, "total_ms")
        )

    def _squeeze(self) -> int:
        """就地压缩 tool 消息的长文本；返回被压缩的条数。"""
        squeezed = 0
        for message in self._messages:
            if message.role != "tool":
                continue
            original = message.content or ""
            compressed = squeeze_text(original)
            if compressed != original:
                message.content = compressed
                squeezed += 1
        return squeezed

    def _truncate(self) -> int:
        """丢弃最旧消息（保留 head + 最近若干条）；返回真实丢弃条数。

        `messages_dropped` 会进 Trace/Benchmark 报告，必须是**真实条数**而不是 token 差。
        """
        head, old, keep = self._split_old()
        self._messages = head + keep
        return len(old)
