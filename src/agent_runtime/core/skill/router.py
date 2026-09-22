from .base import BaseSkill, SkillManifest


class SkillRouter:
    def __init__(self):
        self._skills: list[BaseSkill] = []

    def register(self, skill: BaseSkill) -> None:
        self._skills.append(skill)

    def match(self, task: str, top_k: int = 1) -> list[BaseSkill]:
        matched = [s for s in self._skills if s.matches(task)]
        return matched[:top_k]

    def list_all(self) -> list[SkillManifest]:
        return [s.manifest for s in self._skills]
