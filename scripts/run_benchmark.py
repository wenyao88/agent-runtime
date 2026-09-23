"""跑任务集并产出报告。

    python scripts/run_benchmark.py --provider mock              # 离线跑全量 20 条（不需要 key）
    python scripts/run_benchmark.py --provider real --limit 3    # 真实链路抽样（会花钱）
    python scripts/run_benchmark.py --provider real --judge 3    # 另抽 3 条做 LLM 裁判

顶层只 import 标准库：缺依赖时给可读提示 + 退出码 2，而不是抛 traceback（与 run_demo1_github 同一约定）。
**诚实显示**：分母为 0 的指标是 `None`，一律显示 `—`，绝不显示成 `0%`。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

_DASH = "—"
_METRIC_ROWS = (
    ("任务数", "tasks_total", "int"),
    ("出错任务数", "tasks_errored", "int"),
    ("成功率", "success_rate", "pct"),
    ("工具选择准确率", "tool_selection_accuracy", "pct"),
    ("工具参数准确率", "tool_argument_accuracy", "pct"),
    ("平均步数", "avg_steps", "num"),
    ("平均 token", "avg_total_tokens", "num"),
    ("平均耗时(ms)", "avg_latency_ms", "num"),
    ("压缩比", "compression_ratio", "pct"),
    ("压缩事件", "compaction_events", "int"),
    ("摘要token", "summarizer_tokens", "int"),
    ("摘要耗时(ms)", "summarizer_ms", "int"),
    ("错误恢复率", "error_recovery_rate", "pct"),
    ("裁判均分", "avg_judge_score", "num"),
    ("已判分条数", "judged_tasks", "int"),
)


def _render(value: object, kind: str) -> str:
    if value is None:
        return _DASH  # 没测 ≠ 0 分
    if kind == "pct":
        return f"{float(value) * 100:.1f}%"  # type: ignore[arg-type]
    if kind == "int":
        return str(int(value))  # type: ignore[arg-type]
    return f"{float(value):.1f}"  # type: ignore[arg-type]


def format_metrics(metrics: object) -> str:
    """指标表。`None` → `—`（这是硬要求，不是格式偏好）。"""
    lines = [f"{'指标':<16}{'值':>10}", "─" * 26]
    for label, field, kind in _METRIC_ROWS:
        lines.append(f"{label:<16}{_render(getattr(metrics, field, None), kind):>10}")
    by_strategy = getattr(metrics, "compression_by_strategy", None) or {}
    for strategy, ratio in sorted(by_strategy.items()):
        lines.append(f"{'  · ' + str(strategy):<16}{_render(ratio, 'pct'):>10}")
    return "\n".join(lines)


def format_verdicts(verdicts: list, limit: int = 50) -> str:
    """逐任务明细；失败必须带上原因（缺哪个工具/关键词，或错误）。"""
    lines: list[str] = []
    for verdict in list(verdicts)[:limit]:
        mark = "✓" if getattr(verdict, "success", False) else "✗"
        notes: list[str] = []
        if getattr(verdict, "missing_tools", None):
            notes.append("缺工具 " + ",".join(verdict.missing_tools))
        if getattr(verdict, "missing_keywords", None):
            notes.append("缺关键词 " + ",".join(verdict.missing_keywords))
        if getattr(verdict, "warning", None):
            notes.append(str(verdict.warning))
        if getattr(verdict, "error", ""):
            notes.append(str(verdict.error))
        if getattr(verdict, "extra_tool_calls", 0):
            notes.append(f"多余工具调用 {verdict.extra_tool_calls}")
        suffix = ("  " + "；".join(notes)) if notes else ""
        lines.append(
            f"{mark} {verdict.task_id}  步数 {getattr(verdict, 'steps', 0)}"
            f"  token {getattr(verdict, 'total_tokens', 0)}{suffix}"
        )
    if len(verdicts) > limit:
        lines.append(f"…（共 {len(verdicts)} 条，已省略 {len(verdicts) - limit} 条）")
    return "\n".join(lines)


def _load_settings():
    """有 pydantic_settings 就用真 `Settings`；沙箱缺依赖时退回默认值（`--provider mock` 仍要能用）。"""
    try:
        from agent_runtime.config.settings import Settings
    except ImportError:
        # 与 `Settings` 的默认值等价的一份兜底：沙箱里没有 pydantic_settings，
        # 但 `--provider mock` 必须仍然能用。字段缺失会**响亮地**报出来（而不是悄悄少个能力）。
        return types.SimpleNamespace(
            benchmark_tasks_file="benchmarks/tasks.json",
            benchmark_runs_dir="benchmark_runs",
            llm_model="mock",
            llm_api_key="",
            llm_base_url="",
            judge_llm_model="judge",
            judge_llm_api_key="",
            judge_llm_base_url="",
            tool_http_timeout_seconds=20.0,
        )
    return Settings()


def _real_agent_factory():
    """真实 provider：复用 API 的装配（**脚本**可以 import api；infrastructure 不可以）。"""
    try:
        from agent_runtime.api.deps import get_agent
    except Exception as e:  # noqa: BLE001
        print(f"错误：真实链路需要完整依赖（fastapi / pydantic_settings / openai）：{type(e).__name__}: {e}")
        return None
    from agent_runtime.infrastructure.benchmark.catalog import real_agent_factory

    return real_agent_factory(get_agent)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Benchmark：跑任务集并产出报告")
    parser.add_argument(
        "--provider", choices=["mock", "real"], default="mock",
        help="mock = 离线假 agent（不需要 key）；real = 真实 LLM",
    )
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 条（默认全量）")
    parser.add_argument("--judge", type=int, default=0, help="对前 N 条做 LLM 裁判（默认 0 = 不跑）")
    parser.add_argument("--tasks", default="", help="任务集 JSON 路径（默认取 BENCHMARK_TASKS_FILE）")
    parser.add_argument("--out", default="", help="报告落盘目录（默认取 BENCHMARK_RUNS_DIR）")
    args = parser.parse_args(argv)

    from agent_runtime.core.benchmark.dataset import load_tasks
    from agent_runtime.infrastructure.benchmark.catalog import (
        MOCK_PROVIDER,
        build_runner,
        resolve_runs_dir,
        resolve_tasks_file,
    )
    from agent_runtime.infrastructure.benchmark.service import (
        benchmark_config,
        new_run_id,
        run_and_save,
    )

    settings = _load_settings()
    tasks_file = args.tasks or resolve_tasks_file(settings, str(_ROOT))
    runs_dir = args.out or resolve_runs_dir(settings, str(_ROOT))

    tasks, errors = load_tasks(tasks_file)
    for error in errors:
        print(f"⚠ 任务集问题：{error}")
    if not tasks:
        print(f"错误：任务集为空或读不到（{tasks_file}）")
        return 2

    agent_factory = None
    if args.provider != MOCK_PROVIDER:
        agent_factory = _real_agent_factory()
        if agent_factory is None:
            return 2

    runner, build_errors = build_runner(settings, args.provider, agent_factory=agent_factory)
    for error in build_errors:
        print(f"⚠ {error}")

    config = benchmark_config(
        args.provider,
        model="mock" if args.provider == MOCK_PROVIDER else str(getattr(settings, "llm_model", "")),
        judge=args.judge,
    )
    total = len(tasks) if args.limit is None else min(max(0, args.limit), len(tasks))
    print(f"\n跑 {total} 条任务（provider={args.provider}, judge={config['judge']}）\n" + "─" * 72)

    def on_progress(done: int, count: int, verdict: object) -> None:
        mark = "✓" if getattr(verdict, "success", False) else "✗"
        print(f"{mark} [{done}/{count}] {getattr(verdict, 'task_id', '?')}")

    report = asyncio.run(
        run_and_save(
            runner,
            tasks,
            runs_dir=runs_dir,
            config=config,
            limit=args.limit,
            run_id=new_run_id(args.provider),
            on_progress=on_progress,
        )
    )

    print("\n" + "─" * 72)
    print(format_verdicts(report.verdicts))
    print("\n" + format_metrics(report.metrics))
    print(f"\n报告：{Path(runs_dir) / (report.run_id + '.json')}")
    if report.config.get("save_error"):
        print(f"⚠ 报告落盘失败：{report.config['save_error']}")
    if args.provider == MOCK_PROVIDER:
        print("注意：provider=mock 是离线夹具（合成事件 + 直接按任务声明调用工具），不是真实成绩。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
