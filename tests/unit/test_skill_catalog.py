"""Skill 装配契约（放 infrastructure，纯标准库 → 沙箱可测）。

为什么这段逻辑不放在 `api/deps.py`：deps 依赖 pydantic_settings（本环境装不上），
逻辑一放那儿，"加载了哪些技能、坏文件是否只记错误、目录解析对不对"就完全无法验证。
Phase 2 的 `tools/catalog.py` 出于同样理由放在 infrastructure。

沙箱适配：普通 mkdir 建临时目录（mkdtemp 在本环境拒绝嵌套写入，见 Phase 2 记录）。
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "src"))

from agent_runtime.core.skill.router import SkillRouter
from agent_runtime.infrastructure.skills.catalog import (
    register_skills_from_dir,
    resolve_skills_dir,
    skill_catalog,
)

_BASE = _ROOT / ".testtmp"
_SEQ = 0

GOOD = """---
name: {name}
description: {name} 的描述
triggers: [代码审查]
---

# 正文
基于工具结果作答。
"""


def _new_root() -> str:
    global _SEQ
    _SEQ += 1
    p = _BASE / f"skillcat{_SEQ:02d}"
    p.mkdir(parents=True, exist_ok=True)
    return str(p)


def _write(root: str, filename: str, content: str) -> None:
    (Path(root) / filename).write_text(content, encoding="utf-8")


# ── 目录解析 ──


def test_relative_skills_dir_is_anchored_to_project_root() -> None:
    """相对路径若按 CWD 解析，从别的目录启动服务就会静默加载到 0 个技能。"""
    assert resolve_skills_dir("skills", str(_ROOT)) == str(_ROOT / "skills")


def test_absolute_skills_dir_is_kept() -> None:
    absolute = str(_ROOT / "skills")
    assert resolve_skills_dir(absolute, str(_ROOT)) == absolute


# ── 注册 ──


def test_blank_skills_dir_is_reported_as_skipped() -> None:
    """`SKILLS_DIR=`（空值）若被当成"项目根"，就会去 glob 仓库根目录并报一堆
    「README.md: 缺少 front-matter」——看起来像技能全坏了，实际是配置为空。"""
    router = SkillRouter()
    errors = register_skills_from_dir(router, "")
    assert router.is_empty() is True
    assert errors and "为空" in errors[0], errors


def test_registers_every_skill_in_dir() -> None:
    root = _new_root()
    try:
        _write(root, "a.md", GOOD.format(name="a"))
        _write(root, "b.md", GOOD.format(name="b"))
        router = SkillRouter()
        errors = register_skills_from_dir(router, root)
        assert errors == []
        assert [m.name for m in router.list_all()] == ["a", "b"]
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_missing_dir_is_reported_not_raised() -> None:
    router = SkillRouter()
    errors = register_skills_from_dir(router, str(_BASE / "definitely-missing-dir"))
    assert router.is_empty() is True
    assert errors and "不存在" in errors[0], errors


def test_good_skills_survive_a_broken_one() -> None:
    root = _new_root()
    try:
        _write(root, "good.md", GOOD.format(name="good"))
        _write(root, "broken.md", "---\nname: broken\ndescription: d\n  - oops\n---\n正文")
        router = SkillRouter()
        errors = register_skills_from_dir(router, root)
        assert [m.name for m in router.list_all()] == ["good"]
        assert any("broken.md" in e for e in errors), errors
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_registering_twice_does_not_duplicate() -> None:
    """lifespan 与 Agent 都会触发装配；重复装配不得让技能翻倍。"""
    root = _new_root()
    try:
        _write(root, "a.md", GOOD.format(name="a"))
        router = SkillRouter()
        register_skills_from_dir(router, root)
        register_skills_from_dir(router, root)
        assert len(router.list_all()) == 1
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ── 目录（供 GET /api/skills）──


def test_catalog_exposes_manifest_fields_sorted_by_name() -> None:
    root = _new_root()
    try:
        _write(root, "z.md", GOOD.format(name="zebra"))
        _write(root, "a.md", GOOD.format(name="alpha"))
        router = SkillRouter()
        register_skills_from_dir(router, root)
        entries = skill_catalog(router)
        assert [e["name"] for e in entries] == ["alpha", "zebra"]
        assert set(entries[0]) == {
            "name",
            "description",
            "version",
            "triggers",
            "required_tools",
            "tags",
        }
        assert entries[0]["triggers"] == ["代码审查"]
        assert entries[0]["required_tools"] == []
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_builtin_catalog_lists_both_builtin_skills() -> None:
    router = SkillRouter()
    errors = register_skills_from_dir(router, str(_ROOT / "skills"))
    assert errors == []
    names = [e["name"] for e in skill_catalog(router)]
    assert names == ["github_analysis", "tech_research"]


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
