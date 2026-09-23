"""Runner 契约（`core/benchmark/runner.py`）。

runner 是**纯编排**：注入 agent 工厂 + 用 `run_stream` 观察事件，所以沙箱里用假 agent 就能验证整条管线。

三条必须守住的行为：
  1. **单条任务失败不能中断整轮评测**（一条任务炸了就整批没结果，等于没有评测）；
  2. 每条任务都用**新 agent**（复用 agent 会让上一条任务的上下文/记忆污染下一条的成绩）；
  3. 只收集它需要的事件（`tool_result` 用于成功/失败，`compaction` 用于压缩比），不去猜别的。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from agent_runtime.core.agent.base import AgentResult, AgentStep, FinalAnswer, ToolCall  # noqa: E402
from agent_runtime.core.agent.events import AgentEvent, AgentEventType  # noqa: E402
from agent_runtime.core.benchmark.models import BenchmarkTask  # noqa: E402
from agent_runtime.core.benchmark.runner import BenchmarkRunner  # noqa: E402
from agent_runtime.core.llm.types import TokenUsage  # noqa: E402


def _task(task_id: str, required: list[str] | None = None) -> BenchmarkTask:
    return BenchmarkTask(
        task_id=task_id,
        task=f"做事 {task_id}",
        required_tools=required or ["read_file"],
        expected_keywords=["答案"],
    )


def _result(answer: str = "答案", tools: tuple[str, ...] = ("read_file",)) -> AgentResult:
    steps = [
        AgentStep(step_number=i + 1, thought="t", action=ToolCall(tool_name=name, arguments={}))
        for i, name in enumerate(tools)
    ]
    steps.append(AgentStep(step_number=len(steps) + 1, thought="t", action=FinalAnswer(content=answer)))
    return AgentResult(
        task="t",
        final_answer=answer,
        steps=steps,
        total_tokens=TokenUsage(total_tokens=42),
        total_latency_ms=7,
    )


def _events(*, tool_result: bool = True, compaction: bool = False) -> list[AgentEvent]:
    out = [AgentEvent(AgentEventType.STEP_START, {"step": 1})]
    if tool_result:
        out.append(
            AgentEvent(
                AgentEventType.TOOL_RESULT,
                {"step": 1, "tool": "read_file", "success": True, "result": "内容", "latency_ms": 3},
            )
        )
    if compaction:
        out.append(
            AgentEvent(
                AgentEventType.COMPACTION,
                {
                    "before": 1000, "after": 400, "strategy": "truncate",
                    "summarized": 0, "degraded_from": None, "reason": "", "noop": False,
                },
            )
        )
    return out


class _FakeAgent:
    def __init__(self, events: list[AgentEvent], result: AgentResult | None) -> None:
        self._events = events
        self.last_result = result

    async def run_stream(self, task: str, session_id: str = ""):
        for event in self._events:
            yield event


def _factory(mapping: dict[str, _FakeAgent], built: list[str] | None = None):
    def factory(task: BenchmarkTask):
        # 用"还有哪些任务没跑"来挑 agent：调用顺序与任务顺序一致
        index = len(built) if built is not None else 0
        keys = list(mapping)
        if built is not None:
            built.append(keys[index])
        return mapping[keys[index]]

    return factory


# ── 基本管线 ──


def test_runner_runs_tasks_and_builds_a_report() -> None:
    tasks = [_task("a"), _task("b")]
    mapping = {"a": _FakeAgent(_events(compaction=True), _result()), "b": _FakeAgent(_events(), _result())}
    built: list[str] = []
    runner = BenchmarkRunner(_factory(mapping, built), run_id_factory=lambda cfg: "run-x")

    report = asyncio.run(
        runner.run(tasks, config={"provider": "mock"}, on_progress=None)
    )

    assert report.run_id == "run-x"
    assert report.config["provider"] == "mock"
    assert report.config["tasks_total"] == 2
    assert len(report.verdicts) == 2
    assert [v.task_id for v in report.verdicts] == ["a", "b"]
    assert all(v.success for v in report.verdicts)
    assert report.metrics.tasks_total == 2
    assert report.metrics.success_rate == 1.0
    assert report.metrics.compression_ratio == 0.6, "压缩事件只出现在第一条任务上"
    assert built == ["a", "b"], "每条任务都要新建 agent"


def test_irrelevant_events_are_ignored() -> None:
    tasks = [_task("a", required=[])]
    agent = _FakeAgent(_events(tool_result=False), _result(tools=()))
    report = asyncio.run(
        BenchmarkRunner(lambda task: agent, run_id_factory=lambda cfg: "r").run(tasks)
    )
    assert report.metrics.compression_ratio is None
    assert report.metrics.error_recovery_rate is None


def test_limit_caps_the_number_of_tasks() -> None:
    tasks = [_task("a"), _task("b"), _task("c")]
    mapping = {t: _FakeAgent(_events(), _result()) for t in ("a", "b", "c")}
    report = asyncio.run(
        BenchmarkRunner(_factory(mapping, []), run_id_factory=lambda cfg: "r").run(
            tasks, config={"provider": "mock"}, limit=2
        )
    )
    assert [v.task_id for v in report.verdicts] == ["a", "b"]
    assert report.config["limit"] == 2
    assert report.config["tasks_total"] == 2


# ── 健壮性 ──


def test_a_failing_task_does_not_stop_the_run() -> None:
    tasks = [_task("a"), _task("boom"), _task("c")]
    calls = {"n": 0}

    def factory(task: BenchmarkTask):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("provider 挂了")
        return _FakeAgent(_events(), _result())

    report = asyncio.run(BenchmarkRunner(factory, run_id_factory=lambda cfg: "r").run(tasks))
    assert [v.task_id for v in report.verdicts] == ["a", "boom", "c"]
    boom = next(v for v in report.verdicts if v.task_id == "boom")
    assert boom.success is False
    assert "provider 挂了" in boom.error
    assert report.metrics.tasks_total == 3
    assert report.metrics.success_rate == 2 / 3


def test_a_task_without_a_result_is_marked_failed() -> None:
    agent = _FakeAgent([], None)
    report = asyncio.run(
        BenchmarkRunner(lambda task: agent, run_id_factory=lambda cfg: "r").run([_task("a")])
    )
    verdict = report.verdicts[0]
    assert verdict.success is False
    assert "AgentResult" in verdict.error


# ── 进度与裁判 ──


def test_on_progress_is_called_for_every_task() -> None:
    tasks = [_task("a"), _task("b")]
    mapping = {t: _FakeAgent(_events(), _result()) for t in ("a", "b")}
    seen: list[tuple[int, int, str]] = []
    asyncio.run(
        BenchmarkRunner(_factory(mapping, []), run_id_factory=lambda cfg: "r").run(
            tasks, on_progress=lambda done, total, verdict: seen.append((done, total, verdict.task_id))
        )
    )
    assert seen == [(1, 2, "a"), (2, 2, "b")]


def test_judge_is_sampled_and_optional() -> None:
    tasks = [_task("a"), _task("b")]
    mapping = {t: _FakeAgent(_events(), _result()) for t in ("a", "b")}
    calls: list[str] = []

    async def judge(task: BenchmarkTask, answer: str):
        calls.append(task.task_id)
        return {"completion": 5, "accuracy": 4}

    report = asyncio.run(
        BenchmarkRunner(_factory(mapping, []), judge=judge, run_id_factory=lambda cfg: "r").run(
            tasks, config={"judge": 1}
        )
    )
    assert calls == ["a"], "只采样配置的条数"
    assert report.verdicts[0].judge_scores == {"completion": 5, "accuracy": 4}
    assert report.verdicts[1].judge_scores is None
    assert report.metrics.judged_tasks == 1
    assert report.metrics.avg_judge_score == 4.5

    # 不给裁判时零调用
    no_judge = asyncio.run(
        BenchmarkRunner(_factory(mapping, []), run_id_factory=lambda cfg: "r").run(tasks)
    )
    assert all(v.judge_scores is None for v in no_judge.verdicts)
    assert no_judge.metrics.judged_tasks == 0


def test_a_judge_failure_marks_the_task_unjudged_without_stopping() -> None:
    async def bad_judge(task: BenchmarkTask, answer: str):
        raise TimeoutError("judge 超时")

    report = asyncio.run(
        BenchmarkRunner(
            lambda task: _FakeAgent(_events(), _result()), judge=bad_judge, run_id_factory=lambda cfg: "r"
        ).run([_task("a")], config={"judge": 1})
    )
    verdict = report.verdicts[0]
    assert verdict.judge_scores is None, "判分失败不能记 0 分"
    assert "TimeoutError" in verdict.judge_reason
    assert verdict.success is True, "裁判失败不影响规则判分"


def test_judge_returning_junk_is_reported_not_scored() -> None:
    async def junk_judge(task: BenchmarkTask, answer: str):
        return None

    report = asyncio.run(
        BenchmarkRunner(
            lambda task: _FakeAgent(_events(), _result()), judge=junk_judge, run_id_factory=lambda cfg: "r"
        ).run([_task("a")], config={"judge": 1})
    )
    assert report.verdicts[0].judge_scores is None
    assert "没有返回可用分数" in report.verdicts[0].judge_reason


def test_each_task_gets_its_own_memory_session() -> None:
    """回归（审查 I1）：不传 session_id 会全部落到 `default`。

    memory 开着时（`get_memory_manager()` 是进程单例、working 层按会话累积），
    上一条任务的记忆会被下一条召回 —— "逐任务隔离"就成了空话。
    """
    seen: list[str] = []

    class _Agent:
        last_result = _result()

        async def run_stream(self, task: str, session_id: str = ""):
            seen.append(session_id)
            if False:
                yield None

    asyncio.run(
        BenchmarkRunner(lambda task: _Agent(), run_id_factory=lambda cfg: "run-7").run(
            [_task("a"), _task("b")]
        )
    )
    assert seen == ["bench-run-7-a", "bench-run-7-b"], seen


def test_judge_scores_are_validated_at_the_runner_boundary() -> None:
    """回归（审查 I5）：自定义 judge 返回 bool / 越界分数时，runner 边界也要拦住。

    `bool` 是 `int` 的子类，`True` 会被算成 1 分；99 分更是直接把均分拉爆。
    """

    async def sloppy(task: BenchmarkTask, answer: str):
        return {"completion": True, "accuracy": 99, "citation": 3}

    report = asyncio.run(
        BenchmarkRunner(
            lambda task: _FakeAgent([], _result()),
            judge=sloppy,
            run_id_factory=lambda cfg: "r",
        ).run([_task("a")], config={"judge": 1})
    )
    assert report.verdicts[0].judge_scores == {"citation": 3}
    assert report.metrics.avg_judge_score == 3.0


def test_a_raising_evaluator_does_not_abort_the_run() -> None:
    def boom(task: BenchmarkTask, run) -> None:
        raise RuntimeError("评测器炸了")

    runner = BenchmarkRunner(
        lambda task: _FakeAgent([], _result()), evaluator=boom, run_id_factory=lambda cfg: "r"
    )
    report = asyncio.run(runner.run([_task("a"), _task("b")]))
    assert [v.task_id for v in report.verdicts] == ["a", "b"]
    assert all("评测器异常" in v.error for v in report.verdicts), [
        v.error for v in report.verdicts
    ]


def test_a_rate_limit_error_is_marked_as_a_provider_error() -> None:
    """回归（开跑前检查 D）：限流/超时是 **provider 抽风**，不是 agent 做错了。

    不分开统计，三组的成功率就是被限流噪声污染的数字（300 次真实调用必然撞限流）。
    """
    rate_limit = type("RateLimitError", (Exception,), {})

    def factory(task: BenchmarkTask):
        raise rate_limit("rate limit reached for RPM")

    report = asyncio.run(BenchmarkRunner(factory, run_id_factory=lambda cfg: "r").run([_task("a")]))
    verdict = report.verdicts[0]
    assert verdict.error_kind == "provider"
    assert report.metrics.provider_errors == 1
    assert report.metrics.success_rate == 0.0, "原口径不变：没跑出结果的算失败"
    assert report.metrics.success_rate_measured is None, "排除 provider 抽风后分母为 0 → 没测"


def test_an_agent_bug_is_not_blamed_on_the_provider() -> None:
    def factory(task: BenchmarkTask):
        raise AttributeError("'BenchmarkTask' object has no attribute 'chat'")

    report = asyncio.run(BenchmarkRunner(factory, run_id_factory=lambda cfg: "r").run([_task("a")]))
    assert report.verdicts[0].error_kind == "task"
    assert report.metrics.provider_errors == 0


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
