try:
    import tiktoken
    _ENC = tiktoken.get_encoding("cl100k_base")

    def _count(text: str) -> int:
        return len(_ENC.encode(text))
except ImportError:  # sandbox: no tiktoken — char/4 estimate
    def _count(text: str) -> int:
        return max(1, len(text) // 4)

from ..llm.types import Message
from .budget import TokenBudget
from .compaction import CompactionResult
from .compaction import CompactionStrategy


class ContextManager:
    def __init__(self, budget: TokenBudget | None = None):
        self._budget = budget or TokenBudget()
        self._messages: list[Message] = []

    async def build(
        self,
        task: str,
        tools: list[dict] | None = None,
        memory_entries: list | None = None,
        skills: list | None = None,
        system_prompt: str | None = None,
    ) -> None:
        self._messages = []
        sys_text = system_prompt or "You are a helpful AI assistant with access to tools."
        if skills:
            lines = []
            for s in skills:
                if isinstance(s, dict):
                    lines.append(f"- {s.get('name')}: {s.get('description')}")
                else:
                    lines.append(f"- {s.manifest.name}: {s.manifest.description}")
            sys_text += "\n\nAvailable Skills:\n" + "\n".join(lines)
        if memory_entries:
            sys_text += "\n\nRelevant Memories:\n" + "\n".join(
                f"- {e.content[:200]}" for e in memory_entries
            )
        self._messages.append(Message(role="system", content=sys_text))
        self._messages.append(Message(role="user", content=task))

    def append(self, message: Message) -> None:
        self._messages.append(message)

    def token_count(self) -> int:
        text = "".join(m.content or "" for m in self._messages)
        return _count(text)

    def get_messages(self) -> list[Message]:
        return list(self._messages)

    def should_compact(self) -> bool:
        return self.token_count() > self._budget.compaction_threshold

    async def compact(self, strategy: CompactionStrategy | None = None) -> CompactionResult:
        before = self.token_count()
        actual_strategy = strategy or CompactionStrategy.TRUNCATE
        # Phase 0: drop oldest messages (keep system + last 4)
        if len(self._messages) > 5:
            system = self._messages[0]
            self._messages = [system] + self._messages[-4:]
        after = self.token_count()
        return CompactionResult(
            strategy=actual_strategy,
            tokens_before=before,
            tokens_after=after,
            messages_dropped=before - after,
        )
