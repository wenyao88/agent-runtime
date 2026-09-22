"""API 冒烟测试：chat + ws 全链路，用 MockLLM 驱动，不需要真实 API Key。

无 fastapi/httpx 的环境（本沙箱）自动 SKIP 并 exit 0；用户机器上会真实执行。
两模式：pytest 可收集，也可直接 `python tests/integration/test_api_smoke.py`。
"""
from __future__ import annotations

import sys
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
    """
    from agent_runtime.core.agent.react import ReActLoop
    from agent_runtime.core.context.budget import TokenBudget
    from agent_runtime.core.context.manager import ContextManager
    from agent_runtime.core.llm.types import FunctionCall, LLMResponse
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
    llm = MockLLMProvider(
        [
            LLMResponse(
                content="先读文件",
                tool_calls=[
                    FunctionCall(id="call_1", name="read_file", arguments='{"path": "计划.md"}')
                ],
            ),
            LLMResponse(content="最终答案：已读取"),
        ]
    )
    return ReActLoop(
        llm=llm,
        tool_registry=registry,
        context_manager=ContextManager(budget=TokenBudget()),
        memory_manager=MemoryManager(working=WorkingMemory()),
        skill_router=skills,
        tracer=Tracer(),
        max_steps=5,
    )


# 命中 github_analysis 的中文任务（trigger 含「代码结构」）
TASK = "看看 fastapi/fastapi 的代码结构，并读取 计划.md"


def test_chat_and_ws_smoke() -> None:
    if not _deps_available():
        print("SKIP test_chat_and_ws_smoke (fastapi/httpx not installed)")
        return

    from fastapi.testclient import TestClient

    from agent_runtime.api import deps as deps_mod
    from agent_runtime.api.app import app

    agent = _build_agent()
    app.dependency_overrides[deps_mod.get_agent_dep] = lambda: agent

    # 把 MCP 配置指向不存在的文件：lifespan 照常执行，但不会 spawn 任何 server
    settings = deps_mod.get_settings()
    original_mcp_file = settings.mcp_servers_file
    settings.mcp_servers_file = "/definitely/not/here.json"

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

            # ── REST: GET /api/memories（默认全关：未启用层返回 enabled=false，而不是报错）──
            mem_resp = client.get("/api/memories")
            assert mem_resp.status_code == 200, mem_resp.text
            mem_body = mem_resp.json()
            assert mem_body["settings"]["short_term"]["enabled"] is False, mem_body
            assert mem_body["settings"]["long_term"]["enabled"] is False, mem_body
            assert mem_body["count"] == 0 and mem_body["memories"] == [], mem_body
            assert app.state.memory_errors == [], app.state.memory_errors

            # ── REST: DELETE /api/memories ──
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
    finally:
        ws_mod.get_agent = original_ws_get_agent
        settings.mcp_servers_file = original_mcp_file
        app.dependency_overrides.clear()

    print("PASS test_chat_and_ws_smoke")


if __name__ == "__main__":
    test_chat_and_ws_smoke()
    print("ALL PASS")
