from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from ..llm.types import TokenUsage


class AgentState(str, Enum):
    IDLE = "idle"
    THINKING = "thinking"
    ACTING = "acting"
    OBSERVING = "observing"
    FINISHED = "finished"
    ERROR = "error"


@dataclass
class FinalAnswer:
    content: str


@dataclass
class ToolCall:
    tool_name: str
    arguments: dict


@dataclass
class AgentStep:
    step_number: int
    thought: str = ""
    action: ToolCall | FinalAnswer | None = None
    observation: str | None = None
    token_usage: TokenUsage = field(default_factory=TokenUsage)
    latency_ms: int = 0
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class AgentResult:
    task: str
    final_answer: str = ""
    steps: list[AgentStep] = field(default_factory=list)
    total_tokens: TokenUsage = field(default_factory=TokenUsage)
    total_latency_ms: int = 0
    trace_id: str = ""
    warning: str | None = None
