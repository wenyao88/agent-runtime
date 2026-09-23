"""只读巡检：把"跑挂之后要看的现场"做成一屏输出（**绝不写任何文件，也不参与评测口径**）。

为什么需要它：真实全量跑挂后，要看的东西散在三处 ——
  * 报告 JSON：逐任务判分（谁失败、缺什么关键词、是不是撞了 max_steps）；
  * 进度 JSONL：工具事件（哪个工具在失败、结果多大、失败文本片段）；
  * 报告 config：这一轮到底是什么配置（provider/模型/分组/开关/续跑条数）。
命令行里翻这三样很容易漏（真实案例：只看到"web_scrape 返回 79-113 字符"，
看不出那是 `HTTP 403 for GET <url>`），所以这里一次打全。

用法：
    python scripts/inspect_run.py                     # 概览：所有报告 + 工具失败分布
    python scripts/inspect_run.py --run <run_id>      # 某一轮：逐任务失败原因
    python scripts/inspect_run.py --tools             # 只看工具事件（含失败文本片段）
    python scripts/inspect_run.py --json              # 机器可读（给别的脚本用）
    python scripts/inspect_run.py --runs-dir DIR      # 指定报告目录
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from agent_runtime.core.benchmark.models import BenchmarkReport  # noqa: E402
from agent_runtime.infrastructure.benchmark.progress_log import (  # noqa: E402
    ProgressLog,
    SUFFIX,
)
from agent_runtime.infrastructure.benchmark.store import list_runs, load_report  # noqa: E402


def _dash(value: object, kind: str = "num") -> str:
    if value is None:
        return "—"
    if kind == "pct":
        return f"{float(value) * 100:.1f}%"  # type: ignore[arg-type]
    return f"{float(value):.1f}"  # type: ignore[arg-type]


def collect_runs(directory: str) -> list[dict]:
    """所有**单组**报告的小结（对比报告由 store 按类型挡掉）。"""
    return list_runs(directory)


def load_one(run_id: str, directory: str) -> BenchmarkReport | None:
    return load_report(run_id, directory)


def collect_tool_events(directory: str) -> list[dict]:
    """从所有进度文件里读出工具事件（含所属 run_id / task_id）。

    **只看进度文件**：报告里没有工具文本（那是指标的输入，不是明细）。
    """
    events: list[dict] = []
    try:
        paths = sorted(Path(directory).glob(f"*{SUFFIX}"))
    except OSError:
        return events
    for path in paths:
        run_id = path.name[: -len(SUFFIX)]
        log = ProgressLog(directory, run_id) if _safe(run_id) else None
        if log is None:
            continue
        read = log.read()
        for record in read.records:
            task_id = str(record.get("task_id") or "")
            for event in (record.get("run") or {}).get("tool_events") or []:
                if isinstance(event, dict):
                    events.append({"run_id": run_id, "task_id": task_id, **event})
    return events


def _safe(run_id: str) -> bool:
    from agent_runtime.infrastructure.benchmark.store import safe_name

    return safe_name(run_id) is not None


def format_overview(runs: list[dict]) -> str:
    if not runs:
        return "没有可看的报告（benchmark_runs/ 是空的，或都还没跑完）。"
    lines = ["报告概览", "─" * 100]
    header = (
        f"{'run_id':<44}{'分组 · provider':<26}{'条数':>5}{'成功率':>9}{'均步数':>8}{'均轮次':>8}{'压缩事件':>9}"
    )
    lines.append(header)
    for run in runs:
        metrics = run.get("metrics") or {}
        config = run.get("config") or {}
        group = str(config.get("group") or "—")
        provider = str(config.get("provider") or "?")
        lines.append(
            f"{str(run.get('run_id'))[:43]:<44}{f'{group} · {provider}'[:25]:<26}"
            f"{int(metrics.get('tasks_total') or 0):>5}"
            f"{_dash(metrics.get('success_rate'), 'pct'):>9}"
            f"{_dash(metrics.get('avg_steps')):>8}"
            f"{_dash(metrics.get('avg_rounds')):>8}"
            f"{int(metrics.get('compaction_events') or 0):>9}"
        )
        notes: list[str] = []
        if config.get("resumed"):
            notes.append(f"续跑复用 {config['resumed']} 条")
        if config.get("progress_error"):
            notes.append(f"⚠ 进度写入失败：{config['progress_error']}")
        if config.get("save_error"):
            notes.append(f"⚠ 落盘失败：{config['save_error']}")
        if notes:
            lines.append("    " + "；".join(notes))
    return "\n".join(lines)


def format_failures(report: BenchmarkReport | None) -> str:
    if report is None:
        return "没有这份报告。"
    failed = [v for v in report.verdicts if not v.success]
    if not failed:
        return f"{report.run_id}：没有失败任务（共 {len(report.verdicts)} 条）。"
    lines = [f"{report.run_id}：失败 {len(failed)}/{len(report.verdicts)} 条", "─" * 100]
    for verdict in failed:
        rounds = getattr(verdict, "rounds", 0) or 0
        max_steps = getattr(verdict, "max_steps", 0) or 0
        rounds_text = f"{rounds}/{max_steps}" if max_steps else str(rounds)
        notes: list[str] = []
        if verdict.missing_tools:
            notes.append("缺工具 " + ",".join(verdict.missing_tools))
        if verdict.missing_keywords:
            notes.append("缺关键词 " + ",".join(verdict.missing_keywords))
        if verdict.warning:
            notes.append(str(verdict.warning))
        if verdict.error:
            notes.append(f"{verdict.error_kind or 'error'}: {verdict.error}")
        lines.append(
            f"✗ {verdict.task_id}  步数 {verdict.steps}  轮次 {rounds_text}"
            f"  token {verdict.total_tokens}\n    " + "；".join(notes or ["（没有可读原因）"])
        )
    return "\n".join(lines)


def format_tools(events: list[dict]) -> str:
    if not events:
        return "没有工具事件（进度文件为空，或这一轮没跑过工具）。"
    by_tool: dict[str, list[dict]] = {}
    for event in events:
        by_tool.setdefault(str(event.get("tool") or "?"), []).append(event)

    lines = ["工具事件分布（来自进度文件）", "─" * 100]
    for tool, items in sorted(by_tool.items()):
        failures = [e for e in items if not e.get("success")]
        sizes = [int(e.get("result_chars") or 0) for e in items]
        median = int(statistics.median(sizes)) if sizes else 0
        lines.append(
            f"{tool:<18} 调用 {len(items):>4}  失败 {len(failures)}/{len(items)} "
            f"({len(failures) / len(items) * 100:.0f}%)  结果长度 min/中位/max = "
            f"{min(sizes)}/{median}/{max(sizes)}"
        )
        for excerpt, count in _top_excerpts(failures):
            lines.append(f"    ×{count:<4} {excerpt}")
    return "\n".join(lines)


def _top_excerpts(failures: list[dict], limit: int = 3) -> list[tuple[str, int]]:
    """失败文本片段的 top N（把 URL 归一化成 <url>，否则每条都不同、统计不出频次）。"""
    counts: dict[str, int] = {}
    for event in failures:
        excerpt = str(event.get("error_excerpt") or "").strip()
        if not excerpt:
            continue
        counts[_normalize(excerpt)] = counts.get(_normalize(excerpt), 0) + 1
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]


def _normalize(excerpt: str) -> str:
    """`Error: HTTP 403 for GET https://a/very/long/path` → `Error: HTTP 403 for GET <url>`。

    URL 必须归一化：不归一化的话每条失败文本都不一样，频次统计（"45 次 403"）就出不来。
    """
    text = " ".join(excerpt.split())
    for scheme in ("https://", "http://"):
        index = text.find(scheme)
        if index != -1:
            return f"{text[:index].rstrip()} <url>"
    return text[:120]


def summary_json(directory: str) -> str:
    tools: dict[str, dict] = {}
    for event in collect_tool_events(directory):
        tool = str(event.get("tool") or "?")
        entry = tools.setdefault(tool, {"calls": 0, "failures": 0, "max_chars": 0})
        entry["calls"] += 1
        if not event.get("success"):
            entry["failures"] += 1
        entry["max_chars"] = max(entry["max_chars"], int(event.get("result_chars") or 0))
    return json.dumps(
        {"runs": collect_runs(directory), "tools": tools}, ensure_ascii=False, indent=2
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="只读巡检 benchmark 报告与进度（绝不写文件）")
    parser.add_argument("--runs-dir", default=str(_ROOT / "benchmark_runs"), help="报告目录")
    parser.add_argument("--run", default="", help="只看某一份报告（逐任务失败原因）")
    parser.add_argument("--tools", action="store_true", help="只看工具事件分布")
    parser.add_argument("--json", action="store_true", help="机器可读输出")
    args = parser.parse_args(argv)

    if args.json:
        print(summary_json(args.runs_dir))
        return 0

    if args.run:
        report = load_one(args.run, args.runs_dir)
        if report is None:
            print(f"错误：读不到报告 {args.run!r}（目录 {args.runs_dir}）")
            return 2
        print(format_failures(report))
        print()
        print(format_tools([e for e in collect_tool_events(args.runs_dir) if e["run_id"] == args.run]))
        return 0

    if args.tools:
        print(format_tools(collect_tool_events(args.runs_dir)))
        return 0

    print(format_overview(collect_runs(args.runs_dir)))
    print()
    print(format_tools(collect_tool_events(args.runs_dir)))
    print()
    print("提示：`--run <run_id>` 看某一轮的逐任务失败原因；`--json` 给机器读。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
