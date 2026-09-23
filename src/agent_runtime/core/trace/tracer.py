"""执行追踪：把一次 ReAct 会话记成"逐事件日志 + 按步合并的步骤"两份视图，并（可选）落进 store。

**Phase 8 修复的缺口**：原先 `record_*` 只往 `asyncio.Queue` 里扔事件，`TraceSession.steps` 永远是空的 ——
Trace 页要的步骤树没有数据；而且会话跑完就随着对象一起消失，没有历史可查。

三条不变量：
  1. `record_*` 仍然推事件（WS 的实时流靠它），**行为向后兼容**：`Tracer()` 不传 store 一切照旧；
  2. 同一个 `step_number` 的 thought / tool_call / tool_result 合并成一条 `TraceStep`；
     一轮里可能有**多个**工具调用，所以 `tool_call`/`tool_result` 是列表式累加；
  3. 落库是**可选能力**：写失败只记 `last_store_error`，绝不让任务失败（与 memory/MCP 同一态度）。
"""
from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

from ..tool.base import ToolResult
from ..llm.types import TokenUsage
from .models import TraceEvent, TraceSession, TraceStep
from .store import TraceStore


class Tracer:
    def __init__(
        self,
        store: TraceStore | None = None,
        source: str = "chat",
        session_id: str = "",
    ) -> None:
        self._store = store
        self._source = (source or "chat").strip() or "chat"
        self._session_id = str(session_id or "")
        self._current_session: TraceSession | None = None
        self._event_queue: asyncio.Queue[TraceEvent] = asyncio.Queue()
        self.last_store_error: str = ""
        """最近一次落库失败的原因（可见，不静默）；为空 = 没出过错。"""

    @property
    def source(self) -> str:
        """这次追踪的来源（`chat` / `benchmark` / `demo`）。"""
        return self._source

    @property
    def session(self) -> TraceSession | None:
        return self._current_session

    async def start_session(self, task: str, config: dict | None = None) -> TraceSession:
        merged = dict(config or {})
        if self._session_id:
            merged.setdefault("session_id", self._session_id)
        self._current_session = TraceSession(
            trace_id=str(uuid.uuid4())[:8],
            task=task,
            config=merged,
            source=self._source,
        )
        return self._current_session

    # ── 记录 ──

    def _record(self, event_type: str, step: int, data: dict) -> TraceEvent:
        event = TraceEvent(event_type=event_type, step_number=step, data=data)
        self._event_queue.put_nowait(event)
        if self._current_session is not None:
            self._current_session.events.append(event)
        return event

    def _step(self, number: int) -> TraceStep | None:
        """取（或建）该步号的 `TraceStep`；没有会话时返回 `None`（记录不丢，只是不合并）。"""
        session = self._current_session
        if session is None:
            return None
        for step in session.steps:
            if step.step_number == number:
                return step
        step = TraceStep(step_number=number)
        session.steps.append(step)
        session.steps.sort(key=lambda item: item.step_number)
        return step

    def record_thought(
        self, step: int, content: str, usage: TokenUsage | None = None
    ) -> None:
        """记一步的思考，连带这一轮 LLM 调用的 token 用量。

        口径：**一步 = 一轮 LLM 调用**，所以那轮的 `usage` 就记在这一步上。不传就是"没测到"
        （保持默认 0）—— 别拿估算值糊上去，`None ≠ 0` 这条口径对每一步同样成立。
        """
        self._record("thought", step, {"content": content})
        target = self._step(step)
        if target is not None:
            target.thought = content
            if usage is not None:
                target.token_usage = usage

    def record_tool_call(self, step: int, tool: str, args: dict) -> None:
        payload = {"tool_name": tool, "args": args}
        self._record("tool_call", step, payload)
        target = self._step(step)
        if target is not None:
            target.tool_call = _append(target.tool_call, dict(payload))

    def record_tool_result(self, step: int, result: ToolResult) -> None:
        text = result.text or ""
        payload = {
            "tool_name": result.tool_name,
            "success": bool(result.success),
            "chars": len(text),
            "latency_ms": int(result.latency_ms or 0),
            "text": text,
        }
        self._record(
            "tool_result",
            step,
            {"success": payload["success"], "chars": payload["chars"], "tool": result.tool_name},
        )
        target = self._step(step)
        if target is not None:
            target.tool_result = _append(target.tool_result, payload)
            if payload["latency_ms"]:
                target.latency_ms = payload["latency_ms"]

    def record_compaction(self, before: int, after: int, strategy: str) -> None:
        self._record(
            "compaction",
            0,
            {"tokens_before": before, "tokens_after": after, "strategy": strategy},
        )

    def record_final_answer(self, answer: str) -> None:
        self._record("final_answer", 0, {"content": answer})
        if self._current_session is not None:
            self._current_session.final_answer = answer

    def record_error(self, step: int, error_type: str, message: str) -> None:
        # 错误只进事件日志（`steps` 里没有 error 字段，别为了塞它去改契约）
        self._record("error", step, {"error_type": error_type, "message": message})

    # ── 收尾 ──

    async def end_session(self, result: Any) -> TraceSession | None:
        session = self._current_session
        if session is None:
            return None
        session.final_answer = getattr(result, "final_answer", None) or session.final_answer
        session.total_tokens = (
            getattr(result, "total_tokens", None) or session.total_tokens
        )
        session.total_latency_ms = int(getattr(result, "total_latency_ms", 0) or 0)
        session.status = "finished"
        session.finished_at = datetime.now()
        self._save(session)
        return session

    def _save(self, session: TraceSession) -> None:
        """落库尽力而为：失败只记原因（存储是可选能力，不该影响任务结果）。"""
        if self._store is None:
            return
        try:
            self._store.save(session)
        except Exception as e:  # noqa: BLE001 —— 磁盘/DB 怎么坏都不能让任务失败
            self.last_store_error = f"{type(e).__name__}: {e}"

    async def stream_events(self) -> AsyncIterator[TraceEvent]:
        while True:
            yield await self._event_queue.get()


def _append(current: Any, item: dict) -> Any:
    """把一个工具调用/结果累加到该步上：第一个存对象，第二个起变成列表。"""
    if current is None:
        return item
    if isinstance(current, list):
        return [*current, item]
    return [current, item]
