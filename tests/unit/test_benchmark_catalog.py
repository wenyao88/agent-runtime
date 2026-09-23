"""Benchmark 装配契约（`infrastructure/benchmark/catalog.py`）。

为什么必须有一个**离线** mock provider：报告要能在没有 key、没有网络的环境里完整跑出来 ——
否则"这条管线跑得通"永远只能靠嘴说。mock 报告里 `provider` 字段是 `mock`，不会被误当成真实成绩。

另一条纪律：**infrastructure 不反向依赖 api**。真实 agent 的装配在 `api/deps.get_agent`，
所以真实 provider 必须由调用方注入 `agent_factory` —— 没注入就明确报错，而不是偷偷造一个。
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from agent_runtime.core.benchmark.dataset import load_tasks  # noqa: E402
from agent_runtime.core.benchmark.models import BenchmarkTask  # noqa: E402
from agent_runtime.infrastructure.benchmark.catalog import (  # noqa: E402
    MOCK_PROVIDER,
    build_runner,
    real_agent_factory,
    resolve_runs_dir,
    resolve_tasks_file,
)

_ROOT = Path(__file__).resolve().parents[2]
_TASKS = _ROOT / "benchmarks" / "tasks.json"


class _FakeSettings:
    def __init__(self, **overrides: object) -> None:
        self.benchmark_tasks_file = "benchmarks/tasks.json"
        self.benchmark_runs_dir = "benchmark_runs"
        self.judge_llm_api_key = ""
        self.judge_llm_base_url = ""
        self.judge_llm_model = "judge-model"
        self.llm_api_key = ""
        self.llm_base_url = "http://main/v1"
        self.tool_http_timeout_seconds = 20.0
        for key, value in overrides.items():
            setattr(self, key, value)


class _FakeJudgeProvider:
    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.calls: list[list] = []

    async def chat(self, messages, tools=None):  # noqa: ANN001
        self.calls.append(list(messages))
        return types.SimpleNamespace(
            content='{"completion": 5, "accuracy": 4, "citation": 3, "structure": 4}'
        )


def _repo_tasks() -> list[BenchmarkTask]:
    tasks, errors = load_tasks(str(_TASKS))
    assert errors == [], errors
    return tasks


# ── mock provider：离线跑通整条管线 ──


def test_mock_provider_builds_a_runner_without_errors() -> None:
    runner, errors = build_runner(_FakeSettings(), MOCK_PROVIDER)
    assert errors == []
    assert runner is not None


def test_mock_runner_completes_the_whole_task_set_offline() -> None:
    """沙箱内最有价值的一条：20 条任务 → 事件 → 判分 → 指标 → 报告，全程不碰网络与 LLM。"""
    runner, _ = build_runner(_FakeSettings(), MOCK_PROVIDER)
    report = asyncio.run(
        runner.run(
            _repo_tasks(),
            config={"provider": MOCK_PROVIDER},
            limit=None,
        )
    )
    assert report.config["provider"] == "mock", "报告必须写清 provider，mock 不能被当成真实成绩"
    assert report.config["tasks_total"] == 20
    assert len(report.verdicts) == 20
    assert all(v.success for v in report.verdicts), [
        (v.task_id, v.missing_tools, v.missing_keywords) for v in report.verdicts if not v.success
    ]
    metrics = report.metrics
    assert metrics.success_rate == 1.0
    assert metrics.tool_selection_accuracy == 1.0
    assert metrics.tool_argument_accuracy == 1.0, "github 任务声明的 repo 参数应当命中"
    assert metrics.compression_ratio == 0.75, "mock 事件是确定性的"
    assert metrics.compaction_events == 20, "每条任务一个合成的压缩事件"
    assert metrics.compaction_events_by_strategy == {"summarize": 20}
    assert metrics.summarizer_tokens == 210 * 20, "摘要成本也要能从 mock 管线流到指标里"
    assert metrics.summarizer_ms == 120 * 20
    assert metrics.error_recovery_rate is None, "mock 没有失败的工具调用 → 该指标应为 None"
    assert metrics.avg_steps and metrics.avg_steps > 1


def test_a_custom_factory_receives_each_task() -> None:
    seen: list[str] = []

    def factory(task: BenchmarkTask):
        seen.append(task.task_id)
        return _MockAgent(task)

    class _MockAgent:  # 在函数里定义，避免与 catalog 的 mock 实现混淆
        def __init__(self, task: BenchmarkTask) -> None:
            self._task = task
            self.last_result = None

        async def run_stream(self, task: str, session_id: str = ""):
            if False:  # pragma: no cover —— 空生成器
                yield None

    runner, errors = build_runner(_FakeSettings(), "whatever", agent_factory=factory)
    assert errors == [], "注入了工厂就不该报错"
    asyncio.run(runner.run(_repo_tasks()[:2]))
    assert seen == ["gh-001", "gh-002"]


# ── 真实 provider：必须注入工厂 ──


def test_real_provider_without_a_factory_is_reported_not_faked() -> None:
    runner, errors = build_runner(_FakeSettings(), "real")
    assert len(errors) == 1
    assert "agent_factory" in errors[0]
    report = asyncio.run(runner.run(_repo_tasks()[:1], config={"provider": "real"}))
    verdict = report.verdicts[0]
    assert verdict.success is False
    assert "没有可用的 agent 工厂" in verdict.error, verdict.error


# ── 裁判接线：默认零调用 ──


def test_judge_is_attached_but_not_called_by_default() -> None:
    provider = _FakeJudgeProvider()
    runner, errors = build_runner(
        _FakeSettings(judge_llm_api_key="sk-judge"),
        MOCK_PROVIDER,
        judge_provider_factory=lambda **kwargs: provider,
    )
    assert errors == []
    report = asyncio.run(runner.run(_repo_tasks()[:3], config={"provider": "mock"}))
    assert provider.calls == [], "默认 judge=0：一次都不该调用裁判"
    assert all(v.judge_scores is None for v in report.verdicts)

    judged = asyncio.run(
        runner.run(_repo_tasks()[:3], config={"provider": "mock", "judge": 1})
    )
    assert len(provider.calls) == 1, "judge=1 只采样一条"
    assert judged.verdicts[0].judge_scores == {
        "completion": 5, "accuracy": 4, "citation": 3, "structure": 4
    }
    assert judged.metrics.judged_tasks == 1


# ── 路径解析（相对路径按项目根，不按 CWD） ──


def test_paths_are_anchored_to_the_project_root() -> None:
    settings = _FakeSettings()
    assert resolve_tasks_file(settings, str(_ROOT)) == str(_ROOT / "benchmarks" / "tasks.json")
    assert resolve_runs_dir(settings, str(_ROOT)) == str(_ROOT / "benchmark_runs")


def test_absolute_paths_are_kept() -> None:
    """比 `Path` 而不是比字符串：路径分隔符归一化是正常的（`D:/x` → `D:\\x`）。"""
    settings = _FakeSettings(benchmark_tasks_file="D:/elsewhere/tasks.json")
    resolved = Path(resolve_tasks_file(settings, str(_ROOT)))
    assert resolved.is_absolute()
    assert resolved == Path("D:/elsewhere/tasks.json")


def test_real_agent_factory_does_not_pass_the_task_as_the_llm() -> None:
    """回归（审查 C1）：`get_agent(llm=None)` 的第一个形参是 llm。

    直接把 `get_agent` 当工厂用会让 runner 的 `factory(task)` 把 **BenchmarkTask 当成模型** ——
    每条任务都以 `AttributeError: ... has no attribute 'chat'` 失败，却落出一份
    "成功率 0%"、**看起来像真实成绩**的报告（本机探针实测复现）。
    """
    calls: list[tuple] = []

    def fake_get_agent(*args: object, **kwargs: object) -> str:
        calls.append((args, kwargs))
        return "agent"

    factory = real_agent_factory(fake_get_agent)
    assert factory(BenchmarkTask(task_id="t", task="做事")) == "agent"
    assert calls == [((), {})], "必须无参调用 get_agent（绝不能把 task 塞进 llm 形参）"


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
