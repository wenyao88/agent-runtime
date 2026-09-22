"""零依赖演示：不需要 API Key、不需要装任何第三方包，直接看 ReAct 闭环怎么跑。

    python scripts/run_demo_mock.py

它会用 MockLLM 脚本驱动一次真实任务的完整执行流：
    Step 思考 → 调用 read_file → 观察结果 → 出错重试 → 生成最终答案

想看真实模型链路，见 README「快速开始」（.env 填 LLM_API_KEY + uvicorn + web 前端）。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from agent_runtime.core.agent.react import ReActLoop  # noqa: E402
from agent_runtime.core.context.budget import TokenBudget  # noqa: E402
from agent_runtime.core.context.manager import ContextManager  # noqa: E402
from agent_runtime.core.llm.types import FunctionCall, LLMResponse, TokenUsage  # noqa: E402
from agent_runtime.core.memory.manager import MemoryManager  # noqa: E402
from agent_runtime.core.memory.working import WorkingMemory  # noqa: E402
from agent_runtime.core.tool.registry import ToolRegistry  # noqa: E402
from agent_runtime.core.trace.tracer import Tracer  # noqa: E402
from agent_runtime.infrastructure.llm.mock import MockLLMProvider  # noqa: E402
from agent_runtime.infrastructure.tools.file_reader import FileReaderTool  # noqa: E402

ICON = {
    "step_start": "▶",
    "thought": "💭",
    "tool_call": "🔧",
    "tool_result": "📋",
    "compaction": "⚡",
    "final_answer": "✨",
    "error": "❌",
}


def _usage(p: int, c: int) -> TokenUsage:
    return TokenUsage(prompt_tokens=p, completion_tokens=c, total_tokens=p + c)


async def main() -> int:
    task = "读取 计划.md，先看项目定位那一节，然后总结这个项目要做什么"

    registry = ToolRegistry()
    registry.register(FileReaderTool(root=str(_ROOT)))

    llm = MockLLMProvider(
        [
            LLMResponse(
                content="先读文件看看内容。",
                tool_calls=[
                    FunctionCall(id="c1", name="read_file", arguments='{"path": "计划.md"}')
                ],
                token_usage=_usage(820, 40),
            ),
            LLMResponse(
                content="内容太长，我试试读一个不存在的文件来确认路径规则。",
                tool_calls=[
                    FunctionCall(id="c2", name="read_file", arguments='{"path": "nope.md"}')
                ],
                token_usage=_usage(1180, 52),
            ),
            LLMResponse(
                content=(
                    "## 结论\n\n"
                    "这个项目的定位是**技术研究与研发 Agent Runtime**：\n\n"
                    "1. 从 0 到 1 自研 Agent 核心运行框架（ReAct / Tool Registry / "
                    "Context / Memory / Compaction / Trace / Benchmark）；\n"
                    "2. 底层能力复用成熟组件（LLM API、MCP SDK、FastAPI、PostgreSQL、Redis）；\n"
                    "3. 用 GitHub 仓库分析 + 技术调研两个真实场景验证有效性。\n\n"
                    "> 重点不是做 Chatbot，而是自己实现 Runtime 核心能力。"
                ),
                token_usage=_usage(1420, 180),
            ),
        ]
    )

    agent = ReActLoop(
        llm=llm,
        tool_registry=registry,
        context_manager=ContextManager(budget=TokenBudget()),
        memory_manager=MemoryManager(working=WorkingMemory()),
        tracer=Tracer(),
        max_steps=6,
    )

    print(f"\n任务：{task}\n" + "─" * 68)
    async for ev in agent.run_stream(task):
        d = ev.data
        icon = ICON.get(ev.event_type.value, "·")
        if ev.event_type.value == "thought":
            print(f"{icon} 思考：{d['content']}")
        elif ev.event_type.value == "tool_call":
            print(f"{icon} 调用：{d['tool']}{d['args']}")
        elif ev.event_type.value == "tool_result":
            flag = "成功" if d["success"] else "失败"
            print(f"{icon} 结果：[{flag} {d['latency_ms']}ms] {d['result'][:90]}…")
        elif ev.event_type.value == "step_start":
            print(f"\n{icon} —— Step {d['step']} ——")
        elif ev.event_type.value == "final_answer":
            print(f"\n{icon} 最终答案：\n{d['content']}")

    r = agent.last_result
    assert r is not None
    print("\n" + "─" * 68)
    print(f"步数 {len(r.steps)} · LLM 调用 {llm.calls} 次 · "
          f"token {r.total_tokens.total_tokens} · 耗时 {r.total_latency_ms}ms · "
          f"trace {r.trace_id} · warning {r.warning}")
    print(f"工具错误恢复：2 次调用中 1 次失败并被模型自行纠正 → 最终仍产出答案")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
