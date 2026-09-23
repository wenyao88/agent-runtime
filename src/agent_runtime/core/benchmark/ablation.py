"""消融分组：三组自变量 + 设置覆盖 + 报告快照（纯逻辑，零第三方依赖）。

三组只改"记忆开关"与"摘要压缩开关"，其余（模型、任务集、代码版本、压缩阈值）完全一致；
报告里必须带**开关快照**，否则三份报告分不清谁是谁（Phase 7 开跑前检查的第 3 条缺口）。
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AblationGroup:
    name: str
    memory: bool
    compaction: bool
    session_scope: str = "task"


ABLATION_GROUPS: tuple[AblationGroup, ...] = (
    AblationGroup(name="baseline", memory=False, compaction=False, session_scope="task"),
    AblationGroup(name="memory", memory=True, compaction=False, session_scope="run"),
    AblationGroup(name="memory+compaction", memory=True, compaction=True, session_scope="run"),
)
"""三组与 spec §4 一一对应。两条容易搞错的细节：

* `memory=True` **同时**打开 short_term 与 long_term —— 只开 short_term 时，文本匹配方向是
  "整段 query 文本是条目内容的子串"（`working.py:21`），长任务几乎召不回东西（spec §3-B）。
* `compaction` **只控制 SUMMARIZE**：SQUEEZE/TRUNCATE 是 `compact()` 里的无条件行为（`react.py:211`），
  三组都会压（spec §4）。
"""


def find_group(name: str) -> AblationGroup | None:
    wanted = (name or "").strip().lower()
    for group in ABLATION_GROUPS:
        if group.name == wanted:
            return group
    return None


def apply_group(settings: Any, group: AblationGroup) -> Any:
    """返回**新的** settings：只改本阶段的自变量，绝不动传入对象。

    优先走 pydantic v2 的 `model_copy(update=...)`（正式拷贝路径），
    鸭子类型对象退回 `copy.copy` + `setattr` —— 沙箱里没有 pydantic，两条路径都要能跑。
    """
    overrides = {
        "memory_short_term_enabled": group.memory,
        "memory_long_term_enabled": group.memory,
        # 整理（consolidate）一律关：把"额外 LLM 成本"归因给压缩那一组，别混在一起
        "memory_consolidate_enabled": False,
        "agent_compaction_summarize_enabled": group.compaction,
    }
    model_copy = getattr(settings, "model_copy", None)
    if callable(model_copy):
        try:
            return model_copy(update=overrides)
        except Exception:  # noqa: BLE001 —— 不是 pydantic v2 的 model_copy 就退回浅拷贝
            pass
    clone = copy.copy(settings)
    for key, value in overrides.items():
        setattr(clone, key, value)
    return clone


def group_overrides(group: AblationGroup) -> dict:
    """落进报告 `config` 的开关快照 —— 报告必须自证"我到底开了什么"。"""
    return {
        "group": group.name,
        "memory": group.memory,
        "compaction": group.compaction,
        "session_scope": group.session_scope,
    }
