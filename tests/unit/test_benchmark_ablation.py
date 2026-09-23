"""消融分组契约（`core/benchmark/ablation.py`）。

三组是**自变量**，必须能被报告自证：名字、开关、会话范围都要落进 `config`。
两条容易搞错、这里用测试钉死的细节：

1. **`+Memory` 组必须同时开 short_term 与 long_term**：`working`/`short_term` 的文本匹配方向是
   "整段 query 文本是条目内容的子串"，长任务文本几乎命不中；只有 long_term 的向量相似度才是真召回。
2. **`compaction=False` 只等于"SUMMARIZE 关"**：SQUEEZE/TRUNCATE 是无条件行为（`react.py:211`），关不掉。
3. `memory_consolidate_enabled` **一律 False**：把"额外 LLM 成本"这一项归因给压缩组，别混在一起。
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from agent_runtime.core.benchmark.ablation import (  # noqa: E402
    ABLATION_GROUPS,
    AblationGroup,
    apply_group,
    find_group,
    group_overrides,
)


class _DuckSettings:
    """鸭子类型 settings（沙箱装不上 pydantic_settings）。"""

    def __init__(self, **overrides: object) -> None:
        self.memory_short_term_enabled = False
        self.memory_long_term_enabled = False
        self.memory_consolidate_enabled = True
        self.agent_compaction_summarize_enabled = False
        self.agent_context_compaction_threshold = 0.8
        self.llm_model = "deepseek-chat"
        for key, value in overrides.items():
            setattr(self, key, value)


class _PydanticLikeSettings(_DuckSettings):
    """模拟 pydantic v2 的 `model_copy(update=...)`。"""

    def __init__(self, **overrides: object) -> None:
        super().__init__(**overrides)
        self.copied_with: list[dict] = []

    def model_copy(self, *, update: dict | None = None):
        clone = _PydanticLikeSettings()
        for key, value in vars(self).items():
            if key != "copied_with":
                setattr(clone, key, value)
        for key, value in (update or {}).items():
            setattr(clone, key, value)
        clone.copied_with = list(self.copied_with) + [dict(update or {})]
        return clone


# ── 分组定义 ──


def test_groups_match_the_design() -> None:
    names = [g.name for g in ABLATION_GROUPS]
    assert names == ["baseline", "memory", "memory+compaction"]
    baseline, memory, both = ABLATION_GROUPS
    assert (baseline.memory, baseline.compaction, baseline.session_scope) == (False, False, "task")
    assert (memory.memory, memory.compaction, memory.session_scope) == (True, False, "run")
    assert (both.memory, both.compaction, both.session_scope) == (True, True, "run")


def test_find_group_is_case_insensitive_and_returns_none_for_unknown() -> None:
    assert find_group("MEMORY") is not None
    assert find_group(" memory ") is not None
    assert find_group("memory+compaction") is not None
    assert find_group("nope") is None
    assert find_group("") is None


# ── 设置覆盖 ──


def test_memory_group_opens_both_persistent_layers_and_no_summarizer() -> None:
    group = find_group("memory")
    assert group is not None
    settings = apply_group(_DuckSettings(), group)
    assert settings.memory_short_term_enabled is True
    assert settings.memory_long_term_enabled is True, "只开 short_term 召不回长任务（spec §3-B）"
    assert settings.memory_consolidate_enabled is False, "整理成本不能混进记忆组"
    assert settings.agent_compaction_summarize_enabled is False


def test_only_the_third_group_enables_summarize() -> None:
    for group in ABLATION_GROUPS:
        settings = apply_group(_DuckSettings(), group)
        assert settings.agent_compaction_summarize_enabled is group.compaction, group.name


def test_baseline_turns_everything_off() -> None:
    group = find_group("baseline")
    assert group is not None
    settings = apply_group(
        _DuckSettings(
            memory_short_term_enabled=True,
            memory_long_term_enabled=True,
            memory_consolidate_enabled=True,
            agent_compaction_summarize_enabled=True,
        ),
        group,
    )
    assert settings.memory_short_term_enabled is False
    assert settings.memory_long_term_enabled is False
    assert settings.memory_consolidate_enabled is False
    assert settings.agent_compaction_summarize_enabled is False


def test_apply_group_does_not_mutate_the_input() -> None:
    original = _DuckSettings(memory_short_term_enabled=True)
    apply_group(original, find_group("baseline"))  # type: ignore[arg-type]
    assert original.memory_short_term_enabled is True, "覆盖必须发生在新对象上"
    assert original.agent_compaction_summarize_enabled is False


def test_apply_group_keeps_unrelated_fields() -> None:
    settings = apply_group(_DuckSettings(), find_group("memory"))  # type: ignore[arg-type]
    assert settings.agent_context_compaction_threshold == 0.8
    assert settings.llm_model == "deepseek-chat"


def test_pydantic_style_settings_use_model_copy() -> None:
    original = _PydanticLikeSettings(memory_consolidate_enabled=True)
    settings = apply_group(original, find_group("memory"))  # type: ignore[arg-type]
    assert settings is not original
    assert settings.copied_with, "有 model_copy 就该走它（pydantic v2 的正式拷贝路径）"
    assert original.copied_with == [], "原对象不该被改动"
    assert settings.memory_short_term_enabled is True
    assert settings.memory_consolidate_enabled is False


def test_duck_typed_settings_fall_back_to_a_copy() -> None:
    original = _DuckSettings()
    settings = apply_group(original, find_group("memory"))  # type: ignore[arg-type]
    assert settings is not original
    assert settings.memory_long_term_enabled is True
    assert original.memory_long_term_enabled is False


# ── 报告快照 ──


def test_group_overrides_are_reportable() -> None:
    group = find_group("memory+compaction")
    assert group is not None
    snapshot = group_overrides(group)
    assert snapshot == {
        "group": "memory+compaction",
        "memory": True,
        "compaction": True,
        "session_scope": "run",
    }
    assert group_overrides(AblationGroup("x", memory=False, compaction=False))["session_scope"] == "task"


def _run_all() -> None:
    failed: list[str] = []
    tests = [
        v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)
    ]
    for t in tests:
        print(f"RUN  {t.__name__}")
        try:
            t()
        except Exception as e:  # noqa: BLE001
            print(f"FAIL {t.__name__}: {type(e).__name__}: {e}")
            failed.append(t.__name__)
        else:
            print(f"PASS {t.__name__}")
    if failed:
        raise SystemExit(f"FAILED: {', '.join(failed)}")
    print("ALL PASS")


if __name__ == "__main__":
    _run_all()
