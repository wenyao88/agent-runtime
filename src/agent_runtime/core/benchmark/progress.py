"""进度记录：把"已完成的一条任务"序列化成**可续跑的最小集**（纯逻辑，零第三方依赖）。

为什么需要它：100 条 × 3 组的真实消融要跑几小时，中途限流、断网或被 Ctrl-C 都正常。
没有进度文件时，第 250 条崩掉 = 三组全部重来（约 300 次真实调用白烧）。所以每条任务判分后
立刻追加一行到 `<run_id>.progress.jsonl`，重跑时**跳过已成功的条目**、只补没成功的。

这里只做**数据形状**（字典 ↔ 数据类）与**配置指纹**；真正的文件 IO 在
`infrastructure/benchmark/progress_log.py`（core 不碰磁盘）。

两条口径：
  * 只有 `success=True` 的条目会被跳过（失败的下次重试 —— 限流/超时正是要重试的东西）；
  * 恢复出来的判分带 `skipped=True` 标记（报告里能一眼看出哪些是续跑来的，而不是假装刚跑过）。
"""
from __future__ import annotations

import hashlib
from dataclasses import fields, replace
from typing import Any

from .models import BenchmarkTask, CompactionEvent, TaskRun, TaskVerdict, ToolEvent

KIND_TASK = "task"
_KIND_HEADER = "progress"

RUN_FINGERPRINT_FIELDS = (
    "provider",
    "model",
    "judge",
    "limit",
    "tasks_total",
    "task_ids_hash",
    "group",
    "memory",
    "compaction",
    "session_scope",
)
"""续跑必须保证"同一件事"：这些字段任一变（换模型、换分组、换 limit、换任务集）都不许混跑。"""

MATCH_FIELDS = ("provider", "model", "judge", "limit", "tasks_total", "task_ids_hash")
"""跨组匹配用（找"上一次没跑完的那轮消融"）：不含分组开关，因为三组本来就各不相同。"""


def task_ids_hash(tasks: list[BenchmarkTask]) -> str:
    """任务集身份的廉价指纹：id 集合变了（增删/改名）就不再续跑。"""
    joined = "\n".join(sorted(task.task_id for task in tasks))
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()[:12]


def fingerprint(config: dict, tasks: list[BenchmarkTask]) -> dict[str, Any]:
    """从报告 config 里取出续跑关心的字段；`task_ids_hash` 由当前任务集算。"""
    out: dict[str, Any] = {}
    for name in RUN_FINGERPRINT_FIELDS:
        if name == "task_ids_hash":
            out[name] = task_ids_hash(tasks)
        elif name in config:
            out[name] = config[name]
    return out


def fingerprint_config(
    config: dict, tasks: list[BenchmarkTask], limit: int | None
) -> dict:
    """把 `tasks_total`/`limit` 补进 config，让指纹与 runner 写进报告的 config **同口径**。

    少补这两个字段，指纹就会永远对不上（`fingerprint` 里它们的值是 `None`，而落盘的 header
    里是真值）—— 续跑会静默失效。这是"唯一口径"该待的地方。
    """
    out = dict(config or {})
    out["tasks_total"] = len(tasks) if limit is None else min(max(0, limit), len(tasks))
    out["limit"] = limit
    return out


def same_fingerprint(left: dict, right: dict) -> bool:
    """**完全相同**才允许在同一组里续跑（少一个字段也算不同：宁可重跑，不要混结果）。"""
    return {k: left.get(k) for k in RUN_FINGERPRINT_FIELDS} == {
        k: right.get(k) for k in RUN_FINGERPRINT_FIELDS
    }


def matches_run(left: dict, right: dict) -> bool:
    """只比"这一轮是不是同一件事"，用于在目录里找可续跑的那一轮。"""
    return all(left.get(k) == right.get(k) for k in MATCH_FIELDS)


def header(run_id: str, config: dict, tasks: list[BenchmarkTask], *, ablation_id: str = "") -> dict:
    """进度文件的**首行**：自证身份 + 指纹。续跑时先比它，不匹配就拒绝。"""
    return {
        "kind": _KIND_HEADER,
        "run_id": run_id,
        "ablation_id": ablation_id,
        "group": config.get("group", ""),
        "fingerprint": fingerprint(config, tasks),
    }


def task_record(run: TaskRun, verdict: TaskVerdict) -> dict:
    """一条任务的进度记录：判分 + 重算指标需要的运行聚合（不存答案正文）。

    压缩指标（压缩比/事件数/摘要成本）来自 `compactions`，错误恢复率来自 `tool_events` ——
    续跑后要能重算这些指标，所以它们必须进进度文件；`AgentResult` 只对裁判有用，
    而裁判分数已经在 verdict 里，故不存（文件小一个量级）。
    """
    return {
        "kind": KIND_TASK,
        "task_id": verdict.task_id,
        "success": bool(verdict.success),
        "error_kind": verdict.error_kind,
        "verdict": _dump(verdict),
        "run": {
            "tool_events": [_dump(e) for e in run.tool_events],
            "compactions": [_dump(e) for e in run.compactions],
            "error": run.error,
            "error_kind": run.error_kind,
        },
    }


def restore(task: BenchmarkTask, record: dict) -> tuple[TaskRun, TaskVerdict] | None:
    """把一条进度记录还原成 `(TaskRun, TaskVerdict)`；形状不对返回 `None`（当没这条）。"""
    if not isinstance(record, dict) or record.get("kind") != KIND_TASK:
        return None
    if str(record.get("task_id") or "") != task.task_id:
        return None
    payload = record.get("verdict")
    if not isinstance(payload, dict):
        return None
    raw_run = record.get("run")
    if not isinstance(raw_run, dict):
        raw_run = {}

    verdict = _load(TaskVerdict, payload)
    if verdict is None:
        return None
    # 来源标记：这条不是本轮跑的，而是从进度文件捡回来的
    verdict = replace(verdict, task_id=task.task_id, skipped=True)

    run = TaskRun(
        task=task,
        tool_events=_load_many(ToolEvent, raw_run.get("tool_events")),
        compactions=_load_many(CompactionEvent, raw_run.get("compactions")),
        error=str(raw_run.get("error") or ""),
        error_kind=str(raw_run.get("error_kind") or ""),
    )
    return run, verdict


def resume_map(tasks: list[BenchmarkTask], records: list[dict]) -> dict[str, tuple[TaskRun, TaskVerdict]]:
    """`{task_id: (run, verdict)}` —— **只有成功的**条目（失败的下次要重试）。

    任务集里已经不存在的 id 会被忽略（换了任务集时不该把孤儿结果混进报告）。
    """
    by_id = {task.task_id: task for task in tasks}
    out: dict[str, tuple[TaskRun, TaskVerdict]] = {}
    for record in records:
        if not isinstance(record, dict):
            continue  # 合法 JSON 但不是对象（"abc" / 123）—— 当没这条
        task_id = str(record.get("task_id") or "")
        task = by_id.get(task_id)
        if task is None or not record.get("success"):
            continue
        restored = restore(task, record)
        if restored is not None:
            out[task_id] = restored
    return out


def _dump(obj: Any) -> dict:
    """数据类 → 只含字段名的纯字典（不递归进未知对象，保证可 JSON 序列化）。"""
    out: dict[str, Any] = {}
    for field in fields(obj):
        value = getattr(obj, field.name)
        out[field.name] = value if isinstance(value, (str, int, float, bool, type(None), list, dict)) else str(value)
    return out


def _load(cls: Any, payload: dict) -> Any:
    """按字段名过滤后构造；类型不对（例如已知字段是字符串）返回 `None`。"""
    known = {field.name for field in fields(cls)}
    try:
        return cls(**{k: v for k, v in payload.items() if k in known})
    except (TypeError, ValueError):
        return None


def _load_many(cls: Any, payload: object) -> list:
    if not isinstance(payload, list):
        return []
    out = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        loaded = _load(cls, item)
        if loaded is not None:
            out.append(loaded)
    return out
