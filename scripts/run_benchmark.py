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
    ("成功率(排除 provider)", "success_rate_measured", "pct"),
    ("provider 错误", "provider_errors", "int"),
    ("工具选择准确率", "tool_selection_accuracy", "pct"),
    ("工具参数准确率", "tool_argument_accuracy", "pct"),
    ("平均步数", "avg_steps", "num"),
    ("平均轮次", "avg_rounds", "num"),
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


_ABLATION_ROWS = (
    ("任务数", "tasks_total", "int"),
    ("成功率", "success_rate", "pct"),
    ("成功率(排除 provider)", "success_rate_measured", "pct"),
    ("工具选择准确率", "tool_selection_accuracy", "pct"),
    ("平均步数", "avg_steps", "num"),
    ("平均 token", "avg_total_tokens", "num"),
    ("provider 错误", "provider_errors", "int"),
    ("压缩事件", "compaction_events", "int"),
    ("摘要 token", "summarizer_tokens", "int"),
    ("摘要耗时(ms)", "summarizer_ms", "int"),
    ("压缩比", "compression_ratio", "pct"),
)

_ABLATION_ROLE_ROWS = (
    ("followup 任务数", "tasks", "int"),
    ("followup 成功率", "success_rate", "pct"),
    ("followup 平均步数", "avg_steps", "num"),
    ("followup 平均 token", "avg_total_tokens", "num"),
)

_DELTA_ROWS = (
    ("成功率", "success_rate", "pct"),
    ("平均步数", "avg_steps", "num"),
    ("平均 token", "avg_total_tokens", "num"),
    ("摘要 token", "summarizer_tokens", "int"),
    ("压缩事件", "compaction_events", "int"),
    ("followup 成功率", "role.followup.success_rate", "pct"),
    ("followup 平均步数", "role.followup.avg_steps", "num"),
)


def format_ablation(report: object) -> str:
    """消融并排表：各组**绝对指标** + 相对 baseline 的**差值**（绝对与相对）。

    绝对值表回答"三组各自跑成什么样"，差值表回答"比 baseline 好/差多少"。
    `None` 一律 `—`（没测 ≠ 0），相对差在 baseline 为 0 或没测时算不出来，同样是 `—`。
    """
    groups = [str(name) for name in getattr(report, "groups", {})]
    baseline = str(getattr(report, "baseline", "baseline"))
    width = 22
    col = 18
    lines = [
        f"分组对比（baseline = {baseline}）",
        "指标".ljust(width) + "".join(name.rjust(col) for name in groups),
        "─" * (width + col * len(groups)),
    ]

    def row(label: str, values: list[str]) -> str:
        return label.ljust(width) + "".join(value.rjust(col) for value in values)

    reports = getattr(report, "groups", {})
    for label, key, kind in _ABLATION_ROWS:
        values = [
            _render(getattr(reports[name].metrics, key, None), kind) for name in groups
        ]
        lines.append(row(label, values))

    role_metrics = getattr(report, "role_metrics", {})
    for label, key, kind in _ABLATION_ROLE_ROWS:
        values = [
            _render((role_metrics.get(name, {}).get("followup") or {}).get(key), kind)
            for name in groups
        ]
        lines.append(row(label, values))

    lines.append("")
    lines.append("相对 baseline 的差（绝对值 · 相对值）")
    lines.append("指标".ljust(width) + "".join(name.rjust(18) for name in groups))
    lines.append("─" * (width + 18 * len(groups)))
    deltas = getattr(report, "deltas", {})
    for label, key, kind in _DELTA_ROWS:
        values = []
        for name in groups:
            delta = (deltas.get(name) or {}).get(key) or {}
            absolute = _render(delta.get("abs"), kind)
            relative = delta.get("rel")
            relative_text = "—" if relative is None else f"{float(relative) * 100:+.1f}%"
            values.append(f"{absolute} · {relative_text}")
        lines.append(label.ljust(width) + "".join(value.rjust(18) for value in values))
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
        rounds = getattr(verdict, "rounds", 0) or 0
        max_steps = getattr(verdict, "max_steps", 0) or 0
        # 轮次与步数是两个数：一步一轮发多个工具调用时，只看"步数"会误判离上限还有多远
        rounds_text = f"  轮次 {rounds}/{max_steps}" if max_steps else f"  轮次 {rounds}"
        skipped = "  ↻ 续跑复用" if getattr(verdict, "skipped", False) else ""
        lines.append(
            f"{mark} {verdict.task_id}  步数 {getattr(verdict, 'steps', 0)}"
            f"{rounds_text}  token {getattr(verdict, 'total_tokens', 0)}{skipped}{suffix}"
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
            # 搜索配置：缺字段会让启动校验误报，所以兜底里也要有（默认 duckduckgo 免 key）
            web_search_provider="duckduckgo",
            web_search_api_key="",
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


def _grouped_agent_factory():
    """真实 provider + 分组：用**该组覆盖后的** settings 装配（不碰 lru_cache 单例）。

    每组装一次、组内任务共用同一个 agent —— memory 组的 `session_scope=run` 要靠同一份记忆实例
    才成立；逐任务新建记忆实例等于把这一组测成空的。
    """
    try:
        from agent_runtime.api.deps import build_agent_for_settings
    except Exception as e:  # noqa: BLE001
        print(f"错误：真实链路需要完整依赖（fastapi / pydantic_settings / openai）：{type(e).__name__}: {e}")
        return None
    from agent_runtime.infrastructure.benchmark.catalog import real_agent_factory

    def factory_for_settings(grouped):
        agent = build_agent_for_settings(grouped)
        return real_agent_factory(lambda: agent)

    return factory_for_settings


def _fatal_config_errors(settings: object, provider: str) -> list[str]:
    """真实评测开跑前的致命配置校验（mock 不涉及工具，跳过）。

    真实全量实测：`WEB_SEARCH_PROVIDER=tavily` 没填 key 时，研究类任务会白烧到 `max_steps` 才失败 ——
    宁可在开跑前拒绝，也别让 300 条任务在几个小时后告诉你"搜索从来没成功过"。
    """
    from agent_runtime.infrastructure.benchmark.catalog import MOCK_PROVIDER
    from agent_runtime.infrastructure.tools.catalog import search_config_errors

    if (provider or "").strip().lower() == MOCK_PROVIDER:
        return []
    return search_config_errors(settings)


def _model_label(settings: object, provider: str) -> str:
    from agent_runtime.infrastructure.benchmark.catalog import MOCK_PROVIDER

    return "mock" if provider == MOCK_PROVIDER else str(getattr(settings, "llm_model", ""))


def _progress_printer():
    def on_progress(done: int, count: int, verdict: object) -> None:
        if getattr(verdict, "skipped", False):
            print(f"↻ [{done}/{count}] {getattr(verdict, 'task_id', '?')}（已完成，跳过）")
            return
        mark = "✓" if getattr(verdict, "success", False) else "✗"
        print(f"{mark} [{done}/{count}] {getattr(verdict, 'task_id', '?')}")

    return on_progress


def _resumable_run_id(args, runs_dir: str, tasks: list, config: dict) -> str | None:
    """`--resume`：找一条**配置完全相同**且没跑完的单轮进度接着跑。"""
    from agent_runtime.core.benchmark.progress import fingerprint_config
    from agent_runtime.infrastructure.benchmark.progress_log import find_resumable_run

    if not args.resume:
        return None
    scoped = fingerprint_config(config, tasks, args.limit)
    return find_resumable_run(runs_dir, scoped, tasks)


def _run_one_group(args, settings, tasks, runs_dir, group) -> int:
    """跑单组：`--group` 给定时带设置覆盖，否则就是 Phase 6 的常规单轮评测。"""
    from agent_runtime.core.benchmark.ablation import group_overrides
    from agent_runtime.infrastructure.benchmark.catalog import (
        MOCK_PROVIDER,
        build_group_runner,
        build_runner,
    )
    from agent_runtime.infrastructure.benchmark.service import (
        benchmark_config,
        new_run_id,
        run_and_save,
    )

    extra: dict = {}
    agent_factory = None
    if group is None:
        if args.provider != MOCK_PROVIDER:
            agent_factory = _real_agent_factory()
            if agent_factory is None:
                return 2
        runner, build_errors = build_runner(
            settings, args.provider, agent_factory=agent_factory
        )
    else:
        factory_for_settings = None
        if args.provider != MOCK_PROVIDER:
            factory_for_settings = _grouped_agent_factory()
            if factory_for_settings is None:
                return 2
        runner, build_errors = build_group_runner(
            settings,
            args.provider,
            group,
            agent_factory_for_settings=factory_for_settings,
        )
        extra = group_overrides(group)
        print(f"分组：{group.name}（memory={group.memory}, compaction={group.compaction}, "
              f"session_scope={group.session_scope}）")
    for error in build_errors:
        print(f"⚠ {error}")

    config = benchmark_config(
        args.provider, model=_model_label(settings, args.provider), judge=args.judge, **extra
    )
    total = len(tasks) if args.limit is None else min(max(0, args.limit), len(tasks))
    resume_id = _resumable_run_id(args, runs_dir, tasks, config)
    if args.resume:
        if resume_id:
            print(f"续跑：接着 {resume_id} 跑（已成功的条目会跳过，失败的重试）")
        else:
            print("续跑：没找到配置相同的未完成进度，开始新一轮")
    print(f"\n跑 {total} 条任务（provider={args.provider}, judge={config['judge']}）\n" + "─" * 72)

    report = asyncio.run(
        run_and_save(
            runner,
            tasks,
            runs_dir=runs_dir,
            config=config,
            limit=args.limit,
            run_id=resume_id or new_run_id(args.provider),
            on_progress=_progress_printer(),
            resume=bool(resume_id),
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


def _run_ablation(args, settings, tasks, runs_dir) -> int:
    """跑三组并落盘四份文件（三份组报告 + 一份对比报告），最后打印并排表。

    `--resume` 自动接着上一轮没跑完的消融（按"运行身份"匹配：provider/模型/judge/limit/任务集）；
    `--ablation-id ID` 显式钉住某一轮。
    """
    from agent_runtime.core.benchmark.ablation import AblationRunner
    from agent_runtime.core.benchmark.progress import fingerprint_config
    from agent_runtime.infrastructure.benchmark.catalog import (
        MOCK_PROVIDER,
        build_group_runner,
    )
    from agent_runtime.infrastructure.benchmark.progress_log import find_resumable_ablation
    from agent_runtime.infrastructure.benchmark.service import new_run_id, run_ablation

    factory_for_settings = None
    if args.provider != MOCK_PROVIDER:
        factory_for_settings = _grouped_agent_factory()
        if factory_for_settings is None:
            return 2

    build_errors: list[str] = []

    def runner_factory(group):
        runner, errors = build_group_runner(
            settings,
            args.provider,
            group,
            agent_factory_for_settings=factory_for_settings,
        )
        build_errors.extend(errors)
        return runner

    probe = fingerprint_config(
        {
            "provider": args.provider,
            "model": _model_label(settings, args.provider),
            "judge": max(0, int(args.judge or 0)),
        },
        tasks,
        args.limit,
    )
    ablation_id = args.ablation_id
    if not ablation_id and args.resume:
        ablation_id = find_resumable_ablation(runs_dir, probe, tasks)
        if ablation_id:
            print(f"续跑：接着 {ablation_id} 跑（三组各自跳过已成功的条目，失败的重试）")
        else:
            print("续跑：没找到配置相同的未完成消融，开始新一轮")
    resume = bool(args.resume or args.ablation_id)
    if not ablation_id:
        ablation_id = f"{new_run_id(args.provider)}-ablation"

    total = len(tasks) if args.limit is None else min(max(0, args.limit), len(tasks))
    print(
        f"\n消融：三组各跑 {total} 条任务（provider={args.provider}, judge={args.judge}）\n"
        f"ablation id：{ablation_id}\n" + "─" * 72
    )
    report = asyncio.run(
        run_ablation(
            AblationRunner(runner_factory),
            tasks,
            runs_dir=runs_dir,
            provider=args.provider,
            model=_model_label(settings, args.provider),
            judge=args.judge,
            limit=args.limit,
            ablation_id=ablation_id,
            on_group=lambda name: print(f"\n=== 分组 {name} ==="),
            on_progress=_progress_printer(),
            resume=resume,
        )
    )
    for error in build_errors:
        print(f"⚠ {error}")

    print("\n" + "─" * 72)
    print(format_ablation(report))
    print(f"\n对比报告：{Path(runs_dir) / (report.ablation_id + '.json')}")
    print("三份组报告同在报告目录里（文件名以对比报告 id 开头）。")
    if report.config.get("save_error"):
        print(f"⚠ 报告落盘失败：{report.config['save_error']}")
    print(
        "方法说明：三组同一任务集/同一模型/同一版本代码，只改开关；`compaction=off` **只等于 SUMMARIZE 关**"
        "（SQUEEZE/TRUNCATE 无条件生效）；memory 组用运行内共享会话，组内靠后的任务受益于靠前的任务，"
        "所以**不要**拿它和 baseline 的绝对名次直接比。"
    )
    if args.provider == MOCK_PROVIDER:
        print("注意：provider=mock 是离线夹具，这里的对比表只证明管线通，不是真实结论。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Benchmark：跑任务集并产出报告（含消融三组对照）")
    parser.add_argument(
        "--provider", choices=["mock", "real"], default="mock",
        help="mock = 离线假 agent（不需要 key）；real = 真实 LLM",
    )
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 条（默认全量；消融时三组各跑 N 条）")
    parser.add_argument("--judge", type=int, default=0, help="对前 N 条做 LLM 裁判（默认 0 = 不跑）")
    parser.add_argument("--tasks", default="", help="任务集 JSON 路径（默认取 BENCHMARK_TASKS_FILE）")
    parser.add_argument("--out", default="", help="报告落盘目录（默认取 BENCHMARK_RUNS_DIR）")
    parser.add_argument(
        "--group", default="",
        help="只跑某一组并带设置覆盖：baseline / memory / memory+compaction",
    )
    parser.add_argument(
        "--ablation", action="store_true",
        help="跑三组并落盘四份报告 + 打印并排对比（会跑三倍时间/花费）",
    )
    parser.add_argument(
        "--resume", action="store_true",
        help="接着上一次没跑完的那一轮跑：配置相同的进度文件里已成功的条目直接跳过（省调用）",
    )
    parser.add_argument(
        "--ablation-id", default="",
        help="显式指定消融轮次 id（配合 --resume 续跑；不给则自动新建或用 --resume 自动找）",
    )
    args = parser.parse_args(argv)

    from agent_runtime.core.benchmark.ablation import find_group
    from agent_runtime.core.benchmark.dataset import load_tasks
    from agent_runtime.infrastructure.benchmark.catalog import (
        MOCK_PROVIDER,
        resolve_runs_dir,
        resolve_tasks_file,
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

    if args.ablation and args.group:
        print(f"注意：同时给了 --ablation 与 --group，按 --ablation 跑三组（--group {args.group} 被忽略）")

    fatal = _fatal_config_errors(settings, args.provider)
    if fatal:
        for problem in fatal:
            print(f"错误：{problem}")
        print("已拒绝开跑（真实评测不该拿注定失败的配置烧几小时）。")
        return 2

    from agent_runtime.infrastructure.benchmark.service import ResumeError

    try:
        if args.ablation:
            return _run_ablation(args, settings, tasks, runs_dir)

        group = None
        if args.group:
            group = find_group(args.group)
            if group is None:
                print(
                    f"错误：未知的分组 {args.group!r}；可选：baseline / memory / memory+compaction"
                )
                return 2
        return _run_one_group(args, settings, tasks, runs_dir, group)
    except ResumeError as e:
        print(f"错误：{e}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
