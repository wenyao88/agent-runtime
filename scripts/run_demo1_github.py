"""Demo 1：GitHub 仓库分析 Agent（真实链路，需要 LLM key）。

    python scripts/run_demo1_github.py --repo fastapi/fastapi
    python scripts/run_demo1_github.py --repo pallets/flask --focus "错误处理与重试"

依赖 httpx + openai（见 pyproject）。**模块顶层只 import 标准库**：这样脚本在缺依赖的
受限环境里也能被导入/测试；真正缺依赖时给出可读提示并以退出码 2 结束，而不是抛 traceback。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

DEMO_TASK = """分析 GitHub 仓库 {repo}，给出一份结构化报告：
1. 先用 github_get_repo 了解项目定位、主语言与默认分支；
2. 用 github_list_dir 查看根目录结构（path 传空字符串）；
3. 挑 1-2 个关键文件用 github_read_file 阅读（优先 README 或入口文件）；
4. 最后输出四部分：项目用途、技术栈、目录结构要点、2-3 条具体可执行的改进建议。

要求：每一步都基于工具返回的真实内容，不要凭印象编造；引用文件内容时注明来源路径。
如果某个工具失败（例如 404、403、超时、路径不存在），必须如实说明失败原因与因此拿不到的信息，
禁止用你自己的知识补全仓库结构、文件内容或引用。任务无法完成时，直接给出失败点与建议。"""


def build_task(repo: str, focus: str = "") -> str:
    """构造 Demo 1 的任务描述（纯函数，便于测试）。"""
    task = DEMO_TASK.format(repo=repo.strip())
    if focus.strip():
        task += f"\n额外关注点：{focus.strip()}"
    return task


def format_tool_result(result: str, max_lines: int = 8) -> str:
    """按**行**展示工具结果；被截断时明确标注。

    绝不要按字符数硬截断结构化结果：本机实测中 150 字符的硬截断恰好切在 star 数中间，
    真实的 102528 显示成了 1025 —— 残缺内容看起来像完整内容，比不显示更糟。
    """
    text = result or ""
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    return "\n".join(lines[:max_lines]) + f"\n…（共 {len(lines)} 行，已截断）"


def format_warning(warning: str | None) -> str:
    """把告警渲染成醒目的一行。

    CLI 必须让用户看见"本轮不可信"——只设置不显示等于没有。本机实测中就踩过这个漏。
    """
    if not warning:
        return ""
    return f"⚠ 注意：{warning}"


def format_skills(skills_used: object) -> str:
    """命中技能的可视化一行；没命中就返回空串（不打印"命中 0 个"这类噪音）。

    本机实测暴露的漏：技能明明装配上了，脚本却既不打印 `skill_matched` 事件、
    摘要里也没有 `skills_used` —— 等于把这套可见性机制藏起来。
    """
    names = [str(s) for s in (skills_used or [])]
    if not names:
        return ""
    return f"🎯 命中技能：{', '.join(names)}"


def format_compaction(data: dict) -> str:
    """压缩事件的可视化一行：策略 + 前后 token，并如实带上摘要条数与降级原因。

    事件里早就有这些字段了，CLI 却只打印 `strategy` —— 又是一次"机制做了、展示层藏起来"。
    """
    line = f"⚡ 压缩 {data.get('before')} → {data.get('after')}（{data.get('strategy')}）"
    if data.get("noop"):
        line += " · 无可压缩内容（保留窗口本身已超预算）"
    elif data.get("summarized"):
        line += f" · 摘要 {data['summarized']} 条"
    if data.get("summarizer_tokens") or data.get("summarizer_ms"):
        # 额外成本如实打出；为 0 时不打（"没摘要/没报 usage"用一串 0 表达只会是噪声）
        line += f" · 摘要成本 {data.get('summarizer_tokens') or 0} token / {data.get('summarizer_ms') or 0} ms"
    if data.get("degraded_from"):
        line += f" · ⚠ 降级自 {data['degraded_from']}：{data.get('reason') or '未说明'}"
    return line


def _format_problems(label: str, problems: object) -> str:
    items = [str(e) for e in (problems or [])]
    if not items:
        return ""
    return f"⚠ {label}：" + "；".join(items)


def format_memory_errors(errors: object) -> str:
    """记忆层装配问题的可视化一行；没有问题就返回空串。

    与 `format_warning` / `format_skills` 同理：本项目已经两次栽在"机制做了、展示层藏起来"。
    这次也一样 —— "记忆层根本没装上"在输出里完全没痕迹，才拖到本机实测才发现。
    """
    return _format_problems("记忆层问题", errors)


def format_context_errors(errors: object) -> str:
    """上下文（压缩摘要器）装配问题的可视化一行；没有问题就返回空串。"""
    return _format_problems("上下文问题", errors)


def format_summary(result: object) -> str:
    """收尾摘要：步数 / 轮次 / token / 耗时 / trace / **命中技能**，以及**告警**（有就必须出现）。

    轮次与步数是两个数（一轮里模型可以一次发多个 `tool_calls`），口径与 `run_benchmark.py` 一致；
    拿不到轮次就**不打印** —— `None ≠ 0`，不知道不等于"跑了 0 轮"。
    """
    steps = len(getattr(result, "steps", None) or [])
    usage = getattr(result, "total_tokens", None)
    tokens = getattr(usage, "total_tokens", 0) if usage is not None else 0
    latency = getattr(result, "total_latency_ms", 0)
    trace_id = getattr(result, "trace_id", "")
    rounds = int(getattr(result, "rounds", 0) or 0)
    max_steps = int(getattr(result, "max_steps", 0) or 0)
    segments = [f"步数 {steps}"]
    if rounds:
        segments.append(f"轮次 {rounds}/{max_steps}" if max_steps else f"轮次 {rounds}")
    segments += [f"token {tokens}", f"耗时 {latency}ms", f"trace {trace_id}"]
    lines = [" · ".join(segments)]
    skills_line = format_skills(getattr(result, "skills_used", None))
    if skills_line:
        lines.append(skills_line)
    rendered = format_warning(getattr(result, "warning", None))
    if rendered:
        lines.append(rendered)
    return "\n".join(lines)


def _require_deps() -> str | None:
    """返回缺失依赖的说明；依赖齐备时返回 None。"""
    missing = []
    for name in ("httpx", "openai"):
        try:
            __import__(name)
        except ImportError:
            missing.append(name)
    if not missing:
        return None
    return (
        "缺少依赖："
        + ", ".join(missing)
        + "。请先执行 `pip install -e .`（或 `pip install httpx openai`）后重试。"
    )


def build_skill_router(settings: object):
    """加载 `skills/` 目录里的技能（纯标准库，因此沙箱内可测）。

    Demo 1 的 ReActLoop **必须**拿到这个 router：否则 `matched` 恒为空、
    `skills_used` 永远是 []、`skill_matched` 事件永不触发 —— 这正是本机实测暴露的漏装配。
    相对目录按**项目根**解析（不按 CWD），坏文件只打印告警、不阻断演示。
    """
    from agent_runtime.core.skill.router import SkillRouter
    from agent_runtime.infrastructure.skills.catalog import (
        register_skills_from_dir,
        resolve_skills_dir,
    )

    skills_dir = str(getattr(settings, "skills_dir", "skills"))
    router = SkillRouter()
    for error in register_skills_from_dir(router, resolve_skills_dir(skills_dir, str(_ROOT))):
        print(f"⚠ 技能加载问题：{error}")
    return router


def build_agent(
    settings: object | None = None,
    llm: object | None = None,
    provider_factory: object | None = None,
):
    """真实链路：真实 LLM + 全部原生工具（含 4 个 GitHub 工具）+ 真实技能 + 记忆/压缩装配。

    `settings` / `llm` / `provider_factory` 可注入：沙箱里装不上 openai / pydantic_settings，
    只有让它们可注入，"到底装配了哪些东西"才能被测试真正验证（而不是靠读码相信）。
    `provider_factory` 是压缩摘要器的假 provider，用来证明摘要器真的被接上了。
    """
    from agent_runtime.core.agent.react import ReActLoop
    from agent_runtime.core.tool.registry import ToolRegistry
    from agent_runtime.core.trace.tracer import Tracer
    from agent_runtime.infrastructure.context.catalog import build_context_manager
    from agent_runtime.infrastructure.memory.catalog import build_memory_manager
    from agent_runtime.infrastructure.tools.catalog import register_native_tools

    if settings is None:
        from agent_runtime.config.settings import Settings

        settings = Settings()

    registry = ToolRegistry()
    register_native_tools(
        registry,
        root=str(_ROOT),
        github_token=settings.github_token,
        web_search_provider=settings.web_search_provider,
        web_search_api_key=settings.web_search_api_key,
        http_timeout=float(settings.tool_http_timeout_seconds),
        max_chars=settings.tool_max_chars,
    )

    if llm is None:
        from agent_runtime.infrastructure.llm.openai_compatible import (
            OpenAICompatibleProvider,
        )

        if not settings.llm_api_key:
            raise RuntimeError(
                "未配置 LLM_API_KEY：请复制 .env.example 为 .env，并填入硅基流动/DeepSeek 等 OpenAI 兼容端点的 key"
            )
        llm = OpenAICompatibleProvider(
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            temperature=settings.llm_temperature,
        )

    # 记忆层与上下文压缩都与 API **同源**（infrastructure/*/catalog.py）：
    # 之前这里自己拼了没有持久层的空壳 memory manager、也自己 new 了一个 ContextManager ——
    # 于是开关打开了、`--session-id` 也传对了，却什么都读不到（Phase 4 本机实测的漏装配）。
    memory_manager, memory_errors = build_memory_manager(settings)
    context_manager, context_errors = build_context_manager(
        settings, provider_factory=provider_factory
    )
    for rendered in (
        format_memory_errors(memory_errors),
        format_context_errors(context_errors),
    ):
        if rendered:
            print(rendered)

    return ReActLoop(
        llm=llm,
        tool_registry=registry,
        context_manager=context_manager,
        memory_manager=memory_manager,
        skill_router=build_skill_router(settings),
        skill_top_k=int(getattr(settings, "agent_skill_top_k", 1)),
        tracer=Tracer(),
        max_steps=max(8, settings.agent_max_steps),
    )


async def run(repo: str, focus: str, session_id: str = "") -> int:
    agent = build_agent()
    task = build_task(repo, focus)
    print(f"\n任务：{task}\n会话：{session_id or 'default'}\n" + "─" * 72)

    # 会话标识透传给 ReActLoop：短时记忆按会话隔离，不传就全落 default（多会话会互相污染）
    async for event in agent.run_stream(task, session_id=session_id):
        kind = event.event_type.value
        data = event.data
        if kind == "step_start":
            print(f"\n── Step {data['step']} ──")
        elif kind == "thought" and data.get("content"):
            thought = str(data["content"])
            print(f"💭 {thought[:200]}{'…（已截断）' if len(thought) > 200 else ''}")
        elif kind == "tool_call":
            print(f"🔧 {data['tool']}{data['args']}")
        elif kind == "tool_result":
            flag = "成功" if data.get("success") else "失败"
            body = format_tool_result(str(data.get("result", "")))
            print(f"📋 [{flag} {data.get('latency_ms', 0)}ms] {body}")
        elif kind == "compaction":
            print(format_compaction(data))
        elif kind == "skill_matched":
            line = format_skills(data.get("skills"))
            if line:
                print(line)
        elif kind == "final_answer":
            print(f"\n✨ 最终报告：\n{data['content']}")

    result = agent.last_result
    if result is None:
        print("未产出结果")
        return 1
    print("\n" + "─" * 72)
    print(format_summary(result))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Demo 1：GitHub 仓库分析 Agent")
    parser.add_argument("--repo", required=True, help="仓库全名，如 fastapi/fastapi")
    parser.add_argument("--focus", default="", help="额外关注点，如 '错误处理与重试'")
    parser.add_argument(
        "--session-id",
        default="",
        help="会话标识（短时记忆按会话隔离；不传则用 default）",
    )
    args = parser.parse_args(argv)

    if not args.repo.strip():
        print("错误：--repo 不能为空，格式为 owner/name")
        return 2

    missing = _require_deps()
    if missing:
        print(missing)
        return 2

    try:
        return asyncio.run(run(args.repo, args.focus, args.session_id))
    except RuntimeError as e:
        print(f"错误：{e}")
        return 2
    except Exception as e:  # noqa: BLE001 —— 演示脚本的兜底，给出可读输出
        print(f"运行失败：{type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
