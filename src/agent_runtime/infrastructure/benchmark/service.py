"""给 API 与 CLI 共用的薄服务层：run_id 生成 + 跑完落盘。

两条要求：
  1. `new_run_id` **带上 provider 标签** —— 从文件名就能看出这是 mock 还是真实成绩；
  2. 落盘失败**不能弄丢报告**：结果先返回给调用方，失败原因写进 `config["save_error"]`
     （可见，不静默 —— 这是本项目反复强调的规矩）。
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime
from typing import Any

from ...core.benchmark.models import BenchmarkReport
from .catalog import MOCK_PROVIDER
from .store import save_report

_logger = logging.getLogger(__name__)


def new_run_id(provider_label: str, *, now: datetime | None = None) -> str:
    """`<时间戳>-<provider>-<4 位随机>`。

    随机后缀是必需的：时间戳只有秒级精度，同一秒内两次触发会**撞名并静默覆盖**上一份报告（审查 M3）。
    """
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    label = (provider_label or "").strip().lower() or "run"
    return f"{stamp}-{label}-{secrets.token_hex(2)}"


def benchmark_config(
    provider_label: str, *, model: str = "", judge: int = 0, **extra: object
) -> dict:
    """报告配置的**唯一**构造处。

    `mock` 必须带上 `synthetic: True`：Phase 7 会把多份报告放在一起对比，
    夹具与真实成绩混在一起就是假数据（审查 M9）。
    `extra` 供消融分组写入开关快照（`group`/`memory`/`compaction`/`session_scope`）。
    """
    label = (provider_label or "").strip().lower() or "run"
    config: dict = {
        "provider": label,
        "model": model or ("mock" if label == MOCK_PROVIDER else ""),
        "judge": max(0, int(judge or 0)),
    }
    if label == MOCK_PROVIDER:
        config["synthetic"] = True
    config.update(extra)
    return config


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
        # CLI 会打印它；API 路径下报告根本没落盘，所以 `config["save_error"]` 谁也读不到 ——
        # 必须**打日志**才算"不静默"（审查 I3）
        report.config["save_error"] = f"{type(e).__name__}: {e}"
        _logger.warning(
            "benchmark: 报告落盘失败（run_id=%s, dir=%s）：%s", report.run_id, runs_dir, e
        )
    return report
