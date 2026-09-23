"""Trace 存储装配（**只有一处**）：API / CLI / Demo 共用。

与 `benchmark/catalog.py`、`skills/catalog.py` 同一先例 —— 逻辑放在 infrastructure
（`api/deps.py` 依赖 pydantic_settings，沙箱装不上，"选没选 sqlite、降级没降级"就无法在
沙箱里验证）。两条约定也照抄 benchmark：

* 相对路径按**项目根**解析，不按进程 CWD；
* 装配问题只进返回的 `errors`，**绝不抛**（可用性优先：坏 trace.db 不该让服务起不来）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ...core.trace.store import DEFAULT_CAPACITY, InMemoryTraceStore
from .store import SqliteTraceStore

SQLITE = "sqlite"


def resolve_trace_db_path(settings: Any, project_root: str) -> str:
    """相对路径按**项目根**解析（与 `skills_dir` / `benchmark_*` 同一约定）。"""
    raw = str(getattr(settings, "trace_db_path", "") or "trace.db")
    path = Path(raw)
    return str(path if path.is_absolute() else Path(project_root) / path)


def build_trace_store(settings: Any, project_root: str) -> tuple[Any, list[str]]:
    """按 `settings.trace_store` 装配，返回 `(store, 错误列表)`。**绝不抛**。

    * `sqlite`：落盘、跨重启；**起不来（坏文件 / 路径不可用）就降级为内存**，
      原因进 errors —— 宁可这次重启丢历史，也不要整个服务起不来；
    * 其它值（含空）：内存环形（重启即丢；`trace_capacity` 条，满了淘汰最旧）。
    """
    errors: list[str] = []
    try:
        capacity = int(getattr(settings, "trace_capacity", DEFAULT_CAPACITY))
    except (TypeError, ValueError) as e:
        errors.append(f"TRACE_CAPACITY 不是整数，已按默认 {DEFAULT_CAPACITY} 处理：{e}")
        capacity = DEFAULT_CAPACITY

    kind = str(getattr(settings, "trace_store", "") or "").strip().lower()
    if kind == SQLITE:
        try:
            store = SqliteTraceStore(resolve_trace_db_path(settings, project_root))
        except Exception as e:  # noqa: BLE001 —— 装配绝不抛
            errors.append(f"SQLite trace 存储装配失败，已降级为内存：{type(e).__name__}: {e}")
        else:
            if not store.errors:
                return store, errors
            errors.append(
                "SQLite trace 存储不可用，已降级为内存（本次重启不会保留历史）："
                + "；".join(store.errors)
            )
    return InMemoryTraceStore(capacity=capacity), errors
