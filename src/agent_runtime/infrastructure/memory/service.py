"""`/api/memories` 背后的逻辑（纯逻辑，零第三方依赖 → 沙箱可测）。

放这里的理由与 `tools/catalog.py`、`skills/catalog.py` 一致：路由依赖 fastapi/pydantic，
逻辑一放路由里就完全无法验证。**api 层只做参数解析与序列化。**

语义约定（spec §8）：
  * 未启用的层 → `enabled: false`，**不报错**（默认全关是 D4，API 必须如实体现）；
  * `layer="all"` → 走 `MemoryManager.recall`（与 Agent 的级联/短路/标注语义**完全一致**，
    否则"API 看到的记忆"和"Agent 实际用的记忆"会不一致，排查时最容易被误导）；
  * 指定层 → 只查该层（用 `with_source` 标注，不短路）；
  * 未知 `layer` / 层报错 → 返回可读错误，**不抛异常**（这层是对外接口，不该 500）。
"""
from __future__ import annotations

from typing import Any

from ...core.memory.annotate import with_source
from ...core.memory.base import MemoryEntry, MemoryQuery

LAYER_SHORT = "short_term"
LAYER_LONG = "long_term"
LAYER_ALL = "all"
VALID_LAYERS = (LAYER_ALL, LAYER_SHORT, LAYER_LONG)


def memory_settings_view(
    *, short_term_enabled: bool, long_term_enabled: bool, errors: list[str] | None = None
) -> dict[str, Any]:
    return {
        "short_term": {"enabled": bool(short_term_enabled)},
        "long_term": {"enabled": bool(long_term_enabled)},
        "errors": list(errors or []),
    }


def memory_catalog(entries: list[MemoryEntry] | None) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for entry in entries or []:
        created = entry.created_at
        items.append(
            {
                "content": entry.content,
                "role": entry.role,
                "source": entry.source,
                "created_at": created.isoformat() if created is not None else None,
                "metadata": dict(entry.metadata or {}),
                "id": entry.id,
            }
        )
    return items


def _enabled_view(manager: Any) -> dict[str, bool]:
    return {
        LAYER_SHORT: getattr(manager, "short_term", None) is not None,
        LAYER_LONG: getattr(manager, "long_term", None) is not None,
    }


async def list_memories(
    manager: Any, *, query: str = "", top_k: int = 5, layer: str = LAYER_ALL
) -> dict[str, Any]:
    """查询记忆。`manager` 只需鸭子类型地提供 short_term / long_term / recall。"""
    errors: list[str] = []
    if layer not in VALID_LAYERS:
        errors.append(f"未知 layer={layer!r}：可选 {', '.join(VALID_LAYERS)}")
        return {
            "enabled": _enabled_view(manager),
            "count": 0,
            "memories": [],
            "errors": errors,
        }

    entries: list[MemoryEntry] = []
    if layer == LAYER_ALL:
        try:
            entries = list(await manager.recall(MemoryQuery(text=query or None, top_k=top_k)))
        except Exception as e:  # noqa: BLE001 —— 对外接口不抛，如实回报
            errors.append(f"召回失败：{type(e).__name__}: {e}")
    else:
        target = getattr(manager, layer, None)
        if target is None:
            errors.append(f"{layer} 未启用")
        else:
            try:
                found = await target.query(MemoryQuery(text=query or None, top_k=top_k))
                entries = with_source(list(found or []), layer)
            except Exception as e:  # noqa: BLE001
                errors.append(f"{layer} 查询失败：{type(e).__name__}: {e}")

    memories = memory_catalog(entries)
    return {
        "enabled": _enabled_view(manager),
        "count": len(memories),
        "memories": memories,
        "errors": errors,
    }


async def clear_memories(manager: Any) -> dict[str, Any]:
    """清空**已启用**的持久层，逐层 best-effort。

    注意 `RedisShortTermMemory.clear(session_id=None)` 只删**默认会话**的键（spec §11 天花板）：
    这里不带 session_id，所以语义就是"清默认会话"。
    """
    result: dict[str, Any] = {}
    for name in (LAYER_SHORT, LAYER_LONG):
        target = getattr(manager, name, None)
        if target is None:
            result[name] = {"enabled": False}
            continue
        try:
            await target.clear()
        except Exception as e:  # noqa: BLE001 —— 一层失败不影响另一层，且如实回报
            result[name] = {"enabled": True, "ok": False, "error": f"{type(e).__name__}: {e}"}
        else:
            result[name] = {"enabled": True, "ok": True}
    return result
