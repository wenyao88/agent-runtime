"""上下文装配契约（`infrastructure/context/catalog.py`）。

Phase 4 的教训：装配只要存在两处（API 一处、demo 脚本一处），就一定会漂移 ——
`memory_manager` 漏装配让 `--session-id` 传对了也读不到任何东西。所以上下文装配同样收敛成一处。

摘要器是**可选能力**：默认关、缺 key 只记原因、缺依赖也只记原因，**绝不抛、绝不阻断启动**。
"""
from __future__ import annotations

import asyncio
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from agent_runtime.core.context.compaction import CompactionStrategy  # noqa: E402
from agent_runtime.core.llm.types import Message  # noqa: E402
from agent_runtime.infrastructure.context.catalog import build_context_manager  # noqa: E402


class _FakeSettings:
    def __init__(self, **overrides: object) -> None:
        self.llm_max_tokens = 8192
        self.agent_context_compaction_threshold = 0.8
        self.memory_inject_max_chars = 500
        self.agent_compaction_summarize_enabled = False
        self.judge_llm_api_key = ""
        self.judge_llm_base_url = ""
        self.judge_llm_model = "judge-model"
        self.llm_api_key = ""
        self.llm_base_url = "http://main/v1"
        for key, value in overrides.items():
            setattr(self, key, value)


class _FakeProvider:
    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs

    async def chat(self, messages, tools=None):  # noqa: ANN001
        return types.SimpleNamespace(content="早期经过摘要")


def _factory(built: list[dict]):
    def factory(**kwargs):
        built.append(kwargs)
        return _FakeProvider(**kwargs)

    return factory


async def _force_compaction(cm):
    """塞够消息后强制压一次（预算由 settings 的 `llm_max_tokens` 控制）。"""
    await cm.build(task="任务", system_prompt="system")
    for i in range(8):
        cm.append(Message(role="assistant", content=f"第{i}条"))
    return await cm.compact()


def test_default_settings_build_no_summarizer_and_say_so() -> None:
    cm, errors = build_context_manager(_FakeSettings(llm_max_tokens=1))
    assert errors == [], "默认关不该产生任何错误噪音"
    result = asyncio.run(_force_compaction(cm))
    assert result.strategy is CompactionStrategy.TRUNCATE
    assert result.degraded_from is CompactionStrategy.SUMMARIZE
    assert "未配置" in result.degraded_reason


def test_enabled_without_key_reports_the_switch_and_the_key_name() -> None:
    cm, errors = build_context_manager(
        _FakeSettings(llm_max_tokens=1, agent_compaction_summarize_enabled=True)
    )
    assert len(errors) == 1, errors
    assert "AGENT_COMPACTION_SUMMARIZE_ENABLED" in errors[0], errors[0]
    assert "JUDGE_LLM_API_KEY" in errors[0], errors[0]
    result = asyncio.run(_force_compaction(cm))
    assert result.degraded_from is CompactionStrategy.SUMMARIZE


def test_enabled_with_key_builds_a_working_summarizer() -> None:
    built: list[dict] = []
    cm, errors = build_context_manager(
        _FakeSettings(
            llm_max_tokens=1,
            agent_compaction_summarize_enabled=True,
            judge_llm_api_key="sk-judge",
        ),
        provider_factory=_factory(built),
    )
    assert errors == []
    result = asyncio.run(_force_compaction(cm))
    assert result.strategy is CompactionStrategy.SUMMARIZE
    assert result.summarized_messages >= 1
    assert built[0]["api_key"] == "sk-judge"
    assert built[0]["temperature"] == 0.0


def test_missing_openai_degrades_with_a_readable_reason() -> None:
    try:
        import openai  # noqa: F401
    except ImportError:
        pass
    else:
        raise unittest.SkipTest("openai 已安装：该用例只在缺依赖时有意义")

    cm, errors = build_context_manager(
        _FakeSettings(
            agent_compaction_summarize_enabled=True, judge_llm_api_key="sk-judge"
        )
    )
    assert len(errors) == 1, errors
    assert "上下文摘要器未启用" in errors[0]
    assert "openai" in errors[0], errors[0]


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
