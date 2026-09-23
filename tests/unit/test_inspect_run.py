"""只读巡检脚本契约（`scripts/inspect_run.py`）。

这个脚本存在的理由：真实全量跑挂之后，要看的东西散在三处 —— 报告 JSON（逐任务判分）、
进度 JSONL（工具事件）、以及"哪条任务为什么失败"的口径。命令行里翻这三样很容易漏，
所以把它们做成一屏只读输出。**它绝不改任何文件，也绝不参与评测口径。**
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from agent_runtime.core.benchmark.models import (  # noqa: E402
    BenchmarkMetrics,
    BenchmarkReport,
    BenchmarkTask,
    TaskRun,
    TaskVerdict,
    ToolEvent,
)
from agent_runtime.core.benchmark.progress import task_record  # noqa: E402
from agent_runtime.infrastructure.benchmark.progress_log import ProgressLog  # noqa: E402
from agent_runtime.infrastructure.benchmark.store import save_report  # noqa: E402

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "inspect_run.py"
_BASE = Path(__file__).resolve().parents[2] / ".testtmp"
_SEQ = 0


def _load():
    spec = importlib.util.spec_from_file_location("inspect_run", _SCRIPT)
    assert spec and spec.loader, "无法加载巡检脚本"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _new_dir() -> Path:
    global _SEQ
    _SEQ += 1
    path = _BASE / f"inspect{_SEQ:02d}"
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _seed(directory: Path) -> None:
    """造一份"研究类任务全失败"的现场：报告 + 进度文件。"""
    tasks = [
        BenchmarkTask(task_id="tr-001", task="调研 X"),
        BenchmarkTask(task_id="gh-001", task="分析 Y"),
    ]
    verdicts = [
        TaskVerdict(
            task_id="tr-001", success=False, steps=31, total_tokens=9000,
            rounds=15, max_steps=15, missing_keywords=["pgvector"],
            warning="max_steps(15) reached; forced final answer",
        ),
        TaskVerdict(task_id="gh-001", success=True, steps=4, total_tokens=500, rounds=3, max_steps=15),
    ]
    save_report(
        BenchmarkReport(
            run_id="run-1",
            config={"provider": "real", "group": "baseline", "model": "m"},
            verdicts=verdicts,
            metrics=BenchmarkMetrics(
                tasks_total=2, success_rate=0.5, avg_steps=17.5, avg_rounds=9.0,
                provider_errors=0, compaction_events=2, summarizer_tokens=0,
            ),
        ),
        str(directory),
    )
    log = ProgressLog(str(directory), "run-1")
    log.write_header({"provider": "real"}, tasks)
    log.append(
        task_record(
            TaskRun(
                task=tasks[0],
                tool_events=[
                    ToolEvent(step=1, tool="web_search", success=True, result_chars=16),
                    ToolEvent(
                        step=2, tool="web_scrape", success=False, result_chars=96,
                        error_excerpt="Error: HTTP 403 for GET https://example.test/docs",
                    ),
                    ToolEvent(
                        step=3, tool="web_scrape", success=False, result_chars=96,
                        error_excerpt="Error: HTTP 403 for GET https://example.test/guide",
                    ),
                ],
            ),
            verdicts[0],
        )
    )
    log.append(task_record(TaskRun(task=tasks[1]), verdicts[1]))


# ── 概览 ──


def test_overview_lists_every_run_with_the_numbers_that_matter() -> None:
    module = _load()
    directory = _new_dir()
    try:
        _seed(directory)
        text = module.format_overview(module.collect_runs(str(directory)))
        assert "run-1" in text
        assert "real" in text and "baseline" in text
        assert "50.0%" in text, text
        assert "17.5" in text and "9.0" in text, "步数与轮次要并列（口径不同）"
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_overview_says_so_when_there_is_nothing_to_show() -> None:
    module = _load()
    directory = _new_dir()
    try:
        text = module.format_overview(module.collect_runs(str(directory)))
        assert "没有" in text or "no reports" in text.lower()
    finally:
        shutil.rmtree(directory, ignore_errors=True)


# ── 逐任务失败原因 ──


def test_failures_show_reason_rounds_and_missing_keywords() -> None:
    module = _load()
    directory = _new_dir()
    try:
        _seed(directory)
        report = module.load_one("run-1", str(directory))
        text = module.format_failures(report)
        assert "tr-001" in text
        assert "pgvector" in text, "缺哪个关键词要写出来"
        assert "15/15" in text, "轮次要显示成 x/y（是不是撞上限一眼可见）"
        assert "max_steps" in text
        assert "gh-001" not in text, "成功的任务不进失败清单"
    finally:
        shutil.rmtree(directory, ignore_errors=True)


# ── 工具事件（进度文件） ──


def test_tool_histogram_names_the_failure_and_the_result_size() -> None:
    """这一条就是排查"web_scrape 79-113 字符"那类问题的地方：失败文本要能看出来。"""
    module = _load()
    directory = _new_dir()
    try:
        _seed(directory)
        text = module.format_tools(module.collect_tool_events(str(directory)))
        assert "web_scrape" in text
        assert "403" in text, text
        assert "2/2" in text or "失败 2" in text, "失败比例要能直接读出来"
        assert "web_search" in text
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_tool_events_are_read_from_progress_files_only() -> None:
    module = _load()
    directory = _new_dir()
    try:
        assert module.collect_tool_events(str(directory)) == []
    finally:
        shutil.rmtree(directory, ignore_errors=True)


# ── 端到端（只读） ──


def test_main_is_read_only_and_prints_the_sections() -> None:
    module = _load()
    directory = _new_dir()
    try:
        _seed(directory)
        before = sorted(p.name for p in directory.iterdir())
        code = module.main(["--runs-dir", str(directory)])
        after = sorted(p.name for p in directory.iterdir())
        assert code == 0
        assert before == after, "巡检脚本绝不写文件"
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_main_reports_an_unknown_run_readably() -> None:
    module = _load()
    directory = _new_dir()
    try:
        _seed(directory)
        code = module.main(["--runs-dir", str(directory), "--run", "nope"])
        assert code == 2
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_json_mode_is_machine_readable() -> None:
    module = _load()
    directory = _new_dir()
    try:
        _seed(directory)
        payload = module.summary_json(str(directory))
        data = json.loads(payload)
        assert data["runs"][0]["run_id"] == "run-1"
        assert data["tools"]["web_scrape"]["failures"] == 2
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
