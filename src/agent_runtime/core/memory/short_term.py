"""短时记忆的纯策略：TTL / 条数上限 / 序列化（**不碰 Redis**）。

为什么单独一层：Redis 只负责"存字符串、取字符串"，而**留多久、留几条、坏数据怎么办**是记忆语义。
把它抽出来就能在无 redis 包的环境里把语义钉死，infra 层只剩协议动作（`infrastructure/memory/short_term.py`）。

约定：
  * `loads` 对坏数据（非法 JSON / 非对象 / 缺 content / content 非字符串）一律返回 `None`，**绝不抛** ——
    一条脏记录（旧版本格式、被截断的 JSON）不能毁掉整次召回；
  * `created_at` 坏掉时只丢时间戳，**内容仍然保留**（内容有价值，时间可置空）；
  * `trim` 保留**最近** N 条：Redis List 是 oldest→newest，所以保留尾部；`max_items <= 0` → 空列表。

[Phase 0 遗留] 本文件原先是一个 `ShortTermMemory` 桩类（`store` 返回 `"stub"`、`query` 返回空）。
全仓库没有任何引用（grep 确认），真实实现是 `infrastructure/memory/short_term.py::RedisShortTermMemory`，
故删除该桩，把本文件改为纯策略模块。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .base import MemoryEntry

DEFAULT_TTL_SECONDS = 86400
DEFAULT_MAX_ITEMS = 200


@dataclass
class ShortTermPolicy:
    ttl_seconds: int = DEFAULT_TTL_SECONDS
    max_items: int = DEFAULT_MAX_ITEMS

    def __post_init__(self) -> None:
        """坏值必须**当场报错**，不能带病上线。

        为什么（Phase 4 审查实测）：
          * `max_items = 0` → 基础设施层发的是 `LTRIM key -0 -1` = `LTRIM key 0 -1`，即**保留全部**
            （无界增长），而这里 `trim` 却返回 `[]` → 写入成功、召回永远为空，静默而不一致；
          * `ttl_seconds = 0` → `EXPIRE key 0` 按 Redis 语义**立刻删键** → 每次写入当场消失。
        两种都不报错，只是"能力悄悄没了"——正是本项目反复要抓的那一类。故在校验点直接拒绝。
        """
        if int(self.ttl_seconds) <= 0:
            raise ValueError(
                f"ttl_seconds 必须 > 0（当前 {self.ttl_seconds}）：0 会让 Redis 立刻删除该键，写入即消失"
            )
        if int(self.max_items) <= 0:
            raise ValueError(
                f"max_items 必须 > 0（当前 {self.max_items}）：0 在 Redis 上是 LTRIM 0 -1（保留全部，无界增长），"
                "而本地 trim 会返回空 —— 写入成功但召回永远为空"
            )


def dumps(entry: MemoryEntry) -> str:
    """序列化成一行 JSON。

    `default=str` + `ensure_ascii=False`：metadata 是自由字典（可能塞进 datetime 之类），
    序列化要能降级而不是抛；中文不转义成 `\\uXXXX`，便于人工排查 Redis 里的内容。
    """
    payload: dict[str, Any] = {
        "content": entry.content,
        "role": entry.role,
        "metadata": entry.metadata or {},
        "created_at": entry.created_at.isoformat() if entry.created_at else None,
    }
    return json.dumps(payload, ensure_ascii=False, default=str)


def loads(raw: str) -> MemoryEntry | None:
    """反序列化；任何坏数据都返回 `None`（由调用方跳过该条）。"""
    if not raw or not str(raw).strip():
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    content = data.get("content")
    if not isinstance(content, str):
        return None
    metadata = data.get("metadata")
    return MemoryEntry(
        content=content,
        role=str(data.get("role") or "agent"),
        metadata=metadata if isinstance(metadata, dict) else {},
        created_at=_parse_datetime(data.get("created_at")),
    )


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def trim(raw_items: list[str], policy: ShortTermPolicy) -> list[str]:
    """只保留最近 `max_items` 条（Redis List 尾部最新）。"""
    if policy.max_items <= 0:
        return []
    if len(raw_items) <= policy.max_items:
        return list(raw_items)
    return list(raw_items[-policy.max_items :])
