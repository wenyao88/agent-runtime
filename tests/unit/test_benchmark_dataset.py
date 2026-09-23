"""数据集与报告模型的契约（`core/benchmark/{models,dataset}.py`）。

任务集是**数据**，坏数据的处理方式必须与技能加载一致：只记错误并跳过，**绝不抛**。

另一条更关键的约束：任务集里的 `required_tools` 必须真的存在 —— 否则"成功率"统计的是一个
**不可能完成的任务**，数字再漂亮也是假的。这条由测试对着原生工具目录来钉。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from agent_runtime.core.agent.base import AgentResult  # noqa: E402
from agent_runtime.core.benchmark.dataset import load_tasks, parse_tasks  # noqa: E402
from agent_runtime.core.benchmark.models import (  # noqa: E402
    BenchmarkMetrics,
    BenchmarkReport,
    BenchmarkTask,
    TaskRun,
    TaskVerdict,
)

_ROOT = Path(__file__).resolve().parents[2]
_TASKS = _ROOT / "benchmarks" / "tasks.json"
_KNOWN_CATEGORIES = {"github_analysis", "tech_research"}


def _raw(**overrides: object) -> dict:
    item: dict = {
        "task_id": "gh-001",
        "category": "github_analysis",
        "task": "分析 GitHub 仓库 pallets/flask",
        "required_tools": ["github_get_repo"],
        "expected_keywords": ["flask"],
    }
    item.update(overrides)
    return item


# ── 解析：好数据 ──


def test_parse_builds_tasks_with_defaults() -> None:
    tasks, errors = parse_tasks(json.dumps([_raw()]))
    assert errors == []
    assert len(tasks) == 1
    task = tasks[0]
    assert task.task_id == "gh-001"
    assert task.required_tools == ["github_get_repo"]
    assert task.expected_keywords == ["flask"]
    assert task.expected_args == {} and task.min_steps == 1 and task.expected_answer_hints == ""


def test_parse_accepts_a_wrapped_object() -> None:
    tasks, errors = parse_tasks(json.dumps({"tasks": [_raw()]}))
    assert len(tasks) == 1 and errors == []


# ── 解析：坏数据（只记错误，不抛） ──


def test_parse_broken_json_reports_an_error_instead_of_raising() -> None:
    tasks, errors = parse_tasks("{ this is not json")
    assert tasks == []
    assert len(errors) == 1 and "JSON" in errors[0]


def test_parse_rejects_non_list_payload() -> None:
    tasks, errors = parse_tasks(json.dumps({"nope": 1}))
    assert tasks == [] and len(errors) == 1


def test_parse_skips_items_missing_id_or_task_and_keeps_good_ones() -> None:
    payload = [
        _raw(task_id="ok-1"),
        _raw(task_id="", task="有任务没 id"),
        _raw(task_id="no-task", task="   "),
        "我不是对象",
        _raw(task_id="ok-2"),
    ]
    tasks, errors = parse_tasks(json.dumps(payload))
    assert [t.task_id for t in tasks] == ["ok-1", "ok-2"]
    assert len(errors) == 3
    assert any("task_id" in e for e in errors)


def test_parse_skips_duplicate_ids() -> None:
    tasks, errors = parse_tasks(json.dumps([_raw(task_id="dup"), _raw(task_id="dup")]))
    assert [t.task_id for t in tasks] == ["dup"]
    assert len(errors) == 1 and "重复" in errors[0]


def test_parse_rejects_non_list_fields() -> None:
    tasks, errors = parse_tasks(
        json.dumps([_raw(required_tools="github_get_repo"), _raw(task_id="ok")])
    )
    assert [t.task_id for t in tasks] == ["ok"]
    assert len(errors) == 1 and "required_tools" in errors[0]


def test_parse_survives_a_non_numeric_min_steps() -> None:
    tasks, errors = parse_tasks(json.dumps([_raw(min_steps="三步")]))
    assert tasks[0].min_steps == 1, "坏 min_steps 退化为默认值而不是抛异常"
    assert len(errors) == 1


def test_parse_keeps_an_unknown_category() -> None:
    """未知 category 只是标签，不该让整条任务作废（分组/筛选都能容错）。"""
    tasks, errors = parse_tasks(json.dumps([_raw(category="brand_new")]))
    assert [t.category for t in tasks] == ["brand_new"]
    assert errors == []


# ── 成对任务（消融的 memory 组要靠它测跨任务复用） ──


def test_parse_reads_pair_fields() -> None:
    tasks, errors = parse_tasks(
        json.dumps([_raw(pair_id="gh-p01", pair_role="followup")])
    )
    assert errors == []
    assert tasks[0].pair_id == "gh-p01"
    assert tasks[0].pair_role == "followup"


def test_parse_defaults_a_standalone_task_to_no_pair() -> None:
    tasks, _ = parse_tasks(json.dumps([_raw()]))
    assert tasks[0].pair_id == "" and tasks[0].pair_role == ""


def test_parse_keeps_an_unknown_pair_role_but_says_so() -> None:
    """不认识的 role 只记错误、**不丢任务**（与 category 同一种容错态度）。"""
    tasks, errors = parse_tasks(json.dumps([_raw(pair_id="p1", pair_role="middle")]))
    assert [t.pair_role for t in tasks] == ["middle"]
    assert len(errors) == 1 and "pair_role" in errors[0]


def test_parse_flags_a_role_without_a_pair_id() -> None:
    tasks, errors = parse_tasks(json.dumps([_raw(pair_role="followup")]))
    assert [t.pair_id for t in tasks] == [""]
    assert len(errors) == 1 and "pair_id" in errors[0]


def test_load_missing_file_reports_an_error() -> None:
    tasks, errors = load_tasks(str(_ROOT / "definitely" / "missing.json"))
    assert tasks == [] and len(errors) == 1


# ── 仓库里的那份任务集 ──


_TARGET = 100
_PAIRS_PER_CATEGORY = 10


def test_repo_task_set_is_usable() -> None:
    tasks, errors = load_tasks(str(_TASKS))
    assert errors == [], errors
    assert len(tasks) == _TARGET, len(tasks)
    assert len({t.task_id for t in tasks}) == _TARGET
    assert {t.category for t in tasks} <= _KNOWN_CATEGORIES
    for task in tasks:
        assert task.required_tools, f"{task.task_id} 没声明 required_tools"
        assert task.expected_keywords, f"{task.task_id} 没声明 expected_keywords"
    counts = {c: sum(1 for t in tasks if t.category == c) for c in _KNOWN_CATEGORIES}
    assert counts == {"github_analysis": 50, "tech_research": 50}, counts


def test_repo_task_set_has_paired_followups_in_order() -> None:
    """每类 10 对：`followup` **紧跟**它的 `first`（同会话跑时"上一条"就是那条 first）。

    这是消融 memory 组的量具本身 —— 成对任务写错了，这一组测出来的差别就不是记忆的功劳。
    """
    tasks, _ = load_tasks(str(_TASKS))
    pairs: dict[str, list] = {}
    for task in tasks:
        if task.pair_id:
            pairs.setdefault(task.pair_id, []).append(task)

    by_category: dict[str, int] = {}
    for pair_id, members in pairs.items():
        roles = sorted(t.pair_role for t in members)
        assert roles == ["first", "followup"], f"{pair_id} 的角色不是一 first 一 followup：{roles}"
        first = next(t for t in members if t.pair_role == "first")
        followup = next(t for t in members if t.pair_role == "followup")
        assert first.category == followup.category, f"{pair_id} 跨类别成对"
        # 相邻：followup 就在 first 后面一条
        index = {t.task_id: i for i, t in enumerate(tasks)}
        assert index[followup.task_id] == index[first.task_id] + 1, f"{pair_id} 的两条不相邻"
        # 同主题：followup 的文本里必须出现 first 的某个关键词
        shared = [
            k for k in first.expected_keywords if k.lower() in followup.task.lower()
        ]
        assert shared, (
            f"{pair_id} 的 followup 看起来和 first 不同主题："
            f"{followup.task_id} 文本里没有 {first.expected_keywords}"
        )
        by_category[first.category] = by_category.get(first.category, 0) + 1

    assert by_category == {
        "github_analysis": _PAIRS_PER_CATEGORY,
        "tech_research": _PAIRS_PER_CATEGORY,
    }, by_category
    paired = sum(2 for _ in pairs)
    assert paired == 2 * _PAIRS_PER_CATEGORY * len(_KNOWN_CATEGORIES)


def test_repo_task_set_requires_only_existing_tools() -> None:
    """任务集引用了不存在的工具 → 那是一条**不可能完成**的任务，成功率就是假的。"""
    from agent_runtime.core.tool.registry import ToolRegistry
    from agent_runtime.infrastructure.tools.catalog import (
        register_native_tools,
        tool_catalog,
    )

    registry = ToolRegistry()
    register_native_tools(
        registry,
        root=str(_ROOT),
        github_token="",
        web_search_provider="duckduckgo",
        web_search_api_key="",
        http_timeout=1.0,
        max_chars=1000,
    )
    # 用 API 同款的 `tool_catalog()` 取名字：`list_schemas()` 是 OpenAI 格式，不带顶层 name
    known = {entry["name"] for entry in tool_catalog(registry)}
    tasks, errors = load_tasks(str(_TASKS))
    assert errors == []
    for task in tasks:
        unknown = [t for t in task.required_tools if t not in known]
        assert unknown == [], f"{task.task_id} 引用了不存在的工具：{unknown}"


def test_repo_task_set_keywords_are_findable_in_the_task_text() -> None:
    """关键词必须是任务文本里就有的词：这样离线 mock 跑也能命中，报告才有意义。"""
    tasks, _ = load_tasks(str(_TASKS))
    for task in tasks:
        low = task.task.lower()
        for keyword in task.expected_keywords:
            assert keyword.lower() in low, f"{task.task_id} 的关键词 {keyword!r} 不在任务文本里"


# ── 报告模型 ──


def test_report_round_trips_through_dict() -> None:
    report = BenchmarkReport(
        run_id="run-1",
        config={"provider": "mock", "limit": 3},
        verdicts=[TaskVerdict(task_id="gh-001", success=True, missing_tools=[])],
        metrics=BenchmarkMetrics(tasks_total=1, success_rate=1.0, avg_steps=3.0),
    )
    again = BenchmarkReport.from_dict(json.loads(json.dumps(report.to_dict())))
    assert again.run_id == "run-1"
    assert again.config == {"provider": "mock", "limit": 3}
    assert again.metrics.success_rate == 1.0
    assert again.metrics.tasks_total == 1
    assert again.verdicts[0].task_id == "gh-001"
    assert again.verdicts[0].success is True
    assert again.created_at == report.created_at


def test_report_summary_omits_verdicts() -> None:
    report = BenchmarkReport(run_id="run-2", verdicts=[TaskVerdict(task_id="x")])
    summary = report.summary
    assert summary["run_id"] == "run-2"
    assert "verdicts" not in summary
    assert summary["metrics"]["tasks_total"] == 0


def test_task_and_run_defaults() -> None:
    task = BenchmarkTask(task_id="t", task="做事")
    assert run_defaults(task)


def run_defaults(task: BenchmarkTask) -> bool:
    run = TaskRun(task=task)
    return run.result is None and run.tool_events == [] and run.compactions == [] and run.error == ""


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
