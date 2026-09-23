"""进度文件读写契约（`infrastructure/benchmark/progress_log.py`）。

这是"断点续跑"能不能成立的**物理基础**：文件写不对，续跑就会续错或续丢。所以这里钉住：

  1. 追加一行就 flush（不等 close）—— 被 Ctrl-C 时已完成的任务都还在；
  2. header 只写一次，且带着 run_id/ablation_id/指纹；
  3. 坏行（写了一半、被改坏、不是对象）只记警告并跳过，前面的行照旧可用；
  4. `find_resumable_*` 只认**指纹匹配**且**最终报告还没写**的那一轮（跑完的不再续，配置变了不混跑）。
"""
from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from agent_runtime.core.benchmark.models import (  # noqa: E402
    BenchmarkReport,
    BenchmarkTask,
    TaskVerdict,
)
from agent_runtime.core.benchmark.progress import task_record  # noqa: E402
from agent_runtime.infrastructure.benchmark.progress_log import (  # noqa: E402
    ProgressLog,
    find_resumable_ablation,
    find_resumable_run,
)
from agent_runtime.infrastructure.benchmark.store import save_report  # noqa: E402

_BASE = Path(__file__).resolve().parents[2] / ".testtmp"
_SEQ = 0


def _new_dir() -> Path:
    global _SEQ
    _SEQ += 1
    path = _BASE / f"benchprogress{_SEQ:02d}"
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _tasks() -> list[BenchmarkTask]:
    return [BenchmarkTask(task_id="gh-001", task="做事"), BenchmarkTask(task_id="gh-002", task="做事")]


def _config(**overrides: object) -> dict:
    base = {
        "provider": "real",
        "model": "deepseek-flash",
        "judge": 0,
        "limit": 2,
        "tasks_total": 2,
        "group": "memory",
        "memory": True,
        "compaction": False,
        "session_scope": "run",
        "ablation_id": "ab-1",
    }
    base.update(overrides)
    return base


def _record(task_id: str = "gh-001", *, success: bool = True) -> dict:
    from agent_runtime.core.benchmark.models import TaskRun

    task = next(t for t in _tasks() if t.task_id == task_id)
    return task_record(TaskRun(task=task), TaskVerdict(task_id=task_id, success=success))


# ── 追加与读回 ──


def test_appending_then_reading_round_trips_records_and_header() -> None:
    directory = _new_dir()
    try:
        log = ProgressLog(str(directory), "run-1")
        assert log.exists() is False
        log.write_header(_config(), _tasks(), ablation_id="ab-1")
        log.append(_record("gh-001"))
        log.append(_record("gh-002", success=False))

        out = log.read()
        assert out.exists is True
        assert out.header is not None
        assert out.header["run_id"] == "run-1"
        assert out.header["ablation_id"] == "ab-1"
        assert out.header["fingerprint"]["group"] == "memory"
        assert [r["task_id"] for r in out.records] == ["gh-001", "gh-002"]
        assert out.warnings == []
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_each_appended_line_is_on_disk_immediately() -> None:
    """不等 close 就在磁盘上：否则被 Ctrl-C 时进度全丢（这正是这个文件存在的理由）。"""
    directory = _new_dir()
    try:
        log = ProgressLog(str(directory), "run-1")
        log.append(_record("gh-001"))
        raw = log.path.read_text(encoding="utf-8")
        assert "gh-001" in raw
        assert raw.endswith("\n"), "一行一条（JSONL）"
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_the_header_is_written_only_once() -> None:
    directory = _new_dir()
    try:
        log = ProgressLog(str(directory), "run-1")
        log.write_header(_config(), _tasks(), ablation_id="ab-1")
        log.write_header(_config(model="换了模型"), _tasks(), ablation_id="ab-1")
        out = log.read()
        assert out.header is not None
        assert out.header["fingerprint"]["model"] == "deepseek-flash", "第一次写的 header 不许被改"
        assert len(log.path.read_text(encoding="utf-8").splitlines()) == 1
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_a_half_written_line_is_skipped_with_a_warning() -> None:
    """断电时最后一行可能是半截：必须只丢那一行，不能让几小时的结果作废。"""
    directory = _new_dir()
    try:
        log = ProgressLog(str(directory), "run-1")
        log.write_header(_config(), _tasks(), ablation_id="ab-1")
        log.append(_record("gh-001"))
        with log.path.open("a", encoding="utf-8") as handle:
            handle.write('{"kind": "task", "task_id": "gh-0')
        out = log.read()
        assert [r["task_id"] for r in out.records] == ["gh-001"]
        assert len(out.warnings) == 1 and "第 3 行" in out.warnings[0]
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_non_object_lines_are_skipped_with_a_warning() -> None:
    directory = _new_dir()
    try:
        log = ProgressLog(str(directory), "run-1")
        log.write_header(_config(), _tasks(), ablation_id="ab-1")
        with log.path.open("a", encoding="utf-8") as handle:
            handle.write("123\n\n")
        log.append(_record("gh-001"))
        out = log.read()
        assert [r["task_id"] for r in out.records] == ["gh-001"]
        assert len(out.warnings) == 1 and "不是对象" in out.warnings[0]
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_reading_a_missing_file_is_not_an_error() -> None:
    directory = _new_dir()
    try:
        out = ProgressLog(str(directory), "nope").read()
        assert out.exists is False and out.header is None and out.records == []
        assert out.warnings == []
    finally:
        shutil.rmtree(directory, ignore_errors=True)


# ── 找可续跑的那一轮 ──


def _finished_report(directory: Path, run_id: str) -> None:
    save_report(BenchmarkReport(run_id=run_id), str(directory))


def test_a_single_run_with_a_matching_fingerprint_is_resumable() -> None:
    directory = _new_dir()
    try:
        log = ProgressLog(str(directory), "run-1")
        log.write_header(_config(group=""), _tasks())
        log.append(_record("gh-001"))
        assert find_resumable_run(str(directory), _config(group=""), _tasks()) == "run-1"
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_a_finished_single_run_is_not_resumable() -> None:
    directory = _new_dir()
    try:
        log = ProgressLog(str(directory), "run-1")
        log.write_header(_config(group=""), _tasks())
        _finished_report(directory, "run-1")
        assert find_resumable_run(str(directory), _config(group=""), _tasks()) is None
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_a_changed_config_is_not_resumable() -> None:
    """换模型/换 limit/换任务集 → 不许续（把两套配置混进一份报告就是假数据）。"""
    directory = _new_dir()
    try:
        log = ProgressLog(str(directory), "run-1")
        log.write_header(_config(group=""), _tasks())
        for change in ({"model": "别的模型"}, {"limit": 5}, {"judge": 3}):
            assert find_resumable_run(str(directory), _config(group="", **change), _tasks()) is None, change
        others = [*_tasks(), BenchmarkTask(task_id="gh-003", task="做事")]
        assert find_resumable_run(str(directory), _config(group=""), others) is None
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_the_newest_resumable_run_wins() -> None:
    directory = _new_dir()
    try:
        first = ProgressLog(str(directory), "run-1")
        first.write_header(_config(group=""), _tasks())
        time.sleep(0.02)  # mtime 精度有限：不睡一下两次写入可能是同一个时间戳
        second = ProgressLog(str(directory), "run-2")
        second.write_header(_config(group=""), _tasks())
        assert find_resumable_run(str(directory), _config(group=""), _tasks()) == "run-2"
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_a_single_run_log_is_not_mistaken_for_an_ablation() -> None:
    directory = _new_dir()
    try:
        ProgressLog(str(directory), "run-1").write_header(_config(group=""), _tasks())
        assert find_resumable_ablation(str(directory), _config(), _tasks()) is None
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_an_unfinished_ablation_is_resumable_and_a_finished_one_is_not() -> None:
    directory = _new_dir()
    try:
        log = ProgressLog(str(directory), "ab-1-memory")
        log.write_header(_config(), _tasks(), ablation_id="ab-1")
        # 三组的开关不同，但"运行身份"相同 → 能认出是同一轮
        assert find_resumable_ablation(str(directory), _config(group="baseline", memory=False), _tasks()) == "ab-1"
        _finished_report(directory, "ab-1")
        assert find_resumable_ablation(str(directory), _config(), _tasks()) is None
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_a_log_with_a_broken_header_is_ignored() -> None:
    directory = _new_dir()
    try:
        (directory / "broken.progress.jsonl").write_text("{ half", encoding="utf-8")
        (directory / "empty.progress.jsonl").write_text("", encoding="utf-8")
        assert find_resumable_run(str(directory), _config(group=""), _tasks()) is None
        assert find_resumable_ablation(str(directory), _config(), _tasks()) is None
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_the_progress_file_is_not_a_report() -> None:
    """文件名以 `.progress.jsonl` 结尾 → 历史列表（扫 `*.json`）不会把它当报告。"""
    directory = _new_dir()
    try:
        log = ProgressLog(str(directory), "run-1")
        log.write_header(_config(group=""), _tasks())
        from agent_runtime.infrastructure.benchmark.store import list_runs

        assert list_runs(str(directory)) == []
        assert not (directory / "run-1.json").exists()
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_json_is_utf8_readable() -> None:
    directory = _new_dir()
    try:
        log = ProgressLog(str(directory), "中文-run")
        log.append({"kind": "task", "task_id": "gh-001", "note": "中文备注"})
        raw = log.path.read_text(encoding="utf-8")
        assert "中文备注" in raw
        assert json.loads(raw.strip())["note"] == "中文备注"
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
