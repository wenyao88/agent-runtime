"""Redis 短时记忆的协议逻辑契约（假 client 驱动，不需要 redis 包）。

只测"发了哪些命令、怎么解释结果"，不测真实 Redis —— 真实链路只能在用户机器上验证。
沙箱内**真的没有 redis 包**，所以"未安装 → 可读错误"这条分支是真实执行的，不是模拟。

关键语义：
  * 键 = `{prefix}:{session}:memory`；会话缺省 `default`；
  * `store` = RPUSH + LTRIM(保留最近 max_items) + EXPIRE(ttl)；
  * `query` 取最近若干条、**最新在前**、坏数据跳过、给定 `text` 时做子串过滤
    （短时记忆没有索引能力，但"注入不相关记忆"比"少注入"更糟，所以在 Python 侧过滤）；
  * `clear` 只删**该会话**的键。
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from agent_runtime.core.memory.base import MemoryEntry, MemoryQuery
from agent_runtime.core.memory.short_term import ShortTermPolicy, dumps
from agent_runtime.infrastructure.memory.short_term import (
    MemoryUnavailable,
    RedisShortTermMemory,
)


class FakeRedis:
    def __init__(self, *, raises: Exception | None = None, ping_raises: Exception | None = None):
        self.data: dict[str, list[str]] = {}
        self.expires: list[tuple[str, int]] = []
        self.commands: list[tuple] = []
        self.raises = raises
        self.ping_raises = ping_raises
        self.closed = False

    def _check(self) -> None:
        if self.raises:
            raise self.raises

    async def ping(self):
        if self.ping_raises:
            raise self.ping_raises
        return True

    async def rpush(self, key, value):
        self._check()
        self.commands.append(("rpush", key))
        self.data.setdefault(key, []).append(value)
        return len(self.data[key])

    async def ltrim(self, key, start, stop):
        self._check()
        self.commands.append(("ltrim", key, start, stop))
        items = self.data.get(key, [])
        if stop == -1:
            self.data[key] = items[start:] if start < 0 else items[start:]
        return "OK"

    async def expire(self, key, ttl):
        self._check()
        self.commands.append(("expire", key, ttl))
        self.expires.append((key, ttl))
        return True

    async def lrange(self, key, start, stop):
        self._check()
        self.commands.append(("lrange", key, start, stop))
        return list(self.data.get(key, []))

    async def delete(self, key):
        self._check()
        self.commands.append(("delete", key))
        return 1 if self.data.pop(key, None) is not None else 0

    async def aclose(self):
        self.closed = True


def _mem(client=None, **kw) -> RedisShortTermMemory:
    params = {
        "url": "redis://localhost:6379/0",
        "policy": ShortTermPolicy(ttl_seconds=120, max_items=3),
        "client": client,
    }
    params.update(kw)
    return RedisShortTermMemory(**params)


def _run(coro):
    return asyncio.run(coro)


# ── store ──


def test_store_issues_rpush_ltrim_expire() -> None:
    client = FakeRedis()
    mem = _mem(client)
    _run(mem.store(MemoryEntry(content="内容", metadata={"session_id": "s1"})))
    assert [c[0] for c in client.commands] == ["rpush", "ltrim", "expire"]
    assert client.commands[0][1] == "session:s1:memory"
    assert client.commands[1][2:] == (-3, -1), "LTRIM 必须保留最近 max_items 条"
    assert client.expires == [("session:s1:memory", 120)]


def test_store_defaults_to_default_session() -> None:
    client = FakeRedis()
    _run(_mem(client).store(MemoryEntry(content="x")))
    assert client.commands[0][1] == "session:default:memory"


def test_store_round_trips_through_the_shared_codec() -> None:
    client = FakeRedis()
    when = datetime(2026, 9, 21, 10, 0)
    _run(_mem(client).store(MemoryEntry(content="内容", created_at=when)))
    raw = client.data["session:default:memory"][0]
    from agent_runtime.core.memory.short_term import loads

    restored = loads(raw)
    assert restored is not None and restored.content == "内容" and restored.created_at == when


# ── query ──


def test_query_returns_newest_first_limited_by_top_k() -> None:
    client = FakeRedis()
    client.data["session:default:memory"] = [dumps(MemoryEntry(content=f"m{i}")) for i in range(5)]
    out = _run(_mem(client).query(MemoryQuery(text="m", top_k=2)))
    assert [e.content for e in out] == ["m4", "m3"]


def test_query_skips_corrupt_entries() -> None:
    client = FakeRedis()
    client.data["session:default:memory"] = ["{not json", dumps(MemoryEntry(content="好"))]
    out = _run(_mem(client).query(MemoryQuery(text="", top_k=5)))
    assert [e.content for e in out] == ["好"]


def test_query_filters_by_text_substring() -> None:
    client = FakeRedis()
    client.data["session:default:memory"] = [
        dumps(MemoryEntry(content="关于向量数据库")),
        dumps(MemoryEntry(content="关于前端布局")),
    ]
    out = _run(_mem(client).query(MemoryQuery(text="向量", top_k=5)))
    assert [e.content for e in out] == ["关于向量数据库"]


def test_query_without_text_returns_everything_recent() -> None:
    client = FakeRedis()
    client.data["session:default:memory"] = [dumps(MemoryEntry(content="a")), dumps(MemoryEntry(content="b"))]
    out = _run(_mem(client).query(MemoryQuery(text=None, top_k=5)))
    assert [e.content for e in out] == ["b", "a"]


def test_query_uses_session_from_query() -> None:
    client = FakeRedis()
    client.data["session:s9:memory"] = [dumps(MemoryEntry(content="会话九"))]
    out = _run(_mem(client).query(MemoryQuery(text="", top_k=5, session_id="s9")))
    assert [e.content for e in out] == ["会话九"]


def test_query_on_empty_key_returns_empty_list() -> None:
    assert _run(_mem(FakeRedis()).query(MemoryQuery(text="x", top_k=3))) == []


# ── clear ──


def test_clear_only_removes_the_target_session() -> None:
    client = FakeRedis()
    client.data["session:default:memory"] = ["a"]
    client.data["session:other:memory"] = ["b"]
    _run(_mem(client).clear())
    assert "session:default:memory" not in client.data
    assert "session:other:memory" in client.data, "clear() 不得清掉别的会话"


def test_clear_targets_given_session() -> None:
    client = FakeRedis()
    client.data["session:s2:memory"] = ["a"]
    _run(_mem(client).clear(session_id="s2"))
    assert "session:s2:memory" not in client.data


# ── 连接与缺依赖 ──


def test_connect_uses_injected_client() -> None:
    client = FakeRedis()
    _run(_mem(client).connect())
    assert client.closed is False


def test_connect_wraps_ping_failure_as_memory_unavailable() -> None:
    client = FakeRedis(ping_raises=RuntimeError("connection refused"))
    try:
        _run(_mem(client).connect())
    except MemoryUnavailable as e:
        assert "连接失败" in str(e)
    else:
        raise AssertionError("ping 失败必须报 MemoryUnavailable")


def test_missing_redis_package_gives_readable_error() -> None:
    """真实路径（本沙箱确实没装 redis）：惰性导入失败要变成可读错误。"""
    try:
        import redis  # noqa: F401
    except ImportError:
        pass
    else:
        print("     (装了 redis —— 跳过该分支)")
        return
    try:
        _run(_mem(None).query(MemoryQuery(text="x", top_k=1)))
    except MemoryUnavailable as e:
        assert "redis" in str(e)
    else:
        raise AssertionError("缺 redis 必须给可读错误")


def test_aclose_closes_client() -> None:
    client = FakeRedis()
    _run(_mem(client).aclose())
    assert client.closed is True


def _run_all() -> None:
    failed = []
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"RUN  {name}")
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                print(f"FAIL {name}: {type(e).__name__}: {e}")
                failed.append(name)
            else:
                print(f"PASS {name}")
    if failed:
        raise SystemExit(f"FAILED: {', '.join(failed)}")
    print("ALL PASS")


if __name__ == "__main__":
    _run_all()
