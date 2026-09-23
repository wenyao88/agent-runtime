"""给 API 与 CLI 共用的薄服务层：run_id 生成 + 跑完落盘。

两条要求：
  1. `new_run_id` **带上 provider 标签** —— 从文件名就能看出这是 mock 还是真实成绩；
  2. 落盘失败**不能弄丢报告**：结果先返回给调用方，失败原因写进 `config["save_error"]`
     （可见，不静默 —— 这是本项目反复强调的规矩）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from ...core.benchmark.models import BenchmarkReport
from .store import save_report


def new_run_id(provider_label: str, *, now: datetime | None = None) -> str:
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    label = (provider_label or "").strip().lower() or "run"
    return f"{stamp}-{label}"


async def run_and_save(
    runner: Any,
    tasks: list,
    *,
    runs_dir: str,
    config: dict | None = None,
    limit: int | None = None,
    run_id: str | None = None,
    on_progress: Any = None,
) -> BenchmarkReport:
    """跑一轮评测并落盘。`run_id` 给定时固定用它（API 要先返回 run_id 再后台跑）。"""
    report = await runner.run(
        tasks, config=config, limit=limit, on_progress=on_progress, run_id=run_id
    )
    try:
        save_report(report, runs_dir)
    except (OSError, ValueError) as e:
        report.config["save_error"] = f"{type(e).__name__}: {e}"
    return report
