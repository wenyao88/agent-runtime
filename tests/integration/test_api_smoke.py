"""API 冒烟测试：chat + ws + traces/context/benchmark 全链路，用 MockLLM 驱动，不需要真实 API Key。

无 fastapi/httpx 的环境（本沙箱）自动 SKIP 并 exit 0；用户机器上会真实执行。
两模式：pytest 可收集，也可直接 `python tests/integration/test_api_smoke.py`。

**配置隔离（2026-09-23 修）**：这份用例以前用 `deps.get_settings()`（= 跑它那个人的 `.env`）
却断言**默认配置**的后果 —— 只要谁开了 `MEMORY_SHORT_TERM_ENABLED=true`，
`enabled is False` 就挂（用户实测踩到）。集成测试不该依赖跑它的人怎么配，
所以现在显式构造一份固定配置（`_env_file=None` 不读 `.env`，关键开关逐个钉住）并注入
`deps.get_settings`。**新增断言时别再去读用户的 .env。**
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PROJECT_ROOT / "src"))


def _deps_available() -> bool:
    try:
        import fastapi  # noqa: F401
        import httpx  # noqa: F401
    except ImportError:
        return False
    return True


def _build_agent():
    """每个任务一个独立 agent 实例，避免跨用例共享上下文/记忆。

    带**真实**的 SkillRouter（从仓库 skills/ 目录加载）：这样 /api/chat 与 WS 的
    skills_used 才能被验证到"非空"的情形 —— 否则把 `skills_used=[]` 写死也照样通过。

    也用 API 自己的 trace store 单例（`deps.get_trace_store()`）：另造一份 store 就永远看不到
    真实链路写进去的东西，`/api/traces` 的冒烟会变成"断言一个空列表"。
    """
    from agent_runtime.api import deps as deps_mod
    from agent_runtime.core.agent.react import ReActLoop
    from agent_runtime.core.context.budget import TokenBudget
    from agent_runtime.core.context.manager import ContextManager
    from agent_runtime.core.llm.types import FunctionCall, LLMResponse, TokenUsage
    from agent_runtime.core.memory.manager import MemoryManager
    from agent_runtime.core.memory.working import WorkingMemory
    from agent_runtime.core.skill.router import SkillRouter
    from agent_runtime.core.tool.registry import ToolRegistry
    from agent_runtime.core.trace.tracer import Tracer
    from agent_runtime.infrastructure.llm.mock import MockLLMProvider
    from agent_runtime.infrastructure.skills.catalog import register_skills_from_dir
    from agent_runtime.infrastructure.tools.file_reader import FileReaderTool

    registry = ToolRegistry()
    registry.register(FileReaderTool(root=str(_PROJECT_ROOT)))
    skills = SkillRouter()
    errors = register_skills_from_dir(skills, str(_PROJECT_ROOT / "skills"))
    assert errors == [], errors
    # usage 是**夹具编的**（MockLLM 本来不上报 token）：没有它，`/api/traces` 里每步的
    # `token_usage` 永远是 0，"usage 真的从 ReAct 一路传到 trace 步骤"这条就没有端到端证据。
    llm = MockLLMProvider(
        [
            LLMResponse(
                content="先读文件",
                tool_calls=[
                    FunctionCall(id="call_1", name="read_file", arguments='{"path": "README.md"}')
                ],
                token_usage=TokenUsage(prompt_tokens=30, completion_tokens=12, total_tokens=42),
            ),
            LLMResponse(
                content="最终答案：已读取",
                token_usage=TokenUsage(prompt_tokens=40, completion_tokens=8, total_tokens=48),
            ),
        ]
    )
    return ReActLoop(
        llm=llm,
        tool_registry=registry,
        context_manager=ContextManager(budget=TokenBudget()),
        memory_manager=MemoryManager(working=WorkingMemory()),
        skill_router=skills,
        tracer=Tracer(store=deps_mod.get_trace_store(), source="chat"),
        max_steps=5,
    )


# 命中 github_analysis 的中文任务（trigger 含「代码结构」）
TASK = "看看 fastapi/fastapi 的代码结构，并读取 README.md"


def _isolated_settings(runs_dir: str):
    """给集成测试用的**固定**配置：不读用户的 `.env`，关键开关逐个钉死。

    两条理由：
      * 用例断言的是"默认配置下的可观测后果"（未启用的层报 `enabled:false`、错误列表为空……），
        一旦跑它的人开了某个开关就不再成立 —— 那是测试的问题，不是产品的问题；
      * 构造函数传入的值优先级**高于**环境变量，所以连"用户把开关导成 OS 环境变量"也盖得住。

    故意**不**钉 LLM key：本用例用 MockLLM 注入（chat/WS 走 `_build_agent`，
    `/api/context` 那段走 `deps.get_agent(llm=...)`），一个真实调用都不会发。
    """
    from agent_runtime.config.settings import Settings

    return Settings(
        _env_file=None,  # 关键：不读 .env（pydantic-settings v2 的官方开关）
        memory_short_term_enabled=False,
        memory_long_term_enabled=False,
        memory_consolidate_enabled=False,
        agent_compaction_summarize_enabled=False,
        agent_task_planning_enabled=False,
        skills_dir="skills",
        mcp_servers_file="/definitely/not/here.json",  # lifespan 照常执行，但不 spawn 任何 server
        benchmark_runs_dir=runs_dir,  # 每次跑一个全新的空目录（见下）
        trace_store="memory",
    )


def test_chat_and_ws_smoke() -> None:
    if not _deps_available():
        print("SKIP test_chat_and_ws_smoke (fastapi/httpx not installed)")
        return

    from fastapi.testclient import TestClient

    from agent_runtime.api import deps as deps_mod
    from agent_runtime.api.app import app

    # 报告落到**新的一次性目录**：既不污染仓库，也不让"历史为空"依赖上一次跑剩了什么
    # （以前固定用 `.testtmp/bench_api_smoke`，第二次跑就会看到上次那份报告 → count==0 假失败）
    isolated = _isolated_settings(tempfile.mkdtemp(prefix="bench_api_smoke_"))

    # ── 顺序很重要：先换配置、清单例，**再**造 agent ──
    # `_build_agent()` 内部会 `deps.get_trace_store()`；如果先造 agent 再清缓存，
    # agent 手里会攥着"用用户 .env 装配的那个 store"，而 `/api/traces` 读的是新 store
    # —— 两次会话写进 A、断言读 B，全挂。
    original_get_settings = deps_mod.get_settings
    deps_mod.get_settings = lambda: isolated
    # 这几个是 lru_cache 单例：本进程里若已被别人装配过，会拿着**旧 settings**不放
    singletons = (
        deps_mod.get_tool_registry,
        deps_mod.get_memory_manager,
        deps_mod.get_skill_router,
        deps_mod.get_trace_store,
    )
    for cached in singletons:
        cached.cache_clear()

    # 前置条件：钉住失败要在这里就能看出来，而不是变成后面某条断言的神秘失败
    assert deps_mod.get_settings().memory_short_term_enabled is False
    assert deps_mod.get_settings().trace_store == "memory"
    assert deps_mod.get_settings().benchmark_runs_dir == isolated.benchmark_runs_dir

    agent = _build_agent()
    app.dependency_overrides[deps_mod.get_agent_dep] = lambda: agent

    import agent_runtime.api.ws.agent as ws_mod

    original_ws_get_agent = ws_mod.get_agent
    ws_mod.get_agent = _build_agent
    try:
        # with 形式才会真正执行 lifespan（MCP 发现）
        with TestClient(app) as client:
            # ── REST: POST /api/chat ──
            resp = client.post("/api/chat", json={"task": TASK})
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["final_answer"] == "最终答案：已读取", body
            assert body["steps"] >= 1, body
            assert body["total_tokens"]["total"] >= 0, body
            assert body["skills_used"] == ["github_analysis"], body

            # ── REST: GET /api/tools（原生工具目录）──
            tools_resp = client.get("/api/tools")
            assert tools_resp.status_code == 200, tools_resp.text
            catalog = tools_resp.json()
            assert catalog["count"] == len(catalog["tools"])
            names = {t["name"] for t in catalog["tools"]}
            assert {"read_file", "github_get_repo", "web_search", "pdf_read"} <= names, names
            sample = next(t for t in catalog["tools"] if t["name"] == "read_file")
            assert sample["parameters"]["required"] == ["path"]
            assert app.state.mcp_errors == [], "未配置 MCP 时不应产生错误"

            # ── REST: GET /api/skills（内置技能目录）──
            skills_resp = client.get("/api/skills")
            assert skills_resp.status_code == 200, skills_resp.text
            skills_body = skills_resp.json()
            assert skills_body["count"] == len(skills_body["skills"])
            loaded = {s["name"] for s in skills_body["skills"]}
            assert {"github_analysis", "tech_research"} <= loaded, loaded
            github = next(s for s in skills_body["skills"] if s["name"] == "github_analysis")
            assert github["triggers"], github
            assert app.state.skill_errors == [], app.state.skill_errors

            # ── REST: GET /api/memories（本用例把三层都钉成关：未启用层返回 enabled=false，而不是报错）──
            # 这里能硬断言"关"是因为 `_isolated_settings` 把开关钉死了 —— 不是假设跑它的人没配。
            mem_resp = client.get("/api/memories")
            assert mem_resp.status_code == 200, mem_resp.text
            mem_body = mem_resp.json()
            assert mem_body["settings"]["short_term"]["enabled"] is False, mem_body
            assert mem_body["settings"]["long_term"]["enabled"] is False, mem_body
            assert mem_body["enabled"] == {"short_term": False, "long_term": False}, mem_body
            assert mem_body["count"] == 0 and mem_body["memories"] == [], mem_body
            assert app.state.memory_errors == [], app.state.memory_errors
            assert app.state.context_errors == [], app.state.context_errors

            # ── REST: /api/benchmarks（空目录 → count 0；未知 run_id → 404）──
            bench = client.get("/api/benchmarks")
            assert bench.status_code == 200, bench.text
            assert bench.json()["count"] == 0, bench.json()
            assert client.get("/api/benchmarks/nope").status_code == 404

            # ── REST: POST /api/benchmarks/run（mock：后台跑 1 条，轮询取结果）──
            started = client.post(
                "/api/benchmarks/run", json={"provider": "mock", "limit": 1}
            )
            assert started.status_code == 200, started.text
            started_body = started.json()
            assert started_body["started"] is True, started_body
            assert started_body["run_id"], started_body

            import time

            detail = None
            for _ in range(50):  # 最多等 5 秒：1 条 mock 任务是毫秒级的
                resp = client.get(f"/api/benchmarks/{started_body['run_id']}")
                if resp.status_code == 200:
                    detail = resp.json()
                    break
                time.sleep(0.1)
            assert detail is not None, "后台评测没在 5 秒内产出报告"
            assert detail["config"]["provider"] == "mock"
            assert detail["metrics"]["tasks_total"] == 1

            # ── REST: DELETE /api/memories（层没启用：如实回 enabled=false，不是报错）──
            del_resp = client.delete("/api/memories")
            assert del_resp.status_code == 200, del_resp.text
            assert del_resp.json()["cleared"]["short_term"]["enabled"] is False

            # ── WebSocket: /ws/agent/{session_id} ──
            with client.websocket_connect("/ws/agent/test-session") as ws:
                ws.send_json({"type": "task", "task": TASK})
                frames = []
                while True:
                    frame = ws.receive_json()
                    frames.append(frame)
                    if frame["event_type"] == "done":
                        break

            assert frames[-1]["data"]["final_answer"] == "最终答案：已读取", frames[-1]
            assert frames[-1]["data"]["skills_used"] == ["github_analysis"], frames[-1]
            matched = [f for f in frames if f["event_type"] == "skill_matched"]
            assert matched and matched[0]["data"]["skills"] == ["github_analysis"], frames
            assert any(f["event_type"] == "final_answer" for f in frames), frames
            assert any(
                f["event_type"] == "tool_result" and f["data"]["success"] for f in frames
            ), frames

            # ── REST: /api/traces（REST chat 与 WS 两次会话都该落进同一个 store 单例）──
            # 配置已钉成内存 store，所以这里可以硬断言 store 类型与"装配期错误为空"。
            traces_resp = client.get("/api/traces")
            assert traces_resp.status_code == 200, traces_resp.text
            traces = traces_resp.json()
            assert traces["settings"]["store"] == "InMemoryTraceStore", traces["settings"]
            assert traces["settings"]["errors"] == [], traces["settings"]
            assert traces["count"] >= 2, traces
            assert "events" not in traces["traces"][0], "列表只给小结，不拖明细"
            # 不假设"最新那条一定来自本次运行"：只是找出本次这两条 chat 轨迹
            chat_traces = [item for item in traces["traces"] if item["source"] == "chat"]
            assert len(chat_traces) >= 2, traces

            trace_id = chat_traces[0]["trace_id"]
            detail_resp = client.get(f"/api/traces/{trace_id}")
            assert detail_resp.status_code == 200, detail_resp.text
            detail = detail_resp.json()
            assert detail["available"] is True and detail["reason"] == "", detail
            assert detail["trace"]["steps"], detail
            # 每步 token 现在是真数据（一步 = 一轮），不再是从没写过的 0
            assert detail["trace"]["steps"][0]["token_usage"]["total_tokens"] > 0, detail["trace"]["steps"][0]
            assert client.get(f"/api/traces/{trace_id}/events").json()["count"] >= 1

            # 查不到 → 404 且原因可读（不是 500，也不是空 200）
            missing = client.get("/api/traces/nope")
            assert missing.status_code == 404, missing.text
            assert "nope" in missing.json()["detail"], missing.json()

            # ── REST: GET /api/context（**正面**路径，审查 M6）──
            # 上面两轮会话走的是 dependency_overrides / 打补丁换进去的 agent，**不经过**
            # `deps.get_agent`，所以 `_LAST_CONTEXT` 一直是 None，只测到 available:false 那一半。
            # 这里用真的 `deps.get_agent(llm=...)`（配置是本用例钉的那份：记忆全关、摘要关）
            # 装配一个 agent 并跑一轮 —— MockLLM 直接给答案，不调工具、不发网络请求。
            import asyncio

            from agent_runtime.core.llm.types import LLMResponse
            from agent_runtime.infrastructure.llm.mock import MockLLMProvider

            live = deps_mod.get_agent(llm=MockLLMProvider([LLMResponse(content="直接回答")]))
            asyncio.run(live.run("只回答一句话"))

            ctx_ok = client.get("/api/context")
            assert ctx_ok.status_code == 200, ctx_ok.text
            ctx_body = ctx_ok.json()
            assert ctx_body["available"] is True, ctx_body
            assert ctx_body["used_tokens"] > 0, ctx_body
            assert ctx_body["messages"] >= 2, ctx_body
            assert [s["name"] for s in ctx_body["sections"]] == [
                "system",
                "memory",
                "task",
                "messages",
            ], ctx_body

            # 装配错误要为可见性服务，而不是阻断启动
            assert app.state.trace_errors == [], app.state.trace_errors
    finally:
        ws_mod.get_agent = original_ws_get_agent
        deps_mod.get_settings = original_get_settings
        # 单例清掉：别把"测试用的那份配置"装配出来的对象留给同进程的其它用例
        for cached in singletons:
            cached.cache_clear()
        app.dependency_overrides.clear()

    print("PASS test_chat_and_ws_smoke")


if __name__ == "__main__":
    test_chat_and_ws_smoke()
    print("ALL PASS")
