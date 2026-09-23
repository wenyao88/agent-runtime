"""指标口径契约（`core/benchmark/metrics.py`）。

这里是整个 Phase 6 最容易"看起来对、其实在骗人"的地方，所以口径逐条钉死：

* **分母为 0 → `None`，不是 0**：0 会被读成"很差"，而真实含义是"没测"。
  四种情形都要 `None`：空任务集、没有任何任务声明 expected_args、没有任何任务工具失败、没有任何压缩事件。
* **参数命中率只在声明了 expected_args 的任务上算**（研究类任务没法事先声明 query 参数，
  硬算会把"参数合理"判成 0 分）。
* **错误恢复率的分母是"发生过工具失败的任务"**，不是工具调用数。
* 任何除零都不能抛（`before=0` 的压缩事件是可能的：刚起任务就压）。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from agent_runtime.core.benchmark.metrics import summarize  # noqa: E402
from agent_runtime.core.benchmark.models import (  # noqa: E402
    BenchmarkTask,
    CompactionEvent,
    TaskRun,
    TaskVerdict,
    ToolEvent,
)


def _verdict(
    task_id: str = "t1",
    *,
    success: bool = True,
    tools_ok: bool = True,
    hits: int = 0,
    expected: int = 0,
    steps: int = 2,
    tokens: int = 100,
    latency: int = 10,
    scores: dict | None = None,
    error: str = "",
) -> TaskVerdict:
    return TaskVerdict(
        task_id=task_id,
        success=success,
        required_tools_ok=tools_ok,
        arg_hits=hits,
        arg_expected=expected,
        steps=steps,
        total_tokens=tokens,
        latency_ms=latency,
        judge_scores=scores,
        error=error,
    )


def _run(
    task_id: str = "t1",
    *,
    successes: tuple[bool, ...] = (True,),
    compactions: list[CompactionEvent] | None = None,
) -> TaskRun:
    return TaskRun(
        task=BenchmarkTask(task_id=task_id, task="做事"),
        tool_events=[
            ToolEvent(step=i + 1, tool="read_file", success=ok)
            for i, ok in enumerate(successes)
        ],
        compactions=list(compactions or []),
    )


# ── 空集与除零 ──


def test_empty_input_gives_no_metrics_at_all() -> None:
    metrics = summarize([], [])
    assert metrics.tasks_total == 0
    assert metrics.tasks_errored == 0
    for name in (
        "success_rate",
        "tool_selection_accuracy",
        "tool_argument_accuracy",
        "avg_steps",
        "avg_total_tokens",
        "avg_latency_ms",
        "compression_ratio",
        "error_recovery_rate",
        "avg_judge_score",
    ):
        assert getattr(metrics, name) is None, name
    assert metrics.compression_by_strategy == {}
    assert metrics.judged_tasks == 0


# ── 率 ──


def test_rates_are_fractions_of_tasks() -> None:
    metrics = summarize(
        [
            _verdict("a", success=True, tools_ok=True),
            _verdict("b", success=True, tools_ok=False),
            _verdict("c", success=False, tools_ok=True),
            _verdict("d", success=False, tools_ok=False),
        ],
        [],
    )
    assert metrics.tasks_total == 4
    assert metrics.success_rate == 0.5
    assert metrics.tool_selection_accuracy == 0.5


def test_argument_accuracy_is_none_when_nothing_declares_arguments() -> None:
    metrics = summarize([_verdict(hits=0, expected=0), _verdict(hits=0, expected=0)], [])
    assert metrics.tool_argument_accuracy is None, "没有声明就不能算成 0 分"


def test_argument_accuracy_pools_hits_over_declared_pairs() -> None:
    """池化 vs 逐任务均值**只有在分母不对称时才区分得开**（1/1 + 0/3 → 0.25 而非 0.5）。"""
    metrics = summarize(
        [_verdict("a", hits=1, expected=1), _verdict("b", hits=0, expected=3)], []
    )
    assert metrics.tool_argument_accuracy == 0.25


# ── 均值 ──


def test_averages_over_verdicts() -> None:
    metrics = summarize(
        [
            _verdict("a", steps=2, tokens=100, latency=10),
            _verdict("b", steps=4, tokens=300, latency=30),
        ],
        [],
    )
    assert metrics.avg_steps == 3.0
    assert metrics.avg_total_tokens == 200.0
    assert metrics.avg_latency_ms == 20.0


# ── 错误恢复 ──


def test_error_recovery_rate_is_none_when_nothing_failed() -> None:
    metrics = summarize([_verdict("a")], [_run("a", successes=(True, True))])
    assert metrics.error_recovery_rate is None


def test_error_recovery_counts_tasks_with_failures() -> None:
    """分母是"发生过工具失败的任务"，不是工具调用数。"""
    metrics = summarize(
        [_verdict("a", success=True), _verdict("b", success=False), _verdict("c", success=True)],
        [
            _run("a", successes=(False, True)),   # 失败过，最终成功 → 恢复
            _run("b", successes=(False, False)),  # 失败过，最终失败
            _run("c", successes=(True, True)),    # 没失败过 → 不进分母
        ],
    )
    assert metrics.error_recovery_rate == 0.5


# ── 压缩 ──


def test_compression_ratio_is_none_without_events() -> None:
    metrics = summarize([_verdict("a")], [_run("a")])
    assert metrics.compression_ratio is None
    assert metrics.compression_by_strategy == {}


def test_compression_ratio_and_per_strategy_split() -> None:
    metrics = summarize(
        [_verdict("a")],
        [
            _run(
                "a",
                compactions=[
                    CompactionEvent(before=1000, after=400, strategy="truncate"),
                    CompactionEvent(before=500, after=450, strategy="summarize"),
                ],
            )
        ],
    )
    assert metrics.compression_ratio == 650 / 1500
    assert metrics.compression_by_strategy["truncate"] == 0.6
    assert metrics.compression_by_strategy["summarize"] == 0.1


def test_compression_survives_a_zero_before() -> None:
    metrics = summarize(
        [_verdict("a")],
        [_run("a", compactions=[CompactionEvent(before=0, after=0, strategy="truncate")])],
    )
    assert metrics.compression_ratio is None, "没有可压缩的量 → None，而不是除零"
    assert metrics.compression_by_strategy == {"truncate": None}


# ── 裁判 ──


def test_judge_totals_only_count_judged_tasks() -> None:
    """逐任务均值 vs 池化**只有在分数条数不等时才区分得开**（(5,5)+1 → 3.0 而非 3.667）。"""
    metrics = summarize(
        [
            _verdict("a", scores={"completion": 5, "accuracy": 5}),
            _verdict("b", scores=None),
            _verdict("c", scores={"completion": 1}),
        ],
        [],
    )
    assert metrics.judged_tasks == 2
    assert metrics.avg_judge_score == 3.0


def test_crashed_tasks_do_not_drag_the_averages_to_zero() -> None:
    """回归（审查 I2）：崩掉的任务带着 0 步 0 token 混进均值 = 拿 0 冒充测量值。"""
    metrics = summarize(
        [
            _verdict("ok", steps=2, tokens=500, latency=900),
            _verdict("crash", success=False, steps=0, tokens=0, latency=0, error="AttributeError: x"),
        ],
        [],
    )
    assert metrics.tasks_errored == 1
    assert metrics.avg_steps == 2.0, metrics.avg_steps
    assert metrics.avg_total_tokens == 500.0
    assert metrics.avg_latency_ms == 900.0
    assert metrics.success_rate == 0.5, "但成功率仍要把崩掉的任务算作失败"


def test_all_crashed_gives_none_not_zero() -> None:
    metrics = summarize([_verdict("crash", success=False, error="boom")], [])
    assert metrics.tasks_errored == 1
    assert metrics.avg_steps is None
    assert metrics.avg_total_tokens is None
    assert metrics.avg_latency_ms is None


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
