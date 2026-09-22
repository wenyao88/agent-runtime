"""上下文管理：组装、追加、token 计量、按策略压缩。

压缩的三条不变量（都有测试钉死）：
  1. system prompt 与**当前任务**永不丢弃（spec 优先级 1：任务丢了 Agent 就失去目标）；
  2. 保留段不得以 `role="tool"` 开头 —— 孤立的 tool 结果违反 OpenAI 消息协议约束；
  3. 显式传入的 strategy 优先于自动选择；SUMMARIZE 在 Phase 5 之前降级为 TRUNCATE，
     并如实记录 `degraded_from`（不假装做过摘要）。
"""
from __future__ import annotations

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


class ContextManager:
    def __init__(
        self,
        budget: TokenBudget | None = None,
        keep_recent: int = 6,
        memory_max_chars: int = MAX_DEFAULT_CHARS,
    ):
        self._budget = budget or TokenBudget()
        self._keep_recent = max(0, keep_recent)
        self._memory_max_chars = memory_max_chars
        self._messages: list[Message] = []

    async def build(
        self,
        task: str,
        tools: list[dict] | None = None,
        memory_entries: list | None = None,
        system_prompt: str | None = None,
    ) -> None:
        self._messages = []
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
        degraded_from: CompactionStrategy | None = None
        if chosen is CompactionStrategy.SUMMARIZE:
            # Phase 5 之前没有 summarizer LLM —— 降级，但如实记录降级来源
            degraded_from = CompactionStrategy.SUMMARIZE
            chosen = CompactionStrategy.TRUNCATE

        squeezed = 0
        dropped = 0
        if chosen is CompactionStrategy.SQUEEZE:
            squeezed = self._squeeze()
            if strategy is None and self.should_compact():
                # 自动模式升级：SQUEEZE 压不下去（例如占用主要来自非工具消息）时继续走 TRUNCATE。
                # 否则 ReActLoop 每步都会再调一次 compact()，而 SQUEEZE 已是空操作 ——
                # 空转的同时 token 继续增长，最终上下文溢出。
                dropped = self._truncate()
                chosen = CompactionStrategy.TRUNCATE
        else:
            dropped = self._truncate()

        after = self.token_count()
        return CompactionResult(
            strategy=chosen,
            tokens_before=before,
            tokens_after=after,
            messages_dropped=dropped,
            messages_squeezed=squeezed,
            degraded_from=degraded_from,
        )

    # ── 内部策略实现 ──

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
        """丢弃最旧消息（保留 system + 当前任务 + 最近若干条）；返回真实丢弃条数。"""
        messages = self._messages
        if len(messages) <= 2:
            return 0

        system = messages[0]
        task: Message | None = None
        rest_start = 1
        if messages[1].role == "user":
            task = messages[1]
            rest_start = 2
        rest = messages[rest_start:]

        keep = list(rest[-self._keep_recent :]) if self._keep_recent else []
        # 不变量：保留段不能以 tool 结果开头（否则就是没有对应 assistant tool_calls 的孤儿消息）
        while keep and keep[0].role == "tool" and len(keep) < len(rest):
            keep.pop(0)

        new_messages = [system]
        if task is not None:
            new_messages.append(task)
        new_messages.extend(keep)

        dropped = len(messages) - len(new_messages)
        self._messages = new_messages
        return dropped
