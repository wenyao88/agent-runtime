from dataclasses import dataclass, field
from enum import Enum

from ..llm.base import BaseLLMProvider
from ..llm.types import Message


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


PLANNER_PROMPT = (
    "你是任务规划器。把下面的任务拆成**最多 {max_subtasks} 步**的可执行计划。\n"
    "只输出编号步骤列表（每行一步，写清这一步用什么工具、要拿到什么），"
    "不要输出解释、不要输出前言后语、不要调用工具。\n"
    "可用工具：{tools}\n"
    "任务：{task}"
)

# ponytail 天花板：计划文本会进入**每一步**请求的 system prompt，长度没有上限就是每一步都在烧钱。
# 截断必须带标记 —— 本项目在 Demo 1 已经吃过「无标记截断把数据切成看起来完整的样子」的亏。
MAX_PLAN_CHARS = 2000
TRUNCATED_MARK = "\n…（计划过长，已截断）"


class TaskPlanner:
    """把复杂任务拆成计划**文本**，注入 System Prompt。

    ponytail 天花板：不产出结构化 `TaskPlan`、也不逐子任务执行 —— 控制流仍是单轮 ReAct。
    模型看到计划后会自己按计划推进，但不能保证逐步执行；升级路径 = 让 ReActLoop 消费
    `SubTask` 列表并逐项驱动（需要新的控制流，属于后续阶段）。
    """

    def __init__(self, llm: BaseLLMProvider, max_subtasks: int = 5):
        self.llm = llm
        self.max_subtasks = max(1, max_subtasks)

    def build_prompt(self, task: str, tools: list[str]) -> str:
        tool_list = "、".join(tools) if tools else "（本任务无可用工具）"
        return PLANNER_PROMPT.format(
            max_subtasks=self.max_subtasks, tools=tool_list, task=task
        )

    async def plan(self, task: str, tools: list[str]) -> str:
        """成功 → 计划文本（已 strip）；任何失败/空内容 → `""`（安静降级）。

        只传工具**名字**、不传 schema：规划阶段产生的 tool_calls 无处执行。
        """
        try:
            resp = await self.llm.chat(
                [Message(role="user", content=self.build_prompt(task, tools))]
            )
            text = resp.content
        except Exception:  # noqa: BLE001 — by design: 规划失败绝不能带崩主任务
            return ""
        if not isinstance(text, str):
            return ""
        text = text.strip()
        if len(text) > MAX_PLAN_CHARS:
            return text[:MAX_PLAN_CHARS] + TRUNCATED_MARK
        return text
