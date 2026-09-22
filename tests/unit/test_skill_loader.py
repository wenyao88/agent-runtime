"""SkillLoader 契约：skills/*.md 的 front-matter 解析与加载。

设计约束（已批）：
  * **不引入 PyYAML** —— 只解析受支持的子集（标量 / `[a, b]` / `- item`）；
  * 不支持的形状（嵌套映射、锚点、多行）**明确报错并跳过**，绝不猜测；
  * `load_directory` 返回 (skills, errors)：坏文件只记错误，不影响好文件，也不阻断启动。

沙箱适配：临时目录用普通 `mkdir`（mkdtemp 在本环境拒绝嵌套写入，见 Phase 2 记录）。
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from agent_runtime.core.skill.loader import (
    MarkdownSkill,
    SkillFormatError,
    SkillLoader,
)

_BASE = Path(__file__).resolve().parents[2] / ".testtmp"
_SEQ = 0


def _new_root() -> str:
    global _SEQ
    _SEQ += 1
    p = _BASE / f"skill{_SEQ:02d}"
    p.mkdir(parents=True, exist_ok=True)
    return str(p)


def _write(root: str, name: str, content: str) -> str:
    path = Path(root) / name
    path.write_text(content, encoding="utf-8")
    return str(path)


GOOD = """---
name: github_analysis
description: GitHub 仓库分析 SOP
version: "1.0"
triggers: [代码审查, 分析仓库, code review]
required_tools: [github_get_repo, github_read_file]
tags: [code, github]
---

# 执行步骤
1. 先读 README。
"""


# ── front-matter 解析 ──


def test_parses_scalars_and_inline_list() -> None:
    data, body = SkillLoader.parse_front_matter(GOOD)
    assert data["name"] == "github_analysis"
    assert data["description"] == "GitHub 仓库分析 SOP"
    assert data["version"] == "1.0", "带引号的标量要去掉引号"
    assert data["triggers"] == ["代码审查", "分析仓库", "code review"]
    assert data["required_tools"] == ["github_get_repo", "github_read_file"]
    assert body.lstrip().startswith("# 执行步骤"), body


def test_parses_dash_list() -> None:
    text = """---
name: tech_research
description: 技术调研 SOP
triggers:
  - 调研
  - 技术选型
required_tools:
  - web_search
---

正文
"""
    data, _ = SkillLoader.parse_front_matter(text)
    assert data["triggers"] == ["调研", "技术选型"]
    assert data["required_tools"] == ["web_search"]


def test_unknown_keys_are_ignored_including_their_nested_block() -> None:
    text = """---
name: n
description: d
future_thing:
  - a
  - b
another: 1
---

正文
"""
    data, _ = SkillLoader.parse_front_matter(text)
    assert data["name"] == "n"
    assert "future_thing" not in data and "another" not in data


# ── 明确报错（不猜测）──


def _expect_error(text: str) -> str:
    try:
        SkillLoader.parse_front_matter(text)
    except SkillFormatError as e:
        return str(e)
    raise AssertionError("本应抛出 SkillFormatError")


def test_missing_front_matter_errors() -> None:
    assert "front-matter" in _expect_error("# 没有 front-matter\n正文")


def test_unclosed_front_matter_errors() -> None:
    assert "未闭合" in _expect_error("---\nname: n\n正文没有结束分隔符")


def test_nested_map_value_errors() -> None:
    message = _expect_error("---\nname: n\ndescription: {a: b}\n---\n正文")
    assert "嵌套" in message or "不支持" in message


def test_indented_non_list_line_errors() -> None:
    message = _expect_error("---\nname: n\ndescription: d\n  sub: x\n---\n正文")
    assert "缩进" in message


def test_scalar_key_cannot_also_be_a_list() -> None:
    message = _expect_error("---\nname: n\ndescription: d\n  - item\n---\n正文")
    assert "标量" in message or "列表" in message


# ── 文件级加载 ──


def test_load_file_builds_markdown_skill() -> None:
    root = _new_root()
    try:
        path = _write(root, "github_analysis.md", GOOD)
        skill = SkillLoader.load_file(path)
        assert isinstance(skill, MarkdownSkill)
        assert skill.manifest.name == "github_analysis"
        assert "代码审查" in skill.manifest.triggers
        assert skill.build_prompt_extension("任意任务").lstrip().startswith("# 执行步骤")
        assert skill.source.endswith("github_analysis.md")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_load_file_requires_name() -> None:
    root = _new_root()
    try:
        path = _write(root, "noname.md", "---\ndescription: d\n---\n正文")
        try:
            SkillLoader.load_file(path)
        except SkillFormatError as e:
            assert "name" in str(e)
        else:
            raise AssertionError("缺少 name 必须报错")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_load_file_requires_body() -> None:
    root = _new_root()
    try:
        path = _write(root, "empty.md", "---\nname: n\ndescription: d\n---\n\n")
        try:
            SkillLoader.load_file(path)
        except SkillFormatError as e:
            assert "正文" in str(e)
        else:
            raise AssertionError("正文为空必须报错")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ── 目录加载：坏文件不影响好文件 ──


def test_load_directory_keeps_good_skills_and_reports_bad_ones() -> None:
    root = _new_root()
    try:
        _write(root, "good.md", GOOD)
        _write(root, "broken.md", "---\nname: broken\ndescription: d\n  - item\n---\n正文")
        _write(root, "notes.txt", "不是 markdown，应被忽略")
        skills, errors = SkillLoader.load_directory(root)
        assert [s.manifest.name for s in skills] == ["github_analysis"]
        assert len(errors) == 1
        assert "broken.md" in errors[0]
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_load_directory_missing_dir_is_reported_not_raised() -> None:
    skills, errors = SkillLoader.load_directory(str(Path(_BASE) / "definitely-missing"))
    assert skills == []
    assert errors and "不存在" in errors[0]


def test_load_directory_reports_duplicate_names() -> None:
    root = _new_root()
    try:
        _write(root, "a.md", GOOD)
        _write(root, "b.md", GOOD)  # 同一个 name
        skills, errors = SkillLoader.load_directory(root)
        assert len(skills) == 1, "重复 name 必须只保留一个"
        assert any("重复" in e for e in errors), errors
    finally:
        shutil.rmtree(root, ignore_errors=True)


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
