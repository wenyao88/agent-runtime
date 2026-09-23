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
    new_run_id,
    run_ablation,
    run_and_save,
)
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


def test_a_save_failure_does_not_lose_the_report() -> None:
    runner, _ = build_runner(_FakeSettings(), MOCK_PROVIDER)
    report = asyncio.run(
        run_and_save(
            runner,
            _tasks(1),
            runs_dir=str(_new_dir()),
            config={"provider": MOCK_PROVIDER},
            run_id="../escape",  # 非法 run_id → 落盘抛 ValueError
        )
    )
    assert isinstance(report, BenchmarkReport)
    assert report.metrics.tasks_total == 1, "报告本身必须还在"
    assert "ValueError" in report.config["save_error"]


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
                runs_dir=str(_new_dir()),
                config={"provider": MOCK_PROVIDER},
                run_id="../escape",
            )
        )
    finally:
        logger.removeHandler(handler)
    assert "save_error" in report.config
    assert any("落盘失败" in message for message in records), records


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
