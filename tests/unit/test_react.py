import asyncio, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

# 测试宿主适配（不触碰被测代码）：同 test_file_reader.py —— 本沙箱下
# tempfile 的 mkdtemp / TemporaryDirectory 生成的目录拒绝嵌套写入
# （已双点复现，普通 mkdir 继承工作区 ACL、嵌套读写正常），fixture 用 mkdir。
import shutil

from agent_runtime.core.agent.base import FinalAnswer, ToolCall
from agent_runtime.core.agent.events import AgentEventType
from agent_runtime.core.agent.planner import TaskPlanner
from agent_runtime.core.agent.react import DEFAULT_SYSTEM_PROMPT, ReActLoop
from agent_runtime.core.context.budget import TokenBudget
from agent_runtime.core.context.manager import ContextManager
from agent_runtime.core.llm.types import FunctionCall, LLMResponse, TokenUsage
from agent_runtime.core.memory.base import MemoryEntry, MemoryQuery
from agent_runtime.core.memory.manager import MemoryManager
from agent_runtime.core.memory.working import WorkingMemory
from agent_runtime.core.skill.base import SkillManifest
from agent_runtime.core.skill.loader import MarkdownSkill
from agent_runtime.core.skill.router import SkillRouter
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


def _setup(
    script: list[LLMResponse], *, max_steps: int = 15, memory_manager=None,
    context_manager: ContextManager | None = None, **agent_kw
):
    """真实组件组装：MockLLM + FileReaderTool + 真 ContextManager/MemoryManager/Tracer。

    `**agent_kw` 透传给 ReActLoop（Phase 3 的 skill_router / planner / skill_top_k，
    Phase 4 的 session_id 走这里）；`memory_manager` 可显式覆盖（Phase 4 会话标识测试用）；
    `context_manager` 可显式覆盖（Phase 5 压缩测试要自带预算与摘要器）。
    """
    root = _new_root()
    (Path(root) / "a.txt").write_text("hello react", encoding="utf-8")
    registry = ToolRegistry()
    registry.register(FileReaderTool(root=root))
    ctx = context_manager or ContextManager(budget=TokenBudget())
    memory = memory_manager or MemoryManager(working=WorkingMemory())
    tracer = Tracer()
    agent = ReActLoop(
        llm=MockLLMProvider(script=script),
        tool_registry=registry,
        context_manager=ctx,
        memory_manager=memory,
        tracer=tracer,
        max_steps=max_steps,
        **agent_kw,
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


# ── Phase 3：Skill / TaskPlanner 集成 ──
#
# 本段钉死 Plan §1.3 的集成契约：技能与计划**只能追加**在硬规则之后（注入安全），
# 且"这次用了哪个技能"必须可被外部看见（result.skills_used / skill_matched 事件 / trace config）。

SKILL_BODY = "GH-SOP：先 github_get_repo，再 github_list_dir，最后 github_read_file。"


class _RaisingLLM:
    """规划失败用最小假件：TaskPlanner 只调用 `.chat`，无需完整 BaseLLMProvider。"""

    async def chat(self, messages, tools=None):  # type: ignore[no-untyped-def]
        raise RuntimeError("planner llm down")


def _skill(name: str, triggers: list[str], body: str = SKILL_BODY):
    return MarkdownSkill(
        manifest=SkillManifest(name=name, description=f"{name} 描述", triggers=triggers),
        body=body,
    )


def _router(*skills) -> SkillRouter:
    router = SkillRouter()
    for s in skills:
        router.register(s)
    return router


def _sys_prompt(agent) -> str:
    msgs = agent.llm.last_messages
    assert msgs and msgs[0].role == "system", "必须真的发出过一次带 system 的请求"
    return msgs[0].content or ""


async def _events(agent, task: str) -> list:
    return [ev async for ev in agent.run_stream(task)]


async def test_matched_skill_is_appended_after_hard_rules():
    # 注入安全：技能文本**只能追加**，硬规则必须仍在最前面
    agent, ctx, memory, tracer, root = _setup(
        [LLMResponse(content="done")],
        skill_router=_router(_skill("gh", ["代码审查"])),
    )
    try:
        await agent.run("帮我做代码审查")
        prompt = _sys_prompt(agent)
        assert prompt.startswith(DEFAULT_SYSTEM_PROMPT), "硬规则必须仍在最前面"
        assert SKILL_BODY in prompt, "命中的技能正文应被注入"
        assert prompt.index(SKILL_BODY) > prompt.index("HARD RULES")
    finally:
        shutil.rmtree(root, ignore_errors=True)


async def test_no_match_injects_nothing():
    # 无命中时 system prompt 与 Phase 1 完全一致 —— 不得出现技能列表之类的额外噪音
    agent, ctx, memory, tracer, root = _setup(
        [LLMResponse(content="done")],
        skill_router=_router(_skill("gh", ["代码审查"])),
    )
    try:
        await agent.run("写一首关于秋天的诗")
        assert _sys_prompt(agent) == DEFAULT_SYSTEM_PROMPT
        assert agent.last_result.skills_used == []
    finally:
        shutil.rmtree(root, ignore_errors=True)


async def test_no_skill_router_is_safe():
    agent, ctx, memory, tracer, root = _setup([LLMResponse(content="done")])
    try:
        result = await agent.run("任意任务")
        assert result.final_answer == "done"
        assert result.skills_used == []
    finally:
        shutil.rmtree(root, ignore_errors=True)


async def test_skill_matched_event_reports_names():
    agent, ctx, memory, tracer, root = _setup(
        [LLMResponse(content="done")],
        skill_router=_router(_skill("gh", ["代码审查"])),
    )
    try:
        events = await _events(agent, "帮我做代码审查")
        matched = [e for e in events if e.event_type == AgentEventType.SKILL_MATCHED]
        assert len(matched) == 1, "命中技能应恰好发一次 skill_matched 事件"
        assert matched[0].data["skills"] == ["gh"]
    finally:
        shutil.rmtree(root, ignore_errors=True)


async def test_no_skill_matched_event_without_match():
    agent, ctx, memory, tracer, root = _setup(
        [LLMResponse(content="done")],
        skill_router=_router(_skill("gh", ["代码审查"])),
    )
    try:
        events = await _events(agent, "写一首关于秋天的诗")
        assert not [e for e in events if e.event_type == AgentEventType.SKILL_MATCHED]
    finally:
        shutil.rmtree(root, ignore_errors=True)


async def test_skills_used_lists_matched_skills():
    agent, ctx, memory, tracer, root = _setup(
        [LLMResponse(content="done")],
        skill_router=_router(_skill("gh", ["代码审查"])),
    )
    try:
        result = await agent.run("帮我做代码审查")
        assert result.skills_used == ["gh"]
    finally:
        shutil.rmtree(root, ignore_errors=True)


async def test_trace_session_records_skills():
    agent, ctx, memory, tracer, root = _setup(
        [LLMResponse(content="done")],
        skill_router=_router(_skill("gh", ["代码审查"])),
    )
    try:
        await agent.run("帮我做代码审查")
        # 直接读真实 tracer 状态（唯一通道：end_session 的返回值被 ReActLoop 内部消费）
        assert tracer._current_session.config.get("skills") == ["gh"]
    finally:
        shutil.rmtree(root, ignore_errors=True)


async def test_skill_top_k_limits_injection():
    agent, ctx, memory, tracer, root = _setup(
        [LLMResponse(content="done")],
        skill_router=_router(
            _skill("one_hit", ["代码审查"], body="BODY-ONE"),
            _skill("two_hits", ["代码审查", "分析仓库"], body="BODY-TWO"),
        ),
        skill_top_k=1,
    )
    try:
        await agent.run("代码审查并分析仓库")
        prompt = _sys_prompt(agent)
        assert "BODY-TWO" in prompt and "BODY-ONE" not in prompt
        assert agent.last_result.skills_used == ["two_hits"]
    finally:
        shutil.rmtree(root, ignore_errors=True)


async def test_planner_text_is_injected_after_hard_rules():
    planner = TaskPlanner(llm=MockLLMProvider([LLMResponse(content="计划：1) 读 README 2) 总结")]))
    agent, ctx, memory, tracer, root = _setup([LLMResponse(content="done")], planner=planner)
    try:
        await agent.run("分析 fastapi 仓库")
        prompt = _sys_prompt(agent)
        assert prompt.startswith(DEFAULT_SYSTEM_PROMPT)
        assert "计划：1) 读 README 2) 总结" in prompt
        assert prompt.index("计划：1)") > prompt.index("HARD RULES")
    finally:
        shutil.rmtree(root, ignore_errors=True)


async def test_planner_does_not_consume_the_main_script():
    # 规划器必须用**自己的** LLM 调用；若与主循环共用脚本，第一步就会被计划响应顶掉
    planner = TaskPlanner(llm=MockLLMProvider([LLMResponse(content="计划文本")]))
    agent, ctx, memory, tracer, root = _setup(
        [_tc("c1", '{"path": "a.txt"}'), LLMResponse(content="最终答案")], planner=planner
    )
    try:
        result = await agent.run("读取 a.txt 并总结")
        assert result.final_answer == "最终答案"
        assert agent.last_result.steps[0].observation == "hello react"
    finally:
        shutil.rmtree(root, ignore_errors=True)


async def test_no_planner_means_no_plan_text():
    agent, ctx, memory, tracer, root = _setup([LLMResponse(content="done")])
    try:
        await agent.run("分析 fastapi 仓库")
        assert "计划" not in _sys_prompt(agent)
    finally:
        shutil.rmtree(root, ignore_errors=True)


async def test_planner_failure_is_silent():
    planner = TaskPlanner(llm=_RaisingLLM())
    agent, ctx, memory, tracer, root = _setup([LLMResponse(content="done")], planner=planner)
    try:
        result = await agent.run("分析 fastapi 仓库")
        assert result.final_answer == "done", "规划失败不得影响主任务"
        assert _sys_prompt(agent).startswith(DEFAULT_SYSTEM_PROMPT)
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ── Phase 4：会话标识贯通 + 注入顺序（记忆在最后）──


class _RecordingLayer:
    """记录收到的 MemoryQuery / 存储条目，用于断言会话标识真的传下去了。"""

    def __init__(self, entries=None):
        self.queries = []
        self.stored = []
        self._entries = list(entries or [])

    async def query(self, query):
        self.queries.append(query)
        return list(self._entries)

    async def store(self, entry):
        self.stored.append(entry)
        return "id"

    async def clear(self):
        return None


def _memory_layer(entries=None):
    layer = _RecordingLayer(entries)
    return layer


async def test_run_passes_session_id_into_memory_query() -> None:
    layer = _memory_layer()
    manager = MemoryManager(working=WorkingMemory(), short_term=layer)
    agent, ctx, mem, tracer, root = _setup([LLMResponse(content="done")], memory_manager=manager)
    try:
        await agent.run("任务", session_id="s42")
        assert layer.queries and layer.queries[0].session_id == "s42"
        # 工作记忆先命中时不会查短时层；这里让 working 查不到，确保走到 short_term
    finally:
        shutil.rmtree(root, ignore_errors=True)


async def test_constructor_session_id_is_used_when_call_omits_it() -> None:
    layer = _memory_layer()
    manager = MemoryManager(working=WorkingMemory(), short_term=layer)
    agent, ctx, mem, tracer, root = _setup(
        [LLMResponse(content="done")], memory_manager=manager, session_id="from-ctor"
    )
    try:
        await agent.run("任务")
        assert layer.queries and layer.queries[0].session_id == "from-ctor"
    finally:
        shutil.rmtree(root, ignore_errors=True)


async def test_blank_session_id_normalizes_to_default() -> None:
    layer = _memory_layer()
    manager = MemoryManager(working=WorkingMemory(), short_term=layer)
    agent, ctx, mem, tracer, root = _setup([LLMResponse(content="done")], memory_manager=manager)
    try:
        await agent.run("任务", session_id="   ")
        assert layer.queries[0].session_id == "default"
    finally:
        shutil.rmtree(root, ignore_errors=True)


async def test_task_summary_entry_records_session_id() -> None:
    layer = _memory_layer()
    manager = MemoryManager(working=WorkingMemory(), short_term=layer)
    agent, ctx, mem, tracer, root = _setup([LLMResponse(content="done")], memory_manager=manager)
    try:
        await agent.run("任务", session_id="s7")
        assert layer.stored and layer.stored[0].metadata["session_id"] == "s7"
    finally:
        shutil.rmtree(root, ignore_errors=True)


async def test_memory_is_injected_after_skills_and_plan() -> None:
    """注入顺序：硬规则 → 技能 → 计划 → 记忆。记忆必须**最后**（它是参考资料，优先级最低）。"""
    layer = _memory_layer([MemoryEntry(content="历史结论", source="short_term")])
    manager = MemoryManager(working=WorkingMemory(), short_term=layer)
    planner = TaskPlanner(llm=MockLLMProvider([LLMResponse(content="PLAN-TEXT")]))
    agent, ctx, mem, tracer, root = _setup(
        [LLMResponse(content="done")],
        memory_manager=manager,
        skill_router=_router(_skill("gh", ["代码审查"])),
        planner=planner,
    )
    try:
        await agent.run("帮我做代码审查", session_id="s1")
        prompt = _sys_prompt(agent)
        assert prompt.startswith(DEFAULT_SYSTEM_PROMPT)
        assert prompt.index(SKILL_BODY) > prompt.index("HARD RULES")
        assert prompt.index("PLAN-TEXT") > prompt.index(SKILL_BODY)
        assert prompt.index("历史结论") > prompt.index("PLAN-TEXT"), "记忆必须排在技能与计划之后"
    finally:
        shutil.rmtree(root, ignore_errors=True)


async def test_recall_top_k_is_configurable() -> None:
    """`MEMORY_RECALL_TOP_K` 曾是完全无效的死配置（react.py 里硬编码 3）—— 这条用例钉住它真的生效。"""
    layer = _memory_layer()
    manager = MemoryManager(working=WorkingMemory(), short_term=layer)
    agent, ctx, mem, tracer, root = _setup(
        [LLMResponse(content="done")], memory_manager=manager, recall_top_k=1
    )
    try:
        await agent.run("任务")
        assert layer.queries and layer.queries[0].top_k == 1, layer.queries[0].top_k
    finally:
        shutil.rmtree(root, ignore_errors=True)


async def test_recall_top_k_defaults_to_three() -> None:
    layer = _memory_layer()
    manager = MemoryManager(working=WorkingMemory(), short_term=layer)
    agent, ctx, mem, tracer, root = _setup([LLMResponse(content="done")], memory_manager=manager)
    try:
        await agent.run("任务")
        assert layer.queries[0].top_k == 3
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ── Phase 5：压缩事件的降级可见性 ──
#
# `compact()` 现在会产出"想摘要没做成"这类信息。只写进 `CompactionResult` 而不发到事件里，
# 调用方（CLI / WS / 前端）就完全看不到 —— "机制做了、展示层藏起来"在本项目已发生三次。


def _tiny_ctx(summarizer=None) -> ContextManager:
    """预算小到必然触发压缩：available=1 → 阈值 0，每个工具结果之后都会压一次。"""
    return ContextManager(
        budget=TokenBudget(model_max_tokens=1, reserved_output=0, safety_margin=1.0),
        keep_recent=1,
        summarizer=summarizer,
    )


async def test_compaction_event_reports_a_degraded_summary() -> None:
    agent, ctx, memory, tracer, root = _setup(
        [_tc("c1", '{"path": "big.txt"}'), LLMResponse(content="最终答案")],
        context_manager=_tiny_ctx(),
    )
    try:
        (Path(root) / "big.txt").write_text("Z" * 4000, encoding="utf-8")
        events = [ev async for ev in agent.run_stream("读大文件")]
    finally:
        shutil.rmtree(root, ignore_errors=True)

    compaction = [e for e in events if e.event_type == AgentEventType.COMPACTION]
    assert compaction, "预算这么小必须触发压缩"
    data = compaction[0].data
    assert data["strategy"] == "truncate"
    assert data["degraded_from"] == "summarize", "想摘要却没有摘要器，必须如实标出来"
    assert "未配置" in data["reason"]
    assert data["summarized"] == 0


async def test_compaction_event_reports_a_successful_summary() -> None:
    async def summarizer(text: str) -> str:
        return "早期经过：读了一个大文件"

    agent, ctx, memory, tracer, root = _setup(
        [_tc("c1", '{"path": "big.txt"}'), LLMResponse(content="最终答案")],
        context_manager=_tiny_ctx(summarizer),
    )
    try:
        (Path(root) / "big.txt").write_text("Z" * 4000, encoding="utf-8")
        events = [ev async for ev in agent.run_stream("读大文件")]
    finally:
        shutil.rmtree(root, ignore_errors=True)

    compaction = [e for e in events if e.event_type == AgentEventType.COMPACTION]
    assert compaction
    data = compaction[0].data
    assert data["strategy"] == "summarize"
    assert data["degraded_from"] is None
    assert data["reason"] == ""
    assert data["summarized"] >= 1


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
