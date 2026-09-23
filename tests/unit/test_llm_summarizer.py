"""摘要器装配契约（`infrastructure/llm/summarizer.py`）。

摘要器 = 一次 JUDGE_LLM 调用 + **调用方给的 prompt**。memory 的任务结束整理与 Phase 5 的
上下文压缩共用这套 plumbing，但 prompt 完全不同 —— 所以这里把两件事分别钉住：

  1. key / base_url / model / temperature 怎么解析（判定优先级与兜底）；
  2. prompt 由调用方决定，模型回复会被 strip，且**两个调用方各用各的 prompt**。

沙箱装不上 openai，所以 provider 必须可注入 —— 与 `RedisShortTermMemory(client=...)`、
`OpenAICompatibleEmbedder(transport_factory=...)` 同一手法。
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from agent_runtime.infrastructure.llm.summarizer import build_summarizer  # noqa: E402


class _FakeSettings:
    """鸭子类型 settings：沙箱里装不上 pydantic_settings。"""

    def __init__(self, **overrides: object) -> None:
        self.judge_llm_api_key = ""
        self.judge_llm_base_url = ""
        self.judge_llm_model = "judge-model"
        self.llm_api_key = ""
        self.llm_base_url = "http://main/v1"
        self.tool_http_timeout_seconds = 20.0
        for key, value in overrides.items():
            setattr(self, key, value)


class _FakeProvider:
    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.calls: list[list] = []

    async def chat(self, messages, tools=None):  # noqa: ANN001
        self.calls.append(list(messages))
        return types.SimpleNamespace(content="  摘要正文  ")


def _factory(built: list[dict]):
    def factory(**kwargs):
        built.append(kwargs)
        return _FakeProvider(**kwargs)

    return factory


# ── key 解析 ──


def test_no_key_returns_none_and_never_builds_a_provider() -> None:
    built: list[dict] = []
    assert build_summarizer(_FakeSettings(), "P: {text}", provider_factory=_factory(built)) is None
    assert built == [], "没有 key 就不该构造 provider（默认关时不得新增任何外部依赖）"


def test_judge_settings_win_and_temperature_is_zero() -> None:
    """摘要要的是**稳定复现**，不是创意：temperature 必须钉在 0。

    超时也必须跟随工具配置（默认 60s 会让任务中途卡到 60 秒）。
    """
    built: list[dict] = []
    settings = _FakeSettings(
        judge_llm_api_key="sk-judge",
        judge_llm_base_url="http://judge/v1",
        llm_api_key="sk-main",
    )
    fn = build_summarizer(settings, "P: {text}", provider_factory=_factory(built))
    assert callable(fn)
    assert built[0] == {
        "api_key": "sk-judge",
        "base_url": "http://judge/v1",
        "model": "judge-model",
        "temperature": 0.0,
        "timeout": 20.0,
    }


def test_llm_key_and_base_url_are_the_fallback() -> None:
    built: list[dict] = []
    settings = _FakeSettings(llm_api_key="sk-main")
    assert callable(build_summarizer(settings, "P: {text}", provider_factory=_factory(built)))
    assert built[0]["api_key"] == "sk-main"
    assert built[0]["base_url"] == "http://main/v1", "judge 没配 base_url 时应退回主 provider 的"


# ── prompt 与调用 ──


def test_summarizer_formats_the_prompt_and_strips_the_reply() -> None:
    provider = _FakeProvider()
    fn = build_summarizer(
        _FakeSettings(judge_llm_api_key="k"),
        "P: {text}",
        provider_factory=lambda **kw: provider,
    )
    assert fn is not None
    text = asyncio.run(fn("素材"))
    assert text == "摘要正文", "模型回复必须 strip（否则会带着空白混进上下文）"
    assert len(provider.calls) == 1
    assert provider.calls[0][0].role == "user"
    assert provider.calls[0][0].content == "P: 素材"


def test_prompt_is_decided_by_the_caller() -> None:
    provider = _FakeProvider()
    make = lambda prompt: build_summarizer(  # noqa: E731
        _FakeSettings(judge_llm_api_key="k"), prompt, provider_factory=lambda **kw: provider
    )
    asyncio.run(make("A:{text}")("x"))
    asyncio.run(make("B:{text}")("x"))
    assert provider.calls[0][0].content == "A:x"
    assert provider.calls[1][0].content == "B:x"


def test_memory_keeps_its_own_prompt_after_sharing_the_plumbing() -> None:
    """memory 的公开构造器必须继续走它自己的 prompt（记忆整理 ≠ 上下文压缩）。

    这条是 Phase 5 抽取共享 plumbing 的回归锁：抽错了会让记忆整理悄悄改用压缩的 prompt。
    """
    from agent_runtime.infrastructure.memory.catalog import build_summarizer as memory_builder

    provider = _FakeProvider()
    fn = memory_builder(
        _FakeSettings(judge_llm_api_key="k"), provider_factory=lambda **kw: provider
    )
    assert fn is not None
    asyncio.run(fn("任务记录"))
    prompt = provider.calls[0][0].content
    assert "长期记忆" in prompt, f"memory 的 prompt 丢了：{prompt[:60]}"
    assert "任务记录" in prompt


def _run_all() -> None:
    failed: list[str] = []
    tests = [
        v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)
    ]
    for t in tests:
        print(f"RUN  {t.__name__}")
        try:
            t()
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
