"""续跑进度记录契约（`core/benchmark/progress.py`，纯逻辑）。

真实消融要跑几小时，断一次就全重来是不可接受的 —— 这里钉住的就是"断点能不能真的续上、且不会续错"：

  1. 记录 → 还原必须**信息无损到能重算指标**（`tool_events`/`compactions`/error 都要在）；
  2. 只有 `success=True` 会被跳过（失败的条目下次重试，限流正是要重试的东西）；
  3. 还原出来的判分带 `skipped=True`（报告里看得出哪些是捡回来的）；
  4. 配置指纹不同就**不许续**（换模型/换 limit/换任务集混进同一份报告 = 假数据）；
  5. 坏记录一律当"没这条"，绝不抛（半行 JSON、字段类型不对、id 对不上）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from agent_runtime.core.benchmark.models import (  # noqa: E402
    BenchmarkTask,
    CompactionEvent,
    TaskRun,
    TaskVerdict,
    ToolEvent,
)
from agent_runtime.core.benchmark.progress import (  # noqa: E402
    fingerprint,
    header,
    matches_run,
    resume_map,
    restore,
    same_fingerprint,
    task_ids_hash,
    task_record,
)


def _task(task_id: str = "gh-001") -> BenchmarkTask:
    return BenchmarkTask(task_id=task_id, task="做事", category="github_analysis")


def _run(task_id: str = "gh-001") -> TaskRun:
    return TaskRun(
        task=_task(task_id),
        tool_events=[
            ToolEvent(step=1, tool="read_file", success=True, result_chars=10),
            ToolEvent(step=2, tool="read_file", success=False, result_chars=3),
        ],
        compactions=[
            CompactionEvent(
                before=1000, after=250, strategy="summarize", summarized=4,
                summarizer_tokens=210, summarizer_ms=120,
            )
        ],
        error="",
        error_kind="",
    )


def _verdict(task_id: str = "gh-001", *, success: bool = True) -> TaskVerdict:
    return TaskVerdict(
        task_id=task_id,
        success=success,
        required_tools_ok=success,
        keywords_ok=success,
        steps=4,
        min_steps=3,
        total_tokens=480,
        latency_ms=1234,
        arg_hits=1,
        arg_expected=1,
        judge_scores={"completeness": 4},
    )


def _record(**overrides: object) -> dict:
    record = task_record(_run(), _verdict())
    record.update(overrides)
    return record


# ── 往返 ──


def test_a_record_round_trips_the_verdict_and_the_run_aggregates() -> None:
    restored = restore(_task(), _record())
    assert restored is not None
    run, verdict = restored
    assert verdict.task_id == "gh-001"
    assert verdict.success is True
    assert verdict.steps == 4 and verdict.total_tokens == 480 and verdict.latency_ms == 1234
    assert verdict.judge_scores == {"completeness": 4}
    assert run.error == "" and run.error_kind == ""
    assert [(e.tool, e.success) for e in run.tool_events] == [("read_file", True), ("read_file", False)]
    assert len(run.compactions) == 1
    assert run.compactions[0].summarizer_tokens == 210
    assert run.compactions[0].summarizer_ms == 120
    assert run.compactions[0].strategy == "summarize"


def test_a_restored_verdict_is_marked_as_skipped() -> None:
    """捡回来的条目必须自报来源：报告里不能看起来像"刚跑过"。"""
    run, verdict = restore(_task(), _record())  # type: ignore[misc]
    assert verdict.skipped is True
    assert _verdict().skipped is False, "真跑出来的默认不是 skipped"


def test_the_record_is_json_serializable() -> None:
    """进度文件是 JSONL：记录里不能有数据类实例、`set` 这类东西。"""
    text = json.dumps(_record(), ensure_ascii=False)
    assert "gh-001" in text
    assert json.loads(text)["kind"] == "task"


def test_the_record_carries_the_error_of_a_failed_task() -> None:
    run = _run()
    run.error = "APITimeoutError: timed out"
    run.error_kind = "provider"
    verdict = _verdict(success=False)
    verdict.error = run.error
    verdict.error_kind = "provider"
    record = task_record(run, verdict)
    assert record["success"] is False
    assert record["error_kind"] == "provider"
    restored = restore(_task(), record)
    assert restored is not None
    assert restored[0].error_kind == "provider"
    assert restored[1].error == "APITimeoutError: timed out"


# ── 续跑集合 ──


def test_only_successful_tasks_are_resumed() -> None:
    """失败的下次必须重试（限流/超时正是要重试的东西）。"""
    tasks = [_task("a"), _task("b"), _task("c")]
    records = [
        _record(task_id="a", success=True),
        _record(task_id="b", success=False),
        _record(task_id="c", success=True),
    ]
    resumed = resume_map(tasks, records)
    assert set(resumed) == {"a", "c"}


def test_tasks_missing_from_the_current_task_set_are_ignored() -> None:
    """换了任务集：孤儿结果不许混进报告（否则成功率会被不存在的任务污染）。"""
    resumed = resume_map([_task("a")], [_record(task_id="gh-999", success=True)])
    assert resumed == {}


def test_broken_records_are_ignored_instead_of_raising() -> None:
    tasks = [_task("a")]
    broken = [
        "我不是对象",
        {"kind": "task", "task_id": "a"},  # 缺 verdict
        {"kind": "task", "task_id": "a", "verdict": "不是对象"},
        {"kind": "header", "task_id": "a", "verdict": _verdict().__dict__},
        _record(task_id="b"),  # id 对不上
        {"kind": "task", "task_id": "a", "verdict": {"task_id": "a", "steps": "很多"}},
    ]
    assert resume_map(tasks, broken) == {}


def test_unknown_extra_fields_are_dropped_not_fatal() -> None:
    """未来版本多写一个字段，旧代码读它也不该炸（前后兼容）。"""
    record = _record()
    record["verdict"]["brand_new_field"] = 1
    record["run"]["tool_events"][0]["brand_new_field"] = 2
    restored = restore(_task(), record)
    assert restored is not None
    assert restored[0].tool_events[0].tool == "read_file"


def test_a_record_restores_only_for_its_own_task_id() -> None:
    assert restore(_task("other"), _record(task_id="gh-001")) is None


# ── 指纹 ──


def _config(**overrides: object) -> dict:
    base = {
        "provider": "real",
        "model": "deepseek-flash",
        "judge": 0,
        "limit": 3,
        "tasks_total": 3,
        "group": "memory",
        "memory": True,
        "compaction": False,
        "session_scope": "run",
    }
    base.update(overrides)
    return base


def test_the_fingerprint_covers_everything_that_makes_it_a_different_run() -> None:
    tasks = [_task("a"), _task("b")]
    base = fingerprint(_config(), tasks)
    assert base["task_ids_hash"] == task_ids_hash(tasks)
    for change in (
        {"provider": "mock"},
        {"model": "别的模型"},
        {"judge": 3},
        {"limit": 5},
        {"tasks_total": 5},
        {"group": "baseline"},
        {"memory": False},
        {"compaction": True},
        {"session_scope": "task"},
    ):
        assert not same_fingerprint(base, fingerprint(_config(**change), tasks)), change
    assert same_fingerprint(base, fingerprint(_config(), tasks))


def test_a_changed_task_set_changes_the_fingerprint() -> None:
    """任务集被改过（增删/改名）就不该续跑：混跑出来的成功率是两套数据的混合物。"""
    before = fingerprint(_config(), [_task("a"), _task("b")])
    after = fingerprint(_config(), [_task("a"), _task("c")])
    assert before["task_ids_hash"] != after["task_ids_hash"]
    assert not same_fingerprint(before, after)


def test_the_ids_hash_ignores_order_but_not_membership() -> None:
    assert task_ids_hash([_task("a"), _task("b")]) == task_ids_hash([_task("b"), _task("a")])
    assert task_ids_hash([_task("a")]) != task_ids_hash([_task("a"), _task("b")])


def test_a_missing_field_makes_the_fingerprints_different() -> None:
    """少一个字段也算不同：宁可重跑，不要把不确定的东西接上去。"""
    tasks = [_task("a")]
    left = fingerprint(_config(), tasks)
    right = dict(left)
    right.pop("model")
    assert not same_fingerprint(left, right)


def test_matches_run_ignores_the_group_switches() -> None:
    """找"上一次没跑完的那轮消融"时按运行身份匹配：三组开关本来就不同。"""
    tasks = [_task("a")]
    baseline = fingerprint(_config(group="baseline", memory=False, session_scope="task"), tasks)
    memory = fingerprint(_config(group="memory", memory=True, session_scope="run"), tasks)
    assert matches_run(baseline, memory), "同一轮消融的三组应当互相匹配"
    assert not matches_run(baseline, fingerprint(_config(judge=3), tasks))
    assert not matches_run(baseline, fingerprint(_config(), [_task("z")]))


def test_the_header_self_identifies_the_run() -> None:
    tasks = [_task("a")]
    line = header("run-1", _config(), tasks, ablation_id="ab-1")
    assert line["kind"] == "progress"
    assert line["run_id"] == "run-1"
    assert line["ablation_id"] == "ab-1"
    assert line["group"] == "memory"
    assert line["fingerprint"] == fingerprint(_config(), tasks)


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
