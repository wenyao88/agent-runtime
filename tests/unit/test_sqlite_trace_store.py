"""SQLite trace 存储 + 装配（`infrastructure/trace`，Phase 8 T4）。

`core/trace/store.py` 的 `InMemoryTraceStore` 重启即丢；这里钉住"可选落盘"这一档：

  1. `SqliteTraceStore` 与内存实现**同口径**：`list()` 最新在前且只给 `summary()`、
     重复 `save` 覆盖不新增、不存在的 id 返回 `None`/`[]`；
  2. **跨重启**：换一个实例打开同一个文件仍读得到（沙箱里**起不了子进程**——带管道的
     subprocess 一律被拒——所以用"新实例 + 同一文件"替代真·跨进程；见下方注释）；
  3. **坏文件降级而不是抛**：垃圾字节 / 路径不可用 → 原因进 `errors`，实例仍然"可用"
     （`save` 只记错、`get`/`list` 返回空），绝不把异常甩给调用方；
  4. 装配只有一处：`build_trace_store` 按 settings 选实现，sqlite 起不来就**降级为内存**
     并把原因放进返回的 errors（与 `benchmark/catalog.py` 同一先例）。

设置用 `types.SimpleNamespace` 造：沙箱装不上 pydantic_settings，`config/settings.py`
不能 import（与 `test_benchmark_catalog.py` 同一手法）。
"""
from __future__ import annotations

import contextlib
import sqlite3
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from agent_runtime.core.llm.types import TokenUsage  # noqa: E402
from agent_runtime.core.trace.models import (  # noqa: E402
    TraceEvent,
    TraceSession,
    TraceStep,
)
from agent_runtime.core.trace.store import InMemoryTraceStore  # noqa: E402
from agent_runtime.infrastructure.trace.catalog import build_trace_store  # noqa: E402
from agent_runtime.infrastructure.trace.store import MAX_ERRORS, SqliteTraceStore  # noqa: E402


def _session(trace_id: str = "t1", *, source: str = "chat") -> TraceSession:
    return TraceSession(
        trace_id=trace_id,
        task=f"任务 {trace_id}",
        config={"max_steps": 15},
        steps=[
            TraceStep(
                step_number=1,
                thought="先读文件",
                tool_call={"tool_name": "read_file", "args": {"path": "README.md"}},
                tool_result={"success": True, "chars": 42},
                token_usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
                latency_ms=120,
            )
        ],
        events=[
            TraceEvent(event_type="thought", step_number=1, data={"content": "先读文件"}),
            TraceEvent(event_type="final_answer", step_number=0, data={"content": "答案"}),
        ],
        final_answer="答案",
        total_tokens=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        total_latency_ms=200,
        status="finished",
        source=source,
    )


def _settings(**overrides: object) -> types.SimpleNamespace:
    base = {
        "trace_store": "memory",
        "trace_db_path": "trace.db",
        "trace_capacity": 50,
    }
    base.update(overrides)
    return types.SimpleNamespace(**base)


@contextlib.contextmanager
def _tmpdir():
    """临时目录；**清理失败不算测试失败**。

    存储实例按契约持有一个打开中的连接（协议只有 save/get/list/events，没有 `close()`），
    Windows 上因此删不掉还开着的 `trace.db`。`ignore_cleanup_errors` 让目录留在 %TEMP%，
    而不是把"文件正被本进程使用"变成一条假失败。
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        yield tmp


# ── 往返 ──


def test_save_then_get_round_trips_the_whole_session() -> None:
    with _tmpdir() as tmp:
        store = SqliteTraceStore(str(Path(tmp) / "trace.db"))
        assert store.errors == [], f"干净路径不该有错误：{store.errors}"
        store.save(_session())

        again = store.get("t1")
        assert again is not None
        assert again.trace_id == "t1"
        assert again.source == "chat"
        assert again.status == "finished"
        assert again.final_answer == "答案"
        assert again.config["max_steps"] == 15
        assert again.total_tokens.total_tokens == 15
        assert again.total_latency_ms == 200
        assert len(again.steps) == 1
        step = again.steps[0]
        assert step.thought == "先读文件"
        assert step.tool_call["tool_name"] == "read_file"
        assert step.tool_result["chars"] == 42
        assert step.token_usage.total_tokens == 15
        assert step.latency_ms == 120
        assert [e.event_type for e in again.events] == ["thought", "final_answer"]
        assert [e.event_type for e in store.events("t1")] == ["thought", "final_answer"]


def test_a_new_instance_over_the_same_file_still_reads_it() -> None:
    """**跨重启**的替代验证。

    真·跨进程在这里验不了：沙箱拒绝对外捕获输出的子进程（`subprocess` + 管道 → EPERM），
    所以"重启后还在"退一步用"**新实例 + 同一个文件**"来钉——只要数据真的落到了磁盘上
    （而不是留在第一个实例的内存里），新实例就一定读得到。若实现把会话只放在 `self` 里，
    这条会失败。
    """
    with _tmpdir() as tmp:
        path = str(Path(tmp) / "trace.db")
        first = SqliteTraceStore(path)
        first.save(_session("persisted"))

        second = SqliteTraceStore(path)  # 模拟"重启后重新装配"
        assert second.errors == []
        again = second.get("persisted")
        assert again is not None, "新实例应该从文件里读回会话"
        assert again.final_answer == "答案"
        assert [e.event_type for e in second.events("persisted")] == [
            "thought",
            "final_answer",
        ]
        assert [s["trace_id"] for s in second.list()] == ["persisted"]


def test_saving_the_same_trace_id_twice_overwrites_it() -> None:
    with _tmpdir() as tmp:
        store = SqliteTraceStore(str(Path(tmp) / "trace.db"))
        store.save(_session("t1"))
        updated = _session("t1")
        updated.final_answer = "改过的答案"
        store.save(updated)

        assert [s["trace_id"] for s in store.list()] == ["t1"], "覆盖不新增两条"
        assert store.get("t1").final_answer == "改过的答案"  # type: ignore[union-attr]


def test_list_is_newest_first_and_only_summaries() -> None:
    with _tmpdir() as tmp:
        store = SqliteTraceStore(str(Path(tmp) / "trace.db"))
        store.save(_session("old"))
        store.save(_session("new", source="benchmark"))

        summaries = store.list()
        assert [s["trace_id"] for s in summaries] == ["new", "old"], "最新在前"
        newest = summaries[0]
        assert newest["source"] == "benchmark"
        assert newest["steps"] == 1
        assert newest["total_tokens"] == 15
        assert newest["final_answer"] == "答案"
        assert "events" not in newest, "小结不带逐条明细"


def test_list_honours_the_limit() -> None:
    with _tmpdir() as tmp:
        store = SqliteTraceStore(str(Path(tmp) / "trace.db"))
        for index in range(5):
            store.save(_session(f"t{index}"))

        assert [s["trace_id"] for s in store.list(limit=2)] == ["t4", "t3"]
        assert store.list(limit=0) == []
        assert store.list(limit=-5) == []


def test_missing_trace_returns_none_and_no_events() -> None:
    with _tmpdir() as tmp:
        store = SqliteTraceStore(str(Path(tmp) / "trace.db"))
        assert store.get("nope") is None
        assert store.events("nope") == []
        assert store.list() == []


# ── 坏文件降级 ──


def test_a_garbage_file_degrades_instead_of_raising() -> None:
    """非 SQLite 字节：构造不抛、原因可见、实例仍"可用"（写只记错、读返回空）。"""
    with _tmpdir() as tmp:
        path = Path(tmp) / "trace.db"
        path.write_bytes(b"this is definitely not a sqlite file" * 20)

        store = SqliteTraceStore(str(path))  # 不抛
        assert store.errors, "坏文件必须留下可读原因，不许静默"
        assert any("sqlite" in e.lower() or "trace.db" in e for e in store.errors)

        store.save(_session())  # 降级后写入不抛
        assert store.list() == [], "降级状态下读到的是空，而不是半截数据"
        assert store.get("t1") is None
        assert store.events("t1") == []


def test_the_error_list_is_capped_so_a_long_run_cannot_grow_it_forever() -> None:
    """审查 MAJOR：盘坏 / `database is locked` 时每次写都追加一条，而这份列表会被 `/api/traces`
    整份回给前端；只留最近 `MAX_ERRORS` 条（留着"最近出的问题"就够了）。"""
    with _tmpdir() as tmp:
        store = SqliteTraceStore(str(Path(tmp) / "trace.db"))
        for index in range(MAX_ERRORS + 25):
            store._record(f"第 {index} 条错误")
        assert len(store.errors) == MAX_ERRORS, len(store.errors)
        assert store.errors[-1] == f"第 {MAX_ERRORS + 24} 条错误", store.errors[-1]
        assert "第 0 条错误" not in store.errors, "最旧的应当被挤掉"


def test_a_bad_json_row_is_skipped() -> None:
    """行里的 JSON 坏了 → 当作"没有这条"，不抛。"""
    with _tmpdir() as tmp:
        path = str(Path(tmp) / "trace.db")
        store = SqliteTraceStore(path)
        store.save(_session("good"))

        conn = sqlite3.connect(path)
        try:
            conn.execute(
                "INSERT INTO trace_sessions (trace_id, session_json) VALUES (?, ?)",
                ("broken", "{不是 JSON"),
            )
            conn.commit()
        finally:
            conn.close()

        assert store.get("broken") is None
        assert [s["trace_id"] for s in store.list()] == ["good"], "坏行被跳过，好行照常"


# ── 装配 ──


def test_build_trace_store_defaults_to_memory() -> None:
    with _tmpdir() as tmp:
        store, errors = build_trace_store(_settings(), tmp)
        assert isinstance(store, InMemoryTraceStore)
        assert not isinstance(store, SqliteTraceStore)
        assert errors == []

        store.save(_session("m1"))
        assert store.get("m1") is not None
        assert not (Path(tmp) / "trace.db").exists(), "内存实现不该落盘"


def test_build_trace_store_with_sqlite_resolves_the_path_against_the_project_root() -> None:
    with _tmpdir() as tmp:
        settings = _settings(trace_store="sqlite", trace_db_path="sub/dir/trace.db")
        store, errors = build_trace_store(settings, tmp)

        assert isinstance(store, SqliteTraceStore)
        assert errors == []
        store.save(_session("s1"))
        assert store.get("s1") is not None
        assert (Path(tmp) / "sub" / "dir" / "trace.db").exists(), "相对路径按项目根解析"


def test_build_trace_store_degrades_to_memory_when_the_path_is_unusable() -> None:
    """把"目录"位置做成文件 → sqlite 打不开 → 降级为内存 + errors 非空（绝不抛）。"""
    with _tmpdir() as tmp:
        blocker = Path(tmp) / "blocker"
        blocker.write_text("我是文件，不是目录", encoding="utf-8")
        settings = _settings(trace_store="sqlite", trace_db_path="blocker/trace.db")

        store, errors = build_trace_store(settings, tmp)
        assert isinstance(store, InMemoryTraceStore), "降级为内存实现"
        assert errors, "降级原因必须可见"

        store.save(_session("degraded"))  # 降级后仍然能用
        assert store.get("degraded") is not None


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
