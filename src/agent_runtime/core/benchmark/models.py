"""Benchmark 的数据模型（纯数据类，**零第三方依赖**）。

为什么集中定义：报告要落盘、要经 API 出去、还要被 Phase 7 的消融实验横向对比 ——
字段名与 `to_dict()` 的形状一旦定下就是**对外契约**，所以有专门的往返测试钉住它。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime

from ..agent.base import AgentResult


@dataclass
class BenchmarkTask:
    """一条评测任务。字段与上游 spec §8 对齐。"""

    task_id: str
    task: str
    category: str = "unknown"
    required_tools: list[str] = field(default_factory=list)
    expected_keywords: list[str] = field(default_factory=list)
    expected_args: dict[str, dict] = field(default_factory=dict)
    min_steps: int = 1
    expected_answer_hints: str = ""


@dataclass
class ToolEvent:
    """由 `TOOL_RESULT` 事件归一化出来的工具调用记录。"""

    step: int
    tool: str
    success: bool
    arguments: dict = field(default_factory=dict)
    result_chars: int = 0


@dataclass
class CompactionEvent:
    """由 `COMPACTION` 事件归一化出来的压缩记录（Phase 5 的产物）。"""

    before: int
    after: int
    strategy: str
    summarized: int = 0
    noop: bool = False
    degraded_from: str | None = None


@dataclass
class TaskRun:
    """一条任务的**原始**运行记录（判分的输入）。"""

    task: BenchmarkTask
    result: AgentResult | None = None
    tool_events: list[ToolEvent] = field(default_factory=list)
    compactions: list[CompactionEvent] = field(default_factory=list)
    error: str = ""
    """该任务自身抛异常时记在这里 —— 整轮评测**不因此中断**。"""


@dataclass
class TaskVerdict:
    """一条任务的判分结果。"""

    task_id: str
    success: bool = False
    required_tools_ok: bool = False
    keywords_ok: bool = False
    missing_tools: list[str] = field(default_factory=list)
    missing_keywords: list[str] = field(default_factory=list)
    extra_tool_calls: int = 0
    arg_hits: int = 0
    arg_expected: int = 0
    warning: str | None = None
    skills_used: list[str] = field(default_factory=list)
    steps: int = 0
    total_tokens: int = 0
    latency_ms: int = 0
    error: str = ""
    judge_scores: dict[str, int] | None = None
    judge_reason: str = ""


@dataclass
class BenchmarkMetrics:
    """8 个核心指标。**分母为 0 的一律是 None，不是 0**（0 会被读成"很差"）。"""

    tasks_total: int = 0
    success_rate: float | None = None
    tool_selection_accuracy: float | None = None
    tool_argument_accuracy: float | None = None
    avg_steps: float | None = None
    avg_total_tokens: float | None = None
    avg_latency_ms: float | None = None
    compression_ratio: float | None = None
    compression_by_strategy: dict[str, float] = field(default_factory=dict)
    error_recovery_rate: float | None = None
    judged_tasks: int = 0
    avg_judge_score: float | None = None


@dataclass
class BenchmarkReport:
    run_id: str
    config: dict = field(default_factory=dict)
    verdicts: list[TaskVerdict] = field(default_factory=list)
    metrics: BenchmarkMetrics = field(default_factory=BenchmarkMetrics)
    created_at: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["created_at"] = self.created_at.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "BenchmarkReport":
        created = data.get("created_at")
        return cls(
            run_id=str(data.get("run_id") or ""),
            config=dict(data.get("config") or {}),
            verdicts=[TaskVerdict(**v) for v in data.get("verdicts") or []],
            metrics=BenchmarkMetrics(**(data.get("metrics") or {})),
            created_at=datetime.fromisoformat(created) if isinstance(created, str) else datetime.now(),
        )

    @property
    def summary(self) -> dict:
        """列表接口用的小结：不带逐任务明细。"""
        return {
            "run_id": self.run_id,
            "created_at": self.created_at.isoformat(),
            "config": dict(self.config),
            "metrics": asdict(self.metrics),
        }
