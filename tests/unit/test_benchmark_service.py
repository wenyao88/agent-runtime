"""服务层契约（`infrastructure/benchmark/service.py`）：run_id 生成 + 跑完落盘。

给 API 与 CLI 共用，所以两条要求：
  1. `new_run_id` 必须**带上 provider 标签** —— 报告文件名里就能看出这是 mock 还是真实成绩；
  2. 落盘失败**不能弄丢报告**：先把结果返回给调用方，把失败原因写进 `config["save_error"]`（可见，不静默）。
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from agent_runtime.core.benchmark.models import BenchmarkReport  # noqa: E402
from agent_runtime.infrastructure.benchmark.catalog import (  # noqa: E402
    MOCK_PROVIDER,
    build_runner,
)
from agent_runtime.infrastructure.benchmark.service import (  # noqa: E402
    new_run_id,
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
