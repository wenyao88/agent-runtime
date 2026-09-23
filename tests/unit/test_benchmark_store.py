"""报告落盘契约（`infrastructure/benchmark/store.py`）。

两条要点：
  1. **run_id 变文件名，也是 API 路径参数** —— 必须挡住 `../` 这类越界（信任边界，不是洁癖）；
  2. 写入要**原子**（先写临时文件再 replace），否则并发的读会看到写了一半的报告。
坏文件一律当作"没有这份报告"，绝不让 `/api/benchmarks` 因为一个烂文件整体 500。
"""
from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from agent_runtime.core.benchmark.ablation import AblationReport  # noqa: E402
from agent_runtime.core.benchmark.models import (  # noqa: E402
    BenchmarkMetrics,
    BenchmarkReport,
    TaskVerdict,
)
from agent_runtime.infrastructure.benchmark.store import (  # noqa: E402
    list_runs,
    load_report,
    save_ablation,
    save_report,
)

_BASE = Path(__file__).resolve().parents[2] / ".testtmp"
_SEQ = 0


def _new_dir() -> Path:
    global _SEQ
    _SEQ += 1
    path = _BASE / f"benchstore{_SEQ:02d}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _report(run_id: str, *, minutes_ago: int = 0) -> BenchmarkReport:
    return BenchmarkReport(
        run_id=run_id,
        config={"provider": "mock", "tasks_total": 1},
        verdicts=[TaskVerdict(task_id="t1", success=True)],
        metrics=BenchmarkMetrics(tasks_total=1, success_rate=1.0),
        created_at=datetime(2026, 9, 21, 12, 0, 0) - timedelta(minutes=minutes_ago),
    )


def test_save_then_load_round_trips() -> None:
    directory = _new_dir()
    try:
        path = save_report(_report("run-1"), str(directory))
        assert Path(path).name == "run-1.json"
        again = load_report("run-1", str(directory))
        assert again is not None
        assert again.run_id == "run-1"
        assert again.config["provider"] == "mock"
        assert again.metrics.success_rate == 1.0
        assert again.verdicts[0].task_id == "t1"
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_save_creates_the_directory() -> None:
    directory = _new_dir() / "nested"
    try:
        save_report(_report("run-2"), str(directory))
        assert (directory / "run-2.json").is_file()
    finally:
        shutil.rmtree(directory.parent, ignore_errors=True)


def test_save_leaves_no_temp_file_behind() -> None:
    directory = _new_dir()
    try:
        save_report(_report("run-3"), str(directory))
        assert [p.name for p in directory.iterdir()] == ["run-3.json"]
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_list_runs_is_newest_first() -> None:
    directory = _new_dir()
    try:
        save_report(_report("old", minutes_ago=10), str(directory))
        save_report(_report("new"), str(directory))
        runs = list_runs(str(directory))
        assert [r["run_id"] for r in runs] == ["new", "old"]
        assert "verdicts" not in runs[0], "列表只要小结，不要逐任务明细"
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_list_runs_skips_broken_files_instead_of_raising() -> None:
    directory = _new_dir()
    try:
        save_report(_report("good"), str(directory))
        (directory / "broken.json").write_text("{ not json", encoding="utf-8")
        (directory / "wrong-shape.json").write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        runs = list_runs(str(directory))
        assert [r["run_id"] for r in runs] == ["good"]
    finally:
        shutil.rmtree(directory, ignore_errors=True)


# ── 消融对比报告（与单组报告同目录，但**不是**一份单组报告） ──


def _ablation(ablation_id: str) -> AblationReport:
    return AblationReport(
        ablation_id=ablation_id,
        baseline="baseline",
        groups={"baseline": _report("group-run")},
        deltas={"baseline": {"success_rate": {"baseline": 1.0, "group": 1.0, "abs": 0.0, "rel": 0.0}}},
        role_metrics={"baseline": {"followup": {"tasks": 0, "success_rate": None}}},
        config={"provider": "mock", "groups": ["baseline"]},
        created_at=datetime(2026, 9, 21, 12, 0, 0),
    )


def test_save_ablation_writes_a_kind_marked_file() -> None:
    directory = _new_dir()
    try:
        path = save_ablation(_ablation("ab-1"), str(directory))
        assert Path(path).name == "ab-1.json"
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        assert data["kind"] == "ablation", "类型标记是历史列表跳过它的依据"
        assert data["ablation_id"] == "ab-1"
        assert "baseline" in data["groups"] and "baseline" in data["deltas"]
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_an_ablation_file_is_not_read_as_a_single_run_report() -> None:
    """对比报告的结构与单组报告不同：`load_report`/`list_runs` 必须当它是"另一类东西"。

    否则历史列表会冒出一条 `run_id=""` 的幽灵报告（三组报告已经各自在里面了）。
    """
    directory = _new_dir()
    try:
        save_report(_report("run-1"), str(directory))
        save_ablation(_ablation("ab-1"), str(directory))
        assert [r["run_id"] for r in list_runs(str(directory))] == ["run-1"]
        assert load_report("ab-1", str(directory)) is None
        assert load_report("run-1", str(directory)) is not None
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_save_ablation_rejects_an_unsafe_id() -> None:
    directory = _new_dir()
    try:
        with_raises = False
        try:
            save_ablation(_ablation("../escape"), str(directory))
        except ValueError:
            with_raises = True
        assert with_raises, "同上：ablation_id 也会变成文件名"
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_load_missing_run_returns_none() -> None:
    directory = _new_dir()
    try:
        assert load_report("nope", str(directory)) is None
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_load_on_missing_directory_returns_none_and_empty_list() -> None:
    assert load_report("any", str(_BASE / "definitely-missing")) is None
    assert list_runs(str(_BASE / "definitely-missing")) == []


def test_run_id_cannot_escape_the_directory() -> None:
    """信任边界：run_id 直接来自 API 路径参数。"""
    directory = _new_dir()
    try:
        bad_names = (
            "../evil", "..", ".", "a/b", "a\\b", "", "  ", "a b", "x.json",
            # 审查 M2：前后空格会让"文件名 ≠ run_id"；超长名与 Windows 保留名同样必须被拒
            " x ", "x ", " x", "CON", "nul", "LPT1", "x" * 300,
        )
        for bad in bad_names:
            assert load_report(bad, str(directory)) is None, bad
            try:
                save_report(_report(bad), str(directory))
            except ValueError:
                pass
            else:
                raise AssertionError(f"save_report 不该接受 {bad!r}")
        assert not (directory.parent / "evil.json").exists()
        assert list(directory.glob("*.json")) == [], "坏事一条都不该落盘"
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_saved_file_is_readable_utf8_json() -> None:
    directory = _new_dir()
    try:
        save_report(_report("中文-run"), str(directory))
        raw = (directory / "中文-run.json").read_text(encoding="utf-8")
        assert json.loads(raw)["run_id"] == "中文-run"
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
