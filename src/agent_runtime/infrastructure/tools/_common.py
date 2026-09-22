"""tools 包内部共享件：上下文截断策略 + 工作区路径守卫。

两处逻辑刻意集中在这里，而不是各工具各写一份：
  * 截断策略 = "多少文本进入模型上下文"的统一口径（context 预算宝贵，口径要一致）；
  * 路径守卫 = 安全边界，绝不能存在两份可能漂移的实现。
"""
from __future__ import annotations

import os

TRUNCATED_SUFFIX = "\n...[truncated]"


def truncate_for_context(text: str, max_chars: int) -> tuple[str, bool]:
    """返回 (截断后的文本, 是否发生了截断)。"""
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars] + TRUNCATED_SUFFIX, True


def resolve_within(root: str, path: str) -> str | None:
    """把 path 解析到 root 之内；越界（`..` 或绝对路径）返回 None。"""
    root_abs = os.path.abspath(root)
    candidate = os.path.abspath(os.path.join(root_abs, path))
    if candidate == root_abs or candidate.startswith(root_abs + os.sep):
        return candidate
    return None
