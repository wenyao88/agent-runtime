from dataclasses import dataclass, field
from enum import Enum


class AgentEventType(str, Enum):
    STEP_START = "step_start"
    THOUGHT = "thought"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    COMPACTION = "compaction"
    FINAL_ANSWER = "final_answer"
    ERROR = "error"


@dataclass
class AgentEvent:
    event_type: AgentEventType
    data: dict = field(default_factory=dict)
