"""给 API 与 CLI 共用的薄服务层：run_id 生成 + 跑完落盘 + **断点续跑**（单组 / 消融）。

四条要求：
  1. `new_run_id` **带上 provider 标签** —— 从文件名就能看出这是 mock 还是真实成绩；
  2. 落盘失败**不能弄丢报告**：结果先返回给调用方，失败原因写进 `config["save_error"]`
     （可见，不静默 —— 这是本项目反复强调的规矩）；
  3. 消融落盘是**四份文件**（三份组报告 + 一份对比报告）：少一份就少一个回看点；
     **每组跑完立刻写该组报告**（三组要跑几小时，不能等三组全完）；
  4. **每条任务判分后立刻追加进度文件**（`<run_id>.progress.jsonl`），重跑时跳过已成功的条目：
     100 条 × 3 组断一次就全重来是不可接受的。指纹不匹配时**拒绝续跑**（`ResumeError`），
     绝不把两套配置的结果混进一份报告。
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime
from typing import Any

from ...core.benchmark.ablation import (
    AblationReport,
    group_config,
    group_run_id,
)
from ...core.benchmark.models import BenchmarkReport
from ...core.benchmark.progress import (
    fingerprint,
    fingerprint_config,
    resume_map,
    same_fingerprint,
    task_record,
)
from .catalog import MOCK_PROVIDER
from .progress_log import ProgressLog
from .store import save_ablation, save_report

_logger = logging.getLogger(__name__)


class ResumeError(RuntimeError):
    """请求续跑但接不上（指纹不同 / 进度文件已属于另一轮）—— 宁可报错，不要混结果。"""


def _progress_writer(log: ProgressLog, config: dict, tasks: list, *, ablation_id: str = ""):
    """返回 `(on_verdict, failures)`：进度写入**尽力而为但可见**。

    进度文件写不进去（目录只读、磁盘满）**绝不能中断几小时的评测** —— 报告照样要跑完、
    要落盘，失败原因记进 `config["progress_error"]` 并打日志（静默丢进度是不能接受的：
    那意味着下一次重跑又从零开始，而用户以为有进度）。
    """
    failures: list[str] = []
    state = {"broken": False}

    def note(kind: str, error: Exception) -> None:
        message = f"{kind}: {type(error).__name__}: {error}"
        failures.append(message)
        _logger.warning("benchmark: 进度文件写入失败（%s）：%s", log.path, message)

    try:
        log.write_header(config, tasks, ablation_id=ablation_id)
    except OSError as e:
        state["broken"] = True
        note("write_header", e)

    def on_verdict(run: Any, verdict: Any) -> None:
        if state["broken"]:
            return
        try:
            log.append(task_record(run, verdict))
        except OSError as e:
            state["broken"] = True  # 坏了就只报一次，不必每条都重试
            note("append", e)

    return on_verdict, failures


def _progress_reader(log: ProgressLog) -> Any:
    """读进度文件；失败只记警告（读不出来 = 没有进度，不是崩溃）。"""
    try:
        read = log.read()
    except OSError as e:  # pragma: no cover —— read() 内部已兜住 OSError，这里只是最后一道
        _logger.warning("benchmark: 进度文件读不出来（%s）：%s", log.path, e)
        return None
    for warning in read.warnings:
        _logger.warning("benchmark: %s（%s）", warning, log.run_id)
    return read


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
    resume: bool = False,
) -> BenchmarkReport:
    """跑一轮评测并落盘；**每条任务判分后立刻追加进度文件**。

    `resume=True` 时读取 `<run_id>.progress.jsonl` 里**已成功**的条目并跳过它们（不调用 LLM）；
    指纹对不上就抛 `ResumeError`（不混结果）。`run_id` 给定时固定用它（API 要先返回 run_id 再后台跑）。
    """
    cfg = dict(config or {})
    final_id = run_id or new_run_id(str(cfg.get("provider") or ""))
    log = ProgressLog(runs_dir, final_id)
    scoped = fingerprint_config(cfg, tasks, limit)
    done: dict = {}
    if resume:
        read = _progress_reader(log)
        if read is not None and read.header is not None and not same_fingerprint(
            read.header.get("fingerprint") or {}, fingerprint(scoped, tasks)
        ):
            raise ResumeError(
                f"进度文件与本次配置不一致，拒绝续跑（run_id={final_id}）："
                "换模型/换 limit/换任务集都会改变指纹，混跑出来的指标是两套配置的混合物"
            )
        if read is not None:
            done = resume_map(tasks, read.records)
    elif log.exists():
        raise ResumeError(
            f"进度文件已存在（run_id={final_id}）：要接着跑请加 --resume，"
            "否则新一轮会盖在同一份进度上"
        )

    on_verdict, progress_failures = _progress_writer(log, scoped, tasks)
    report = await runner.run(
        tasks,
        config=cfg,
        limit=limit,
        on_progress=on_progress,
        run_id=final_id,
        done=done or None,
        on_verdict=on_verdict,
    )
    skipped = sum(1 for verdict in report.verdicts if getattr(verdict, "skipped", False))
    if skipped:
        # 报告自证：这一轮有多少条是从进度文件捡回来的
        report.config["resumed"] = skipped
    if progress_failures:
        report.config["progress_error"] = "；".join(progress_failures)
    try:
        save_report(report, runs_dir)
    except Exception as e:  # noqa: BLE001 —— 落盘怎么坏都不能弄丢报告（含 JSON 序列化失败）
        # CLI 会打印它；API 路径下报告根本没落盘，所以 `config["save_error"]` 谁也读不到 ——
        # 必须**打日志**才算"不静默"（审查 I3）
        report.config["save_error"] = f"{type(e).__name__}: {e}"
        _logger.warning(
            "benchmark: 报告落盘失败（run_id=%s, dir=%s）：%s", report.run_id, runs_dir, e
        )
    return report


async def run_ablation(
    ablation_runner: Any,
    tasks: list,
    *,
    runs_dir: str,
    provider: str,
    model: str = "",
    judge: int = 0,
    limit: int | None = None,
    ablation_id: str | None = None,
    on_group: Any = None,
    on_progress: Any = None,
    resume: bool = False,
) -> AblationReport:
    """跑三组并落盘**四份**文件：三份组报告 + 一份对比报告。落盘失败绝不弄丢结果。

    断点续跑：三组各有**自己**的 `<组 run_id>.progress.jsonl`，每条任务判分后立刻追加；
    `resume=True` 时按组跳过已成功的条目（每组各认各的进度）。组报告在**该组跑完时立刻**落盘，
    所以就算第三组崩了，前两组的报告也已经在磁盘上。

    不续跑（`resume=False`）却撞上已有进度文件时抛 `ResumeError`：绝不把两轮写进同一份进度。
    """
    groups = list(getattr(ablation_runner, "groups", ()))
    final_id = ablation_id or f"{new_run_id(provider)}-ablation"
    scoped_limit = limit

    def group_scope(group) -> dict:
        return fingerprint_config(
            group_config(group, provider=provider, model=model, judge=judge),
            tasks,
            scoped_limit,
        )

    def group_log(group) -> ProgressLog:
        return ProgressLog(runs_dir, group_run_id(final_id, group))

    if not resume:
        existing = [group.name for group in groups if group_log(group).exists()]
        if existing:
            raise ResumeError(
                f"{final_id} 已有进度文件（{', '.join(existing)}）：要接着跑请加 --resume，"
                "否则新一轮会盖在同一份进度上"
            )

    progress_failures: list[str] = []
    writers: dict[str, Any] = {}

    def done_for_group(group) -> dict:
        log = group_log(group)
        read = _progress_reader(log)
        if read is not None and read.header is not None and not same_fingerprint(
            read.header.get("fingerprint") or {}, fingerprint(group_scope(group), tasks)
        ):
            raise ResumeError(
                f"进度文件与本次配置不一致，拒绝续跑（{log.run_id}）："
                "换模型/换 limit/换任务集都会改变指纹"
            )
        return resume_map(tasks, read.records) if read is not None else {}

    def on_verdict_for_group(group):
        log = group_log(group)
        on_verdict, failures = _progress_writer(
            log, group_scope(group), tasks, ablation_id=final_id
        )
        writers[group.name] = failures
        return on_verdict

    failures: list[str] = []

    def on_group_done(name: str, group_report: BenchmarkReport) -> None:
        skipped = sum(
            1 for verdict in group_report.verdicts if getattr(verdict, "skipped", False)
        )
        if skipped:
            group_report.config["resumed"] = skipped
        if writers.get(name):
            group_report.config["progress_error"] = "；".join(writers[name])
        try:
            # 这一组已经花掉的时间立刻变成磁盘上的报告（不等另外两组）
            save_report(group_report, runs_dir)
        except Exception as e:  # noqa: BLE001 —— 落盘怎么坏都不能弄丢结果
            failures.append(f"{name}: {type(e).__name__}: {e}")

    report = await ablation_runner.run(
        tasks,
        provider=provider,
        model=model,
        judge=judge,
        limit=limit,
        ablation_id=final_id,
        on_group=on_group,
        on_group_done=on_group_done,
        on_progress=on_progress,
        done_for_group=done_for_group,
        on_verdict_for_group=on_verdict_for_group,
    )
    try:
        save_ablation(report, runs_dir)
    except Exception as e:  # noqa: BLE001
        failures.append(f"对比报告: {type(e).__name__}: {e}")
    if failures:
        joined = "；".join(failures)
        report.config["save_error"] = joined
        _logger.warning(
            "benchmark: 消融落盘失败（ablation_id=%s, dir=%s）：%s",
            report.ablation_id,
            runs_dir,
            joined,
        )
    return report
