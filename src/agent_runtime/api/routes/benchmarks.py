"""Benchmark 的三个端点（薄适配：逻辑在 `infrastructure/benchmark/` 与 `core/benchmark/`）。

三个端点与上游 spec §6 一致；**不做** `POST /ablation`（那是 Phase 7）。
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/benchmarks", tags=["benchmarks"])


class RunRequest(BaseModel):
    provider: Literal["mock", "real"] = "mock"
    limit: int | None = None
    judge: int = 0


@router.get("")
async def list_benchmarks() -> dict:
    """历史报告小结（读落盘目录；坏文件被跳过，不会整体 500）。"""
    from ..deps import list_benchmark_runs

    runs = list_benchmark_runs()
    return {"count": len(runs), "runs": runs}


@router.get("/{run_id}")
async def get_benchmark(run_id: str) -> dict:
    from ..deps import load_benchmark_report

    report = load_benchmark_report(run_id)
    if report is None:
        raise HTTPException(status_code=404, detail=f"没有这份报告：{run_id}")
    return report.to_dict()


@router.post("/run")
async def start_benchmark_run(request: RunRequest | None = None) -> dict:
    """后台跑一次评测，**立即**返回 run_id（轮询 `GET /api/benchmarks/{run_id}` 取结果）。"""
    from ..deps import start_benchmark_run as start

    req = request or RunRequest()
    return start(provider=req.provider, limit=req.limit, judge=req.judge)
