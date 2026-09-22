"""短时记忆的纯策略契约：TTL / 条数上限 / 序列化。

为什么单独一层纯策略：Redis 只负责"存字符串、取字符串"，而**留多久、留几条、坏数据怎么办**
是记忆语义。把它抽出来，就能在沙箱内（无 redis 包）把语义钉死，infra 层只剩协议动作。

设计约定（spec §4.4）：
  * `loads` 对坏数据（非法 JSON / 非对象 / 缺 content / content 非字符串）一律返回 `None`，
    **绝不抛** —— 一条脏记录不能毁掉整次召回；
  * `trim` 保留**最近** N 条（Redis List 顺序为 oldest→newest，故保留尾部）；
  * `max_items <= 0` → 返回空列表。
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from agent_runtime.core.memory.base import MemoryEntry
from agent_runtime.core.memory.short_term import ShortTermPolicy, dumps, loads, trim


def _entry(content: str = "内容", **kw) -> MemoryEntry:
    return MemoryEntry(content=content, **kw)


# ── 策略默认值 ──


def test_policy_defaults() -> None:
    policy = ShortTermPolicy()
    assert policy.ttl_seconds == 86400
    assert policy.max_items == 200


# ── 序列化往返 ──


def test_round_trip_preserves_fields() -> None:
    when = datetime(2026, 9, 21, 13, 5, 30)
    original = MemoryEntry(
        content="记忆内容", role="user", metadata={"type": "summary", "session_id": "s1"},
        created_at=when, source="short_term",
    )
    restored = loads(dumps(original))
    assert restored is not None
    assert restored.content == "记忆内容"
    assert restored.role == "user"
    assert restored.metadata == {"type": "summary", "session_id": "s1"}
    assert restored.created_at == when


def test_round_trip_without_created_at() -> None:
    restored = loads(dumps(_entry()))
    assert restored is not None
    assert restored.created_at is None


def test_dumps_survives_non_serializable_metadata() -> None:
    """metadata 是自由字典，可能塞进 datetime 之类；序列化必须降级而不是抛。"""
    entry = MemoryEntry(content="x", metadata={"when": datetime(2026, 1, 1)})
    raw = dumps(entry)
    restored = loads(raw)
    assert restored is not None
    assert "2026" in str(restored.metadata["when"])


def test_dumps_keeps_chinese_readable() -> None:
    assert "记忆" in dumps(_entry("记忆")), "不应把中文转义成 \\uXXXX（便于人工排查）"


# ── 坏数据一律 None ──


def test_loads_returns_none_for_invalid_json() -> None:
    assert loads("{not json") is None


def test_loads_returns_none_for_non_object_json() -> None:
    assert loads("[1, 2, 3]") is None
    assert loads('"just a string"') is None


def test_loads_returns_none_when_content_missing() -> None:
    assert loads('{"role": "agent"}') is None


def test_loads_returns_none_when_content_is_not_str() -> None:
    assert loads('{"content": 123}') is None


def test_loads_returns_none_for_empty_input() -> None:
    assert loads("") is None
    assert loads("   ") is None


def test_loads_tolerates_bad_created_at() -> None:
    """时间戳坏了不该让整条记忆作废 —— 内容仍然有价值，时间置空即可。"""
    restored = loads('{"content": "x", "created_at": "not-a-date"}')
    assert restored is not None
    assert restored.content == "x"
    assert restored.created_at is None


# ── trim ──


def test_trim_keeps_the_most_recent_items() -> None:
    items = [dumps(_entry(f"m{i}")) for i in range(5)]
    kept = trim(items, ShortTermPolicy(max_items=2))
    assert len(kept) == 2
    assert [loads(x).content for x in kept] == ["m3", "m4"], "必须保留最近两条"


def test_trim_keeps_everything_when_under_limit() -> None:
    items = [dumps(_entry(f"m{i}")) for i in range(3)]
    assert trim(items, ShortTermPolicy(max_items=10)) == items


def test_trim_zero_or_negative_returns_empty() -> None:
    items = [dumps(_entry("m0"))]
    assert trim(items, ShortTermPolicy(max_items=0)) == []
    assert trim(items, ShortTermPolicy(max_items=-5)) == []


def test_trim_on_empty_list() -> None:
    assert trim([], ShortTermPolicy(max_items=3)) == []


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
