"""记忆层装配契约（`infrastructure/memory/catalog.py`）。

本机实测暴露的漏：`scripts/run_demo1_github.py` **自己拼了一套装配**
（`MemoryManager(working=WorkingMemory())`），与 API 的 `deps.get_memory_manager()` 漂移 ——
于是 demo 用 `--session-id my-test` 跑完，Redis 里 `session:my-test:*` 一条都没有、召回永远为空。
与 Phase 3 的 `skill_router` 漏装配是同一类：**装配必须只有一处**。

这个文件锁住 `build_memory_manager` 的语义：
  1. 默认全关（spec D4）—— 不开就绝不建层；
  2. 开了就真的建层（构造期不连 Redis/PG，所以沙箱内可断言）；
  3. 建不了（缺 key / 策略非法）→ 跳过该层 + **可读原因**，绝不抛、绝不阻断启动。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "src"))


class _FakeSettings:
    """鸭子类型的 settings：沙箱里装不上 pydantic_settings，装配逻辑必须可注入才可测。"""

    def __init__(self, **overrides: object) -> None:
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
        self.llm_api_key = ""
        self.llm_base_url = ""
        self.tool_http_timeout_seconds = 1.0
        for key, value in overrides.items():
            setattr(self, key, value)


def test_default_is_all_layers_off() -> None:
    """默认全关：不该因为"顺手"就把 Redis/PG 接上（spec D4）。"""
    from agent_runtime.infrastructure.memory.catalog import build_memory_manager

    manager, errors = build_memory_manager(_FakeSettings())
    assert manager.short_term is None, "默认不该构造短时记忆层"
    assert manager.long_term is None, "默认不该构造长期记忆层"
    assert errors == [], errors


def test_short_term_enabled_builds_the_redis_layer() -> None:
    """开了就必须**真的**建层 —— 这正是 demo 缺的那一层。

    `RedisShortTermMemory.__init__` 只存参数（惰性 connect），所以沙箱里也能断言。
    """
    from agent_runtime.infrastructure.memory.catalog import build_memory_manager
    from agent_runtime.infrastructure.memory.short_term import RedisShortTermMemory

    manager, errors = build_memory_manager(
        _FakeSettings(memory_short_term_enabled=True)
    )
    assert isinstance(manager.short_term, RedisShortTermMemory), (
        "MEMORY_SHORT_TERM_ENABLED=true 却没装配 Redis 层：记忆写了也读不到"
    )
    assert errors == [], errors


def test_long_term_without_embedding_key_is_skipped_with_reason() -> None:
    """开了长期记忆却没有 embedding key：跳过该层，但必须给出可读原因（不能静默）。"""
    from agent_runtime.infrastructure.memory.catalog import build_memory_manager

    manager, errors = build_memory_manager(
        _FakeSettings(memory_long_term_enabled=True)
    )
    assert manager.long_term is None
    assert any("EMBEDDING_API_KEY" in e for e in errors), errors


def test_invalid_short_term_policy_never_raises() -> None:
    """TTL=0 会让 Redis 的键立刻过期（等于没存）。宁可跳过并说明，也不能带着坏策略启动。"""
    from agent_runtime.infrastructure.memory.catalog import build_memory_manager

    manager, errors = build_memory_manager(
        _FakeSettings(memory_short_term_enabled=True, memory_short_term_ttl_seconds=0)
    )
    assert manager.short_term is None, "非法策略下不该建出一个存了等于没存的层"
    assert any("短时记忆" in e for e in errors), errors


def test_consolidate_without_openai_degrades_with_reason() -> None:
    """consolidate 开了但缺 openai：跳过摘要模型 + 可读原因，绝不阻断装配（"可选能力"契约）。"""
    try:
        import openai  # noqa: F401
    except ImportError:
        pass
    else:
        raise unittest.SkipTest("openai 已安装：该用例只在缺依赖时有意义")

    from agent_runtime.infrastructure.memory.catalog import build_memory_manager

    manager, errors = build_memory_manager(
        _FakeSettings(memory_consolidate_enabled=True, judge_llm_api_key="sk-test")
    )
    assert manager.summarizer is None
    assert any("consolidate" in e for e in errors), errors


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
