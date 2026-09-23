"""Benchmark 装配（与 `tools` / `skills` / `memory` / `context` 的 catalog 同一先例）。

`core/benchmark` 只做纯逻辑；这里回答"用哪个 provider、裁判怎么造、任务集与报告落在哪"。

**真实 agent 的装配不在这里**：infrastructure 不反向依赖 `api`。真实路径由调用方注入
`agent_factory`（`api/deps.py` 与 CLI 各自把自己的 `get_agent` 传进来）—— 这样既守分层，
也避免第三次出现"两处装配慢慢漂移"（Phase 4 的 memory、Phase 5 的 context 都栽过）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ...core.benchmark.runner import BenchmarkRunner
from .judge import build_judge
from .mock_agent import MockBenchmarkAgent

MOCK_PROVIDER = "mock"


def build_runner(
    settings: Any,
    provider_label: str,
    *,
    agent_factory: Any = None,
    judge_provider_factory: Any = None,
) -> tuple[BenchmarkRunner, list[str]]:
    """按 provider 装配 runner，返回 `(runner, 错误列表)`。**绝不抛**。

    * `mock`：自带离线假 agent（不需要 key、不 import 任何第三方）；
    * 其它：必须由调用方注入 `agent_factory`，否则每个任务都会以可读错误失败（不会假装成功）。
    """
    errors: list[str] = []
    label = (provider_label or "").strip().lower()
    factory = agent_factory
    if factory is None:
        if label == MOCK_PROVIDER:
            factory = MockBenchmarkAgent
        else:
            errors.append(
                f"provider={provider_label!r} 需要调用方注入 agent_factory"
                "（真实 agent 的装配在 api/deps.get_agent；infrastructure 不反向依赖 api）"
            )

            def factory(_task, _label=label):  # type: ignore[misc]
                raise RuntimeError(f"provider {_label!r} 没有可用的 agent 工厂")

    judge = build_judge(settings, provider_factory=judge_provider_factory)
    return BenchmarkRunner(factory, judge=judge), errors


def real_agent_factory(get_agent: Any) -> Any:
    """把 `get_agent` 适配成 benchmark 的 agent 工厂。

    **必须包一层**：`get_agent(llm=None)` 的第一个形参是 llm，而 runner 会用 `factory(task)` 调用它 ——
    直接把 task 透传就会让 **BenchmarkTask 变成"模型"**：每条任务都以
    `AttributeError: 'BenchmarkTask' object has no attribute 'chat'` 失败，然后落出一份
    "成功率 0%"、**看起来像真实成绩**的报告（审查 C1 实测复现）。
    """

    def factory(_task: Any) -> Any:
        return get_agent()

    return factory


def resolve_tasks_file(settings: Any, project_root: str) -> str:
    """相对路径按**项目根**解析（与 `skills_dir` 同一约定），不按进程 CWD。"""
    raw = str(getattr(settings, "benchmark_tasks_file", "") or "benchmarks/tasks.json")
    path = Path(raw)
    return str(path if path.is_absolute() else Path(project_root) / path)


def resolve_runs_dir(settings: Any, project_root: str) -> str:
    raw = str(getattr(settings, "benchmark_runs_dir", "") or "benchmark_runs")
    path = Path(raw)
    return str(path if path.is_absolute() else Path(project_root) / path)
