"""服务层契约（`infrastructure/benchmark/service.py`）：run_id 生成 + 跑完落盘。

给 API 与 CLI 共用，所以两条要求：
  1. `new_run_id` 必须**带上 provider 标签** —— 报告文件名里就能看出这是 mock 还是真实成绩；
  2. 落盘失败**不能弄丢报告**：先把结果返回给调用方，把失败原因写进 `config["save_error"]`（可见，不静默）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from agent_runtime.core.benchmark.ablation import AblationRunner  # noqa: E402
from agent_runtime.core.benchmark.models import BenchmarkReport  # noqa: E402
from agent_runtime.infrastructure.benchmark.catalog import (  # noqa: E402
    MOCK_PROVIDER,
    build_runner,
)
from agent_runtime.infrastructure.benchmark.service import (  # noqa: E402
    ResumeError,
    new_run_id,
    run_ablation,
    run_and_save,
)
from agent_runtime.infrastructure.benchmark.progress_log import ProgressLog  # noqa: E402
from agent_runtime.infrastructure.benchmark.store import load_report  # noqa: E402

_ROOT = Path(__file__).resolve().parents[2]
_BASE = _ROOT / ".testtmp"
_SEQ = 0


def _new_dir() -> Path:
    global _SEQ
    _SEQ += 1
    path = _BASE / f"benchservice{_SEQ:02d}"
    # 目录序号在多次运行之间会复用：先清干净，否则上一轮留下的报告会让"文件清单"类断言假失败
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    return path


class _FakeSettings:
    benchmark_tasks_file = "benchmarks/tasks.json"
    benchmark_runs_dir = "benchmark_runs"
    judge_llm_api_key = ""
    judge_llm_base_url = ""
    judge_llm_model = "judge"
    llm_api_key = ""
    llm_base_url = "http://main/v1"
    tool_http_timeout_seconds = 20.0


def _tasks(limit: int):
    from agent_runtime.core.benchmark.dataset import load_tasks

    tasks, errors = load_tasks(str(_ROOT / "benchmarks" / "tasks.json"))
    assert errors == []
    return tasks[:limit]


def test_new_run_id_embeds_the_provider_and_the_time() -> None:
    run_id = new_run_id("mock", now=datetime(2026, 9, 21, 15, 30, 0))
    assert run_id.startswith("20260921-153000-mock-")
    assert len(run_id.rsplit("-", 1)[-1]) == 4, "必须有随机后缀（审查 M3：同秒撞名会静默覆盖报告）"
    assert new_run_id("", now=datetime(2026, 9, 21, 15, 30, 0)).startswith("20260921-153000-run-")


def test_two_runs_in_the_same_second_do_not_collide() -> None:
    stamps = {new_run_id("mock", now=datetime(2026, 9, 21, 15, 30, 0)) for _ in range(50)}
    assert len(stamps) > 40, f"同一秒内应当几乎不撞名，实际只得到 {len(stamps)} 个不同 id"


def test_run_and_save_writes_a_loadable_report() -> None:
    directory = _new_dir()
    try:
        runner, errors = build_runner(_FakeSettings(), MOCK_PROVIDER)
        assert errors == []
        report = asyncio.run(
            run_and_save(
                runner,
                _tasks(3),
                runs_dir=str(directory),
                config={"provider": MOCK_PROVIDER},
                run_id="fixed-run",
            )
        )
        assert report.run_id == "fixed-run"
        again = load_report("fixed-run", str(directory))
        assert again is not None
        assert again.metrics.tasks_total == 3
        assert "save_error" not in report.config
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _unwritable_dir() -> str:
    """把"目录"做成一个**文件**：落盘与进度写入都会失败（原始意图："落盘失败"）。"""
    blocker = _new_dir() / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    return str(blocker)


def test_an_unsafe_run_id_is_rejected_before_anything_runs() -> None:
    """信任边界：run_id 会变成报告名与进度文件名，`../escape` 不许被"尽力而为"地放过去。"""
    runner, _ = build_runner(_FakeSettings(), MOCK_PROVIDER)
    with_raises = False
    try:
        asyncio.run(
            run_and_save(
                runner,
                _tasks(1),
                runs_dir=str(_new_dir()),
                config={"provider": MOCK_PROVIDER},
                run_id="../escape",
            )
        )
    except ValueError as e:
        with_raises = True
        assert "../escape" in str(e)
    assert with_raises


def test_a_save_failure_does_not_lose_the_report() -> None:
    """落盘目录不可用时：报告照样跑完、照样返回，失败原因可见（含进度写入失败）。"""
    runner, _ = build_runner(_FakeSettings(), MOCK_PROVIDER)
    report = asyncio.run(
        run_and_save(
            runner,
            _tasks(1),
            runs_dir=_unwritable_dir(),
            config={"provider": MOCK_PROVIDER},
            run_id="run-1",
        )
    )
    assert isinstance(report, BenchmarkReport)
    assert report.metrics.tasks_total == 1, "报告本身必须还在"
    assert "save_error" in report.config
    assert "progress_error" in report.config, "进度写不进去也必须说出来（否则用户以为有进度）"


def test_a_save_failure_is_logged_not_only_stored() -> None:
    """回归（审查 I3）：API 路径下报告根本没落盘，`config["save_error"]` 谁也读不到 —— 必须打日志。"""
    records: list[str] = []

    class _Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    logger = logging.getLogger("agent_runtime.infrastructure.benchmark.service")
    handler = _Handler()
    logger.addHandler(handler)
    try:
        runner, _ = build_runner(_FakeSettings(), MOCK_PROVIDER)
        report = asyncio.run(
            run_and_save(
                runner,
                _tasks(1),
                runs_dir=_unwritable_dir(),
                config={"provider": MOCK_PROVIDER},
                run_id="run-1",
            )
        )
    finally:
        logger.removeHandler(handler)
    assert "save_error" in report.config
    assert any("落盘失败" in message for message in records), records
    assert any("进度文件写入失败" in message for message in records), records


def test_benchmark_config_merges_extra_snapshot_fields() -> None:
    """消融要往 config 里写开关快照（group/memory/compaction/session_scope）。"""
    from agent_runtime.infrastructure.benchmark.service import benchmark_config

    config = benchmark_config("real", model="m", judge=2, group="memory", session_scope="run")
    assert config["provider"] == "real" and config["model"] == "m" and config["judge"] == 2
    assert config["group"] == "memory" and config["session_scope"] == "run"
    assert "synthetic" not in config, "真实 provider 不能被标成夹具"

    mock_config = benchmark_config("mock", group="baseline")
    assert mock_config["synthetic"] is True and mock_config["group"] == "baseline"


def test_runner_receives_the_fixed_run_id() -> None:
    runner, _ = build_runner(_FakeSettings(), MOCK_PROVIDER)
    report = asyncio.run(
        run_and_save(
            runner,
            _tasks(1),
            runs_dir=str(_new_dir()),
            config={"provider": MOCK_PROVIDER},
            run_id="want-this-id",
        )
    )
    assert report.run_id == "want-this-id"


# ── 消融落盘（Phase 7） ──


def _mock_runner_factory(group):
    runner, errors = build_runner(_FakeSettings(), MOCK_PROVIDER)
    assert errors == []
    return runner


def test_run_ablation_saves_three_group_reports_and_one_comparison() -> None:
    """四份文件：三份组报告 + 一份对比报告。少了组报告就没法逐组回看，少了对比就没有结论。"""
    directory = _new_dir()
    try:
        report = asyncio.run(
            run_ablation(
                AblationRunner(_mock_runner_factory),
                _tasks(2),
                runs_dir=str(directory),
                provider=MOCK_PROVIDER,
                ablation_id="ab-1",
            )
        )
        files = {path.name for path in directory.glob("*.json")}
        assert files == {
            "ab-1.json",
            "ab-1-baseline.json",
            "ab-1-memory.json",
            "ab-1-memory_compaction.json",
        }, files
        assert load_report("ab-1-baseline", str(directory)).config["group"] == "baseline"
        data = json.loads((directory / "ab-1.json").read_text(encoding="utf-8"))
        assert data["kind"] == "ablation"
        assert set(data["groups"]) == {"baseline", "memory", "memory+compaction"}
        assert set(data["role_metrics"]) == {"baseline", "memory", "memory+compaction"}
        assert "save_error" not in report.config
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_an_ablation_save_failure_does_not_lose_the_report() -> None:
    """落盘目录不可用时，三组结果必须照样返回，且失败原因可见（不静默）。"""
    blocker = _new_dir() / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    report = asyncio.run(
        run_ablation(
            AblationRunner(_mock_runner_factory),
            _tasks(1),
            runs_dir=str(blocker),
            provider=MOCK_PROVIDER,
            ablation_id="ab-2",
        )
    )
    assert set(report.groups) == {"baseline", "memory", "memory+compaction"}
    assert report.groups["baseline"].metrics.tasks_total == 1
    assert "save_error" in report.config
    assert "baseline" in report.config["save_error"], report.config["save_error"]
    assert "对比报告" in report.config["save_error"], report.config["save_error"]


# ── 断点续跑（进度文件 + 跳过已成功） ──


def _counting_runner(calls: list[str], *, fail_on: set[str] | None = None):
    """记录"真的跑了哪些任务"的假 runner（续跑的核心断言：跳过的绝不能出现在这里）。"""
    from agent_runtime.core.benchmark.models import (
        BenchmarkMetrics,
        BenchmarkReport,
        TaskRun,
        TaskVerdict,
    )

    class _Runner:
        async def run(self, tasks, *, config=None, limit=None, on_progress=None, run_id=None,
                      done=None, on_verdict=None):
            selected = list(tasks)
            if limit is not None:
                selected = selected[: max(0, limit)]
            reclaimed = dict(done or {})
            verdicts = []
            for index, task in enumerate(selected, start=1):
                stored = reclaimed.get(task.task_id)
                if stored is not None:
                    verdicts.append(stored[1])
                    if on_progress is not None:
                        on_progress(index, len(selected), stored[1])
                    continue
                calls.append(task.task_id)
                run = TaskRun(task=task)
                ok = task.task_id not in (fail_on or set())
                verdict = TaskVerdict(task_id=task.task_id, success=ok, steps=2, total_tokens=100)
                if on_verdict is not None:
                    on_verdict(run, verdict)
                if on_progress is not None:
                    on_progress(index, len(selected), verdict)
                verdicts.append(verdict)
            return BenchmarkReport(
                run_id=run_id or "r",
                config=dict(config or {}),
                verdicts=verdicts,
                metrics=BenchmarkMetrics(
                    tasks_total=len(verdicts),
                    success_rate=(
                        sum(1 for v in verdicts if v.success) / len(verdicts)
                        if verdicts
                        else None
                    ),
                ),
            )

    return _Runner()


def test_a_progress_file_is_written_for_every_task() -> None:
    directory = _new_dir()
    try:
        calls: list[str] = []
        report = asyncio.run(
            run_and_save(
                _counting_runner(calls),
                _tasks(3),
                runs_dir=str(directory),
                config={"provider": "mock"},
                run_id="run-1",
            )
        )
        log = ProgressLog(str(directory), "run-1")
        read = log.read()
        assert [r["task_id"] for r in read.records] == [
            t.task_id for t in _tasks(3)
        ], "每条任务都要落一行"
        assert read.header is not None and read.header["fingerprint"]["limit"] is None
        assert calls == [t.task_id for t in _tasks(3)]
        assert report.metrics.tasks_total == 3
        assert "resumed" not in report.config
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_resume_skips_successful_tasks_and_retries_failed_ones() -> None:
    """这是整个机制的意义：断在第 250 条时，重跑只补没成功的几条。"""
    directory = _new_dir()
    try:
        calls: list[str] = []
        runner = _counting_runner(calls, fail_on={"gh-002"})
        tasks = _tasks(3)
        asyncio.run(
            run_and_save(
                runner, tasks, runs_dir=str(directory),
                config={"provider": "mock"}, run_id="run-1",
            )
        )
        assert calls == ["gh-001", "gh-002", "gh-003"]

        calls.clear()
        report = asyncio.run(
            run_and_save(
                runner, tasks, runs_dir=str(directory),
                config={"provider": "mock"}, run_id="run-1", resume=True,
            )
        )
        assert calls == ["gh-002"], "只有失败的那条重跑，成功的两条直接复用"
        assert report.metrics.tasks_total == 3, "报告仍然是完整的一份"
        assert report.config["resumed"] == 2
        skipped = [v.task_id for v in report.verdicts if v.skipped]
        assert skipped == ["gh-001", "gh-003"]
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_resume_refuses_a_different_fingerprint() -> None:
    """换模型/换 limit 之后续跑 = 把两套配置的指标混成一份报告 —— 必须拒绝。"""
    directory = _new_dir()
    try:
        calls: list[str] = []
        runner = _counting_runner(calls)
        asyncio.run(
            run_and_save(
                runner, _tasks(3), runs_dir=str(directory),
                config={"provider": "mock"}, run_id="run-1",
            )
        )
        for change in ({"model": "别的模型"}, {"judge": 3}):
            with_raises = False
            try:
                asyncio.run(
                    run_and_save(
                        runner, _tasks(3), runs_dir=str(directory),
                        config={"provider": "mock", **change}, run_id="run-1", resume=True,
                    )
                )
            except ResumeError as e:
                with_raises = True
                assert "拒绝续跑" in str(e)
            assert with_raises, change
        with_raises = False
        try:
            asyncio.run(
                run_and_save(
                    runner, _tasks(3), runs_dir=str(directory),
                    config={"provider": "mock"}, run_id="run-1", limit=2, resume=True,
                )
            )
        except ResumeError:
            with_raises = True
        assert with_raises, "limit 变了也是另一件事"
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_a_second_run_without_resume_refuses_to_reuse_a_progress_file() -> None:
    """不续跑却撞上同一份进度文件 → 报错，而不是把两轮写进同一个文件。"""
    directory = _new_dir()
    try:
        calls: list[str] = []
        runner = _counting_runner(calls)
        asyncio.run(
            run_and_save(
                runner, _tasks(1), runs_dir=str(directory),
                config={"provider": "mock"}, run_id="run-1",
            )
        )
        with_raises = False
        try:
            asyncio.run(
                run_and_save(
                    runner, _tasks(1), runs_dir=str(directory),
                    config={"provider": "mock"}, run_id="run-1",
                )
            )
        except ResumeError as e:
            with_raises = True
            assert "--resume" in str(e)
        assert with_raises
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_an_ablation_writes_one_progress_file_per_group() -> None:
    directory = _new_dir()
    try:
        report = asyncio.run(
            run_ablation(
                AblationRunner(_mock_runner_factory),
                _tasks(2),
                runs_dir=str(directory),
                provider=MOCK_PROVIDER,
                ablation_id="ab-1",
            )
        )
        logs = {path.name for path in directory.glob("*.progress.jsonl")}
        assert logs == {
            "ab-1-baseline.progress.jsonl",
            "ab-1-memory.progress.jsonl",
            "ab-1-memory_compaction.progress.jsonl",
        }, logs
        for name in ("baseline", "memory", "memory+compaction"):
            assert report.groups[name].metrics.tasks_total == 2
        read = ProgressLog(str(directory), "ab-1-memory").read()
        assert read.header is not None and read.header["ablation_id"] == "ab-1"
        assert read.header["fingerprint"]["session_scope"] == "run"
        assert read.header["fingerprint"]["group"] == "memory"
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_an_ablation_resumes_each_group_independently() -> None:
    """三组各认各的进度：某一组跑完了，续跑时它一条都不该再跑。"""
    directory = _new_dir()
    try:
        calls: dict[str, list[str]] = {"baseline": [], "memory": [], "memory+compaction": []}

        def runner_factory(group):
            tasks = _tasks(2)
            runner = _counting_runner(calls[group.name], fail_on=({"gh-002"} if group.name == "baseline" else set()))
            return runner

        asyncio.run(
            run_ablation(
                AblationRunner(runner_factory), _tasks(2), runs_dir=str(directory),
                provider=MOCK_PROVIDER, ablation_id="ab-1",
            )
        )
        assert calls["baseline"] == ["gh-001", "gh-002"]
        assert calls["memory"] == ["gh-001", "gh-002"]

        for group_calls in calls.values():
            group_calls.clear()
        asyncio.run(
            run_ablation(
                AblationRunner(runner_factory), _tasks(2), runs_dir=str(directory),
                provider=MOCK_PROVIDER, ablation_id="ab-1", resume=True,
            )
        )
        assert calls["baseline"] == ["gh-002"], "只有失败的那条重跑"
        assert calls["memory"] == [], "这一组全都成功过 → 一条都不再跑"
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_an_ablation_refuses_to_reuse_an_existing_round_without_resume() -> None:
    """库里直接调 `run_ablation(resume=False)` 撞上已有进度 → 拒绝（CLI 用新 id 规避）。"""
    directory = _new_dir()
    try:
        asyncio.run(
            run_ablation(
                AblationRunner(_mock_runner_factory), _tasks(1), runs_dir=str(directory),
                provider=MOCK_PROVIDER, ablation_id="ab-1",
            )
        )
        with_raises = False
        try:
            asyncio.run(
                run_ablation(
                    AblationRunner(_mock_runner_factory), _tasks(1), runs_dir=str(directory),
                    provider=MOCK_PROVIDER, ablation_id="ab-1",
                )
            )
        except ResumeError as e:
            with_raises = True
            assert "已有进度文件" in str(e)
        assert with_raises
    finally:
        shutil.rmtree(directory, ignore_errors=True)


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
