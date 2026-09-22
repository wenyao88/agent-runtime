import asyncio, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

# 测试宿主适配（不触碰被测代码）：同 test_file_reader.py —— 本沙箱下
# tempfile 的 mkdtemp / TemporaryDirectory 生成的目录拒绝嵌套写入
# （已双点复现，普通 mkdir 继承工作区 ACL、嵌套读写正常），fixture 用 mkdir。
import shutil

from agent_runtime.core.agent.base import FinalAnswer, ToolCall
from agent_runtime.core.agent.events import AgentEventType
from agent_runtime.core.agent.react import ReActLoop
from agent_runtime.core.context.budget import TokenBudget
from agent_runtime.core.context.manager import ContextManager
from agent_runtime.core.llm.types import FunctionCall, LLMResponse, TokenUsage
from agent_runtime.core.memory.manager import MemoryManager
from agent_runtime.core.memory.working import WorkingMemory
from agent_runtime.core.tool.registry import ToolRegistry
from agent_runtime.core.trace.tracer import Tracer
from agent_runtime.infrastructure.llm.mock import MockLLMProvider
from agent_runtime.infrastructure.tools.file_reader import FileReaderTool

_BASE = Path(__file__).resolve().parents[2] / ".testtmp"
_SEQ = 0


def _new_root() -> str:
    """新建一次性 fixture 目录（普通 mkdir，继承沙箱可写 ACL）。"""
    global _SEQ
    _SEQ += 1
    p = _BASE / f"react{_SEQ:02d}"
    p.mkdir(parents=True, exist_ok=True)
    return str(p)


def _tc(call_id: str, arguments: str, usage: TokenUsage | None = None) -> LLMResponse:
    """单 tool_call 响应（read_file + 任意 arguments 字符串）。"""
    return LLMResponse(
        content=None,
        tool_calls=[FunctionCall(id=call_id, name="read_file", arguments=arguments)],
        token_usage=usage or TokenUsage(),
    )


def _setup(script: list[LLMResponse], *, max_steps: int = 15):
    """真实组件组装：MockLLM + FileReaderTool + 真 ContextManager/MemoryManager/Tracer。"""
    root = _new_root()
    (Path(root) / "a.txt").write_text("hello react", encoding="utf-8")
    registry = ToolRegistry()
    registry.register(FileReaderTool(root=root))
    ctx = ContextManager(budget=TokenBudget())
    memory = MemoryManager(working=WorkingMemory())
    tracer = Tracer()
    agent = ReActLoop(
        llm=MockLLMProvider(script=script),
        tool_registry=registry,
        context_manager=ctx,
        memory_manager=memory,
        tracer=tracer,
        max_steps=max_steps,
    )
    return agent, ctx, memory, tracer, root


async def test_normal_three_steps():
    # ① 正常三步：成功读取 → 错误恢复（文件不存在，observation 给 LLM）→ 最终回答
    script = [
        _tc("c1", '{"path": "a.txt"}'),
        _tc("c2", '{"path": "missing.txt"}'),
        LLMResponse(content="最终答案：X"),
    ]
    agent, ctx, memory, tracer, root = _setup(script)
    try:
        result = await agent.run("读取 a.txt 和 missing.txt")
        assert result.final_answer == "最终答案：X"
        assert len(result.steps) == 3
        assert result.warning is None
        # 步骤结构：两步 ToolCall + 一步 FinalAnswer
        assert isinstance(result.steps[0].action, ToolCall)
        assert result.steps[0].observation == "hello react"
        assert isinstance(result.steps[1].action, ToolCall)
        assert "not found" in (result.steps[1].observation or "")
        assert isinstance(result.steps[2].action, FinalAnswer)
        assert result.steps[2].action.content == "最终答案：X"
        # tracer：session 结束、trace_id 一致
        assert tracer._current_session is not None
        assert tracer._current_session.status == "finished"
        assert result.trace_id == tracer._current_session.trace_id
        # memory：出现 task_summary 条目
        entries = memory.working._entries
        assert any(e.metadata.get("type") == "task_summary" for e in entries)
        # ctx 最后一条是 tool 消息（final assistant 内容不回写 ctx）
        msgs = ctx.get_messages()
        assert msgs[-1].role == "tool"
        assert "not found" in (msgs[-1].content or "")
        assert any(m.role == "tool" and m.content == "hello react" for m in msgs)
    finally:
        shutil.rmtree(root, ignore_errors=True)


async def test_bad_json_args_recovery():
    # ② 坏 JSON 参数：不炸工具层，转成 observation；下一轮恢复成功
    script = [
        _tc("c1", "not json"),
        LLMResponse(content="恢复"),
    ]
    agent, ctx, memory, tracer, root = _setup(script)
    try:
        result = await agent.run("演示坏参数恢复")
        assert result.final_answer == "恢复"
        # 行为变更（Demo 1 修复）：本次唯一的工具调用失败了，所以答案**没有**任何成功工具
        # 结果支撑 —— 必须带告警。旧断言 `warning is None` 固化的是修复前的行为，已过时。
        assert result.warning is not None, "全工具失败必须告警"
        assert "1/1" in result.warning
        assert "not valid JSON" in (result.steps[0].observation or "")
        tool_msgs = [m for m in ctx.get_messages() if m.role == "tool"]
        assert len(tool_msgs) == 1
        assert "not valid JSON" in (tool_msgs[0].content or "")
    finally:
        shutil.rmtree(root, ignore_errors=True)


async def test_max_steps_forced_finish():
    # ③ max_steps=2 强制收尾。MockLLMProvider 消耗顺序推演：
    #    第1步 chat 弹出 tc1；第2步 chat 弹出 tc2；循环结束 answered=False
    #    → forced 调用(tools=None)：script 剩单条 content("兜底答案")，
    #    单条模式不弹出、永久复用 → forced 调用返回兜底文案，不报错。
    script = [
        _tc("c1", '{"path": "a.txt"}'),
        _tc("c2", '{"path": "a.txt"}'),
        LLMResponse(content="兜底答案"),
    ]
    agent, ctx, memory, tracer, root = _setup(script, max_steps=2)
    try:
        result = await agent.run("永不满足的任务")
        assert result.warning is not None
        assert "max_steps" in result.warning
        assert result.final_answer == "兜底答案"
        # 2 个 tool step + 1 个 forced final step
        assert len(result.steps) == 3
        assert result.steps[-1].thought == "(forced)"
        assert isinstance(result.steps[-1].action, FinalAnswer)
        # 最后一次 LLM 调用确实是 forced（不带 tools）
        assert agent.llm.last_tools is None
        assert agent.llm.calls == 3
    finally:
        shutil.rmtree(root, ignore_errors=True)


async def test_event_stream():
    # ④ 事件流：与场景①同脚本改用 run_stream；含 5 类事件，
    #    TOOL_RESULT 恰两个：一成一败（错误恢复可观测）
    script = [
        _tc("c1", '{"path": "a.txt"}'),
        _tc("c2", '{"path": "missing.txt"}'),
        LLMResponse(content="最终答案：X"),
    ]
    agent, ctx, memory, tracer, root = _setup(script)
    try:
        types = []
        tool_results = []
        async for ev in agent.run_stream("读取 a.txt 和 missing.txt"):
            types.append(ev.event_type)
            if ev.event_type == AgentEventType.TOOL_RESULT:
                tool_results.append(ev.data)
        for t in (AgentEventType.STEP_START, AgentEventType.THOUGHT,
                  AgentEventType.TOOL_CALL, AgentEventType.TOOL_RESULT,
                  AgentEventType.FINAL_ANSWER):
            assert t in types
        assert len(tool_results) == 2
        assert [d["success"] for d in tool_results] == [True, False]
        assert tool_results[0]["result"] == "hello react"
        assert "not found" in tool_results[1]["result"]
        # 流消费完毕后 last_result 可用
        assert agent.last_result is not None
        assert agent.last_result.final_answer == "最终答案：X"
    finally:
        shutil.rmtree(root, ignore_errors=True)


async def test_token_aggregation():
    # ⑤ token 汇总：三个响应的 usage 逐项累加（10/5/15 + 20/10/30 + 30/20/50）
    script = [
        _tc("c1", '{"path": "a.txt"}', TokenUsage(10, 5, 15)),
        _tc("c2", '{"path": "a.txt"}', TokenUsage(20, 10, 30)),
        LLMResponse(content="done", token_usage=TokenUsage(30, 20, 50)),
    ]
    agent, ctx, memory, tracer, root = _setup(script)
    try:
        result = await agent.run("数 token")
        assert result.total_tokens.prompt_tokens == 60
        assert result.total_tokens.completion_tokens == 35
        assert result.total_tokens.total_tokens == 95
    finally:
        shutil.rmtree(root, ignore_errors=True)


async def test_system_prompt_forbids_fabricating_tool_results():
    # ⑥ 工具失败时只能如实报告，禁止用自己的知识编造工具结果（小模型尤其容易犯）
    from agent_runtime.core.agent.react import DEFAULT_SYSTEM_PROMPT as prompt

    low = prompt.lower()
    assert "fabricat" in low or "编造" in prompt, "必须明确禁止编造工具结果"
    assert "fail" in low or "失败" in prompt, "必须说明工具失败时怎么办"
    assert "tool" in low or "工具" in prompt


async def test_all_tools_failed_sets_warning():
    # ⑦ 所有工具都失败时，结果必须带 warning —— 否则用户会把编造内容当真
    script = [
        _tc("c1", '{"path": "missing.txt"}'),
        LLMResponse(content="## 报告\n1. 项目用途：某框架\n（这一段是编造的）"),
    ]
    agent, ctx, memory, tracer, root = _setup(script)
    try:
        result = await agent.run("读取 missing.txt 并总结")
        assert result.warning, "全工具失败必须产生 warning"
        assert "失败" in result.warning or "fail" in result.warning.lower()
        assert "1/1" in result.warning or "1" in result.warning
    finally:
        shutil.rmtree(root, ignore_errors=True)


async def test_tool_failure_evidence_reaches_the_model():
    # ⑧ 失败原因必须真的进入上下文，模型才有据可依地如实报告
    script = [
        _tc("c1", '{"path": "missing.txt"}'),
        LLMResponse(content="如实报告：目标文件不存在，无法分析"),
    ]
    agent, ctx, memory, tracer, root = _setup(script)
    try:
        await agent.run("读取 missing.txt")
        messages = getattr(agent.llm, "last_messages", None)
        assert messages, "Mock LLM 应记录最后一次收到的上下文"
        joined = "\n".join(m.content or "" for m in messages)
        assert "not found" in joined, joined[-400:]
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    _failed = []
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            print(f"RUN  {_name}")
            try:
                asyncio.run(_fn())
            except Exception as _e:
                print(f"FAIL {_name}: {type(_e).__name__}: {_e}")
                _failed.append(_name)
            else:
                print(f"PASS {_name}")
    if _failed:
        raise SystemExit(f"FAILED: {', '.join(_failed)}")
    print("ALL PASS")
