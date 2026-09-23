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
    skills_used: list[str] = field(default_factory=list)

    rounds: int = 0
    """真实用掉的 **LLM 轮次**（循环迭代次数）。

    与 `steps` **不是一回事**：`steps` 是"工具调用数 + 1"，一轮里模型可以一次发多个
    `tool_calls`（真实全量里常见 2 个），于是 `len(steps)` 会明显大于轮次 ——
    CLI 里曾出现"步数 31 + max_steps(15)"这种看着自相矛盾的组合。
    不含撞 `max_steps` 之后那次强制收尾的额外调用（那是 `warning` 的事）。"""

    max_steps: int = 0
    """这一轮的轮次上限（`0` = 调用方没给，展示时省略"x/y"）。"""
