"""PG 长时记忆的契约（假 session/embedder 驱动，不需要 SQLAlchemy 与数据库）。

只测"发了什么 SQL、怎么解释结果、什么时候不该调 embedding 接口"，真实 PG 链路只能在用户机器上验证。

关键取舍：
  * `query.embedding` 已给时**绝不重复调 embedding 接口** —— 那是按 token 计费的，重复调用就是直接浪费钱；
  * 维度不匹配（换了 embedding 模型但没迁移）必须给**点名维度**的可读错误，而不是让 PG 抛一个看不懂的类型错误。
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from agent_runtime.core.memory.base import MemoryEntry, MemoryQuery
from agent_runtime.core.memory.embedding import BaseEmbedder
from agent_runtime.infrastructure.memory.long_term import (
    DatabaseUnavailable,
    EmbeddingDimMismatch,
    PostgresLongTermMemory,
)

DIM = 1024


class FakeEmbedder(BaseEmbedder):
    def __init__(self, *, vector=None, raises: Exception | None = None):
        self.calls: list[list[str]] = []
        self.vector = vector or [0.5] * DIM
        self.raises = raises

    async def embed(self, texts):
        self.calls.append(list(texts))
        if self.raises:
            raise self.raises
        return [list(self.vector) for _ in texts]


class FakeRow(dict):
    """行对象：既支持 `row["content"]` 也支持属性访问。"""

    def __getattr__(self, item):
        try:
            return self[item]
        except KeyError as e:  # pragma: no cover
            raise AttributeError(item) from e


class FakeResult:
    def __init__(self, rows=None, scalar=None):
        self._rows = list(rows or [])
        self._scalar = scalar

    def fetchall(self):
        return list(self._rows)

    def scalar(self):
        return self._scalar


class FakeSession:
    def __init__(self, rows=None, scalar=None, raises: Exception | None = None):
        self.rows = rows
        self.scalar_value = scalar
        self.raises = raises
        self.executed: list[tuple[str, dict]] = []
        self.commits = 0

    async def execute(self, stmt, params=None):
        self.executed.append((str(stmt), dict(params or {})))
        if self.raises:
            raise self.raises
        return FakeResult(self.rows, self.scalar_value)

    async def commit(self):
        self.commits += 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSessionFactory:
    def __init__(self, session: FakeSession):
        self.session = session
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.session


def _mem(session=None, embedder=None, **kw) -> PostgresLongTermMemory:
    session = session or FakeSession()
    params = {
        "session_factory": FakeSessionFactory(session),
        "embedder": embedder if embedder is not None else FakeEmbedder(),
        "dimension": DIM,
    }
    params.update(kw)
    return PostgresLongTermMemory(**params)


def _run(coro):
    return asyncio.run(coro)


def _row(content="内容", role="agent", created=None, meta=None):
    return FakeRow(
        content=content,
        role=role,
        created_at=created or datetime(2026, 9, 21, 8, 0),
        meta=meta if meta is not None else {"type": "summary"},
    )


# ── query ──


def test_query_uses_given_embedding_without_calling_embedder() -> None:
    session = FakeSession(rows=[_row()])
    embedder = FakeEmbedder()
    mem = _mem(session, embedder)
    _run(mem.query(MemoryQuery(embedding=[0.1] * DIM, top_k=3)))
    assert embedder.calls == [], "已给向量就不该再调 embedding 接口（按量计费）"
    assert session.executed[0][1]["k"] == 3


def test_query_embeds_text_when_embedding_missing() -> None:
    session = FakeSession(rows=[_row()])
    embedder = FakeEmbedder()
    mem = _mem(session, embedder)
    _run(mem.query(MemoryQuery(text="向量数据库", top_k=2)))
    assert embedder.calls == [["向量数据库"]]


def test_query_uses_cosine_distance_and_limit() -> None:
    session = FakeSession(rows=[])
    mem = _mem(session)
    _run(mem.query(MemoryQuery(text="x", top_k=4)))
    sql, params = session.executed[0]
    assert "<=>" in sql, "必须用余弦距离排序（与 ivfflat vector_cosine_ops 索引一致）"
    assert "ORDER BY" in sql.upper()
    assert "LIMIT" in sql.upper()
    assert params["k"] == 4
    assert isinstance(params["vec"], str), "必须绑 pgvector 文本字面量，而不是 Python list（asyncpg 需要编解码器）"
    assert params["vec"].startswith("[") and params["vec"].endswith("]")
    assert params["vec"].count(",") == DIM - 1


def test_store_binds_vector_as_text_literal() -> None:
    """最高未验证风险的回归：绑 list 需要 asyncpg 的 vector 编解码器（本仓库未注册），绑文本字面量则不需要。"""
    session = FakeSession(scalar="id-1")
    _run(_mem(session).store(MemoryEntry(content="x")))
    bound = session.executed[0][1]["vec"]
    assert isinstance(bound, str) and bound.startswith("[") and bound.endswith("]")


def test_query_maps_rows_to_entries() -> None:
    session = FakeSession(rows=[_row(content="历史结论", role="user")])
    out = _run(_mem(session).query(MemoryQuery(text="x", top_k=1)))
    assert len(out) == 1
    assert out[0].content == "历史结论"
    assert out[0].role == "user"
    assert out[0].created_at == datetime(2026, 9, 21, 8, 0)
    assert out[0].metadata == {"type": "summary"}


def test_query_skips_rows_without_content() -> None:
    session = FakeSession(rows=[_row(content=None), _row(content="有效")])
    out = _run(_mem(session).query(MemoryQuery(text="x", top_k=5)))
    assert [e.content for e in out] == ["有效"]


def test_query_without_text_or_embedding_returns_recent_by_time() -> None:
    """本机实测暴露：不带 `query` 查长期记忆曾永远返回空。

    期望：没有查询意图时按 `created_at` **倒序**取最近 N 条（而不是向量检索、更不是返回空）。
    """
    session = FakeSession(rows=[_row(content="最近一条")])
    embedder = FakeEmbedder()
    out = _run(_mem(session, embedder).query(MemoryQuery(text=None, top_k=3)))
    assert [e.content for e in out] == ["最近一条"]
    sql, params = session.executed[0]
    assert "created_at DESC" in sql, sql
    assert "<=>" not in sql, "没有查询意图时不该做向量检索"
    assert params == {"k": 3}
    assert embedder.calls == [], "没有文本就不该调 embedding 接口"


def test_query_recent_path_keeps_rows_without_embedding() -> None:
    """按时间取最近时不该要求 embedding 非空（行没算向量也仍然是一条记忆）。"""
    session = FakeSession(rows=[_row(content="x")])
    _run(_mem(session).query(MemoryQuery(text=None, top_k=1)))
    assert "embedding IS NOT NULL" not in session.executed[0][0]


# ── store ──


def test_store_embeds_content_when_entry_has_no_vector() -> None:
    session = FakeSession(scalar="abc-123")
    embedder = FakeEmbedder()
    new_id = _run(_mem(session, embedder).store(MemoryEntry(content="要记住的内容")))
    assert embedder.calls == [["要记住的内容"]]
    sql, params = session.executed[0]
    assert "INSERT INTO memory_entries" in sql
    assert "RETURNING" in sql.upper()
    assert params["content"] == "要记住的内容"
    assert isinstance(params["vec"], str) and params["vec"].count(",") == DIM - 1
    assert new_id == "abc-123"


def test_store_reuses_entry_embedding() -> None:
    session = FakeSession(scalar="id-1")
    embedder = FakeEmbedder()
    entry = MemoryEntry(content="x", embedding=[0.2] * DIM)
    _run(_mem(session, embedder).store(entry))
    assert embedder.calls == [], "条目自带向量时不该再调接口"


def test_store_records_session_id() -> None:
    session = FakeSession(scalar="id-1")
    _run(_mem(session).store(MemoryEntry(content="x", metadata={"session_id": "s7"})))
    assert session.executed[0][1]["session_id"] == "s7"


def test_store_defaults_session_id() -> None:
    session = FakeSession(scalar="id-1")
    _run(_mem(session).store(MemoryEntry(content="x")))
    assert session.executed[0][1]["session_id"] == "default"


# ── 维度校验 ──


def test_dimension_mismatch_on_store_names_both_dimensions() -> None:
    session = FakeSession()
    entry = MemoryEntry(content="x", embedding=[0.1, 0.2, 0.3])
    try:
        _run(_mem(session).store(entry))
    except EmbeddingDimMismatch as e:
        assert "3" in str(e) and str(DIM) in str(e)
    else:
        raise AssertionError("维度不匹配必须报错")


def test_dimension_mismatch_on_query_names_both_dimensions() -> None:
    session = FakeSession()
    try:
        _run(_mem(session).query(MemoryQuery(embedding=[0.1, 0.2], top_k=1)))
    except EmbeddingDimMismatch as e:
        assert "2" in str(e) and str(DIM) in str(e)
    else:
        raise AssertionError("维度不匹配必须报错")
    assert session.executed == [], "维度不匹配时不该发 SQL"


# ── clear / 连接 / 缺依赖 ──


def test_clear_deletes_all_rows() -> None:
    session = FakeSession()
    _run(_mem(session).clear())
    assert "DELETE FROM memory_entries" in session.executed[0][0]


def test_embedder_failure_propagates_for_the_manager_to_record() -> None:
    embedder = FakeEmbedder(raises=RuntimeError("embedding down"))
    try:
        _run(_mem(FakeSession(), embedder).query(MemoryQuery(text="x", top_k=1)))
    except RuntimeError as e:
        assert "embedding down" in str(e)
    else:
        raise AssertionError("embedder 失败应原样抛出，由 MemoryManager 逐层记账")


def test_missing_sqlalchemy_gives_readable_error() -> None:
    """真实路径（本沙箱确实没装 SQLAlchemy）：惰性导入失败要变成可读错误。

    装了 SQLAlchemy 的环境必须**跳过**而不是 `return`（后者报绿却零断言）。
    """
    try:
        import sqlalchemy  # noqa: F401
    except ImportError:
        pass
    else:
        raise unittest.SkipTest("sqlalchemy 已安装：该用例只在缺依赖时有意义")
    mem = PostgresLongTermMemory(dsn="postgresql+asyncpg://x/y", embedder=FakeEmbedder(), dimension=DIM)
    try:
        _run(mem.connect())
    except DatabaseUnavailable as e:
        assert "sqlalchemy" in str(e).lower()
    else:
        raise AssertionError("缺 SQLAlchemy 必须给可读错误")


def _run_all() -> None:
    failed = []
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"RUN  {name}")
            try:
                fn()
            except unittest.SkipTest as e:
                print(f"SKIP {name}: {e}")
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
