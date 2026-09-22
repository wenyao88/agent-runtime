import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import datetime

from ..tool.base import ToolResult
from .models import TraceEvent, TraceSession


class Tracer:
    def __init__(self):
        self._current_session: TraceSession | None = None
        self._event_queue: asyncio.Queue[TraceEvent] = asyncio.Queue()

    async def start_session(self, task: str, config: dict | None = None) -> TraceSession:
        self._current_session = TraceSession(
            trace_id=str(uuid.uuid4())[:8],
            task=task,
            config=config or {},
        )
        return self._current_session

    def _record(self, event_type: str, step: int, data: dict) -> None:
        self._event_queue.put_nowait(TraceEvent(event_type=event_type, step_number=step, data=data))

    def record_thought(self, step: int, content: str) -> None:
        self._record("thought", step, {"content": content})

    def record_tool_call(self, step: int, tool: str, args: dict) -> None:
        self._record("tool_call", step, {"tool_name": tool, "args": args})

    def record_tool_result(self, step: int, result: ToolResult) -> None:
        self._record("tool_result", step, {"result": result.text, "success": result.success})

    def record_compaction(self, before: int, after: int, strategy: str) -> None:
        self._record("compaction", 0, {"tokens_before": before, "tokens_after": after, "strategy": strategy})

    def record_final_answer(self, answer: str) -> None:
        self._record("final_answer", 0, {"content": answer})

    def record_error(self, step: int, error_type: str, message: str) -> None:
        self._record("error", step, {"error_type": error_type, "message": message})

    async def end_session(self, result) -> TraceSession:
        if self._current_session:
            self._current_session.final_answer = getattr(result, "final_answer", None)
            self._current_session.total_tokens = getattr(result, "total_tokens", None) or self._current_session.total_tokens
            self._current_session.total_latency_ms = getattr(result, "total_latency_ms", 0)
            self._current_session.status = "finished"
            self._current_session.finished_at = datetime.now()
        return self._current_session

    async def stream_events(self) -> AsyncIterator[TraceEvent]:
        while True:
            yield await self._event_queue.get()
