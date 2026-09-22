from dataclasses import dataclass
from enum import Enum


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
