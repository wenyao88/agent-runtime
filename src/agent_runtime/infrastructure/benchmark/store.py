"""报告落盘（JSON 文件；纯 IO，零第三方依赖）。

三条要点：
  1. **`run_id` 会变成文件名，也是 API 的路径参数** —— 必须挡住 `../`、绝对路径、双重扩展名这类越界。
     这是**信任边界**，不是洁癖：一个 `run_id="../../x"` 就能把报告写到仓库外面去。
  2. 写入**原子**（先写 `.tmp` 再 `os.replace`），否则并发读到的是写了一半的报告。
  3. 同一目录里还放**消融对比报告**（`kind: "ablation"`）：结构与单组报告不同，读的时候必须按类型分开，
     否则历史列表会多出一条 `run_id=""` 的幽灵报告。

坏文件一律当作"没有这份报告"：一个烂文件不能让 `/api/benchmarks` 整体 500。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from ...core.benchmark.ablation import KIND_ABLATION, AblationReport
from ...core.benchmark.models import BenchmarkReport

_SAFE_NAME = re.compile(r"^[\w.-]+$", re.UNICODE)
_RESERVED_SUFFIXES = (".json", ".tmp")
_MAX_NAME_LENGTH = 120
_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL"} | {
    f"{prefix}{digit}" for prefix in ("COM", "LPT") for digit in range(1, 10)
}


def safe_name(run_id: str) -> str | None:
    """把 run_id 校验成"安全的文件名"，不合格返回 None。

    比"防越界"更严一点，都是为了让契约**完备**（审查 M2）：
    前后空格会让"文件名 ≠ run_id"、超长名会以 `FileNotFoundError` 而不是 `ValueError` 失败、
    `CON`/`NUL` 这类 Windows 保留名在部分环境下根本无法创建文件。

    **公开**：报告与进度文件是同一类信任边界，必须用同一份判据（各写一份迟早出现"报告挡住了、
    进度文件没挡住"的缺口）。
    """
    raw = str(run_id or "")
    if not raw or raw != raw.strip() or ".." in raw or raw.strip(".") == "":
        return None
    if not _SAFE_NAME.match(raw) or len(raw) > _MAX_NAME_LENGTH:
        return None
    if raw.upper() in _RESERVED_NAMES or raw.lower().endswith(_RESERVED_SUFFIXES):
        return None
    return raw


def require_safe_name(run_id: str, what: str) -> str:
    """校验不通过就抛 `ValueError`（静默写到别处比报错危险得多）。"""
    name = safe_name(run_id)
    if name is None:
        raise ValueError(f"{what} 不能用作文件名：{run_id!r}")
    return name


def _write_json(name: str, payload: dict, directory: str) -> str:
    """原子写入 `<directory>/<name>.json`（先写 `.tmp` 再 `os.replace`）。"""
    target_dir = Path(directory)
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{name}.json"
    tmp = target_dir / f"{name}.json.tmp"
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(tmp, path)
    return str(path)


def save_report(report: BenchmarkReport, directory: str) -> str:
    """原子写入 `<directory>/<run_id>.json`，返回写出的路径。

    `run_id` 不合格时抛 `ValueError` —— 静默写到别处比报错危险得多。
    """
    name = require_safe_name(report.run_id, "run_id")
    return _write_json(name, report.to_dict(), directory)


def save_ablation(report: AblationReport, directory: str) -> str:
    """原子写入对比报告（与单组报告同目录，靠 `kind` 字段区分）。"""
    name = require_safe_name(report.ablation_id, "ablation_id")
    return _write_json(name, report.to_dict(), directory)


def _read_json(name: str, directory: str) -> dict | None:
    path = Path(directory) / f"{name}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def load_report(run_id: str, directory: str) -> BenchmarkReport | None:
    """读一份**单组**报告；不存在 / 坏文件 / run_id 不合格 / 是对比报告都返回 None。

    对比报告按类型拒掉：`BenchmarkReport.from_dict` 会在它身上"成功"解析出一份
    `run_id=""` 的空报告，那会变成历史列表里的幽灵条目。
    """
    name = safe_name(run_id)
    if name is None:
        return None
    data = _read_json(name, directory)
    if data is None or data.get("kind") == KIND_ABLATION:
        return None
    try:
        return BenchmarkReport.from_dict(data)
    except Exception:  # noqa: BLE001 —— 结构不对的报告当作"没有"
        return None


def list_runs(directory: str) -> list[dict]:
    """列出历史**单组**报告的小结，**按时间倒序**；目录不存在或坏文件都不抛。

    对比报告被 `load_report` 按类型拒掉，所以不会出现在这里（三份组报告各自在里面，
    对比本身另有文件）—— 前端列表因此只会出现可比的三行。
    """
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
