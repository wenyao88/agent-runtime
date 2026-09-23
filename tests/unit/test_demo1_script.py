"""Demo 1 脚本契约：可测的部分（任务构造、依赖缺失时的降级、参数校验）。

沙箱限制：**带管道的子进程被拒**，所以不能用 subprocess 跑脚本；
改为按路径 import 脚本模块后直接调函数 —— 这也要求脚本顶层只能 import 标准库。
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
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
        raise unittest.SkipTest("依赖齐备：该用例只在缺依赖时有意义")
    assert "httpx" in message and "openai" in message
    assert "pip install" in message


def test_main_degrades_readably_without_third_party_deps() -> None:
    module = _load()
    if module._require_deps() is None:
        raise unittest.SkipTest("依赖齐备：该用例只在缺依赖时有意义")
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

    def __init__(self, **overrides: object) -> None:
        self.llm_api_key = "sk-test"
        self.llm_base_url = "http://localhost"
        self.llm_model = "test-model"
        self.llm_temperature = 0.0
        self.llm_max_tokens = 8192
        self.agent_max_steps = 3
        self.agent_skill_top_k = 1
        self.skills_dir = "skills"
        self.github_token = ""
        self.web_search_provider = "duckduckgo"
        self.web_search_api_key = ""
        self.tool_http_timeout_seconds = 1.0
        self.tool_max_chars = 1000
        # 记忆层（默认值与 `Settings` 一致：全关）
        self.memory_short_term_enabled = False
        self.memory_long_term_enabled = False
        self.memory_consolidate_enabled = False
        self.memory_recall_top_k = 3
        self.memory_inject_max_chars = 500
        self.memory_short_term_ttl_seconds = 86400
        self.memory_short_term_max_items = 200
        self.memory_embedding_dim = 1024
        self.redis_url = "redis://localhost:6379/0"
        self.database_url = "postgresql+asyncpg://localhost/agent"
        self.embedding_api_key = ""
        self.embedding_base_url = "http://localhost/v1"
        self.embedding_model = "test-embed"
        self.judge_llm_api_key = ""
        self.judge_llm_base_url = ""
        self.judge_llm_model = "test-judge"
        # 上下文压缩（Phase 5）
        self.agent_context_compaction_threshold = 0.8
        self.agent_compaction_summarize_enabled = False
        for key, value in overrides.items():
            setattr(self, key, value)


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
    if module._require_deps() is None:
        # 依赖齐备时 main() 会真的跑一次任务（真实网络 + LLM），不适合放进单测
        raise unittest.SkipTest("依赖齐备：该用例只在缺依赖时验证 argparse 通路")
    code = module.main(["--repo", "octocat/Hello-World", "--session-id", "smoke-1"])
    assert code == 2, "参数应被 argparse 接受（缺依赖时以退出码 2 可读结束）"


# ── Demo 1 的记忆层装配（本机实测"Redis 里一条都没有"暴露出的漏装配）──
#
# 症状：`--session-id my-test` 跑完，`session:my-test:*` 在 Redis 里不存在，召回也永远为空。
# 根因**不是** session_id 没传（run → run_stream(task, session_id=...) 那条链路是通的），
# 而是 `build_agent()` 自己拼了个 `MemoryManager(working=WorkingMemory())` —— 没有任何持久层，
# 于是 store 只落进进程内 working 层，recall 也自然拿不到东西。API 用的是
# `deps.get_memory_manager()` 那套真装配，两边漂移。与 Phase 3 的 skill_router 漏装配同一类：
# 修法不是"给脚本补一行"，而是**装配只有一处**（`infrastructure/memory/catalog.py`）。


def test_build_agent_wires_real_memory_layers() -> None:
    """核心回归：Demo 1 必须装配**真的**短时记忆层，否则 `--session-id` 写了也读不到。"""
    from agent_runtime.core.llm.types import LLMResponse
    from agent_runtime.infrastructure.llm.mock import MockLLMProvider
    from agent_runtime.infrastructure.memory.short_term import RedisShortTermMemory

    module = _load()
    agent = module.build_agent(
        settings=_FakeSettings(memory_short_term_enabled=True),
        llm=MockLLMProvider([LLMResponse(content="报告")]),
    )
    assert isinstance(agent.memory.short_term, RedisShortTermMemory), (
        "build_agent 漏装短时记忆层：与 API 的装配漂移，session 写了也读不到"
    )
    assert agent.memory.long_term is None, "没开长期记忆就不该建层"


def test_build_agent_uses_the_same_catalog_as_the_api() -> None:
    """装配必须与 API **同源**：同一条 settings 经两处装配应得到同样的层。

    这条测试的价值在于防止"脚本又自己拼一套"回归 —— 之前正是这么漂移的。
    """
    from agent_runtime.core.llm.types import LLMResponse
    from agent_runtime.infrastructure.llm.mock import MockLLMProvider
    from agent_runtime.infrastructure.memory.catalog import build_memory_manager

    module = _load()
    settings = _FakeSettings(memory_short_term_enabled=True)
    agent = module.build_agent(
        settings=settings, llm=MockLLMProvider([LLMResponse(content="报告")])
    )
    expected, _ = build_memory_manager(settings)
    assert type(agent.memory.short_term) is type(expected.short_term)


def test_format_memory_errors_is_empty_when_clean() -> None:
    module = _load()
    assert module.format_memory_errors([]) == ""
    assert module.format_memory_errors(None) == ""


def test_format_memory_errors_marks_each_problem() -> None:
    """装配问题必须**看得见**：本项目已经两次栽在"机制做了但展示层藏起来"。"""
    module = _load()
    text = module.format_memory_errors(
        ["MEMORY_LONG_TERM_ENABLED=true 但缺少 EMBEDDING_API_KEY：已跳过长期记忆层"]
    )
    assert "⚠" in text
    assert "EMBEDDING_API_KEY" in text


class _FakeSummaryProvider:
    """假摘要 provider：用来证明 demo 真的把摘要器接上了（沙箱装不上 openai）。"""

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs

    async def chat(self, messages, tools=None):  # noqa: ANN001
        import types

        return types.SimpleNamespace(content="早期经过摘要")


def test_build_agent_uses_the_shared_context_assembly() -> None:
    """核心回归：demo 的 `ContextManager` 必须来自 `build_context_manager`（与 API 同源）。

    Phase 4 就是这么漂移的：`build_agent` 自己拼了个没有持久层的 `MemoryManager`，
    于是 `--session-id` 传对了、开关也开了，Redis 里却一条都没有。
    这里用「预算=1 必然压缩 + 注入假 provider」把装配路径端到端钉住：
    摘要器在、prompt 走到了 provider、事件里如实报 summarize。
    """
    import asyncio

    from agent_runtime.core.agent.events import AgentEventType
    from agent_runtime.core.llm.types import FunctionCall, LLMResponse
    from agent_runtime.infrastructure.llm.mock import MockLLMProvider

    module = _load()
    built: list[dict] = []

    def factory(**kwargs):
        built.append(kwargs)
        return _FakeSummaryProvider(**kwargs)

    agent = module.build_agent(
        settings=_FakeSettings(
            llm_max_tokens=1,  # 压缩阈值 = 0 → 每个工具结果之后都会压一次
            agent_compaction_summarize_enabled=True,
            judge_llm_api_key="sk-judge",
        ),
        llm=MockLLMProvider(
            [
                LLMResponse(
                    content=None,
                    tool_calls=[
                        FunctionCall(
                            id=f"c{i}", name="read_file", arguments='{"path": "README.md"}'
                        )
                    ],
                )
                for i in range(1, 5)
            ]
            + [LLMResponse(content="报告")]
        ),
        provider_factory=factory,
    )

    async def collect():
        return [ev async for ev in agent.run_stream(module.build_task("fastapi/fastapi"))]

    events = asyncio.run(collect())
    compaction = [e for e in events if e.event_type == AgentEventType.COMPACTION]
    assert compaction, "预算=1 必然触发压缩"
    assert built and built[0]["api_key"] == "sk-judge", "装配没走到共享 catalog"
    # 消息足够多（> keep_recent）时才有的可摘；短历史会如实报"没有可摘要的早期消息"
    assert any(e.data["strategy"] == "summarize" for e in compaction), [
        e.data for e in compaction
    ]


def test_format_context_errors_marks_each_problem() -> None:
    module = _load()
    assert module.format_context_errors([]) == ""
    assert module.format_context_errors(None) == ""
    text = module.format_context_errors(["AGENT_COMPACTION_SUMMARIZE_ENABLED=true 但缺 key"])
    assert "⚠" in text and "上下文" in text and "缺 key" in text


def test_format_compaction_shows_tokens_and_strategy() -> None:
    module = _load()
    line = module.format_compaction(
        {"before": 100, "after": 40, "strategy": "truncate", "summarized": 0,
         "degraded_from": None, "reason": ""}
    )
    assert "100" in line and "40" in line and "truncate" in line
    assert "⚠" not in line and "摘要" not in line


def test_format_compaction_shows_the_summary_count() -> None:
    module = _load()
    line = module.format_compaction(
        {"before": 100, "after": 30, "strategy": "summarize", "summarized": 5,
         "degraded_from": None, "reason": ""}
    )
    assert "summarize" in line
    assert "摘要 5 条" in line, line


def test_format_compaction_surfaces_the_degradation() -> None:
    """事件早带上了降级信息，CLI 却只打印策略 —— 又是一次"机制做了、展示层藏起来"。"""
    module = _load()
    line = module.format_compaction(
        {"before": 100, "after": 20, "strategy": "truncate", "summarized": 0,
         "degraded_from": "summarize", "reason": "未配置摘要器"}
    )
    assert "⚠" in line and "summarize" in line and "未配置摘要器" in line, line


def test_format_compaction_tolerates_a_missing_reason() -> None:
    module = _load()
    line = module.format_compaction(
        {"before": 1, "after": 1, "strategy": "truncate", "summarized": 0,
         "degraded_from": "summarize", "reason": ""}
    )
    assert "⚠" in line and "未说明" in line, line


def _run_all() -> None:
    failed: list[str] = []
    tests = [
        v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)
    ]
    for t in tests:
        print(f"RUN  {t.__name__}")
        try:
            t()
        except unittest.SkipTest as e:
            print(f"SKIP {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            print(f"FAIL {t.__name__}: {type(e).__name__}: {e}")
            failed.append(t.__name__)
        else:
            print(f"PASS {t.__name__}")
    if failed:
        raise SystemExit(f"FAILED: {', '.join(failed)}")
    print("ALL PASS")


if __name__ == "__main__":
    _run_all()
