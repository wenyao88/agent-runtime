"""LLM 裁判（JUDGE_LLM）：四维度 1~5 分的**采样**深度评判。

三条纪律：
  1. 有 key 也只是**构造** provider，默认零调用（`--judge 0`）；
  2. 分数解析**严格**：只认 1~5 的整数。一条合法分都没有 → 返回 `None`，
     由调用方记成"未判分 + 原因"，**绝不记 0 分**（0 分会被读成"做得很差"）；
  3. 裁判自身失败（超时/网络）返回 `None`，不影响规则判分。
"""
from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

from ...core.benchmark.models import BenchmarkTask
from ...core.llm.types import Message

Judge = Callable[[BenchmarkTask, str], Awaitable[dict | None]]

DIMENSIONS = ("completion", "accuracy", "citation", "structure")
MAX_SCORE = 5

JUDGE_PROMPT = (
    "你是严格的评审。请评价这次 Agent 任务的完成质量，对四个维度各打 1-5 分：\n"
    "- completion：任务完成度（是否真的回答了要求的内容）\n"
    "- accuracy：准确性（是否与给定证据一致、有无编造）\n"
    "- citation：引用完整性（结论是否指明来源）\n"
    "- structure：结构清晰度（是否分点、可读）\n"
    "只输出一个 JSON 对象，键为上面四个英文名，值为 1-5 的整数，不要输出其它内容。\n\n"
    "任务：\n{task}\n\n期望要点（仅供参考）：{hints}\n\nAgent 的最终答案：\n{answer}"
)

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def parse_scores(raw: Any) -> dict[str, int] | None:
    """从模型回复里抠出合法分数。返回 `None` 表示"没拿到可用分数"。"""
    if not isinstance(raw, str):
        return None
    match = _JSON_BLOCK.search(raw)
    if match is None:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    scores: dict[str, int] = {}
    for key in DIMENSIONS:
        value = data.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        if 1 <= value <= MAX_SCORE:
            scores[key] = value
    return scores or None


def build_judge(settings: Any, *, provider_factory: Any = None) -> Judge | None:
    """按 JUDGE_LLM 配置构造裁判；没有可用 key 返回 `None`。

    `provider_factory` 仅供测试注入（沙箱装不上 openai）。
    """
    api_key = settings.judge_llm_api_key or settings.llm_api_key
    if not api_key:
        return None

    if provider_factory is None:
        # 惰性 import：不跑裁判时不该因为缺 openai 而影响装配
        from ..llm.openai_compatible import OpenAICompatibleProvider

        provider_factory = OpenAICompatibleProvider

    provider = provider_factory(
        api_key=api_key,
        base_url=settings.judge_llm_base_url or settings.llm_base_url,
        model=settings.judge_llm_model,
        temperature=0.0,
        timeout=float(getattr(settings, "tool_http_timeout_seconds", 60.0)),
    )

    async def judge(task: BenchmarkTask, answer: str) -> dict | None:
        prompt = JUDGE_PROMPT.format(
            task=task.task,
            hints=task.expected_answer_hints or "（未给出）",
            answer=answer or "（空）",
        )
        try:
            resp = await provider.chat([Message(role="user", content=prompt)])
        except Exception:  # noqa: BLE001 —— 裁判失败由调用方记成"未判分"
            return None
        return parse_scores(getattr(resp, "content", None))

    return judge
