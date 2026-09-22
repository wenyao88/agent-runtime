from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class SkillManifest:
    name: str
    description: str
    version: str = "1.0"
    triggers: list[str] = field(default_factory=list)
    required_tools: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)


@dataclass
class SkillStep:
    order: int
    description: str
    expected_output: str = ""


class BaseSkill(ABC):
    manifest: SkillManifest

    @abstractmethod
    def build_prompt_extension(self, task: str) -> str: ...

    def match_score(self, task: str) -> int:
        """命中 trigger 的个数 —— 简单、可解释、可测（刻意不做 embedding/语义匹配）。

        空白 trigger 必须忽略：空字符串是任何任务的子串，放过去就等于「命中一切任务」。
        """
        task_lower = (task or "").lower()
        score = 0
        for trigger in self.manifest.triggers:
            needle = trigger.strip().lower()
            if needle and needle in task_lower:
                score += 1
        return score

    def matches(self, task: str) -> bool:
        return self.match_score(task) > 0
