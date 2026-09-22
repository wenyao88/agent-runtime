from dataclasses import dataclass, field
from datetime import datetime

from ..llm.types import TokenUsage


@dataclass
class TraceEvent:
    event_type: str
    step_number: int
    timestamp: datetime = field(default_factory=datetime.now)
    data: dict = field(default_factory=dict)


@dataclass
class TraceStep:
    step_number: int
    thought: str | None = None
    tool_call: dict | None = None
    tool_result: dict | None = None
    token_usage: TokenUsage = field(default_factory=TokenUsage)
    latency_ms: int = 0


@dataclass
class TraceSession:
    trace_id: str
    task: str
    config: dict = field(default_factory=dict)
    steps: list[TraceStep] = field(default_factory=list)
    final_answer: str | None = None
    total_tokens: TokenUsage = field(default_factory=TokenUsage)
    total_latency_ms: int = 0
    status: str = "running"
    started_at: datetime = field(default_factory=datetime.now)
    finished_at: datetime | None = None
