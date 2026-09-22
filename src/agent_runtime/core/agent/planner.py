from dataclasses import dataclass, field
from enum import Enum


class SubTaskStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


@dataclass
class SubTask:
    id: str
    description: str
    tools_needed: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    status: SubTaskStatus = SubTaskStatus.PENDING


@dataclass
class TaskPlan:
    original_task: str
    subtasks: list[SubTask] = field(default_factory=list)
