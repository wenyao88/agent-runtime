"""CLI 契约（`scripts/run_benchmark.py`）。

沙箱内可验证的高价值部分：**`--provider mock` 能真的把 20 条任务跑完、算出指标、把报告写到磁盘**。
格式化函数的重点在诚实：分母为 0 的指标是 `None`，必须显示 `—` 而不是 `0%`。
"""
from __future__ import annotations

import importlib.util
import shutil
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "scripts" / "run_benchmark.py"
_BASE = _ROOT / ".testtmp"
_SEQ = 0


def _load():
    spec = importlib.util.spec_from_file_location("run_benchmark", _SCRIPT)
    assert spec and spec.loader, "无法加载 benchmark CLI"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _new_dir() -> Path:
    global _SEQ
    _SEQ += 1
    path = _BASE / f"benchcli{_SEQ:02d}"
    # 序号在多次运行之间会复用：先清干净，否则上一轮的文件会让"文件个数"类断言假失败
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _metrics(**overrides: object):
    from agent_runtime.core.benchmark.models import BenchmarkMetrics

    base = {
        "tasks_total": 20,
        "success_rate": 0.85,
        "tool_selection_accuracy": 0.9,
        "tool_argument_accuracy": None,
        "avg_steps": 4.2,
        "avg_total_tokens": 1234.5,
        "avg_latency_ms": 800.0,
        "compression_ratio": 0.375,
        "compression_by_strategy": {"summarize": 0.5},
        "compaction_events": 3,
        "compaction_events_by_strategy": {"summarize": 3},
        "summarizer_tokens": 300,
        "summarizer_ms": 160,
        "error_recovery_rate": None,
        "judged_tasks": 0,
        "avg_judge_score": None,
    }
    base.update(overrides)
    return BenchmarkMetrics(**base)  # type: ignore[arg-type]


# ── 诚实显示 ──


def test_none_metrics_are_shown_as_a_dash_not_zero() -> None:
    module = _load()
    table = module.format_metrics(_metrics())
    assert "—" in table
    lines = {line.split()[0]: line for line in table.splitlines() if line.strip()}
    arg_line = next(v for k, v in lines.items() if "参数" in k)
    assert "0%" not in arg_line, arg_line
    assert "—" in arg_line, arg_line


def test_rates_are_rendered_as_percentages() -> None:
    module = _load()
    table = module.format_metrics(_metrics(success_rate=0.85, tool_selection_accuracy=1.0))
    assert "85.0%" in table
    assert "100.0%" in table


def test_metrics_show_the_task_count_and_compression() -> None:
    module = _load()
    table = module.format_metrics(_metrics())
    assert "20" in table
    assert "37.5%" in table or "0.375" in table


def test_metrics_show_compaction_event_counts_and_summarizer_cost() -> None:
    """压缩事件数与摘要额外成本必须出现在 CLI 表里（消融的成本归因靠它读）。"""
    module = _load()
    table = module.format_metrics(_metrics())
    assert "压缩事件" in table
    assert "3" in table
    assert "摘要token" in table or "摘要 token" in table, table
    assert "300" in table and "160" in table
    # 旧报告没有这两个字段：必须显示 `—`，不能凭空冒出一个 0
    old = module.format_metrics(
        _metrics(compaction_events=None, summarizer_tokens=None, summarizer_ms=None)
    )
    assert "—" in old


# ── 逐任务明细 ──


def test_verdict_lines_mark_failures_with_reasons() -> None:
    module = _load()
    from agent_runtime.core.benchmark.models import TaskVerdict

    verdicts = [
        TaskVerdict(task_id="a", success=True, steps=3, total_tokens=100),
        TaskVerdict(
            task_id="b",
            success=False,
            missing_tools=["web_search"],
            missing_keywords=["flask"],
            error="RuntimeError: boom",
        ),
    ]
    text = module.format_verdicts(verdicts)
    assert "a" in text and "b" in text
    assert "✗" in text and "✓" in text
    assert "web_search" in text and "RuntimeError" in text


# ── 端到端（mock） ──


def test_main_runs_the_mock_task_set_and_saves_a_report() -> None:
    module = _load()
    out = _new_dir()
    try:
        code = module.main(
            ["--provider", "mock", "--limit", "3", "--out", str(out)]
        )
        assert code == 0
        reports = list(out.glob("*.json"))
        assert len(reports) == 1, reports
        import json

        data = json.loads(reports[0].read_text(encoding="utf-8"))
        assert data["config"]["provider"] == "mock"
        assert data["metrics"]["tasks_total"] == 3
        assert data["metrics"]["success_rate"] == 1.0
    finally:
        shutil.rmtree(out, ignore_errors=True)


def test_main_rejects_an_unknown_provider() -> None:
    module = _load()
    try:
        module.main(["--provider", "gpt5"])
    except SystemExit as e:
        assert e.code == 2
    else:
        raise AssertionError("未知 provider 应该被 argparse 拒绝")


def test_main_reports_a_missing_task_set_readably() -> None:
    module = _load()
    code = module.main(["--provider", "mock", "--tasks", str(_ROOT / "nope.json")])
    assert code == 2, "任务集读不到应当以可读提示 + 退出码 2 结束"


# ── 消融（决定 A：`--group` 装配；G：`--ablation` 对比） ──


def test_group_run_applies_the_group_and_snapshots_it() -> None:
    """`--group memory` 必须在**本次运行**里带上开关覆盖，并把快照写进报告。"""
    module = _load()
    out = _new_dir()
    try:
        code = module.main(
            ["--provider", "mock", "--group", "memory", "--limit", "2", "--out", str(out)]
        )
        assert code == 0
        import json

        reports = list(out.glob("*.json"))
        assert len(reports) == 1, reports
        config = json.loads(reports[0].read_text(encoding="utf-8"))["config"]
        assert config["group"] == "memory"
        assert config["memory"] is True
        assert config["compaction"] is False
        assert config["session_scope"] == "run", "memory 组要用运行内共享会话"
    finally:
        shutil.rmtree(out, ignore_errors=True)


def test_an_unknown_group_is_rejected_readably() -> None:
    module = _load()
    assert module.main(["--provider", "mock", "--group", "nope"]) == 2


def test_ablation_run_saves_four_reports_and_a_comparison() -> None:
    """三份组报告 + 一份对比报告：`--ablation --limit 2` 在 mock 下必须跑完。"""
    module = _load()
    out = _new_dir()
    try:
        code = module.main(
            ["--provider", "mock", "--ablation", "--limit", "2", "--out", str(out)]
        )
        assert code == 0
        names = {path.name for path in out.glob("*.json")}
        assert len(names) == 4, names
        assert any(name.endswith("-ablation.json") for name in names), names
        import json

        comparisons = [
            path for path in out.glob("*.json") if path.name.endswith("-ablation.json")
        ]
        data = json.loads(comparisons[0].read_text(encoding="utf-8"))
        assert data["kind"] == "ablation"
        assert set(data["groups"]) == {"baseline", "memory", "memory+compaction"}
        for group_report in data["groups"].values():
            assert group_report["metrics"]["tasks_total"] == 2
    finally:
        shutil.rmtree(out, ignore_errors=True)


def test_format_ablation_prints_a_side_by_side_table() -> None:
    """并排表：每组一列、`None` 显示 `—`、followup 单独一行、差值表带相对值。"""
    module = _load()
    from agent_runtime.core.benchmark.ablation import AblationReport
    from agent_runtime.core.benchmark.models import BenchmarkMetrics, BenchmarkReport

    def group_report(name: str, success: float, tokens: int) -> BenchmarkReport:
        return BenchmarkReport(
            run_id=f"ab-{name}",
            config={"group": name},
            metrics=BenchmarkMetrics(
                tasks_total=10,
                success_rate=success,
                success_rate_measured=None,
                avg_steps=4.0,
                summarizer_tokens=tokens,
                compaction_events=0,
            ),
        )

    report = AblationReport(
        ablation_id="ab-1",
        groups={
            "baseline": group_report("baseline", 0.5, 0),
            "memory": group_report("memory", 0.8, 0),
        },
        deltas={
            "baseline": {"success_rate": {"baseline": 0.5, "group": 0.5, "abs": 0.0, "rel": 0.0}},
            "memory": {"success_rate": {"baseline": 0.5, "group": 0.8, "abs": 0.3, "rel": 0.6}},
        },
        role_metrics={
            "baseline": {"followup": {"tasks": 2, "success_rate": 0.0}},
            "memory": {"followup": {"tasks": 2, "success_rate": 1.0}},
        },
    )
    text = module.format_ablation(report)
    assert "baseline" in text and "memory" in text
    assert "50.0%" in text and "80.0%" in text
    assert "followup 成功率" in text
    assert "+60.0%" in text, text
    assert "—" in text, "没测的指标必须是 —（success_rate_measured=None）"


def test_resume_continues_the_unfinished_run_and_skips_completed_tasks() -> None:
    """`--resume` 接着**没跑完**的那一轮：已成功的条目跳过（报告里 `resumed` 自证）。"""
    module = _load()
    import json

    out = _new_dir()
    try:
        assert module.main(["--provider", "mock", "--limit", "2", "--out", str(out)]) == 0
        reports = list(out.glob("*.json"))
        assert len(reports) == 1
        run_id = reports[0].stem
        reports[0].unlink()  # 模拟"还没写最终报告就断了"

        assert module.main(
            ["--provider", "mock", "--limit", "2", "--out", str(out), "--resume"]
        ) == 0
        names = {path.name for path in out.glob("*.json")}
        assert names == {f"{run_id}.json"}, f"续跑应当写回同一个 run_id：{names}"
        data = json.loads((out / f"{run_id}.json").read_text(encoding="utf-8"))
        assert data["config"]["resumed"] == 2
        assert all(v["skipped"] for v in data["verdicts"])
    finally:
        shutil.rmtree(out, ignore_errors=True)


def test_a_finished_run_is_not_resumed() -> None:
    """跑完的不再续（最终报告已在磁盘上）——`--resume` 会开新一轮，而不是把完成的那轮改坏。"""
    module = _load()
    out = _new_dir()
    try:
        assert module.main(["--provider", "mock", "--limit", "1", "--out", str(out)]) == 0
        assert module.main(
            ["--provider", "mock", "--limit", "1", "--out", str(out), "--resume"]
        ) == 0
        assert len(list(out.glob("*.json"))) == 2, "应当是一轮新的，不是覆盖"
    finally:
        shutil.rmtree(out, ignore_errors=True)


def test_ablation_resume_reuses_the_unfinished_round() -> None:
    module = _load()
    import json

    out = _new_dir()
    try:
        assert module.main(
            ["--provider", "mock", "--ablation", "--limit", "2", "--out", str(out)]
        ) == 0
        comparison = [path for path in out.glob("*-ablation.json")][0]
        ablation_id = comparison.stem
        comparison.unlink()  # 模拟"第三组还没跑完就断了"

        assert module.main(
            ["--provider", "mock", "--ablation", "--limit", "2", "--out", str(out), "--resume"]
        ) == 0
        assert (out / f"{ablation_id}.json").exists(), "应当续跑同一个 ablation id"
        for name in ("baseline", "memory", "memory_compaction"):
            data = json.loads((out / f"{ablation_id}-{name}.json").read_text(encoding="utf-8"))
            assert data["config"]["resumed"] == 2, name
        assert len(list(out.glob("*-ablation.json"))) == 1, "不该多出一轮"
    finally:
        shutil.rmtree(out, ignore_errors=True)


def test_ablation_id_pins_the_round() -> None:
    module = _load()
    out = _new_dir()
    try:
        assert module.main(
            [
                "--provider", "mock", "--ablation", "--limit", "1",
                "--out", str(out), "--ablation-id", "ab-fixed",
            ]
        ) == 0
        names = {path.name for path in out.glob("*.json")}
        assert names == {
            "ab-fixed.json",
            "ab-fixed-baseline.json",
            "ab-fixed-memory.json",
            "ab-fixed-memory_compaction.json",
        }, names
    finally:
        shutil.rmtree(out, ignore_errors=True)


def test_ablation_id_continues_the_same_round() -> None:
    """`--ablation-id` 是"接着这一轮跑"：同一 id 再跑一次不会多出一轮，也不会重跑已成功的条目。"""
    module = _load()
    import json

    out = _new_dir()
    try:
        args = [
            "--provider", "mock", "--ablation", "--limit", "1",
            "--out", str(out), "--ablation-id", "ab-fixed",
        ]
        assert module.main(args) == 0
        assert module.main(args) == 0
        names = {path.name for path in out.glob("*.json")}
        assert names == {
            "ab-fixed.json",
            "ab-fixed-baseline.json",
            "ab-fixed-memory.json",
            "ab-fixed-memory_compaction.json",
        }, names
        data = json.loads((out / "ab-fixed-baseline.json").read_text(encoding="utf-8"))
        assert data["config"]["resumed"] == 1, "第二次应当复用已成功的那条"
    finally:
        shutil.rmtree(out, ignore_errors=True)


def test_a_fatal_search_config_is_refused_before_a_real_run() -> None:
    """真实全量实测：tavily 没 key → 每条研究类任务白烧到 max_steps。开跑前就该拒绝。"""
    module = _load()
    import types

    original = module._load_settings
    module._load_settings = lambda: types.SimpleNamespace(
        benchmark_tasks_file="benchmarks/tasks.json",
        benchmark_runs_dir="benchmark_runs",
        llm_model="m", llm_api_key="k", llm_base_url="http://x",
        judge_llm_model="j", judge_llm_api_key="", judge_llm_base_url="",
        tool_http_timeout_seconds=20.0,
        web_search_provider="tavily",
        web_search_api_key="",
    )
    try:
        code = module.main(["--provider", "real", "--limit", "1", "--out", str(_new_dir())])
    finally:
        module._load_settings = original
    assert code == 2, "配置注定失败时不许开跑"


def test_a_fatal_search_config_does_not_block_a_mock_run() -> None:
    """mock 夹具不调用工具，所以照样能跑（否则离线自检会被搜索配置卡死）。"""
    module = _load()
    import types

    original = module._load_settings
    module._load_settings = lambda: types.SimpleNamespace(
        benchmark_tasks_file="benchmarks/tasks.json",
        benchmark_runs_dir="benchmark_runs",
        llm_model="m", llm_api_key="", llm_base_url="http://x",
        judge_llm_model="j", judge_llm_api_key="", judge_llm_base_url="",
        tool_http_timeout_seconds=20.0,
        web_search_provider="tavily",
        web_search_api_key="",
    )
    out = _new_dir()
    try:
        code = module.main(["--provider", "mock", "--limit", "1", "--out", str(out)])
    finally:
        module._load_settings = original
        shutil.rmtree(out, ignore_errors=True)
    assert code == 0


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
