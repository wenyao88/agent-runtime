from .base import BaseSkill, SkillManifest


class SkillRouter:
    def __init__(self):
        self._skills: list[BaseSkill] = []

    def register(self, skill: BaseSkill) -> None:
        self._skills.append(skill)

    def match(self, task: str, top_k: int = 1) -> list[BaseSkill]:
        """按命中 trigger 数降序返回前 top_k 个；同分保持注册顺序；无命中 → []。

        宁可返回空也不用无关 SOP：注入错技能的代价（把 Agent 引向错误流程）高于不注入。
        """
        if top_k <= 0:
            return []
        scored = [(s.match_score(task), i, s) for i, s in enumerate(self._skills)]
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [s for score, _, s in scored[:top_k] if score > 0]

    def list_all(self) -> list[SkillManifest]:
        return [s.manifest for s in self._skills]

    def is_empty(self) -> bool:
        return not self._skills
