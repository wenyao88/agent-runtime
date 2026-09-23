"""ReAct Loop：单一代码路径。_execute() 是 async generator，逐步 yield AgentEvent；
run() 只消费事件流；run_stream() 原样转发。错误一律转 observation。

关于"模型编造"：光靠"证据进了上下文"是不够的 —— Demo 1 实测中，工具全部失败后
模型仍然凭自身知识写出了完整报告。因此这里做两件事：
  1. System Prompt 里写明硬规则：只能基于工具结果陈述，失败必须如实报告，禁止编造；
  2. 循环结束时若**所有工具调用都失败**，在 AgentResult.warning 与 FINAL_ANSWER 事件里
     明确告警 —— 把"不可采信"这件事变成用户可见的事实，而不是信任模型自觉。
"""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator

from ..context.manager import ContextManager
from ..llm.base import BaseLLMProvider
from ..llm.types import FunctionCall, Message, TokenUsage
from ..memory.base import MemoryEntry, MemoryQuery, normalize_session_id
from ..memory.manager import MemoryManager
from ..skill.router import SkillRouter
from ..trace.tracer import Tracer
from ..tool.base import ToolResult
from ..tool.registry import ToolRegistry
from .base import AgentResult, AgentStep, FinalAnswer, ToolCall
from .events import AgentEvent, AgentEventType
from .planner import TaskPlanner

DEFAULT_SYSTEM_PROMPT = (
    "You are a capable technical R&D agent. Think step by step, "
    "call tools when needed, then give the final answer. "
    "Reply in the user's language, concisely.\n"
    "HARD RULES about evidence:\n"
    "1. Every factual claim about the subject must come from tool results in this conversation. "
    "Do not use your own memory to fill in repository contents, file bodies, API signatures or citations.\n"
    "2. If a tool fails or returns an error, say so plainly and state what could not be obtained. "
    "Never fabricate tool output to cover a failure.\n"
    "3. If failures prevent completing the task, answer with what failed, its readable cause, "
    "and what the user should try next. A short honest report beats a complete-looking invented one.\n"
    "工具失败时必须如实说明失败原因与拿不到的信息；严禁依据自身知识编造工具结果、文件内容、"
    "仓库结构或引用来源。任务无法完成时，直接说明失败点与建议，不要为了凑完整而编造。"
)


class ReActLoop:
    def __init__(
        self,
        llm: BaseLLMProvider,
        tool_registry: ToolRegistry,
        context_manager: ContextManager,
        memory_manager: MemoryManager | None = None,
        skill_router: SkillRouter | None = None,
        planner: TaskPlanner | None = None,
        skill_top_k: int = 1,
        session_id: str = "",
        recall_top_k: int = 3,
        tracer: Tracer | None = None,
        max_steps: int = 15,
        tool_timeout: float = 30.0,
    ):
        self.llm = llm
        self.tools = tool_registry
        self.ctx = context_manager
        self.memory = memory_manager
        self.skills = skill_router
        self.planner = planner
        self.skill_top_k = skill_top_k
        self.session_id = session_id
        self.recall_top_k = recall_top_k
        self.tracer = tracer
        self.max_steps = max_steps
        self.tool_timeout = tool_timeout
        self.last_result: AgentResult | None = None

    # ---- public API ----
    async def run(self, task: str, session_id: str = "") -> AgentResult:
        async for _ in self._execute(task, session_id):
            pass
        return self.last_result

    async def run_stream(self, task: str, session_id: str = "") -> AsyncIterator[AgentEvent]:
        async for ev in self._execute(task, session_id):
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

    async def _execute(self, task: str, session_id: str = "") -> AsyncIterator[AgentEvent]:
        t_start = time.monotonic()
        total = TokenUsage()
        steps: list[AgentStep] = []
        trace_id = ""
        # 会话标识归一（空串 → default）：短时记忆按会话隔离，不补这条链路所有会话会挤在同一个键里
        sid = normalize_session_id(session_id or self.session_id)

        # 1) Skill 命中（纯计算，先做：trace session 一开始就能带上 skills）
        matched = self.skills.match(task, self.skill_top_k) if self.skills else []
        skills_used = [s.manifest.name for s in matched]

        if self.tracer:
            session = await self.tracer.start_session(
                task, {"max_steps": self.max_steps, "skills": skills_used}
            )
            trace_id = session.trace_id

        # 2) 技能文本 + 计划文本：**只能追加**在硬规则之后（顺序有测试钉死）
        sys_extra = ""
        if matched:
            sys_extra = "\n\n" + "\n\n".join(s.build_prompt_extension(task) for s in matched)
        if self.planner:
            plan_text = await self.planner.plan(task, self.tools.get_names())
            if plan_text:
                sys_extra += "\n\n## 执行计划（仅供参考，可按实际情况调整）\n" + plan_text
        # 3) 可选 Memory 召回（带会话标识：短时记忆按会话隔离）
        memories = []
        if self.memory:
            memories = await self.memory.recall(
                MemoryQuery(text=task, top_k=self.recall_top_k, session_id=sid)
            )

        await self.ctx.build(
            task=task, tools=self.tools.list_schemas(), memory_entries=memories,
            system_prompt=DEFAULT_SYSTEM_PROMPT + sys_extra,
        )

        if matched:
            yield AgentEvent(AgentEventType.SKILL_MATCHED, {"skills": skills_used})

        answered = False
        answer = ""
        final_step = 0
        tool_calls_total = 0
        tool_calls_failed = 0

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
                # 最终答案统一在循环之后发出：那时才知道"工具是否全部失败"，
                # 才能把告警一并带给用户，而不是让它以为这是可信结论。
                answer = resp.content or ""
                answered = True
                break

            # assistant tool_calls 消息必须先入 context，再逐个回 tool 消息
            self.ctx.append(Message(role="assistant", content=resp.content,
                                    tool_calls=resp.tool_calls))
            for tc in resp.tool_calls:
                tool_calls_total += 1
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
                if not result.success:
                    tool_calls_failed += 1
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
            # 压缩必须发生在**整批 tool 结果都入 context 之后**：压缩可能丢弃带 `tool_calls`
            # 的 assistant 消息，若还在逐个追加 tool 结果，后面追加的那些就变成孤儿 tool 消息
            # （协议要求 tool 紧跟 tool_calls），下一次 llm.chat() 会被端点直接拒绝。
            if self.ctx.should_compact():
                cr = await self.ctx.compact()
                if self.tracer:
                    self.tracer.record_compaction(cr.tokens_before, cr.tokens_after,
                                                  cr.strategy.value)
                yield AgentEvent(AgentEventType.COMPACTION, {
                    "before": cr.tokens_before, "after": cr.tokens_after,
                    "strategy": cr.strategy.value,
                    # 降级原因与摘要条数必须发出去：只写进 CompactionResult 的话，
                    # CLI / WS / 前端完全看不到"想摘要却没做成"（展示层藏机制是老坑）。
                    "degraded_from": (
                        cr.degraded_from.value if cr.degraded_from else None
                    ),
                    "reason": cr.degraded_reason,
                    "summarized": cr.summarized_messages,
                    # 什么都没改变也要如实报（否则"压不动"看起来像一次成功的压缩）
                    "noop": cr.noop})

        warnings: list[str] = []
        if tool_calls_total and tool_calls_failed == tool_calls_total:
            warnings.append(
                f"所有工具调用都失败了（{tool_calls_failed}/{tool_calls_total}）："
                "最终答案没有建立在真实工具结果之上，请勿直接采信"
            )

        if not answered:
            warnings.append(f"max_steps({self.max_steps}) reached; forced final answer")
            resp = await self.llm.chat(self.ctx.get_messages(), tools=None)
            answer = resp.content or ""
            total.prompt_tokens += resp.token_usage.prompt_tokens
            total.completion_tokens += resp.token_usage.completion_tokens
            total.total_tokens += resp.token_usage.total_tokens
            steps.append(AgentStep(step_number=final_step, thought="(forced)",
                                   action=FinalAnswer(content=answer)))
        else:
            steps.append(AgentStep(step_number=final_step, thought=thought,
                                   action=FinalAnswer(content=answer)))

        warning = "；".join(warnings) if warnings else None
        if self.tracer:
            self.tracer.record_final_answer(answer)
        yield AgentEvent(AgentEventType.FINAL_ANSWER, {"content": answer, "warning": warning})

        total_latency = int((time.monotonic() - t_start) * 1000)
        self.last_result = AgentResult(
            task=task, final_answer=answer, steps=steps, total_tokens=total,
            total_latency_ms=total_latency, trace_id=trace_id, warning=warning,
            skills_used=skills_used,
        )
        if self.memory:
            await self.memory.store(MemoryEntry(
                content=f"task: {task}\nanswer: {answer[:500]}",
                role="agent", metadata={"type": "task_summary", "session_id": sid}))
        if self.tracer:
            await self.tracer.end_session(self.last_result)
