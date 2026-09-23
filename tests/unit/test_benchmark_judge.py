"""LLM 裁判契约（`infrastructure/benchmark/judge.py`）。

裁判是**采样**的深度评判（四个维度各 1~5 分），所以它必须：
  1. 默认零调用（`--judge 0`）：有 key 也只是**构造** provider，不发起请求；
  2. 分数解析**严格**：只认 1~5 的整数，其它一律丢掉；一条合法分都没有 → 返回 `None`
     （调用方据此记"未判分"，**绝不记 0 分**）；
  3. 沙箱可测：provider 可注入（与 Phase 5 摘要器同一手法）。
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from agent_runtime.core.benchmark.models import BenchmarkTask  # noqa: E402
from agent_runtime.infrastructure.benchmark.judge import build_judge  # noqa: E402

_DIMENSIONS = ("completion", "accuracy", "citation", "structure")


class _FakeSettings:
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
    def __init__(self, reply: str = "", **kwargs: object) -> None:
        self.reply = reply
        self.kwargs = kwargs
        self.calls: list[list] = []

    async def chat(self, messages, tools=None):  # noqa: ANN001
        self.calls.append(list(messages))
        return types.SimpleNamespace(content=self.reply)


def _factory(provider: _FakeProvider, built: list[dict] | None = None):
    def factory(**kwargs):
        if built is not None:
            built.append(kwargs)
        provider.kwargs = kwargs
        return provider

    return factory


def _task() -> BenchmarkTask:
    return BenchmarkTask(
        task_id="gh-001",
        task="分析 GitHub 仓库 pallets/flask",
        expected_answer_hints="项目用途 / 技术栈",
    )


# ── 装配 ──


def test_no_key_returns_none_and_never_builds_a_provider() -> None:
    built: list[dict] = []
    judge = build_judge(_FakeSettings(), provider_factory=_factory(_FakeProvider(), built))
    assert judge is None
    assert built == [], "没有 key 就不该构造 provider（默认零额外调用）"


def test_judge_settings_win_and_temperature_is_zero() -> None:
    built: list[dict] = []
    settings = _FakeSettings(
        judge_llm_api_key="sk-judge", judge_llm_base_url="http://judge/v1", llm_api_key="sk-main"
    )
    judge = build_judge(settings, provider_factory=_factory(_FakeProvider(), built))
    assert judge is not None
    assert built[0] == {
        "api_key": "sk-judge",
        "base_url": "http://judge/v1",
        "model": "judge-model",
        "temperature": 0.0,
        "timeout": 20.0,
    }


# ── prompt 与解析 ──


def test_prompt_contains_the_task_the_answer_and_four_dimensions() -> None:
    provider = _FakeProvider(reply='{"completion": 5}')
    judge = build_judge(_FakeSettings(judge_llm_api_key="k"), provider_factory=_factory(provider))
    assert judge is not None
    asyncio.run(judge(_task(), "这是最终答案"))
    prompt = provider.calls[0][0].content
    assert "分析 GitHub 仓库 pallets/flask" in prompt
    assert "这是最终答案" in prompt
    for dimension in _DIMENSIONS:
        assert dimension in prompt, dimension
    assert "1-5" in prompt or "1~5" in prompt


def test_scores_are_parsed_from_a_json_block_inside_prose() -> None:
    reply = (
        "好的，我的评分如下：\n```json\n"
        '{"completion": 5, "accuracy": 4, "citation": 3, "structure": 4}\n```\n以上。'
    )
    judge = build_judge(
        _FakeSettings(judge_llm_api_key="k"), provider_factory=_factory(_FakeProvider(reply=reply))
    )
    scores = asyncio.run(judge(_task(), "答案"))
    assert scores == {"completion": 5, "accuracy": 4, "citation": 3, "structure": 4}


def test_out_of_range_and_non_numeric_scores_are_dropped() -> None:
    reply = '{"completion": 9, "accuracy": "4", "citation": 3, "structure": 0}'
    judge = build_judge(
        _FakeSettings(judge_llm_api_key="k"), provider_factory=_factory(_FakeProvider(reply=reply))
    )
    scores = asyncio.run(judge(_task(), "答案"))
    assert scores == {"citation": 3}, "只认 1~5 的整数，其它丢掉"


def test_all_invalid_scores_return_none() -> None:
    reply = '{"completion": 99, "accuracy": "优秀"}'
    judge = build_judge(
        _FakeSettings(judge_llm_api_key="k"), provider_factory=_factory(_FakeProvider(reply=reply))
    )
    assert asyncio.run(judge(_task(), "答案")) is None


def test_a_valid_score_object_after_another_object_is_recovered() -> None:
    """回归（审查 M1）：`r"\\{.*\\}"` 贪婪匹配会把多段花括号整块当一个对象，直接解析失败。"""
    reply = '先说明一下：{"note": "很长的说明"}{"completion": 5, "accuracy": 4}'
    judge = build_judge(
        _FakeSettings(judge_llm_api_key="k"), provider_factory=_factory(_FakeProvider(reply=reply))
    )
    assert asyncio.run(judge(_task(), "答案")) == {"completion": 5, "accuracy": 4}


def test_a_stray_trailing_brace_does_not_break_parsing() -> None:
    judge = build_judge(
        _FakeSettings(judge_llm_api_key="k"),
        provider_factory=_factory(_FakeProvider(reply='{"completion": 5} 多打了一个 }')),
    )
    assert asyncio.run(judge(_task(), "答案")) == {"completion": 5}


def test_unparseable_reply_returns_none() -> None:
    judge = build_judge(
        _FakeSettings(judge_llm_api_key="k"),
        provider_factory=_factory(_FakeProvider(reply="我觉得挺好的，没有问题。")),
    )
    assert asyncio.run(judge(_task(), "答案")) is None


def test_non_string_content_returns_none() -> None:
    provider = _FakeProvider()
    provider.reply = None  # type: ignore[assignment]
    judge = build_judge(_FakeSettings(judge_llm_api_key="k"), provider_factory=_factory(provider))
    assert asyncio.run(judge(_task(), "答案")) is None


def test_provider_failure_propagates_as_none() -> None:
    """裁判自己抛异常时，judge 返回 None（由 runner 记成"未判分 + 原因"）。"""

    class _Boom:
        def __init__(self, **kwargs: object) -> None:
            pass

        async def chat(self, messages, tools=None):  # noqa: ANN001
            raise TimeoutError("judge 超时")

    judge = build_judge(_FakeSettings(judge_llm_api_key="k"), provider_factory=_Boom)
    assert judge is not None
    assert asyncio.run(judge(_task(), "答案")) is None


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
