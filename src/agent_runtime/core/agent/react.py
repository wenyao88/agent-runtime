"""ReAct Loop：单一代码路径。_execute() 是 async generator，逐步 yield AgentEvent；
run() 只消费事件流；run_stream() 原样转发。错误一律转 observation。"""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator

from ..context.manager import ContextManager
from ..llm.base import BaseLLMProvider
from ..llm.types import FunctionCall, Message, TokenUsage
from ..memory.base import MemoryEntry, MemoryQuery
from ..memory.manager import MemoryManager
from ..skill.router import SkillRouter
from ..trace.tracer import Tracer
from ..tool.base import ToolResult
from ..tool.registry import ToolRegistry
from .base import AgentResult, AgentStep, FinalAnswer, ToolCall
from .events import AgentEvent, AgentEventType

DEFAULT_SYSTEM_PROMPT = (
    "You are a capable technical R&D agent. Think step by step, "
    "call tools when needed, then give the final answer. "
    "Reply in the user's language, concisely."
)


class ReActLoop:
    def __init__(
        self,
        llm: BaseLLMProvider,
        tool_registry: ToolRegistry,
        context_manager: ContextManager,
        memory_manager: MemoryManager | None = None,
        skill_router: SkillRouter | None = None,
        tracer: Tracer | None = None,
        max_steps: int = 15,
        tool_timeout: float = 30.0,
    ):
        self.llm = llm
        self.tools = tool_registry
        self.ctx = context_manager
        self.memory = memory_manager
        self.skills = skill_router
        self.tracer = tracer
        self.max_steps = max_steps
        self.tool_timeout = tool_timeout
        self.last_result: AgentResult | None = None

    # ---- public API ----
    async def run(self, task: str) -> AgentResult:
        async for _ in self._execute(task):
            pass
        return self.last_result

    async def run_stream(self, task: str) -> AsyncIterator[AgentEvent]:
        async for ev in self._execute(task):
            yield ev

    # ---- internals ----
    @staticmethod
    def _parse_args(raw: str) -> dict | None:
        try:
            v = json.loads(raw or "{}")
            return v if isinstance(v, dict) else None
        except (json.JSONDecodeError, TypeError):
            return None

    async def _exec_tool(self, tc: FunctionCall, args: dict) -> ToolResult:
        tool = self.tools.get(tc.name)
        if tool is None:
            available = ", ".join(self.tools.get_names()) or "(none)"
            return ToolResult(tool_name=tc.name, success=False,
                              text=f"Error: tool '{tc.name}' not found. Available tools: {available}")
        t0 = time.monotonic()
        try:
            r = await asyncio.wait_for(tool.execute(**args), timeout=self.tool_timeout)
            r.latency_ms = int((time.monotonic() - t0) * 1000)
            return r
        except asyncio.TimeoutError:
            return ToolResult(tool_name=tc.name, success=False,
                              text=f"Error: tool '{tc.name}' timed out after {self.tool_timeout}s")
        except Exception as e:  # noqa: BLE001 — by design: error becomes observation
            return ToolResult(tool_name=tc.name, success=False,
                              text=f"Error: {type(e).__name__}: {e}")

    async def _execute(self, task: str) -> AsyncIterator[AgentEvent]:
        t_start = time.monotonic()
        total = TokenUsage()
        steps: list[AgentStep] = []
        warning: str | None = None
        trace_id = ""

        if self.tracer:
            session = await self.tracer.start_session(task, {"max_steps": self.max_steps})
            trace_id = session.trace_id

        # 1) 可选 Skill 注入
        sys_extra = ""
        matched = self.skills.match(task) if self.skills else []
        if matched:
            sys_extra = "\n\n".join(s.build_prompt_extension(task) for s in matched)
        # 2) 可选 Memory 召回
        memories = []
        if self.memory:
            memories = await self.memory.recall(MemoryQuery(text=task, top_k=3))

        await self.ctx.build(
            task=task, tools=self.tools.list_schemas(), memory_entries=memories,
            skills=matched, system_prompt=DEFAULT_SYSTEM_PROMPT + sys_extra,
        )

        answered = False
        answer = ""
        final_step = 0

        for n in range(1, self.max_steps + 1):
            final_step = n
            yield AgentEvent(AgentEventType.STEP_START, {"step": n})
            resp = await self.llm.chat(self.ctx.get_messages(), tools=self.tools.list_schemas())
            total.prompt_tokens += resp.token_usage.prompt_tokens
            total.completion_tokens += resp.token_usage.completion_tokens
            total.total_tokens += resp.token_usage.total_tokens or (
                resp.token_usage.prompt_tokens + resp.token_usage.completion_tokens)

            thought = resp.content or ""
            if self.tracer:
                self.tracer.record_thought(n, thought)
            yield AgentEvent(AgentEventType.THOUGHT, {"step": n, "content": thought})

            if not resp.tool_calls:
                answer = resp.content or ""
                steps.append(AgentStep(step_number=n, thought=thought,
                                       action=FinalAnswer(content=answer)))
                if self.tracer:
                    self.tracer.record_final_answer(answer)
                yield AgentEvent(AgentEventType.FINAL_ANSWER, {"content": answer})
                answered = True
                break

            # assistant tool_calls 消息必须先入 context，再逐个回 tool 消息
            self.ctx.append(Message(role="assistant", content=resp.content,
                                    tool_calls=resp.tool_calls))
            for tc in resp.tool_calls:
                args = self._parse_args(tc.arguments)
                if self.tracer:
                    self.tracer.record_tool_call(n, tc.name, tc.arguments)
                yield AgentEvent(AgentEventType.TOOL_CALL, {"step": n, "tool": tc.name,
                                                            "args": tc.arguments})
                if args is None:
                    result = ToolResult(tool_name=tc.name, success=False,
                                        text=f"Error: arguments of '{tc.name}' is not valid JSON object: {tc.arguments!r}")
                else:
                    result = await self._exec_tool(tc, args)
                obs_text = result.text  # 错误同样可读——成功/失败都直接用 result.text
                self.ctx.append(Message(role="tool", content=obs_text, tool_call_id=tc.id))
                if self.tracer:
                    self.tracer.record_tool_result(n, result)
                yield AgentEvent(AgentEventType.TOOL_RESULT, {
                    "step": n, "tool": tc.name, "success": result.success,
                    "result": obs_text[:2000], "latency_ms": result.latency_ms})
                steps.append(AgentStep(step_number=n, thought=thought,
                                       action=ToolCall(tool_name=tc.name, arguments=args or {}),
                                       observation=obs_text[:2000]))
                if self.ctx.should_compact():
                    cr = await self.ctx.compact()
                    if self.tracer:
                        self.tracer.record_compaction(cr.tokens_before, cr.tokens_after,
                                                      cr.strategy.value)
                    yield AgentEvent(AgentEventType.COMPACTION, {
                        "before": cr.tokens_before, "after": cr.tokens_after,
                        "strategy": cr.strategy.value})

        if not answered:
            warning = f"max_steps({self.max_steps}) reached; forced final answer"
            resp = await self.llm.chat(self.ctx.get_messages(), tools=None)
            answer = resp.content or ""
            total.prompt_tokens += resp.token_usage.prompt_tokens
            total.completion_tokens += resp.token_usage.completion_tokens
            total.total_tokens += resp.token_usage.total_tokens
            steps.append(AgentStep(step_number=final_step, thought="(forced)",
                                   action=FinalAnswer(content=answer)))
            if self.tracer:
                self.tracer.record_final_answer(answer)
            yield AgentEvent(AgentEventType.FINAL_ANSWER, {"content": answer, "warning": warning})

        total_latency = int((time.monotonic() - t_start) * 1000)
        self.last_result = AgentResult(
            task=task, final_answer=answer, steps=steps, total_tokens=total,
            total_latency_ms=total_latency, trace_id=trace_id, warning=warning,
        )
        if self.memory:
            await self.memory.store(MemoryEntry(
                content=f"task: {task}\nanswer: {answer[:500]}",
                role="agent", metadata={"type": "task_summary"}))
        if self.tracer:
            await self.tracer.end_session(self.last_result)
