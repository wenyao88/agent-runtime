"""消融分组：三组自变量 + 设置覆盖 + 对比报告（纯逻辑，零第三方依赖）。

三组只改"记忆开关"与"摘要压缩开关"，其余（模型、任务集、代码版本、压缩阈值）完全一致；
报告里必须带**开关快照**，否则三份报告分不清谁是谁（Phase 7 开跑前检查的第 3 条缺口）。

`AblationRunner` 的 `runner_factory` 由调用方注入（`core` 不 import 任何真实 provider / 装配），
与 Phase 6 的 `agent_factory` 同一手法。
"""
from __future__ import annotations

import copy
import re
import secrets
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Callable

from .metrics import summarize
from .models import BenchmarkMetrics, BenchmarkReport
from .runner import BenchmarkRunner


@dataclass(frozen=True)
class AblationGroup:
    name: str
    memory: bool
    compaction: bool
    session_scope: str = "task"


ABLATION_GROUPS: tuple[AblationGroup, ...] = (
    AblationGroup(name="baseline", memory=False, compaction=False, session_scope="task"),
    AblationGroup(name="memory", memory=True, compaction=False, session_scope="run"),
    AblationGroup(name="memory+compaction", memory=True, compaction=True, session_scope="run"),
)
"""三组与 spec §4 一一对应。两条容易搞错的细节：

* `memory=True` **同时**打开 short_term 与 long_term —— 只开 short_term 时，文本匹配方向是
  "整段 query 文本是条目内容的子串"（`working.py:21`），长任务几乎召不回东西（spec §3-B）。
* `compaction` **只控制 SUMMARIZE**：SQUEEZE/TRUNCATE 是 `compact()` 里的无条件行为（`react.py:211`），
  三组都会压（spec §4）。
"""


def find_group(name: str) -> AblationGroup | None:
    wanted = (name or "").strip().lower()
    for group in ABLATION_GROUPS:
        if group.name == wanted:
            return group
    return None


def apply_group(settings: Any, group: AblationGroup) -> Any:
    """返回**新的** settings：只改本阶段的自变量，绝不动传入对象。

    优先走 pydantic v2 的 `model_copy(update=...)`（正式拷贝路径），
    鸭子类型对象退回 `copy.copy` + `setattr` —— 沙箱里没有 pydantic，两条路径都要能跑。
    """
    overrides = {
        "memory_short_term_enabled": group.memory,
        "memory_long_term_enabled": group.memory,
        # 整理（consolidate）一律关：把"额外 LLM 成本"归因给压缩那一组，别混在一起
        "memory_consolidate_enabled": False,
        "agent_compaction_summarize_enabled": group.compaction,
    }
    model_copy = getattr(settings, "model_copy", None)
    if callable(model_copy):
        try:
            return model_copy(update=overrides)
        except Exception:  # noqa: BLE001 —— 不是 pydantic v2 的 model_copy 就退回浅拷贝
            pass
    clone = copy.copy(settings)
    for key, value in overrides.items():
        setattr(clone, key, value)
    return clone


def group_overrides(group: AblationGroup) -> dict:
    """落进报告 `config` 的开关快照 —— 报告必须自证"我到底开了什么"。"""
    return {
        "group": group.name,
        "memory": group.memory,
        "compaction": group.compaction,
        "session_scope": group.session_scope,
    }


def group_config(
    group: AblationGroup, *, provider: str, model: str = "", judge: int = 0
) -> dict:
    """一组跑一次用的完整 config：开关快照 + provider/model/judge。"""
    return {
        **group_overrides(group),
        "provider": (provider or "").strip().lower() or "run",
        "model": model,
        "judge": max(0, int(judge or 0)),
    }


# ── 对比报告 ──

KIND_ABLATION = "ablation"
"""落盘时的类型标记：历史列表据此**跳过**对比报告（它不是一份单组报告）。"""

DELTA_METRICS = (
    "success_rate",
    "success_rate_measured",
    "tool_selection_accuracy",
    "tool_argument_accuracy",
    "avg_steps",
    "avg_total_tokens",
    "avg_latency_ms",
    "provider_errors",
    "compaction_events",
    "summarizer_tokens",
    "summarizer_ms",
    "compression_ratio",
)
"""参与对比的指标。**包含计数与成本** —— 消融要回答的正是"记忆/压缩各花了多少"。"""

ROLES = ("first", "followup", "standalone")
"""`followup` 是记忆效应的直接观测点；`standalone` 是既不是 first 也不是 followup 的独立任务。"""

_ROLE_FIELDS = ("success_rate", "success_rate_measured", "avg_steps", "avg_total_tokens")


def _delta(baseline_value: object, group_value: object) -> dict:
    """绝对差 + 相对差（`绝对差 / baseline`）；任一侧是 `None`（没测）或非数字 → 差值也是 `None`。

    `baseline == 0` 时相对差**没有定义**（不是 0）：成功率从 0% 提到 100% 的"相对提升"是无穷大，
    记 0 会被读成"没变化"，记 `None` 才是诚实的"算不出来"。
    `baseline < 0` 时同样给 `None`：**压缩比可以为负**（`after > before`，上下文变大），
    用负基线算相对差会让"变好"显示成 -200%（审查 M-4 实测）。
    """
    low = _number(baseline_value)
    high = _number(group_value)
    if low is None or high is None:
        abs_delta: float | None = None
    else:
        abs_delta = high - low
    rel: float | None = None
    if abs_delta is not None and low > 0:
        rel = abs_delta / low
    return {
        "baseline": low,
        "group": high,
        "abs": abs_delta,
        "rel": rel,
    }


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _role_of(task_id: str, roles_by_id: dict[str, str]) -> str:
    role = roles_by_id.get(task_id, "")
    return role if role in ROLES else "standalone"


def role_metrics(report: BenchmarkReport, roles_by_id: dict[str, str]) -> dict[str, dict]:
    """按 `pair_role` 切分逐任务结果，给出 `tasks/success_rate/avg_steps/avg_tokens`。

    口径复用 `metrics.summarize`（压缩相关字段天然是 `None`：报告里没有事件流）——
    切分统计如果自己再算一遍，迟早和总表的口径漂移。
    """
    out: dict[str, dict] = {}
    for role in ROLES:
        verdicts = [v for v in report.verdicts if _role_of(v.task_id, roles_by_id) == role]
        sub: BenchmarkMetrics = summarize(verdicts, [])
        out[role] = {"tasks": len(verdicts)}
        out[role].update({name: getattr(sub, name) for name in _ROLE_FIELDS})
    return out


@dataclass
class AblationReport:
    """三份组报告 + 相对 baseline 的差 + 按 `pair_role` 切分的指标。"""

    ablation_id: str
    baseline: str = "baseline"
    groups: dict[str, BenchmarkReport] = field(default_factory=dict)
    deltas: dict[str, dict] = field(default_factory=dict)
    role_metrics: dict[str, dict] = field(default_factory=dict)
    """组名 → `pair_role` → `{tasks, success_rate, success_rate_measured, avg_steps, avg_total_tokens}`。

    放在对比报告而不是单组报告里：它是消融的观测口径，不是单轮评测的指标。"""

    config: dict = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.now)

    @property
    def kind(self) -> str:
        return KIND_ABLATION

    def to_dict(self) -> dict:
        return {
            "kind": KIND_ABLATION,
            "ablation_id": self.ablation_id,
            "baseline": self.baseline,
            "created_at": self.created_at.isoformat(),
            "config": dict(self.config),
            "groups": {name: report.to_dict() for name, report in self.groups.items()},
            "deltas": {name: dict(metrics) for name, metrics in self.deltas.items()},
            "role_metrics": {
                name: {role: dict(values) for role, values in roles.items()}
                for name, roles in self.role_metrics.items()
            },
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AblationReport":
        created = data.get("created_at")
        groups = {
            str(name): BenchmarkReport.from_dict(payload)
            for name, payload in (data.get("groups") or {}).items()
            if isinstance(payload, dict)
        }
        return cls(
            ablation_id=str(data.get("ablation_id") or ""),
            baseline=str(data.get("baseline") or "baseline"),
            groups=groups,
            deltas={
                str(name): dict(metrics)
                for name, metrics in (data.get("deltas") or {}).items()
                if isinstance(metrics, dict)
            },
            role_metrics={
                str(name): {
                    str(role): dict(values)
                    for role, values in (roles or {}).items()
                    if isinstance(values, dict)
                }
                for name, roles in (data.get("role_metrics") or {}).items()
                if isinstance(roles, dict)
            },
            config=dict(data.get("config") or {}),
            created_at=datetime.fromisoformat(created) if isinstance(created, str) else datetime.now(),
        )

    @property
    def summary(self) -> dict:
        return {
            "kind": KIND_ABLATION,
            "ablation_id": self.ablation_id,
            "created_at": self.created_at.isoformat(),
            "config": dict(self.config),
            "groups": {
                name: {"run_id": report.run_id, "metrics": asdict(report.metrics)}
                for name, report in self.groups.items()
            },
        }


RunnerFactory = Callable[[AblationGroup], BenchmarkRunner]
AblationIdFactory = Callable[[dict], str]


def _slug(name: str) -> str:
    """组名会进 `run_id`，而 `run_id` 会变成文件名：非 `[\\w.-]` 一律换成 `_`。

    `memory+compaction` 里的 `+` 会被报告落盘的安全校验拒掉（`store._safe_name`）。
    """
    return re.sub(r"[^\w.-]", "_", name)


def group_run_id(ablation_id: str, group: AblationGroup) -> str:
    """某一组在这一轮消融里的 `run_id`（`<ablation_id>-<slug(组名)>`）。

    **唯一出处**：`AblationRunner` 与进度文件都用它 —— 两处各写一遍迟早漂移
    （那就变成"报告写这里、进度写那里"，续跑直接失效）。
    """
    return f"{ablation_id}-{_slug(group.name)}"


def _default_ablation_id(config: dict) -> str:
    """`<时间戳>-<provider>-ablation-<4 位随机>`。

    随机后缀与 `service.new_run_id` 同理：时间戳只有秒级精度，同一秒跑两次会**静默覆盖**四份报告
    （审查 I-1 实测复现）。
    """
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{config.get('provider') or 'run'}-ablation-{secrets.token_hex(2)}"


class AblationRunner:
    """依次跑三组，产出 `AblationReport`。

    调用方给的 `runner_factory(group)` 必须返回**按该组设置装配好**的 runner ——
    同一个 runner 复用三次就等于三组同一套配置（开跑前检查 #1 的坑）。
    """

    def __init__(
        self,
        runner_factory: RunnerFactory,
        *,
        ablation_id_factory: AblationIdFactory | None = None,
        groups: tuple[AblationGroup, ...] = ABLATION_GROUPS,
    ) -> None:
        self._runner_factory = runner_factory
        self._ablation_id_factory = ablation_id_factory or _default_ablation_id
        self._groups = tuple(groups)

    @property
    def groups(self) -> tuple[AblationGroup, ...]:
        return self._groups

    async def run(
        self,
        tasks: list,
        *,
        provider: str,
        model: str = "",
        judge: int = 0,
        limit: int | None = None,
        on_group: Callable[[str], None] | None = None,
        on_group_done: Callable[[str, BenchmarkReport], None] | None = None,
        on_progress: Callable[[int, int, Any], None] | None = None,
        ablation_id: str | None = None,
        done_for_group: Callable[[AblationGroup], dict] | None = None,
        on_verdict_for_group: Callable[[AblationGroup], Callable | None] | None = None,
    ) -> AblationReport:
        """依次跑三组，产出 `AblationReport`。

        * `on_group(name)`：该组**开跑前**回调（打印分隔标题）；
        * `on_group_done(name, report)`：该组**一跑完立刻**回调 —— 三组要跑几小时，落盘不能等三组全完
          （否则第一组跑完的东西也会随第二组的崩溃一起丢掉）；
        * `done_for_group(group)`：给该组返回"上一轮已成功的条目"，用于续跑（不调用 LLM）；
        * `on_verdict_for_group(group)`：给该组返回"每条跑完就回调"的函数（追加该组的进度文件）；
        * `on_progress`：原样转发给当组 runner。
        """
        cfg = {
            "provider": (provider or "").strip().lower() or "run",
            "model": model,
            "judge": max(0, int(judge or 0)),
            "groups": [group.name for group in self._groups],
            "tasks_total": len(tasks) if limit is None else min(max(0, limit), len(tasks)),
        }
        final_id = ablation_id or self._ablation_id_factory(cfg)
        baseline_name = self._groups[0].name if self._groups else "baseline"

        reports: dict[str, BenchmarkReport] = {}
        role_metrics_by_group: dict[str, dict] = {}
        roles_by_id = {
            task.task_id: getattr(task, "pair_role", "") for task in tasks
        }
        for group in self._groups:
            if on_group is not None:
                on_group(group.name)
            runner = self._runner_factory(group)
            report = await runner.run(
                tasks,
                config=group_config(
                    group, provider=provider, model=model, judge=judge
                ),
                limit=limit,
                on_progress=on_progress,
                run_id=group_run_id(final_id, group),
                done=done_for_group(group) if done_for_group is not None else None,
                on_verdict=(
                    on_verdict_for_group(group)
                    if on_verdict_for_group is not None
                    else None
                ),
            )
            reports[group.name] = report
            role_metrics_by_group[group.name] = role_metrics(report, roles_by_id)
            if on_group_done is not None:
                # 立刻交出去落盘：这一组已经花掉的时间不能因为下一组崩掉而白费
                on_group_done(group.name, report)

        deltas = self._deltas(reports, role_metrics_by_group, baseline_name)
        return AblationReport(
            ablation_id=final_id,
            baseline=baseline_name,
            groups=reports,
            deltas=deltas,
            role_metrics=role_metrics_by_group,
            config=cfg,
        )

    @staticmethod
    def _deltas(
        reports: dict[str, BenchmarkReport],
        role_metrics_by_group: dict[str, dict],
        baseline_name: str,
    ) -> dict[str, dict]:
        baseline = reports.get(baseline_name)
        out: dict[str, dict] = {}
        for name, report in reports.items():
            metrics = report.metrics if report is not None else BenchmarkMetrics()
            entry: dict[str, dict] = {}
            for metric in DELTA_METRICS:
                base_value = getattr(baseline.metrics, metric, None) if baseline else None
                entry[metric] = _delta(base_value, getattr(metrics, metric, None))
            # followup 的 delta 直接给出来：读的人不该自己做减法
            base_roles = role_metrics_by_group.get(baseline_name, {})
            for field in _ROLE_FIELDS:
                base_value = (base_roles.get("followup") or {}).get(field)
                group_value = (role_metrics_by_group.get(name, {}).get("followup") or {}).get(field)
                entry[f"role.followup.{field}"] = _delta(base_value, group_value)
            out[name] = entry
        return out
