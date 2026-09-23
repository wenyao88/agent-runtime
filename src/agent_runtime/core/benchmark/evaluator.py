"""规则评测（纯函数）：把一条任务的运行记录判成 `TaskVerdict`。

口径见 spec §5 与 `tests/unit/test_benchmark_evaluator.py` 顶部。三条要点：
  1. `success` 还要求**没有任何告警** —— 告警只有两种（所有工具都失败 / 撞到 max_steps 强制收尾），
     两者都意味着结果不可信；但工具是否调到要**分开表达**，别把失败原因混成一个 bool。
  2. 工具选择与参数命中看 `AgentStep.action`（带类型的 `ToolCall`），不看字符串；
     事件流只负责"成功/失败"这种它独有的信息。
  3. **绝不抛**：没有结果、steps 是垃圾、参数不是 dict，都只判成失败。
"""
from __future__ import annotations

from ..agent.base import AgentResult, AgentStep, ToolCall
from .models import BenchmarkTask, TaskRun, TaskVerdict


def _steps(result: AgentResult | None) -> list[AgentStep]:
    steps = getattr(result, "steps", None)
    if not isinstance(steps, list):
        return []
    return [s for s in steps if isinstance(s, AgentStep)]


def _called_tools(result: AgentResult | None) -> list[str]:
    return [s.action.tool_name for s in _steps(result) if isinstance(s.action, ToolCall)]


def _arguments(result: AgentResult | None, tool: str) -> list[dict]:
    calls: list[dict] = []
    for step in _steps(result):
        if isinstance(step.action, ToolCall) and step.action.tool_name == tool:
            args = step.action.arguments
            calls.append(args if isinstance(args, dict) else {})
    return calls


def evaluate(task: BenchmarkTask, run: TaskRun) -> TaskVerdict:
    """判一条任务。`success` 的三条判据缺一不可，且每条都能在 verdict 里单独看到。"""
    error = run.error or ""
    verdict = TaskVerdict(
        task_id=task.task_id,
        error=error,
        # 手工构造的 run 可能没标 error_kind：有错就当任务失败，别默认成 provider 抽风
        error_kind=run.error_kind or ("task" if error else ""),
        min_steps=task.min_steps,
    )
    result = run.result
    if result is None:
        # 任务根本没跑出结果（异常/中断）：必需工具与关键词当然一条都没兑现
        verdict.missing_tools = list(task.required_tools)
        verdict.missing_keywords = list(task.expected_keywords)
        return verdict

    used = _called_tools(result)
    verdict.missing_tools = [t for t in task.required_tools if t not in used]
    verdict.required_tools_ok = not verdict.missing_tools
    verdict.extra_tool_calls = sum(1 for t in used if t not in task.required_tools)

    answer = result.final_answer if isinstance(result.final_answer, str) else ""
    lowered = answer.lower()
    verdict.missing_keywords = [
        k for k in task.expected_keywords if str(k).lower() not in lowered
    ]
    verdict.keywords_ok = not verdict.missing_keywords

    for tool, spec in task.expected_args.items():
        if not isinstance(spec, dict):
            continue
        calls = _arguments(result, tool)
        for key, value in spec.items():
            verdict.arg_expected += 1
            wanted = str(value).lower()
            if any(str(call.get(key, "")).lower() == wanted for call in calls):
                verdict.arg_hits += 1

    verdict.warning = result.warning
    verdict.skills_used = list(result.skills_used or [])
    verdict.steps = len(_steps(result))
    verdict.total_tokens = int(getattr(result.total_tokens, "total_tokens", 0) or 0)
    verdict.latency_ms = int(result.total_latency_ms or 0)
    verdict.success = bool(
        verdict.required_tools_ok and verdict.keywords_ok and not result.warning
    )
    return verdict
