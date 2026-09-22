from dataclasses import dataclass, field
from typing import Any


@dataclass
class FunctionCall:
    id: str | None = None
    name: str = ""
    arguments: str = ""  # JSON string


@dataclass
class Message:
    role: str  # system | user | assistant | tool
    content: str | None = None
    tool_calls: list[FunctionCall] | None = None
    tool_call_id: str | None = None


@dataclass
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass
class LLMResponse:
    content: str | None = None
    tool_calls: list[FunctionCall] = field(default_factory=list)
    token_usage: TokenUsage = field(default_factory=TokenUsage)
    finish_reason: str = "stop"


@dataclass
class LLMStreamChunk:
    delta_content: str | None = None
    delta_tool_call: FunctionCall | None = None
    finish_reason: str | None = None
