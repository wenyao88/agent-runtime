"""Benchmark Runner：把一份任务集跑成一份**自带配置**的报告（纯编排，零第三方依赖）。

三条设计要点：
  1. **注入 agent 工厂**：runner 不 import 任何真实 provider，沙箱里用假 agent 就能测整条管线；
     每条任务都新建 agent（复用会让上一条的上下文/记忆污染下一条的成绩）。
  2. **只观察，不改造**：通过 `run_stream` 收集 `tool_result`（成功/失败）与 `compaction`（压缩比），
     不改 `ReActLoop` / `AgentResult` 的行为。
  3. **单条任务失败不中断整轮**：一条任务炸了就丢掉整批结果，等于没有评测 —— 记进 `TaskRun.error` 继续跑。
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import datetime
from typing import Any

from ..agent.base import AgentResult
from .errors import classify_error
from .evaluator import evaluate
from .metrics import summarize
from .models import (
    BenchmarkReport,
    BenchmarkTask,
    CompactionEvent,
    TaskRun,
    TaskVerdict,
    ToolEvent,
)

Judge = Callable[[BenchmarkTask, str], Awaitable[dict | None]]
AgentFactory = Callable[[BenchmarkTask], Any]
"""按任务造 agent。**必须接收 task**：离线假 agent 需要知道这条任务要调哪些工具。

工厂返回的对象必须有：`run_stream(task, session_id="")` 与 `last_result`（跑完后的 `AgentResult`）。
`last_result` 是**契约的一部分**，不是可选装饰：runner 靠它拿步数/token/耗时（见 `_run_one`）。
"""
RunIdFactory = Callable[[dict], str]

_TOOL_RESULT = "tool_result"
_COMPACTION = "compaction"
_MAX_JUDGE_SCORE = 5
ERROR_EXCERPT_CHARS = 200
"""失败结果只留前 200 字符（进度文件要能自诊断，又不能被长文本撑爆）。"""


def clean_judge_scores(raw: Any) -> dict[str, int] | None:
    """裁判分数的**唯一**校验口径：只接受 1~5 的整数，一条都不合法 → `None`。

    `bool` 是 `int` 的子类，必须显式排除（`True` 会被算成 1 分）；超出 1~5 的也丢掉。
    校验放在这里（core），`infrastructure/benchmark/judge.py` 复用同一个谓词 ——
    自定义 judge callable 从 runner 边界进来时同样受约束（审查 I5）。
    """
    if not isinstance(raw, dict):
        return None
    scores: dict[str, int] = {}
    for key, value in raw.items():
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        if 1 <= value <= _MAX_JUDGE_SCORE:
            scores[str(key)] = value
    return scores or None


def _default_run_id(config: dict) -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{config.get('provider') or 'run'}"


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class BenchmarkRunner:
    def __init__(
        self,
        agent_factory: AgentFactory,
        *,
        judge: Judge | None = None,
        evaluator: Callable[[BenchmarkTask, TaskRun], TaskVerdict] = evaluate,
        run_id_factory: RunIdFactory | None = None,
        session_scope: str = "task",
    ) -> None:
        self._agent_factory = agent_factory
        self._judge = judge
        self._evaluator = evaluator
        self._run_id_factory = run_id_factory or _default_run_id
        # 非法值一律退回逐任务隔离：宁可隔离，也不要让任务之间互相污染
        self._session_scope = "run" if str(session_scope).strip().lower() == "run" else "task"

    @property
    def session_scope(self) -> str:
        """生效的会话范围（`task` / `run`）—— 调用方要把它写进报告 config。"""
        return self._session_scope

    async def run(
        self,
        tasks: list[BenchmarkTask],
        *,
        config: dict | None = None,
        limit: int | None = None,
        on_progress: Callable[[int, int, TaskVerdict], None] | None = None,
        run_id: str | None = None,
        done: dict[str, tuple[TaskRun, TaskVerdict]] | None = None,
        on_verdict: Callable[[TaskRun, TaskVerdict], None] | None = None,
    ) -> BenchmarkReport:
        """跑一批任务并汇总。

        `config["judge"] = N` 时只对**前 N 条**采样裁判（确定性，便于复现）。
        `run_id` 给定时就用它（API 需要先返回 run_id 再后台跑）。

        `done` = 上一轮**已经成功**的条目（`{task_id: (run, verdict)}`）：命中的任务**不调用 agent**，
        直接用已存结果补齐报告，`verdict.skipped` 标为 `True`。这是断点续跑的落点 ——
        100 条 × 3 组要跑几小时，省下的就是这些真实调用（失败的条目不进 `done`，下次重试）。
        `on_verdict` 在**每条真正跑完的任务**判分后立刻回调（调用方据此追加进度文件；跳过的条目不回调）。
        """
        selected = list(tasks)
        if limit is not None:
            selected = selected[: max(0, limit)]
        cfg = dict(config or {})
        cfg["tasks_total"] = len(selected)
        if limit is not None:
            cfg["limit"] = limit
        judge_limit = max(0, _as_int(cfg.get("judge")))
        final_run_id = run_id or self._run_id_factory(cfg)
        reused = dict(done or {})

        runs: list[TaskRun] = []
        verdicts: list[TaskVerdict] = []
        for index, task in enumerate(selected):
            stored = reused.get(task.task_id)
            if stored is not None:
                run, verdict = stored[0], replace(stored[1], skipped=True)
            else:
                run = await self._run_one(task, final_run_id)
                try:
                    verdict = self._evaluator(task, run)
                except Exception as e:  # noqa: BLE001 —— 自定义评测器抛异常也不能中断整轮
                    verdict = TaskVerdict(
                        task_id=task.task_id, error=f"评测器异常：{type(e).__name__}: {e}"
                    )
                if self._judge is not None and index < judge_limit:
                    await self._apply_judge(task, run, verdict)
                if on_verdict is not None:
                    # 成功与失败都回调（进度文件要如实记录失败，续跑时才不会把它当已完成）
                    on_verdict(run, verdict)
            runs.append(run)
            verdicts.append(verdict)
            if on_progress is not None:
                on_progress(index + 1, len(selected), verdict)

        return BenchmarkReport(
            run_id=final_run_id,
            config=cfg,
            verdicts=verdicts,
            metrics=summarize(verdicts, runs),
        )

    # ── 内部 ──

    def _session_id(self, run_id: str, task: BenchmarkTask) -> str:
        """逐任务隔离（`task` 范围，默认）或整轮共用一个会话（`run` 范围）。

        默认不传 session_id 会全部落到 `default`，而 memory 的 working/short_term 是按会话组织、
        且 `get_memory_manager()` 是进程单例 —— 于是开着记忆时，上一条任务的记忆会被下一条召回，
        "逐任务隔离"就成了空话（审查 I1）。

        `run` 范围是给消融的 memory 组用的：记忆组要考的就是"跨任务复用"，逐任务隔离会让这组
        永远考不出差别，所以整轮共用 `bench-<run_id>`，同一轮里的上一条任务才有机会被下一条召回。
        """
        if self._session_scope == "run":
            return f"bench-{run_id}"
        return f"bench-{run_id}-{task.task_id}"

    async def _run_one(self, task: BenchmarkTask, run_id: str) -> TaskRun:
        run = TaskRun(task=task)
        try:
            agent = self._agent_factory(task)
            async for event in agent.run_stream(
                task.task, session_id=self._session_id(run_id, task)
            ):
                self._collect(event, run)
            result = getattr(agent, "last_result", None)
            if isinstance(result, AgentResult):
                run.result = result
            else:
                run.error = "agent 没有产出 AgentResult"
                run.error_kind = "task"
        except Exception as e:  # noqa: BLE001 —— 单条任务失败绝不中断整轮评测
            run.error = f"{type(e).__name__}: {e}"
            # 限流/超时是 provider 抽风，不是 agent 做错了（300 次真实调用必然撞上）
            run.error_kind = classify_error(e)
        return run

    @staticmethod
    def _collect(event: Any, run: TaskRun) -> None:
        kind = getattr(getattr(event, "event_type", None), "value", "")
        data = getattr(event, "data", None)
        if not isinstance(data, dict):
            return
        if kind == _TOOL_RESULT:
            success = bool(data.get("success"))
            text = str(data.get("result") or "")
            run.tool_events.append(
                ToolEvent(
                    step=_as_int(data.get("step")),
                    tool=str(data.get("tool") or ""),
                    success=success,
                    result_chars=len(text),
                    # 失败原因只留片段：进度文件是跑挂之后唯一的现场，长文本不进文件
                    error_excerpt="" if success else text[:ERROR_EXCERPT_CHARS],
                )
            )
        elif kind == _COMPACTION:
            run.compactions.append(
                CompactionEvent(
                    before=_as_int(data.get("before")),
                    after=_as_int(data.get("after")),
                    strategy=str(data.get("strategy") or ""),
                    summarized=_as_int(data.get("summarized")),
                    noop=bool(data.get("noop")),
                    degraded_from=data.get("degraded_from") or None,
                    summarizer_tokens=_as_int(data.get("summarizer_tokens")),
                    summarizer_ms=_as_int(data.get("summarizer_ms")),
                )
            )

    async def _apply_judge(
        self, task: BenchmarkTask, run: TaskRun, verdict: TaskVerdict
    ) -> None:
        """裁判失败**绝不影响**规则判分，也绝不记 0 分 —— 只记"未判分 + 原因"。"""
        if self._judge is None:
            return
        answer = ""
        if run.result is not None and isinstance(run.result.final_answer, str):
            answer = run.result.final_answer
        try:
            scores = await self._judge(task, answer)
        except Exception as e:  # noqa: BLE001
            verdict.judge_reason = f"裁判失败：{type(e).__name__}: {e}"
            return
        if isinstance(scores, dict):
            clean = clean_judge_scores(scores)
            if clean:
                verdict.judge_scores = clean
                return
            verdict.judge_reason = "裁判返回的分数不可解析（只认 1~5 的整数）"
            return
        verdict.judge_reason = "裁判没有返回可用分数"
