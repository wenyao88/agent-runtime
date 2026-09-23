"""任务集解析（纯函数：JSON 文本 → `(tasks, errors)`）。

坏数据的态度与技能加载、MCP 配置加载一致：**只记错误并跳过该条，绝不抛**。
一条坏任务不该让整轮评测跑不起来 —— 但也绝不能悄悄变成一条"必然失败"的任务。

`required_tools` 是否真实存在**不在这里校验**：core 不该 import infrastructure 的工具目录。
这条由测试对着原生工具目录断言（`tests/unit/test_benchmark_dataset.py`）。
"""
from __future__ import annotations

import json
from pathlib import Path

from .models import BenchmarkTask


def _as_int(value: object, default: int) -> tuple[int, bool]:
    """返回 (值, 是否使用了默认值)。"""
    if isinstance(value, bool) or value is None or value == "":
        return default, value is not None and value != ""
    try:
        return int(value), False  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default, True


def parse_tasks(text: str) -> tuple[list[BenchmarkTask], list[str]]:
    """解析任务集文本。返回 `(tasks, errors)`；`errors` 里的人话要能直接给用户看。"""
    try:
        raw = json.loads(text)
    except Exception as e:  # noqa: BLE001 —— 坏 JSON 只记错误
        return [], [f"任务集不是合法 JSON：{type(e).__name__}: {e}"]

    if isinstance(raw, dict):
        raw = raw.get("tasks")
    if not isinstance(raw, list):
        return [], ['任务集必须是数组，或形如 {"tasks": [...]}']

    tasks: list[BenchmarkTask] = []
    errors: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        task, error = _parse_one(item, index, seen)
        if error:
            errors.append(error)
        if task is not None:
            seen.add(task.task_id)
            tasks.append(task)
    return tasks, errors


def _parse_one(
    item: object, index: int, seen: set[str]
) -> tuple[BenchmarkTask | None, str]:
    where = f"第 {index + 1} 条"
    if not isinstance(item, dict):
        return None, f"{where}不是对象，已跳过"

    task_id = str(item.get("task_id") or "").strip()
    text = str(item.get("task") or "").strip()
    if not task_id or not text:
        return None, f"{where}缺少 task_id 或 task，已跳过"
    if task_id in seen:
        return None, f"task_id 重复：{task_id}，已跳过"

    required = item.get("required_tools", [])
    if not isinstance(required, list):
        return None, f"{task_id}: required_tools 必须是数组，已跳过"
    keywords = item.get("expected_keywords", [])
    if not isinstance(keywords, list):
        return None, f"{task_id}: expected_keywords 必须是数组，已跳过"
    expected_args = item.get("expected_args", {})
    if not isinstance(expected_args, dict):
        return None, f"{task_id}: expected_args 必须是对象，已跳过"

    min_steps, bad_steps = _as_int(item.get("min_steps"), 1)
    note = f"{task_id}: min_steps 不是数字，已按 {min_steps} 处理" if bad_steps else ""
    return (
        BenchmarkTask(
            task_id=task_id,
            task=text,
            category=str(item.get("category") or "unknown"),
            required_tools=[str(t) for t in required],
            expected_keywords=[str(k) for k in keywords],
            expected_args={
                str(tool): dict(spec)
                for tool, spec in expected_args.items()
                if isinstance(spec, dict)
            },
            min_steps=min_steps,
            expected_answer_hints=str(item.get("expected_answer_hints") or ""),
        ),
        note,
    )


def load_tasks(path: str) -> tuple[list[BenchmarkTask], list[str]]:
    """读任务集文件；文件缺失/编码坏了也只记错误。"""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return [], [f"读取任务集失败（{path}）：{type(e).__name__}: {e}"]
    return parse_tasks(text)
