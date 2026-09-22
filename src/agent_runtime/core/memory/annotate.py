"""记忆召回注入的纯逻辑：来源标注、去重、排序、渲染。

为什么放 core：注入 System Prompt 的质量只取决于**顺序对不对、来源说不说得清、截断有没有标记**，
这三件事都是纯数据变换 —— 与"用 Redis 还是 PG 存"无关，所以放这里、可在沙箱内钉死。

排序规则（spec §4.2，必须精确）：
  1. 层优先级 `working < short_term < long_term`；
  2. 同层按 `created_at` **倒序**（越新越靠前）；
  3. `created_at is None` 排在该层**末尾**（WorkingMemory 的条目没有时间戳）；
  4. 键完全相同时保持**输入顺序**（稳定，显式用序号兜底）。
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from .base import MemoryEntry

SOURCE_WORKING = "working"
SOURCE_SHORT = "short_term"
SOURCE_LONG = "long_term"

MAX_DEFAULT_CHARS = 500
TRUNCATED_MARK = "…"

_SOURCE_ORDER: dict[str, int] = {
    SOURCE_WORKING: 0,
    SOURCE_SHORT: 1,
    SOURCE_LONG: 2,
}


def with_source(entries: list[MemoryEntry], source: str) -> list[MemoryEntry]:
    """给某一层返回的条目打上来源。

    **不就地修改入参**（`replace` 返回新对象）：同一条记忆可能同时存在于多层，
    就地改会让先标注的层"串味"到后标注的层。

    调用方给出的 `source` 是**权威**的（它最清楚自己查的是哪一层），覆盖条目自带的值。
    """
    return [replace(e, source=source) for e in entries]


def dedupe(entries: list[MemoryEntry]) -> list[MemoryEntry]:
    """按内容去重，保留**首次出现**的那条。

    级联召回天然会重复（工作记忆与长期记忆可能存了同一段文字），
    重复注入既浪费 token 又会让模型误以为"这条被强调过"。首现在前的层优先级更高，故保留首次。
    """
    seen: set[str] = set()
    kept: list[MemoryEntry] = []
    for entry in entries:
        key = (entry.content or "").strip()
        if key in seen:
            continue
        seen.add(key)
        kept.append(entry)
    return kept


def _sort_key(item: tuple[int, MemoryEntry]) -> tuple[int, int, float, int]:
    index, entry = item
    order = _SOURCE_ORDER.get(entry.source, len(_SOURCE_ORDER))
    created: datetime | None = entry.created_at
    if created is None:
        return (order, 1, 0.0, index)  # 无时间戳 → 该层末尾
    return (order, 0, -created.timestamp(), index)


def sort_entries(entries: list[MemoryEntry]) -> list[MemoryEntry]:
    return [entry for _, entry in sorted(enumerate(entries), key=_sort_key)]


def format_line(entry: MemoryEntry, max_chars: int = MAX_DEFAULT_CHARS) -> str:
    """渲染成 System Prompt 里的一行：`- [来源 · YYYY-MM-DD] 内容`。

    * 内容压成**一行**：多行会破坏 prompt 里的列表结构；
    * `max_chars` 为 `0` 或 `None` 表示不截断；
    * 截断**必须带省略号** —— 本项目在 Demo 1 吃过"无标记截断把残缺当完整"的亏。
    """
    text = " ".join((entry.content or "").split())
    label = entry.source or ""
    if entry.created_at is not None:
        stamp = entry.created_at.strftime("%Y-%m-%d")
        label = f"{label} · {stamp}" if label else stamp
    if max_chars and len(text) > max_chars:
        text = text[:max_chars] + TRUNCATED_MARK
    prefix = f"[{label}] " if label else ""
    return f"- {prefix}{text}"
