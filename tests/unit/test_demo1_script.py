"""Demo 1 脚本契约：可测的部分（任务构造、依赖缺失时的降级、参数校验）。

沙箱限制：**带管道的子进程被拒**，所以不能用 subprocess 跑脚本；
改为按路径 import 脚本模块后直接调函数 —— 这也要求脚本顶层只能 import 标准库。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "src"))

_SCRIPT = _ROOT / "scripts" / "run_demo1_github.py"


def _load():
    spec = importlib.util.spec_from_file_location("run_demo1_github", _SCRIPT)
    assert spec and spec.loader, "无法加载 demo 脚本"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_build_task_names_the_repo_and_required_tools() -> None:
    task = _load().build_task("fastapi/fastapi")
    assert "fastapi/fastapi" in task
    for tool in ("github_get_repo", "github_list_dir", "github_read_file"):
        assert tool in task, f"任务描述应明确要求使用 {tool}"
    assert "改进建议" in task
    assert "不要凭印象编造" in task, "必须要求基于真实工具返回，抑制幻觉"


def test_build_task_includes_optional_focus() -> None:
    module = _load()
    assert "错误处理" in module.build_task("a/b", focus="错误处理")
    assert "额外关注点" not in module.build_task("a/b")


def test_missing_deps_message_names_them() -> None:
    module = _load()
    message = module._require_deps()
    if message is None:
        print("     (依赖齐备 —— 跳过该分支)")
        return
    assert "httpx" in message and "openai" in message
    assert "pip install" in message


def test_main_degrades_readably_without_third_party_deps() -> None:
    module = _load()
    if module._require_deps() is None:
        print("     (依赖齐备 —— 跳过该分支)")
        return
    code = module.main(["--repo", "octocat/Hello-World"])
    assert code == 2, "缺依赖时应以可读提示 + 退出码 2 结束，而不是抛异常"


def test_main_rejects_blank_repo_before_touching_deps() -> None:
    module = _load()
    assert module.main(["--repo", "   "]) == 2


def test_format_warning_is_empty_when_no_warning() -> None:
    module = _load()
    assert module.format_warning(None) == ""
    assert module.format_warning("") == ""


def test_summary_surfaces_the_warning() -> None:
    """CLI 必须把"本轮不可信"显示出来 —— 本机实测中告警被设置了却没被打印。"""
    import types

    module = _load()

    class FakeResult:
        steps = [1, 2]
        total_tokens = types.SimpleNamespace(total_tokens=123)
        total_latency_ms = 45
        trace_id = "abc123"
        warning = "所有工具调用都失败了（1/1）：最终答案没有建立在真实工具结果之上，请勿直接采信"

    text = module.format_summary(FakeResult())
    assert "trace abc123" in text
    assert "所有工具调用都失败了" in text, "告警必须出现在收尾摘要里"
    assert "⚠" in text


def test_summary_has_no_warning_marker_on_success() -> None:
    import types

    module = _load()

    class FakeResult:
        steps = [1]
        total_tokens = types.SimpleNamespace(total_tokens=10)
        total_latency_ms = 5
        trace_id = "ok"
        warning = None

    text = module.format_summary(FakeResult())
    assert "⚠" not in text


def test_tool_result_is_truncated_by_lines_and_marked() -> None:
    """工具结果必须**按行**截断并明确标注。

    本机实测踩过这个坑：按 150 字符硬截断恰好切在 star 数中间，
    真实的 102528 显示成了 1025 —— 残缺内容被当成了完整内容。
    """
    module = _load()
    many = "\n".join(f"line{i}" for i in range(10))
    shown = module.format_tool_result(many, max_lines=3)
    assert shown.startswith("line0\nline1\nline2")
    assert "line3" not in shown
    assert "已截断" in shown, "被截断必须明确标注"
    assert "10" in shown, "应告知总行数"


def test_tool_result_keeps_whole_numbers() -> None:
    """核心回归：数字绝不能被切断。"""
    module = _load()
    text = "repo: fastapi/fastapi\nlanguage: Python\nstars: 102528\ndefault_branch: master"
    shown = module.format_tool_result(text, max_lines=6)
    assert shown == text
    assert "102528" in shown and "1025\n" not in shown


def test_tool_result_short_text_is_untouched() -> None:
    module = _load()
    assert module.format_tool_result("", max_lines=3) == ""
    assert module.format_tool_result("only line", max_lines=3) == "only line"


# ── Demo 1 的 Skill 装配（本机实测"看不到 skill_matched"暴露出的漏装配）──
#
# 症状：跑 Demo 1 时没有任何技能痕迹。根因不是匹配失败，而是
# `build_agent()` 建 ReActLoop 时**根本没传 skill_router** —— 于是
# react.py 里 `matched = self.skills.match(...) if self.skills else []` 恒为空，
# `skills_used` 永远是 []，`skill_matched` 永远不会触发。
# 第二层原因：脚本既没打印 skill_matched 事件，收尾摘要也不显示 skills_used ——
# 与之前"告警设了却没打印"是同一类"机制做了但展示层藏起来"的漏。


class _FakeSettings:
    """鸭子类型的 settings：沙箱里装不上 pydantic_settings，装配逻辑必须可注入才能被测试。"""

    llm_api_key = "sk-test"
    llm_base_url = "http://localhost"
    llm_model = "test-model"
    llm_temperature = 0.0
    llm_max_tokens = 8192
    agent_max_steps = 3
    agent_skill_top_k = 1
    skills_dir = "skills"
    github_token = ""
    web_search_provider = "duckduckgo"
    web_search_api_key = ""
    tool_http_timeout_seconds = 1.0
    tool_max_chars = 1000


def test_demo_task_matches_a_builtin_skill() -> None:
    """Demo 1 的任务文本必须真的命中内置技能，否则技能系统对这个主打场景等于不存在。"""
    from agent_runtime.core.skill.router import SkillRouter
    from agent_runtime.infrastructure.skills.catalog import register_skills_from_dir

    module = _load()
    router = SkillRouter()
    errors = register_skills_from_dir(router, str(_ROOT / "skills"))
    assert errors == [], errors

    matched = router.match(module.build_task("fastapi/fastapi"), top_k=1)
    assert [s.manifest.name for s in matched] == ["github_analysis"]


def test_build_skill_router_loads_builtin_skills() -> None:
    module = _load()
    router = module.build_skill_router(_FakeSettings())
    assert {m.name for m in router.list_all()} >= {"github_analysis", "tech_research"}


def test_build_agent_wires_the_skill_router() -> None:
    """核心回归：Demo 1 的 agent 跑真实任务后，skills_used **不能是空的**。

    llm 注入 MockLLMProvider，所以这条端到端断言在沙箱内就能跑（不需要网络与 openai）。
    """
    import asyncio

    from agent_runtime.core.llm.types import LLMResponse
    from agent_runtime.infrastructure.llm.mock import MockLLMProvider

    module = _load()
    agent = module.build_agent(
        settings=_FakeSettings(),
        llm=MockLLMProvider([LLMResponse(content="报告：已分析")]),
    )

    assert agent.skills is not None, "ReActLoop 必须拿到 skill_router，否则技能永远不生效"
    assert {m.name for m in agent.skills.list_all()} >= {"github_analysis"}

    result = asyncio.run(agent.run(module.build_task("fastapi/fastapi")))
    assert result.skills_used == ["github_analysis"], result.skills_used


def test_build_agent_reports_the_skill_matched_event() -> None:
    """`skill_matched` 事件必须真的发出来 —— 这正是本机实测中"看不到"的东西。"""
    import asyncio

    from agent_runtime.core.agent.events import AgentEventType
    from agent_runtime.core.llm.types import LLMResponse
    from agent_runtime.infrastructure.llm.mock import MockLLMProvider

    module = _load()
    agent = module.build_agent(
        settings=_FakeSettings(),
        llm=MockLLMProvider([LLMResponse(content="报告")]),
    )

    async def collect():
        return [
            ev async for ev in agent.run_stream(module.build_task("fastapi/fastapi"))
        ]

    events = asyncio.run(collect())
    matched = [e for e in events if e.event_type == AgentEventType.SKILL_MATCHED]
    assert matched, "Demo 1 必须发出 skill_matched 事件"
    assert matched[0].data["skills"] == ["github_analysis"]


def test_format_skills_is_empty_when_nothing_matched() -> None:
    module = _load()
    assert module.format_skills(None) == ""
    assert module.format_skills([]) == ""


def test_summary_shows_matched_skills() -> None:
    import types

    module = _load()

    class FakeResult:
        steps = [1]
        total_tokens = types.SimpleNamespace(total_tokens=10)
        total_latency_ms = 5
        trace_id = "t1"
        warning = None
        skills_used = ["github_analysis"]

    text = module.format_summary(FakeResult())
    assert "github_analysis" in text, "用了哪个技能必须在收尾摘要里可见"


def test_summary_omits_skills_line_when_none_matched() -> None:
    import types

    module = _load()

    class FakeResult:
        steps = [1]
        total_tokens = types.SimpleNamespace(total_tokens=10)
        total_latency_ms = 5
        trace_id = "t2"
        warning = None
        skills_used = []

    assert "命中技能" not in module.format_summary(FakeResult())


def test_run_accepts_session_id() -> None:
    """`--session-id` 必须真的能传到 ReActLoop：短时记忆按会话隔离，不传就全落 default。"""
    import inspect

    module = _load()
    assert "session_id" in inspect.signature(module.run).parameters


def test_main_accepts_session_id_flag() -> None:
    module = _load()
    code = module.main(["--repo", "octocat/Hello-World", "--session-id", "smoke-1"])
    assert code == 2, "参数应被 argparse 接受（缺依赖时以退出码 2 可读结束）"


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
