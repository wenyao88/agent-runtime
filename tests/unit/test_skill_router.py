"""SkillRouter 契约：按「命中 trigger 数」打分排序（Phase 3 已批决策）。

设计约束：
  * 打分 = 命中的 trigger 个数（不是字符长度、不是 embedding）—— 刻意保持可解释、可测；
  * 同分保持**注册顺序**（稳定排序）—— 否则加载顺序一变，行为就随机变；
  * 无命中返回 `[]`（不是"随便给一个"）—— 宁可不用技能，也不要注入无关 SOP；
  * 空 router 必须安全（服务启动时没有 skills/ 目录是正常情况）。

沙箱适配：全部用纯标准库假 Skill，不碰文件系统与第三方包。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from agent_runtime.core.skill.base import BaseSkill, SkillManifest
from agent_runtime.core.skill.router import SkillRouter


class FakeSkill(BaseSkill):
    def __init__(self, name: str, triggers: list[str], body: str = "指导文本") -> None:
        self.manifest = SkillManifest(name=name, description=f"{name} 描述", triggers=triggers)
        self.body = body

    def build_prompt_extension(self, task: str) -> str:
        return self.body


# ── match_score ──


def test_match_score_counts_matched_triggers() -> None:
    skill = FakeSkill("gh", ["代码审查", "分析仓库", "code review"])
    assert skill.match_score("帮我做代码审查，分析仓库结构") == 2


def test_match_score_is_case_insensitive() -> None:
    skill = FakeSkill("gh", ["Code Review"])
    assert skill.match_score("please do a CODE REVIEW for me") == 1


def test_trigger_repeated_in_task_counts_once() -> None:
    skill = FakeSkill("gh", ["代码审查"])
    assert skill.match_score("代码审查，代码审查，再做代码审查") == 1


def test_blank_trigger_never_matches() -> None:
    """空字符串是任何任务的子串；若不过滤，一个空 trigger 会让技能命中一切任务。"""
    skill = FakeSkill("bad", ["", "   "])
    assert skill.match_score("任意任务") == 0
    assert skill.matches("任意任务") is False


def test_no_triggers_scores_zero() -> None:
    assert FakeSkill("none", []).match_score("任意任务") == 0


def test_matches_agrees_with_match_score() -> None:
    skill = FakeSkill("gh", ["代码审查"])
    assert skill.matches("代码审查") is True
    assert skill.matches("写一首诗") is False


# ── SkillRouter ──


def test_no_match_returns_empty_list() -> None:
    router = SkillRouter()
    router.register(FakeSkill("gh", ["代码审查"]))
    assert router.match("今天天气如何") == []


def test_more_matched_triggers_rank_first() -> None:
    router = SkillRouter()
    router.register(FakeSkill("one_hit", ["代码审查"]))
    router.register(FakeSkill("two_hits", ["代码审查", "分析仓库"]))
    matched = router.match("代码审查并分析仓库", top_k=5)
    assert [s.manifest.name for s in matched] == ["two_hits", "one_hit"]


def test_ties_keep_registration_order() -> None:
    router = SkillRouter()
    router.register(FakeSkill("first", ["代码审查"]))
    router.register(FakeSkill("second", ["分析仓库"]))
    matched = router.match("代码审查与分析仓库", top_k=5)
    assert [s.manifest.name for s in matched] == ["first", "second"]


def test_top_k_limits_results() -> None:
    router = SkillRouter()
    for i in range(3):
        router.register(FakeSkill(f"s{i}", ["代码审查"]))
    assert len(router.match("代码审查", top_k=2)) == 2
    assert [s.manifest.name for s in router.match("代码审查", top_k=1)] == ["s0"]


def test_default_top_k_is_one() -> None:
    router = SkillRouter()
    router.register(FakeSkill("a", ["代码审查"]))
    router.register(FakeSkill("b", ["代码审查"]))
    assert len(router.match("代码审查")) == 1


def test_empty_router_is_safe() -> None:
    router = SkillRouter()
    assert router.match("任意任务") == []
    assert router.list_all() == []
    assert router.is_empty() is True


def test_is_empty_false_after_register() -> None:
    router = SkillRouter()
    router.register(FakeSkill("a", ["t"]))
    assert router.is_empty() is False


def test_list_all_returns_manifests_in_registration_order() -> None:
    router = SkillRouter()
    router.register(FakeSkill("a", ["t1"]))
    router.register(FakeSkill("b", ["t2"]))
    manifests = router.list_all()
    assert [m.name for m in manifests] == ["a", "b"]
    assert all(isinstance(m, SkillManifest) for m in manifests)


def test_duplicate_trigger_entries_count_once() -> None:
    """`triggers: [x, x]` 是手写 md 的常见复制粘贴失误；若按条数计分，
    它会靠"重复"压过真正命中两个不同关键词的技能。"""
    skill = FakeSkill("dup", ["x", "x"])
    assert skill.match_score("x") == 1


def test_duplicate_name_is_ignored() -> None:
    """同名技能只保留第一个：否则重复装配（如 lifespan 跑两次）会让技能列表与注入内容翻倍。

    与 ToolRegistry 的去重语义一致（按名字唯一）。
    """
    router = SkillRouter()
    router.register(FakeSkill("gh", ["代码审查"], body="FIRST"))
    router.register(FakeSkill("gh", ["代码审查"], body="SECOND"))
    assert len(router.list_all()) == 1
    assert router.match("代码审查", top_k=5)[0].build_prompt_extension("t") == "FIRST"


def _run_all() -> None:
    tests = [
        v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)
    ]
    for t in tests:
        print(f"RUN  {t.__name__}")
        t()
        print(f"PASS {t.__name__}")
    print("ALL PASS")


if __name__ == "__main__":
    _run_all()
