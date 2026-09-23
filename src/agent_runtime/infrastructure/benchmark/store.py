"""报告落盘（JSON 文件；纯 IO，零第三方依赖）。

两条要点：
  1. **`run_id` 会变成文件名，也是 API 的路径参数** —— 必须挡住 `../`、绝对路径、双重扩展名这类越界。
     这是**信任边界**，不是洁癖：一个 `run_id="../../x"` 就能把报告写到仓库外面去。
  2. 写入**原子**（先写 `.tmp` 再 `os.replace`），否则并发读到的是写了一半的报告。

坏文件一律当作"没有这份报告"：一个烂文件不能让 `/api/benchmarks` 整体 500。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from ...core.benchmark.models import BenchmarkReport

_SAFE_NAME = re.compile(r"^[\w.-]+$", re.UNICODE)
_RESERVED_SUFFIXES = (".json", ".tmp")


def repo_default_dir(project_root: str) -> str:
    """默认落盘目录（与 `skills/` 同级；已 gitignore）。"""
    return str(Path(project_root) / "benchmark_runs")


def _safe_name(run_id: str) -> str | None:
    """把 run_id 校验成"安全的文件名"，不合格返回 None。"""
    name = str(run_id or "").strip()
    if not name or ".." in name or name.strip(".") == "":
        return None
    if not _SAFE_NAME.match(name):
        return None
    if name.lower().endswith(_RESERVED_SUFFIXES):
        return None
    return name


def save_report(report: BenchmarkReport, directory: str) -> str:
    """原子写入 `<directory>/<run_id>.json`，返回写出的路径。

    `run_id` 不合格时抛 `ValueError` —— 静默写到别处比报错危险得多。
    """
    name = _safe_name(report.run_id)
    if name is None:
        raise ValueError(f"run_id 不能用作文件名：{report.run_id!r}")
    target_dir = Path(directory)
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{name}.json"
    tmp = target_dir / f"{name}.json.tmp"
    tmp.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(tmp, path)
    return str(path)


def load_report(run_id: str, directory: str) -> BenchmarkReport | None:
    """读一份报告；不存在 / 坏文件 / run_id 不合格都返回 None。"""
    name = _safe_name(run_id)
    if name is None:
        return None
    path = Path(directory) / f"{name}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        return BenchmarkReport.from_dict(data)
    except Exception:  # noqa: BLE001 —— 结构不对的报告当作"没有"
        return None


def list_runs(directory: str) -> list[dict]:
    """列出历史报告的小结，**按时间倒序**；目录不存在或坏文件都不抛。"""
    try:
        paths = sorted(Path(directory).glob("*.json"))
    except OSError:
        return []
    summaries = []
    for path in paths:
        report = load_report(path.stem, directory)
        if report is not None:
            summaries.append(report.summary)
    summaries.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return summaries
