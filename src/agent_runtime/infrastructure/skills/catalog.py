"""Skill 装配与目录（纯逻辑、零第三方依赖）。

为什么放在 infrastructure 而不是 `api/deps.py`：deps 依赖 pydantic_settings（本环境装不上），
逻辑一放那儿，"能加载哪些技能、坏文件是否只记错误、相对目录解析对不对"就都无法在沙箱内验证。
与 Phase 2 的 `tools/catalog.py` 同理 —— api 层只做薄封装。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ...core.skill.loader import SkillLoader
from ...core.skill.router import SkillRouter
from ...core.skill.base import SkillManifest


def resolve_skills_dir(skills_dir: str, project_root: str) -> str:
    """把相对 skills 目录锚定到项目根，而不是进程 CWD。

    否则从别的目录启动服务（uvicorn 常见）会静默加载到 0 个技能：不报错、只是技能全部失效。
    **空字符串由调用方拒绝**（见 `register_skills_from_dir`）：`Path("")` 会变成 `.`，
    即仓库根目录，于是 glob 出一堆 README 当成坏技能。
    """
    path = Path(skills_dir)
    return str(path if path.is_absolute() else Path(project_root) / path)


def register_skills_from_dir(router: SkillRouter, skills_dir: str) -> list[str]:
    """加载目录下全部技能并注册进 router，返回错误列表（**绝不抛**，与 MCP 加载一致）。

    坏文件 / 坏目录只记错误、不阻断启动；`SkillRouter.register` 按名字去重，
    所以重复装配（lifespan 与 Agent 都会触发）不会让技能翻倍。
    """
    if not skills_dir.strip():
        # `SKILLS_DIR=`（空值）若被当成"项目根"，就会去 glob 仓库根目录，
        # 报出一堆「README.md: 缺少 front-matter」——看起来像技能全坏了，实际只是配置为空。
        return ["SKILLS_DIR 为空：已跳过技能加载（应指向 skills 目录）"]
    try:
        skills, errors = SkillLoader.load_directory(skills_dir)
    except Exception as e:  # noqa: BLE001 —— 兜底：技能是可选能力，永不阻断启动
        return [f"skills 加载异常：{type(e).__name__}: {e}"]
    for skill in skills:
        router.register(skill)
    return errors


def skill_catalog(router: SkillRouter) -> list[dict[str, Any]]:
    """给 GET /api/skills 用的目录：字段与 `SkillManifest` 一一对应，按名称排序。"""
    entries = [_entry(m) for m in router.list_all()]
    entries.sort(key=lambda entry: entry["name"])
    return entries


def _entry(manifest: SkillManifest) -> dict[str, Any]:
    return {
        "name": manifest.name,
        "description": manifest.description,
        "version": manifest.version,
        "triggers": list(manifest.triggers),
        "required_tools": list(manifest.required_tools),
        "tags": list(manifest.tags),
    }
