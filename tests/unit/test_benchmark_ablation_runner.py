"""消融编排契约（`core/benchmark/ablation.py` 的 `AblationRunner` / `AblationReport`）。

消融的**全部价值**在于"只改自变量，别的都一样"，所以这里钉住的都是"会不会把三组跑成同一组"：

  1. 每一组必须拿到**它自己**的 runner（工厂按组调用），且 `config` 里带该组的开关快照；
  2. 每组的 `run_id` 独立（否则三份报告互相覆盖，只剩最后一份）；
  3. `deltas` 以 baseline 为基准给绝对差 + 相对差；`baseline==0` 时相对差是 `None` 而不是除零；
  4. `followup` 分组指标单独给（记忆效应的直接证据），且按 `pair_role` 正确分组；
  5. 某组报告缺指标（`None`）时 delta 也是 `None`，**不能**当 0 算。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from agent_runtime.core.benchmark.ablation import (  # noqa: E402
    ABLATION_GROUPS,
    AblationGroup,
    AblationReport,
    AblationRunner,
    group_config,
)
from agent_runtime.core.benchmark.models import (  # noqa: E402
    BenchmarkMetrics,
    BenchmarkReport,
    BenchmarkTask,
    TaskVerdict,
)


def _tasks() -> list[BenchmarkTask]:
    return [
        BenchmarkTask(task_id="gh-001", task="分析 flask", pair_id="p1", pair_role="first"),
        BenchmarkTask(task_id="gh-002", task="继续 flask", pair_id="p1", pair_role="followup"),
        BenchmarkTask(task_id="gh-003", task="独立任务"),
    ]


class _FakeRunner:
    """按组返回预设结果的假 runner（记录自己收到的 config / limit / run_id）。"""

    def __init__(self, outcome: dict) -> None:
        self._outcome = outcome
        self.calls: list[dict] = []


def _metrics_of(verdicts: list[TaskVerdict]) -> BenchmarkMetrics:
    from agent_runtime.core.benchmark.metrics import summarize

    return summarize(verdicts, [])


def _verdict(task_id: str, success: bool, *, steps: int = 2, tokens: int = 100) -> TaskVerdict:
    return TaskVerdict(task_id=task_id, success=success, steps=steps, total_tokens=tokens)


def _run_for(task_id: str) -> TaskRun:
    return TaskRun(task=BenchmarkTask(task_id=task_id, task="做事"))


def _factory_runner(outcomes: dict[str, list[TaskVerdict]], built: list[str]):
    """按组名建 runner，并记录装配顺序（"每组各装一次"是硬要求）。"""

    class _Runner(_FakeRunner):
        def __init__(self, group: AblationGroup) -> None:
            super().__init__({})
            self._group = group

        async def run(
            self, tasks, *, config=None, limit=None, on_progress=None, run_id=None, done=None,
            on_verdict=None,
        ):
            self.calls.append(
                {"config": dict(config or {}), "limit": limit, "run_id": run_id, "done": done}
            )
            verdicts = outcomes[self._group.name]
            for verdict in verdicts:
                if on_verdict is not None:
                    on_verdict(_run_for(verdict.task_id), verdict)
            if on_progress is not None:
                for index, verdict in enumerate(verdicts, start=1):
                    on_progress(index, len(tasks), verdict)
            return BenchmarkReport(
                run_id=run_id or "r",
                config=dict(config or {}),
                verdicts=verdicts,
                metrics=_metrics_of(verdicts),
            )

    def factory(group: AblationGroup):
        built.append(group.name)
        return _Runner(group)

    return factory


def test_each_group_gets_its_own_runner_and_config() -> None:
    """三组必须**各装一次**，且报告 config 带该组的开关快照（"我以为开了"不算数）。"""
    built: list[str] = []
    factory = _factory_runner(
        {
            "baseline": [_verdict("gh-001", True)],
            "memory": [_verdict("gh-001", True)],
            "memory+compaction": [_verdict("gh-001", True)],
        },
        built,
    )
    report = asyncio.run(
        AblationRunner(factory).run(
            _tasks(), provider="mock", model="m", judge=0, ablation_id="ab-1"
        )
    )
    assert built == ["baseline", "memory", "memory+compaction"], "三组各装一次、顺序固定"
    assert list(report.groups) == ["baseline", "memory", "memory+compaction"]
    configs = {name: r.config for name, r in report.groups.items()}
    assert configs["baseline"] == {
        "group": "baseline", "memory": False, "compaction": False, "session_scope": "task",
        "provider": "mock", "model": "m", "judge": 0,
    }
    assert configs["memory"]["memory"] is True
    assert configs["memory"]["session_scope"] == "run"
    assert configs["memory+compaction"]["compaction"] is True


def test_each_group_gets_a_distinct_run_id() -> None:
    """run_id 相同 → 后写的组报告覆盖前一份，最后只剩一份（消融白跑）。"""
    built: list[str] = []
    factory = _factory_runner(
        {name: [_verdict("gh-001", True)] for name in ("baseline", "memory", "memory+compaction")},
        built,
    )
    runner = AblationRunner(factory)
    report = asyncio.run(
        runner.run(_tasks(), provider="mock", ablation_id="ab-1")
    )
    run_ids = [r.run_id for r in report.groups.values()]
    assert len(set(run_ids)) == 3, run_ids
    assert all("+" not in rid for rid in run_ids), f"run_id 会变成文件名，不能带 +：{run_ids}"
    assert run_ids[0].startswith("ab-1")


def test_progress_and_group_callbacks_fire_for_every_group() -> None:
    built: list[str] = []
    factory = _factory_runner(
        {name: [_verdict("gh-001", True), _verdict("gh-002", True)] for name in ("baseline", "memory", "memory+compaction")},
        built,
    )
    started: list[str] = []
    progress: list[tuple[int, int]] = []
    asyncio.run(
        AblationRunner(factory).run(
            _tasks(),
            provider="mock",
            ablation_id="ab-1",
            on_group=started.append,
            on_progress=lambda done, count, verdict: progress.append((done, count)),
        )
    )
    assert started == ["baseline", "memory", "memory+compaction"]
    assert progress == [(1, 3), (2, 3)] * 3, "每组内部都要报进度"


def test_deltas_are_absolute_and_relative_to_baseline() -> None:
    built: list[str] = []
    factory = _factory_runner(
        {
            "baseline": [_verdict("gh-001", False, steps=4, tokens=1000)],
            "memory": [_verdict("gh-001", True, steps=2, tokens=500)],
            "memory+compaction": [_verdict("gh-001", True, steps=3, tokens=700)],
        },
        built,
    )
    report = asyncio.run(
        AblationRunner(factory).run(_tasks(), provider="mock", ablation_id="ab-1")
    )
    baseline = report.deltas["baseline"]
    assert baseline["success_rate"]["abs"] == 0.0
    assert baseline["success_rate"]["rel"] is None, "baseline=0 时相对差算不出来（0/0）"
    memory = report.deltas["memory"]
    assert memory["success_rate"]["baseline"] == 0.0
    assert memory["success_rate"]["group"] == 1.0
    assert memory["success_rate"]["abs"] == 1.0
    assert memory["success_rate"]["rel"] is None, "baseline=0 时相对差没有定义（不能除零）"
    assert memory["avg_steps"]["abs"] == -2.0
    assert memory["avg_steps"]["rel"] == -0.5
    assert memory["avg_total_tokens"]["abs"] == -500.0
    assert report.deltas["memory+compaction"]["avg_steps"]["abs"] == -1.0


def test_a_missing_metric_stays_none_instead_of_becoming_zero() -> None:
    """`None` 是"没测"：差值是 `None`，不能拿 0 冒充。"""
    built: list[str] = []
    factory = _factory_runner(
        {name: [_verdict("gh-001", True)] for name in ("baseline", "memory", "memory+compaction")},
        built,
    )
    report = asyncio.run(
        AblationRunner(factory).run(_tasks(), provider="mock", ablation_id="ab-1")
    )
    assert report.deltas["memory"]["compression_ratio"]["abs"] is None
    assert report.deltas["memory"]["summarizer_tokens"]["abs"] == 0, "计数类指标真是 0"


def test_followup_metrics_are_split_by_pair_role() -> None:
    """记忆效应的直接证据：`followup` 单独统计（独立任务只会把效应摊平）。"""
    built: list[str] = []
    factory = _factory_runner(
        {
            "baseline": [
                _verdict("gh-001", True),
                _verdict("gh-002", False),
                _verdict("gh-003", True),
            ],
            "memory": [
                _verdict("gh-001", True),
                _verdict("gh-002", True),
                _verdict("gh-003", True),
            ],
            "memory+compaction": [
                _verdict("gh-001", True),
                _verdict("gh-002", True),
                _verdict("gh-003", False),
            ],
        },
        built,
    )
    report = asyncio.run(
        AblationRunner(factory).run(_tasks(), provider="mock", ablation_id="ab-1")
    )
    followups = {name: roles["followup"] for name, roles in report.role_metrics.items()}
    assert followups["baseline"]["tasks"] == 1
    assert followups["baseline"]["success_rate"] == 0.0
    assert followups["memory"]["success_rate"] == 1.0
    assert followups["memory+compaction"]["success_rate"] == 1.0
    assert report.role_metrics["baseline"]["standalone"]["tasks"] == 1
    assert report.role_metrics["baseline"]["first"]["tasks"] == 1
    # 落盘后 role_metrics 也要在（对比报告的自证信息之一）
    assert AblationReport.from_dict(report.to_dict()).role_metrics == report.role_metrics
    # followup 的 delta 直接进 deltas，免得读的人自己减
    assert report.deltas["memory"]["role.followup.success_rate"]["abs"] == 1.0
    assert report.deltas["memory"]["role.followup.success_rate"]["baseline"] == 0.0


def test_limit_is_forwarded_to_every_group() -> None:
    built: list[str] = []
    seen: list[int | None] = []

    def factory(group: AblationGroup):
        class _R:
            async def run(
                self, tasks, *, config=None, limit=None, on_progress=None, run_id=None,
                done=None, on_verdict=None,
            ):
                seen.append(limit)
                return BenchmarkReport(run_id=run_id or "r", metrics=_metrics_of([]))

        return _R()

    asyncio.run(
        AblationRunner(factory).run(_tasks(), provider="mock", limit=3, ablation_id="ab-1")
    )
    assert seen == [3, 3, 3]


def test_report_round_trips_through_dict() -> None:
    built: list[str] = []
    factory = _factory_runner(
        {name: [_verdict("gh-001", True)] for name in ("baseline", "memory", "memory+compaction")},
        built,
    )
    report = asyncio.run(
        AblationRunner(factory).run(_tasks(), provider="mock", ablation_id="ab-1")
    )
    data = report.to_dict()
    assert data["kind"] == "ablation", "落盘的类型标记：历史列表要能跳过对比报告"
    assert data["ablation_id"] == "ab-1"
    assert data["baseline"] == "baseline"
    assert set(data["groups"]) == {"baseline", "memory", "memory+compaction"}
    again = AblationReport.from_dict(data)
    assert again.ablation_id == "ab-1"
    assert again.baseline == "baseline"
    assert again.created_at == report.created_at
    assert again.groups["memory"].metrics.success_rate == 1.0
    assert again.deltas == report.deltas


def test_done_for_group_is_passed_to_each_group_runner() -> None:
    """续跑：三组各自的"已成功条目"要分别交给对应组的 runner（各写各的进度文件）。"""
    built: list[str] = []
    factory = _factory_runner(
        {name: [_verdict("gh-001", True)] for name in ("baseline", "memory", "memory+compaction")},
        built,
    )
    runners = {}

    def factory_with_tracking(group: AblationGroup):
        runner = factory(group)
        runners[group.name] = runner
        return runner

    seen_groups: list[str] = []

    def done_for_group(group: AblationGroup) -> dict:
        seen_groups.append(group.name)
        return {"gh-001": ("已存的 run", "已存的 verdict")}

    asyncio.run(
        AblationRunner(factory_with_tracking).run(
            _tasks(), provider="mock", ablation_id="ab-1", done_for_group=done_for_group
        )
    )
    assert seen_groups == ["baseline", "memory", "memory+compaction"]
    for name, runner in runners.items():
        assert runner.calls[0]["done"] == {"gh-001": ("已存的 run", "已存的 verdict")}, name


def test_each_group_report_is_handed_over_as_soon_as_it_finishes() -> None:
    """`on_group_done` 在**每组跑完立刻**回调：三组要跑几小时，不能等三组全完才落盘。"""
    built: list[str] = []
    factory = _factory_runner(
        {name: [_verdict("gh-001", True)] for name in ("baseline", "memory", "memory+compaction")},
        built,
    )
    order: list[str] = []

    def on_group_done(name: str, report) -> None:
        order.append(f"{name}:{report.run_id}")

    report = asyncio.run(
        AblationRunner(factory).run(
            _tasks(), provider="mock", ablation_id="ab-1", on_group_done=on_group_done
        )
    )
    assert order == [
        "baseline:ab-1-baseline",
        "memory:ab-1-memory",
        "memory+compaction:ab-1-memory_compaction",
    ]
    assert order[-1].split(":")[0] == list(report.groups)[-1]


def test_the_default_ablation_id_does_not_collide_within_the_same_second() -> None:
    """回归（审查 I-1）：默认 id 只有秒级精度 → 同一秒两次会把四份报告静默覆盖。"""
    from agent_runtime.core.benchmark.ablation import _default_ablation_id

    ids = {_default_ablation_id({"provider": "mock"}) for _ in range(10)}
    assert len(ids) > 5, f"同一秒内应当几乎不撞名，实际只得到 {len(ids)} 个不同 id"
    assert all("-mock-" in value for value in ids)


def test_group_config_merges_the_switch_snapshot() -> None:
    group = AblationGroup(name="memory", memory=True, compaction=False, session_scope="run")
    config = group_config(group, provider="real", model="deepseek-flash", judge=3)
    assert config == {
        "group": "memory", "memory": True, "compaction": False, "session_scope": "run",
        "provider": "real", "model": "deepseek-flash", "judge": 3,
    }


def test_the_default_groups_are_the_three_from_the_spec() -> None:
    assert [g.name for g in ABLATION_GROUPS] == ["baseline", "memory", "memory+compaction"]


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
