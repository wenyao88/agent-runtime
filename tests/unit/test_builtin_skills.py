"""内置 Skill 文件的契约：`skills/*.md` 必须真的能加载、命中、并重申诚实规则。

为什么这些断言值得写：skill 文件是**内容资产**，不受类型检查保护 ——
    * `required_tools` 写错一个名字，模型就会去调用不存在的工具；
    * trigger 写得太偏，中文任务永远命中不了，整个 Skill 系统等于没接；
    * 正文若忘了重申"基于工具结果、失败如实报告"，注入的 SOP 反而鼓励模型自由发挥。
这三类错误都只在运行时才暴露，所以用测试钉住。

沙箱适配：纯标准库 + 直接读仓库内的真实文件。
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "src"))

from agent_runtime.core.skill.loader import SkillLoader
from agent_runtime.core.skill.router import SkillRouter
from agent_runtime.infrastructure.tools.catalog import NATIVE_TOOL_NAMES

SKILLS_DIR = _ROOT / "skills"
REQUIRED_SKILLS = {"github_analysis", "tech_research"}


def _load() -> tuple[list, list[str]]:
    return SkillLoader.load_directory(str(SKILLS_DIR))


def _by_name() -> dict:
    skills, _ = _load()
    return {s.manifest.name: s for s in skills}


def test_builtin_skills_load_without_errors() -> None:
    skills, errors = _load()
    assert errors == [], f"内置 skill 不应有加载错误：{errors}"
    names = {s.manifest.name for s in skills}
    assert REQUIRED_SKILLS <= names, f"缺少内置技能，实际加载：{sorted(names)}"


def test_every_builtin_skill_has_triggers() -> None:
    skills, _ = _load()
    for skill in skills:
        assert skill.manifest.triggers, f"{skill.manifest.name} 没有 triggers，永远不会被命中"
        assert all(t.strip() for t in skill.manifest.triggers), skill.manifest.name
        assert skill.manifest.description.strip(), f"{skill.manifest.name} 缺 description"


def test_required_tools_exist_in_native_catalog() -> None:
    skills, _ = _load()
    known = set(NATIVE_TOOL_NAMES)
    for skill in skills:
        unknown = [t for t in skill.manifest.required_tools if t not in known]
        assert not unknown, f"{skill.manifest.name} 引用了不存在的工具：{unknown}"


def test_bodies_restate_the_evidence_rules() -> None:
    """技能正文必须重申硬规则：只能基于工具结果、失败如实报告、禁止编造。"""
    for name, skill in _by_name().items():
        body = skill.build_prompt_extension("任意任务")
        assert "工具结果" in body or "工具返回" in body, f"{name} 正文未要求基于工具结果"
        assert "失败" in body, f"{name} 正文未要求如实报告失败"
        assert "编造" in body or "禁止" in body, f"{name} 正文未禁止编造"


def test_github_analysis_hits_a_chinese_repo_task() -> None:
    router = SkillRouter()
    for skill in _load()[0]:
        router.register(skill)
    matched = router.match("帮我分析一下 fastapi/fastapi 这个仓库的代码结构", top_k=1)
    assert [s.manifest.name for s in matched] == ["github_analysis"]


def test_tech_research_hits_a_chinese_research_task() -> None:
    router = SkillRouter()
    for skill in _load()[0]:
        router.register(skill)
    matched = router.match("帮我做一份关于向量数据库的技术调研报告", top_k=1)
    assert [s.manifest.name for s in matched] == ["tech_research"]


def test_english_task_hits_tech_research() -> None:
    router = SkillRouter()
    for skill in _load()[0]:
        router.register(skill)
    matched = router.match("do a tech research report on vector databases", top_k=1)
    assert [s.manifest.name for s in matched] == ["tech_research"]


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
