"""离线假 agent（`--provider mock`）：确定性地把一条任务的完整事件流走一遍。

它**不是**模型：不调 API、不需要 key、不 import 任何第三方。存在的唯一目的是让
"跑任务 → 事件 → 判分 → 指标 → 报告"这条管线在没有依赖的环境里也能完整跑通并断言 ——
因此报告里 `provider` 必须是 `mock`，绝不能被当成真实成绩。

[这是夹具，不是测量] 它发出的 `compaction` 事件是**合成的**（固定 1000 → 250），
目的是让"压缩比"这个指标也有非空值；工具参数直接取任务声明的 `expected_args`，
所以 mock 跑出来的参数命中率必然是 1.0 —— 真实成绩只能由真实 provider 跑出来。
"""
from __future__ import annotations

from ...core.agent.base import AgentResult, AgentStep, FinalAnswer, ToolCall
from ...core.agent.events import AgentEvent, AgentEventType
from ...core.benchmark.models import BenchmarkTask
from ...core.llm.types import TokenUsage

_TOKENS_PER_STEP = 120
_LATENCY_PER_STEP_MS = 5
_FAKE_BEFORE = 1000
_FAKE_AFTER = 250


class MockBenchmarkAgent:
    """可作为 agent 工厂直接使用：`MockBenchmarkAgent(task)`。"""

    def __init__(self, task: BenchmarkTask) -> None:
        self._task = task
        self.last_result: AgentResult | None = None

    async def run_stream(self, task: str, session_id: str = ""):
        steps: list[AgentStep] = []
        for index, tool in enumerate(self._task.required_tools, start=1):
            arguments = dict(self._task.expected_args.get(tool) or {})
            yield AgentEvent(
                AgentEventType.TOOL_CALL, {"step": index, "tool": tool, "args": "{}"}
            )
            yield AgentEvent(
                AgentEventType.TOOL_RESULT,
                {
                    "step": index,
                    "tool": tool,
                    "success": True,
                    "result": f"{tool} 的 mock 结果",
                    "latency_ms": _LATENCY_PER_STEP_MS,
                },
            )
            steps.append(
                AgentStep(
                    step_number=index,
                    thought=f"调用 {tool}",
                    action=ToolCall(tool_name=tool, arguments=arguments),
                    observation="mock 结果",
                    latency_ms=_LATENCY_PER_STEP_MS,
                )
            )

        yield AgentEvent(
            AgentEventType.COMPACTION,
            {
                "before": _FAKE_BEFORE,
                "after": _FAKE_AFTER,
                "strategy": "truncate",
                "summarized": 0,
                "degraded_from": None,
                "reason": "",
                "noop": False,
            },
        )

        # 最终答案带上任务原文：任务集的关键词都取自任务文本，于是关键词判据也能命中
        answer = f"{task}\n\n（mock 报告）已按任务要求给出结论与要点。"
        steps.append(
            AgentStep(
                step_number=len(steps) + 1,
                thought="整理成报告",
                action=FinalAnswer(content=answer),
            )
        )
        self.last_result = AgentResult(
            task=task,
            final_answer=answer,
            steps=steps,
            total_tokens=TokenUsage(total_tokens=_TOKENS_PER_STEP * len(steps)),
            total_latency_ms=_LATENCY_PER_STEP_MS * len(steps),
            trace_id="mock",
        )
        yield AgentEvent(AgentEventType.FINAL_ANSWER, {"content": answer})
