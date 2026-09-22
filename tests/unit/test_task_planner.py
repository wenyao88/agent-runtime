"""TaskPlanner 契约：把任务拆成计划文本（只注入 System Prompt，不改变 ReAct 控制流）。

设计约束（已批）：
  * 产出是**一段文本**，不是结构化 TaskPlan、也不逐子任务执行 —— 天花板已写在计划文档里；
  * 失败一律**安静降级**返回 `""`：规划是"锦上添花"，任何异常都不允许把主任务带崩；
  * 不把工具 schema 传给规划 LLM：规划只需工具**名字**，传 schema 会让模型倾向直接发 tool_calls，
    而规划阶段产生 tool_calls 无处执行（本轮不改变控制流）。

沙箱适配：脚本化 MockLLMProvider + 纯标准库假件。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from agent_runtime.core.agent.planner import TaskPlanner
from agent_runtime.core.llm.base import BaseLLMProvider
from agent_runtime.core.llm.types import LLMResponse
from agent_runtime.infrastructure.llm.mock import MockLLMProvider

TOOLS = ["github_get_repo", "github_list_dir", "github_read_file"]
PLAN = "1. 读取仓库概况\n2. 查看目录结构\n3. 阅读关键文件"


class RaisingLLM(BaseLLMProvider):
    """真实会遇到的失败：网络/鉴权/超时最终都表现为这里抛异常。"""

    def __init__(self, exc: Exception) -> None:
        self.exc = exc
        self.calls = 0

    async def chat(self, messages, tools=None):  # type: ignore[no-untyped-def]
        self.calls += 1
        raise self.exc

    async def stream(self, messages, tools=None):  # type: ignore[no-untyped-def]
        raise self.exc


def _planner(llm, max_subtasks: int = 5) -> TaskPlanner:
    return TaskPlanner(llm=llm, max_subtasks=max_subtasks)


def _prompt_sent(llm: MockLLMProvider) -> str:
    assert llm.last_messages, "规划器必须真的调用一次 LLM"
    return "\n".join(m.content or "" for m in llm.last_messages)


# ── 正常路径 ──


def test_plan_returns_llm_text() -> None:
    llm = MockLLMProvider([LLMResponse(content=PLAN)])
    assert asyncio.run(_planner(llm).plan("分析 fastapi 仓库", TOOLS)) == PLAN


def test_plan_strips_surrounding_whitespace() -> None:
    llm = MockLLMProvider([LLMResponse(content=f"\n\n  {PLAN}  \n")])
    assert asyncio.run(_planner(llm).plan("分析 fastapi 仓库", TOOLS)) == PLAN


def test_plan_prompt_contains_every_tool_name() -> None:
    llm = MockLLMProvider([LLMResponse(content=PLAN)])
    asyncio.run(_planner(llm).plan("分析 fastapi 仓库", TOOLS))
    prompt = _prompt_sent(llm)
    for name in TOOLS:
        assert name in prompt, f"提示词里缺少工具 {name}"


def test_plan_prompt_contains_the_task() -> None:
    llm = MockLLMProvider([LLMResponse(content=PLAN)])
    asyncio.run(_planner(llm).plan("分析 fastapi 仓库", TOOLS))
    assert "分析 fastapi 仓库" in _prompt_sent(llm)


def test_subtask_limit_reaches_the_prompt() -> None:
    """max_subtasks 是配置项，必须真的影响提示词，否则就是死配置。"""
    a = MockLLMProvider([LLMResponse(content=PLAN)])
    b = MockLLMProvider([LLMResponse(content=PLAN)])
    asyncio.run(_planner(a, max_subtasks=3).plan("t", TOOLS))
    asyncio.run(_planner(b, max_subtasks=7).plan("t", TOOLS))
    assert _prompt_sent(a) != _prompt_sent(b)


def test_plan_does_not_pass_tool_schemas_to_llm() -> None:
    llm = MockLLMProvider([LLMResponse(content=PLAN)])
    asyncio.run(_planner(llm).plan("分析 fastapi 仓库", TOOLS))
    assert llm.last_tools is None, "规划阶段不得传 tools：规划期产生的 tool_calls 无处执行"


def test_plan_works_without_any_tools() -> None:
    llm = MockLLMProvider([LLMResponse(content=PLAN)])
    assert asyncio.run(_planner(llm).plan("写一段说明", [])) == PLAN


# ── 降级路径：一律返回 ""，绝不抛 ──


def test_llm_exception_returns_empty_string() -> None:
    assert asyncio.run(_planner(RaisingLLM(RuntimeError("boom"))).plan("t", TOOLS)) == ""


def test_llm_timeout_returns_empty_string() -> None:
    assert asyncio.run(_planner(RaisingLLM(asyncio.TimeoutError())).plan("t", TOOLS)) == ""


def test_none_content_returns_empty_string() -> None:
    llm = MockLLMProvider([LLMResponse(content=None)])
    assert asyncio.run(_planner(llm).plan("t", TOOLS)) == ""


def test_whitespace_only_content_returns_empty_string() -> None:
    llm = MockLLMProvider([LLMResponse(content="   \n\t ")])
    assert asyncio.run(_planner(llm).plan("t", TOOLS)) == ""


def test_non_string_content_returns_empty_string() -> None:
    """真实厂商偶发返回非字符串 content；降级比崩掉好。"""
    llm = MockLLMProvider([LLMResponse(content=123)])  # type: ignore[arg-type]
    assert asyncio.run(_planner(llm).plan("t", TOOLS)) == ""


def _run_all() -> None:
    tests = [
        v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)
    ]
    for t in tests:
        print(f"RUN  {t.__name__}")
        t()
        print(f"PASS {t.__name__}")
    print("ALL PASS")


if __name__ == "__main__":
    _run_all()
