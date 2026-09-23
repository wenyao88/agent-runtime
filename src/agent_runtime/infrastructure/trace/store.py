"""Trace 落盘（可选 SQLite；只用标准库 `sqlite3`，零第三方）。

`core/trace/store.py` 的内存实现**重启即丢**；这一档负责"跑完之后跨重启还能看"。

与内存实现**同口径**（同一批契约测试同时钉两边）：`list()` 最新在前且只给 `summary()`、
重复 `save` 同一个 `trace_id` 只覆盖不新增、`limit <= 0` 返回 `[]`、不存在的 id
得到 `None` / `[]`。

只在落盘实现里才有的两条：

1. **坏文件降级，绝不抛**：垃圾字节 / 路径不可用 / 半截 JSON 行 → 原因进 `errors`
   （可见、不静默），实例退化为"可用但读空"。一个烂 `trace.db` 不能让
   `/api/traces` 整体 500 —— 与 `benchmark/store.py` 对坏报告的态度一致。
2. **同步 IO 是刻意的**：`TraceStore` 协议本身是同步的（`Tracer._save` 在 async 里
   直接调用它），一条 session 是 KB 级、本地文件写入是毫秒级，所以直接同步写。
   要更大规模就换 PG / 异步驱动，**不要**在这里给 `sqlite3` 套一层 `asyncio.to_thread`。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from ...core.trace.models import TraceEvent, TraceSession
from ...core.trace.store import DEFAULT_CAPACITY

# 两张表：session 的**可查询列** + 整份 `session.to_dict()`。
# 逐条 events 只随 `session_json` 落盘（同一事实只留一份真相）；`trace_events` 按 schema
# 契约建出来备用，但不写入——要按事件检索时再回填，别现在就写两套真相。
_SCHEMA = """
CREATE TABLE IF NOT EXISTS trace_sessions (
    trace_id TEXT PRIMARY KEY,
    task TEXT,
    source TEXT,
    status TEXT,
    started_at TEXT,
    finished_at TEXT,
    total_tokens INTEGER,
    total_latency_ms INTEGER,
    config_json TEXT,
    final_answer TEXT,
    session_json TEXT
);
CREATE TABLE IF NOT EXISTS trace_events (
    trace_id TEXT,
    seq INTEGER,
    event_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_trace_sessions_started_at
    ON trace_sessions (started_at DESC);
"""

# `INSERT OR REPLACE` 就是 SQLite 的 UPSERT：同一个 trace_id 只留一条。
# 它同时会给该行一个新 rowid，所以 `ORDER BY rowid DESC` = "最近写入在前"，
# 与内存实现"覆盖时也算最近写入"（pop 再塞回）完全一致。
_SAVE_SQL = """
INSERT OR REPLACE INTO trace_sessions
    (trace_id, task, source, status, started_at, finished_at,
     total_tokens, total_latency_ms, config_json, final_answer, session_json)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


class SqliteTraceStore:
    """`TraceStore` 协议的 SQLite 实现（小型本地文件；坏文件降级为"可用但读空"）。"""

    def __init__(self, path: str) -> None:
        self.path = str(path or "")
        self.errors: list[str] = []
        self._conn: sqlite3.Connection | None = None

        if not self.path:
            self.errors.append("SQLite trace 存储需要一个文件路径，当前为空 → 降级为空读空写")
            return

        conn: sqlite3.Connection | None = None
        try:
            parent = Path(self.path).parent
            if str(parent) and not parent.exists():
                parent.mkdir(parents=True, exist_ok=True)  # 首次落盘时顺手建目录
            conn = sqlite3.connect(self.path)
            conn.executescript(_SCHEMA)
            conn.commit()
        except (OSError, sqlite3.Error) as e:
            # 打不开 / 不是 SQLite 文件 / 目录不可写：记原因并退化为可用状态，绝不抛给调用方。
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:  # noqa: BLE001 —— 关不掉也不该把异常甩出去
                    pass
            self.errors.append(
                f"SQLite trace 存储不可用（{self.path}）：{type(e).__name__}: {e}"
                " → 读写降级为空，建议修路径或改 TRACE_STORE=memory"
            )
            return

        self._conn = conn

    # ── 写 ──

    def save(self, session: TraceSession) -> None:
        """覆盖写（UPSERT）。任何失败都只记进 `errors`，不抛。"""
        if session is None or not getattr(session, "trace_id", ""):
            return
        trace_id = str(session.trace_id)
        if self._conn is None:
            self.errors.append(f"trace {trace_id} 未写入：SQLite 存储不可用（见上一条错误）")
            return
        try:
            payload = session.to_dict()
        except Exception as e:  # noqa: BLE001 —— 会话本身不成形，也只是记错误
            self.errors.append(f"trace {trace_id} 未写入：会话无法序列化 {type(e).__name__}: {e}")
            return
        try:
            finished_at = payload.get("finished_at")
            self._conn.execute(
                _SAVE_SQL,
                (
                    trace_id,
                    str(payload.get("task") or ""),
                    str(payload.get("source") or "chat"),
                    str(payload.get("status") or "running"),
                    payload.get("started_at"),
                    finished_at if isinstance(finished_at, str) else None,
                    int((payload.get("total_tokens") or {}).get("total_tokens") or 0),
                    int(payload.get("total_latency_ms") or 0),
                    json.dumps(payload.get("config") or {}, ensure_ascii=False),
                    payload.get("final_answer"),
                    json.dumps(payload, ensure_ascii=False),
                ),
            )
            self._conn.commit()  # 显式提交：否则"新实例重开文件"读不到（连接关闭即回滚）
        except (sqlite3.Error, TypeError, ValueError) as e:
            self.errors.append(f"trace {trace_id} 未写入：{type(e).__name__}: {e}")

    # ── 读 ──

    def get(self, trace_id: str) -> TraceSession | None:
        if self._conn is None:
            return None
        try:
            row = self._conn.execute(
                "SELECT session_json FROM trace_sessions WHERE trace_id = ?",
                (str(trace_id or ""),),
            ).fetchone()
        except sqlite3.Error as e:
            self.errors.append(f"读取 trace {trace_id} 失败：{type(e).__name__}: {e}")
            return None
        return self._decode(row[0]) if row else None

    def list(self, limit: int = DEFAULT_CAPACITY) -> list[dict]:
        """最新写入在前，**只给 `summary()`**（列表接口不拖逐条明细）。"""
        size = max(0, int(limit if limit is not None else DEFAULT_CAPACITY))
        if size == 0 or self._conn is None:
            return []
        try:
            rows = self._conn.execute(
                "SELECT session_json FROM trace_sessions ORDER BY rowid DESC LIMIT ?",
                (size,),
            ).fetchall()
        except sqlite3.Error as e:
            self.errors.append(f"列出 trace 失败：{type(e).__name__}: {e}")
            return []
        summaries = []
        for (payload,) in rows:
            session = self._decode(payload)
            if session is not None:
                summaries.append(session.summary())
        return summaries

    def events(self, trace_id: str) -> list[TraceEvent]:
        session = self.get(trace_id)
        return list(session.events) if session is not None else []

    # ── 内部 ──

    def _decode(self, payload: object) -> TraceSession | None:
        """坏行跳过（记一条原因，返回 None），不让一条烂 JSON 拖垮整个列表。"""
        try:
            data = json.loads(payload) if isinstance(payload, str) else None
            if not isinstance(data, dict):
                raise ValueError("session_json 不是 JSON 对象")
            return TraceSession.from_dict(data)
        except (ValueError, TypeError, KeyError, AttributeError) as e:
            self.errors.append(f"跳过一条坏 trace 记录：{type(e).__name__}: {e}")
            return None
