"""Redis 短时记忆（协议逻辑；惰性 redis + 可注入 client）。

本层只做三件事：把条目写进会话 List（带上限与 TTL）、按会话读回最近的若干条、删除单会话。
**语义**（TTL、上限、坏数据、序列化）都在 `core/memory/short_term.py` 里，本文件不重复判断。

失败策略：`connect()` 把连接问题包成 `MemoryUnavailable`（含"未安装 redis"这种可读原因）；
单次命令失败则**原样抛出**，由调用方 `MemoryManager` 逐层捕获并记账 —— 它已经有那条 try。

[已知天花板] `clear()` 只删**指定会话**的键；全量清空需要 `SCAN` 遍历前缀，本阶段不做（spec §11）。
[已知天花板] 按 `text` 过滤是在 Python 侧做的子串匹配（Redis 无索引能力）：
宁可多花一点 CPU，也不要把不相关的记忆注入 System Prompt。
"""
from __future__ import annotations

from typing import Any

from ...core.memory.base import BaseMemory, MemoryEntry, MemoryQuery, normalize_session_id
from ...core.memory.short_term import ShortTermPolicy, dumps, loads, trim


class MemoryUnavailable(Exception):
    """记忆层不可用（缺依赖 / 连不上）。由装配层记录，不阻断启动与任务。"""


class RedisShortTermMemory(BaseMemory):
    def __init__(
        self,
        *,
        url: str = "",
        policy: ShortTermPolicy | None = None,
        client: Any = None,
        key_prefix: str = "session",
    ) -> None:
        self._url = url or "redis://localhost:6379/0"
        self._policy = policy or ShortTermPolicy()
        self._client = client
        self._key_prefix = key_prefix

    # ── 连接 ──

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from redis.asyncio import from_url
        except ImportError:  # 惰性导入：未安装时给可读原因
            raise MemoryUnavailable(
                "redis 未安装：请先 `pip install redis`，或给 RedisShortTermMemory 注入 client"
            ) from None
        self._client = from_url(self._url, decode_responses=True)
        return self._client

    async def connect(self) -> None:
        try:
            client = self._get_client()
            await client.ping()
        except MemoryUnavailable:
            raise
        except Exception as e:  # noqa: BLE001 —— 归一为"不可用"，由装配层记录
            raise MemoryUnavailable(f"Redis 连接失败：{type(e).__name__}: {e}") from None

    async def aclose(self) -> None:
        client = self._client
        if client is not None and hasattr(client, "aclose"):
            await client.aclose()

    # ── 键 ──

    def _key(self, session_id: str | None) -> str:
        return f"{self._key_prefix}:{normalize_session_id(session_id)}:memory"

    # ── 读写 ──

    async def store(self, entry: MemoryEntry) -> str:
        client = self._get_client()
        key = self._key(entry.metadata.get("session_id"))
        await client.rpush(key, dumps(entry))
        # 只留最近 max_items 条（List 尾部最新），并续上 TTL —— TTL 不续会"写进去也会自己过期"
        await client.ltrim(key, -self._policy.max_items, -1)
        await client.expire(key, self._policy.ttl_seconds)
        return str(entry.id or "")

    async def query(self, query: MemoryQuery) -> list[MemoryEntry]:
        client = self._get_client()
        raw = await client.lrange(self._key(query.session_id), 0, -1)
        raw = trim(list(raw or []), self._policy)
        needle = (query.text or "").lower()
        found: list[MemoryEntry] = []
        for item in reversed(raw):  # 尾部最新 → 倒序即"最新在前"
            entry = loads(item)
            if entry is None:  # 坏数据跳过，不毁掉整次召回
                continue
            if needle and needle not in (entry.content or "").lower():
                continue
            found.append(entry)
            if len(found) >= query.top_k:
                break
        return found

    async def clear(self, session_id: str | None = None) -> None:
        client = self._get_client()
        await client.delete(self._key(session_id))
