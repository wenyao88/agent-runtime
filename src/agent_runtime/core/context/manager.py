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

    async def build(
        self,
        task: str,
        tools: list[dict] | None = None,
        memory_entries: list | None = None,
        system_prompt: str | None = None,
    ) -> None:
        self._messages = []
        self._pinned_summary = None
        sys_text = system_prompt or "You are a helpful AI assistant with access to tools."
        if memory_entries:
            # 记忆行带 [来源 · 日期] 标注：模型必须能区分"历史记忆"与"本轮工具结果"。
            # 截断长度由构造参数决定（来自 MEMORY_INJECT_MAX_CHARS），截断必带省略号。
            sys_text += "\n\nRelevant Memories:\n" + "\n".join(
                format_line(e, self._memory_max_chars) for e in memory_entries
            )
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

    async def compact(self, strategy: CompactionStrategy | None = None) -> CompactionResult:
        before = self.token_count()
        chosen = strategy or decide_strategy(self.token_ratio())
        result = CompactionResult(strategy=chosen, tokens_before=before, tokens_after=before)

        if chosen is CompactionStrategy.SQUEEZE:
            result.messages_squeezed = self._squeeze()
            if strategy is not None or not self.should_compact():
                # 显式要求 SQUEEZE，或自动模式下压完已经不超了。
                result.tokens_after = self.token_count()
                return result

        # 显式 TRUNCATE/SUMMARIZE，或自动模式下 SQUEEZE 没压下去：
        # **先试摘要，再丢消息** —— TRUNCATE 一跑，要摘要的素材就没了。
        degraded = False
        if chosen is not CompactionStrategy.TRUNCATE:
            ok = await self._try_summarize(result)
            if ok:
                result.strategy = CompactionStrategy.SUMMARIZE
                if not self.should_compact():
                    result.tokens_after = self.token_count()
                    return result
            else:
                result.degraded_from = CompactionStrategy.SUMMARIZE
                degraded = True

        dropped = self._truncate()
        result.messages_dropped = dropped
        if degraded or dropped:
            # 摘要没做成、或摘要后仍超阈值又丢了一批 → 真正把体积降下来的是 TRUNCATE
            result.strategy = CompactionStrategy.TRUNCATE
        result.tokens_after = self.token_count()
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

    async def _try_summarize(self, result: CompactionResult) -> bool:
        """把"保留段之外"的早期消息折叠成一条带标记的摘要。**绝不外抛**。

        失败原因写进 `result.degraded_reason`，由调用方把策略落回 TRUNCATE。
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
            return False

        # 上一轮的摘要要一并喂进去，否则跨多轮压缩会一层层丢信息
        previous = self._pinned_summary
        material = ([previous] if previous else []) + old
        try:
            raw = await self._summarizer(
                SUMMARY_PROMPT.format(text=render_for_summary(material))
            )
        except Exception as e:  # noqa: BLE001 —— 摘要失败绝不能让整轮任务炸掉
            result.degraded_reason = f"摘要失败，降级为丢弃：{type(e).__name__}: {e}"
            return False

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

        head = [m for m in head if m is not self._pinned_summary]
        summary = Message(role="user", content=format_summary_message(text, len(old)))
        self._pinned_summary = summary
        head.append(summary)
        self._messages = head + keep
        result.summarized_messages = len(old)
        return True

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
