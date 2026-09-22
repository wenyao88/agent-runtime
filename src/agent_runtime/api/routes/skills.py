"""技能目录 API：把已加载的技能暴露给前端，便于确认"这次到底加载了哪些 SOP"。"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ...core.skill.router import SkillRouter
from ...infrastructure.skills.catalog import skill_catalog
from ..deps import get_skill_router

router = APIRouter(prefix="/api", tags=["skills"])


@router.get("/skills")
async def list_skills(skills: SkillRouter = Depends(get_skill_router)) -> dict:
    entries = skill_catalog(skills)
    return {"count": len(entries), "skills": entries}
