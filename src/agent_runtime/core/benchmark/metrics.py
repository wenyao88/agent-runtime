"""8 个核心指标（纯函数）。口径见 spec §5 与 `tests/unit/test_benchmark_metrics.py` 顶部。

一句话原则：**分母为 0 的指标返回 `None`，不是 0** —— 0 会被读成"很差"，而真实含义是"没测"。
"""
from __future__ import annotations

from .models import (
    BenchmarkMetrics,
    CompactionEvent,
    TaskRun,
    TaskVerdict,
)


def _rate(numerator: int, denominator: int) -> float | None:
    return (numerator / denominator) if denominator > 0 else None


def _mean(values: list[int]) -> float | None:
    return (sum(values) / len(values)) if values else None


def _judge_average(scores: dict | None) -> float | None:
    if not isinstance(scores, dict):
        return None
    numbers = [v for v in scores.values() if isinstance(v, (int, float))]
    return (sum(numbers) / len(numbers)) if numbers else None


def _compression_ratio(events: list[CompactionEvent]) -> float | None:
    before = sum(e.before for e in events)
    if before <= 0:
        return None
    return (before - sum(e.after for e in events)) / before


def summarize(
    verdicts: list[TaskVerdict], runs: list[TaskRun]
) -> BenchmarkMetrics:
    """把逐任务判分与原始运行记录汇总成 8 个指标。绝不抛（空集/垃圾输入都返回 None 而不是异常）。"""
    metrics = BenchmarkMetrics(tasks_total=len(verdicts))
    if not verdicts:
        return metrics

    metrics.success_rate = _rate(sum(1 for v in verdicts if v.success), len(verdicts))
    metrics.tool_selection_accuracy = _rate(
        sum(1 for v in verdicts if v.required_tools_ok), len(verdicts)
    )

    # 参数命中：只在**声明过 expected_args** 的任务上算（研究类任务没法事先声明 query 参数）
    metrics.tool_argument_accuracy = _rate(
        sum(v.arg_hits for v in verdicts), sum(v.arg_expected for v in verdicts)
    )

    # 均值只在**真的产出了结果**的任务上算：崩掉的任务是"没测"，不是"0 步 0 token"
    # （`TaskVerdict.error` 的含义就是"这条任务没能产出结果"）
    measured = [v for v in verdicts if not v.error]
    metrics.tasks_errored = len(verdicts) - len(measured)
    metrics.avg_steps = _mean([v.steps for v in measured])
    metrics.avg_total_tokens = _mean([v.total_tokens for v in measured])
    metrics.avg_latency_ms = _mean([v.latency_ms for v in measured])

    # 压缩比来自事件流（`AgentResult` 里没有压缩信息）
    all_events = [e for run in runs for e in run.compactions]
    metrics.compression_ratio = _compression_ratio(all_events)
    by_strategy: dict[str, float | None] = {}
    for strategy in sorted({e.strategy for e in all_events}):
        by_strategy[strategy] = _compression_ratio(
            [e for e in all_events if e.strategy == strategy]
        )
    metrics.compression_by_strategy = by_strategy

    # 错误恢复：分母是"发生过工具失败的任务"
    success_by_id = {v.task_id: v.success for v in verdicts}
    failing = [
        run.task.task_id
        for run in runs
        if any(not e.success for e in run.tool_events)
        and run.task.task_id in success_by_id  # 没有对应判分的孤儿运行不算进分母
    ]
    metrics.error_recovery_rate = _rate(
        sum(1 for tid in failing if success_by_id.get(tid)), len(failing)
    )

    judged = [_judge_average(v.judge_scores) for v in verdicts]
    judged = [score for score in judged if score is not None]
    metrics.judged_tasks = len(judged)
    metrics.avg_judge_score = _mean(judged) if judged else None
    return metrics
