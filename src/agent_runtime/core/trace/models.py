"""Trace 数据模型（Phase 8 补：JSON 往返 + `source` + 事件日志）。

为什么要能 JSON 往返：trace 要落盘（可选 SQLite）、要经 API 出去、要给前端渲染 ——
`to_dict`/`from_dict` 的形状一旦定下就是**对外契约**（与 Benchmark 报告同一套理由）。
"""
from dataclasses import dataclass, field
from datetime import datetime

from ..llm.types import TokenUsage


def _tokens_to_dict(usage: TokenUsage) -> dict:
    return {
        "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
        "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
        "total_tokens": int(getattr(usage, "total_tokens", 0) or 0),
    }


def _tokens_from_dict(data: object) -> TokenUsage:
    if not isinstance(data, dict):
        return TokenUsage()
    return TokenUsage(
        prompt_tokens=int(data.get("prompt_tokens") or 0),
        completion_tokens=int(data.get("completion_tokens") or 0),
        total_tokens=int(data.get("total_tokens") or 0),
    )


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def _parse(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


@dataclass
class TraceEvent:
    event_type: str
    step_number: int
    timestamp: datetime = field(default_factory=datetime.now)
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "event_type": self.event_type,
            "step_number": self.step_number,
            "timestamp": self.timestamp.isoformat(),
            "data": dict(self.data or {}),
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "TraceEvent":
        return cls(
            event_type=str(payload.get("event_type") or ""),
            step_number=int(payload.get("step_number") or 0),
            timestamp=_parse(payload.get("timestamp")) or datetime.now(),
            data=dict(payload.get("data") or {}),
        )


@dataclass
class TraceStep:
    step_number: int
    thought: str | None = None
    tool_call: dict | None = None
    tool_result: dict | None = None
    token_usage: TokenUsage = field(default_factory=TokenUsage)
    latency_ms: int = 0

    def to_dict(self) -> dict:
        return {
            "step_number": self.step_number,
            "thought": self.thought,
            "tool_call": dict(self.tool_call) if self.tool_call else None,
            "tool_result": dict(self.tool_result) if self.tool_result else None,
            "token_usage": _tokens_to_dict(self.token_usage),
            "latency_ms": self.latency_ms,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "TraceStep":
        tool_call = payload.get("tool_call")
        tool_result = payload.get("tool_result")
        return cls(
            step_number=int(payload.get("step_number") or 0),
            thought=payload.get("thought"),
            tool_call=dict(tool_call) if isinstance(tool_call, dict) else None,
            tool_result=dict(tool_result) if isinstance(tool_result, dict) else None,
            token_usage=_tokens_from_dict(payload.get("token_usage")),
            latency_ms=int(payload.get("latency_ms") or 0),
        )


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

    source: str = "chat"
    """这次 trace 从哪来：`chat`（WS 会话）/ `benchmark`（评测逐任务）/ `demo`。

    UI 要能按来源过滤，也必须能一眼看出"这条 trace 属于哪条评测任务"（task 字段里带 task_id）。"""

    events: list[TraceEvent] = field(default_factory=list)
    """原始事件日志（与 `steps` 是同一批事实的两种视图：一个是逐事件、一个是按步合并）。"""

    def to_dict(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "task": self.task,
            "config": dict(self.config or {}),
            "steps": [step.to_dict() for step in self.steps],
            "final_answer": self.final_answer,
            "total_tokens": _tokens_to_dict(self.total_tokens),
            "total_latency_ms": self.total_latency_ms,
            "status": self.status,
            "started_at": self.started_at.isoformat(),
            "finished_at": _iso(self.finished_at),
            "source": self.source,
            "events": [event.to_dict() for event in self.events],
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "TraceSession":
        steps = payload.get("steps")
        events = payload.get("events")
        return cls(
            trace_id=str(payload.get("trace_id") or ""),
            task=str(payload.get("task") or ""),
            config=dict(payload.get("config") or {}),
            steps=[TraceStep.from_dict(s) for s in (steps or []) if isinstance(s, dict)],
            final_answer=payload.get("final_answer"),
            total_tokens=_tokens_from_dict(payload.get("total_tokens")),
            total_latency_ms=int(payload.get("total_latency_ms") or 0),
            status=str(payload.get("status") or "running"),
            started_at=_parse(payload.get("started_at")) or datetime.now(),
            finished_at=_parse(payload.get("finished_at")),
            source=str(payload.get("source") or "chat"),
            events=[TraceEvent.from_dict(e) for e in (events or []) if isinstance(e, dict)],
        )

    def summary(self) -> dict:
        """列表接口用的小结（不带逐条明细）。"""
        return {
            "trace_id": self.trace_id,
            "task": self.task,
            "source": self.source,
            "status": self.status,
            "started_at": self.started_at.isoformat(),
            "finished_at": _iso(self.finished_at),
            "steps": len(self.steps),
            "total_tokens": int(getattr(self.total_tokens, "total_tokens", 0) or 0),
            "total_latency_ms": int(self.total_latency_ms or 0),
            "final_answer": self.final_answer,
        }
