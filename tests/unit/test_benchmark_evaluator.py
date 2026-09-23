"""规则评测契约（`core/benchmark/evaluator.py`）。

口径必须与 spec §5 完全一致 —— 指标数字是拿去给别人看的，口径含糊等于数字无效：

* `success` = required_tools 全覆盖 **且** expected_keywords 全命中 **且** **没有任何告警**
  （告警只有两种：所有工具都失败 / 撞到 max_steps 强制收尾 —— 两者都意味着结果不可信）。
* 工具选择/参数命中都看 `AgentStep.action`（带类型的 ToolCall），不看字符串；
  事件流只负责"成功/失败"这种它独有的信息。
* `min_steps` **只记录不判分**（它与 required_tools 数量强相关，当门槛会惩罚把多个工具合并到一轮的高效 Agent）。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from agent_runtime.core.agent.base import (  # noqa: E402
    AgentResult,
    AgentStep,
    FinalAnswer,
    ToolCall,
)
from agent_runtime.core.benchmark.evaluator import evaluate  # noqa: E402
from agent_runtime.core.benchmark.models import BenchmarkTask, TaskRun  # noqa: E402
from agent_runtime.core.llm.types import TokenUsage  # noqa: E402


def _tool_step(n: int, tool: str, args: dict | None = None) -> AgentStep:
    return AgentStep(
        step_number=n, thought="t", action=ToolCall(tool_name=tool, arguments=args or {}),
        observation="obs",
    )


def _final_step(n: int, answer: str) -> AgentStep:
    return AgentStep(step_number=n, thought="t", action=FinalAnswer(content=answer))


def _result(
    steps: list[AgentStep],
    *,
    answer: str = "答案",
    warning: str | None = None,
    tokens: int = 100,
    latency: int = 10,
    skills: list[str] | None = None,
) -> AgentResult:
    return AgentResult(
        task="t",
        final_answer=answer,
        steps=steps,
        total_tokens=TokenUsage(total_tokens=tokens),
        total_latency_ms=latency,
        warning=warning,
        skills_used=list(skills or []),
    )


def _task(**overrides: object) -> BenchmarkTask:
    base: dict = {
        "task_id": "t1",
        "task": "读一个文件并总结",
        "required_tools": ["read_file"],
        "expected_keywords": ["答案"],
    }
    base.update(overrides)
    return BenchmarkTask(**base)  # type: ignore[arg-type]


def _run(task: BenchmarkTask, result: AgentResult | None) -> TaskRun:
    return TaskRun(task=task, result=result)


# ── 成功判据 ──


def test_success_needs_tools_keywords_and_no_warning() -> None:
    task = _task()
    run = _run(task, _result([_tool_step(1, "read_file"), _final_step(2, "这是答案")]))
    verdict = evaluate(task, run)
    assert verdict.success is True
    assert verdict.required_tools_ok and verdict.keywords_ok
    assert verdict.missing_tools == [] and verdict.missing_keywords == []


def test_missing_tools_are_listed_and_fail_the_task() -> None:
    task = _task(required_tools=["read_file", "web_search"])
    run = _run(task, _result([_tool_step(1, "read_file"), _final_step(2, "答案")]))
    verdict = evaluate(task, run)
    assert verdict.missing_tools == ["web_search"]
    assert verdict.required_tools_ok is False
    assert verdict.success is False


def test_keywords_are_case_insensitive_substrings_of_the_answer() -> None:
    task = _task(expected_keywords=["flask", "路由"])
    run = _run(task, _result([_tool_step(1, "read_file")], answer="这个 Flask 项目用装饰器做路由"))
    verdict = evaluate(task, run)
    assert verdict.keywords_ok is True
    assert verdict.missing_keywords == []

    run2 = _run(task, _result([_tool_step(1, "read_file")], answer="这个项目用装饰器做路由"))
    verdict2 = evaluate(task, run2)
    assert verdict2.missing_keywords == ["flask"]
    assert verdict2.success is False


def test_any_warning_makes_the_task_unsuccessful() -> None:
    """两种告警都意味着结果不可信：全工具失败 / 撞到 max_steps 强制收尾。"""
    task = _task()
    for warning in ("所有工具调用都失败了（1/1）：…", "max_steps(3) reached; forced final answer"):
        verdict = evaluate(task, _run(task, _result([_tool_step(1, "read_file")], warning=warning)))
        assert verdict.success is False, warning
        assert verdict.warning == warning
        assert verdict.required_tools_ok is True, "工具确实调到了，失败原因要如实分开表达"


# ── 参数与多余调用 ──


def test_argument_hits_count_only_matching_values() -> None:
    task = _task(
        required_tools=["github_get_repo"],
        expected_args={"github_get_repo": {"repo": "pallets/flask"}},
    )
    good = _run(task, _result([_tool_step(1, "github_get_repo", {"repo": "Pallets/Flask"})]))
    verdict = evaluate(task, good)
    assert (verdict.arg_hits, verdict.arg_expected) == (1, 1), "大小写不同也算命中"

    bad = _run(task, _result([_tool_step(1, "github_get_repo", {"repo": "django/django"})]))
    verdict2 = evaluate(task, bad)
    assert (verdict2.arg_hits, verdict2.arg_expected) == (0, 1)


def test_argument_accuracy_denominator_counts_every_declared_pair() -> None:
    task = _task(
        expected_args={"github_get_repo": {"repo": "a/b", "ref": "main"}},
        required_tools=["github_get_repo"],
    )
    run = _run(task, _result([_tool_step(1, "github_get_repo", {"repo": "a/b"})]))
    verdict = evaluate(task, run)
    assert (verdict.arg_hits, verdict.arg_expected) == (1, 2)


def test_extra_tool_calls_counts_tools_outside_required() -> None:
    task = _task(required_tools=["read_file"])
    run = _run(
        task,
        _result(
            [
                _tool_step(1, "read_file"),
                _tool_step(2, "web_search"),
                _tool_step(3, "web_scrape"),
                _final_step(4, "答案"),
            ]
        ),
    )
    verdict = evaluate(task, run)
    assert verdict.extra_tool_calls == 2
    assert verdict.missing_tools == []


# ── 健壮性 ──


def test_no_result_is_a_failure_without_raising() -> None:
    task = _task()
    run = TaskRun(task=task, error="RuntimeError: boom")
    verdict = evaluate(task, run)
    assert verdict.success is False
    assert verdict.error == "RuntimeError: boom"
    assert verdict.missing_tools == ["read_file"], "没跑成的任务，必需工具当然一条都没调"


def test_junk_steps_never_raise() -> None:
    task = _task(required_tools=["read_file"], expected_keywords=["答案"])
    messy = AgentResult(
        task="t",
        final_answer=None,  # type: ignore[arg-type]
        steps=[
            AgentStep(step_number=1, thought="", action=None),
            AgentStep(step_number=2, thought="", action=FinalAnswer(content="")),
        ],
    )
    verdict = evaluate(task, _run(task, messy))
    assert verdict.success is False
    assert verdict.missing_keywords == ["答案"]


def test_verdict_records_steps_tokens_latency_and_skills() -> None:
    task = _task(min_steps=3)
    run = _run(
        task,
        _result(
            [_tool_step(1, "read_file"), _final_step(2, "答案")],
            tokens=321,
            latency=45,
            skills=["github_analysis"],
        ),
    )
    verdict = evaluate(task, run)
    assert verdict.steps == 2
    assert verdict.total_tokens == 321
    assert verdict.latency_ms == 45
    assert verdict.skills_used == ["github_analysis"]
    assert verdict.min_steps == 3, "min_steps 只记录不判分（口径见 spec §5 说明）"


def _run_all() -> None:
    failed: list[str] = []
    tests = [
        v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)
    ]
    for t in tests:
        print(f"RUN  {t.__name__}")
        try:
            t()
        except Exception as e:  # noqa: BLE001
            print(f"FAIL {t.__name__}: {type(e).__name__}: {e}")
            failed.append(t.__name__)
        else:
            print(f"PASS {t.__name__}")
    if failed:
        raise SystemExit(f"FAILED: {', '.join(failed)}")
    print("ALL PASS")


if __name__ == "__main__":
    _run_all()
