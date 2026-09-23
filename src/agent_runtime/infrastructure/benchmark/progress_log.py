"""进度文件的读写（`<run_id>.progress.jsonl`；纯 IO，零第三方依赖）。

三个决定：
  1. **一行一条、写完就 flush**：断电/被 Ctrl-C 最多丢最后一行，前面跑过的都在磁盘上；
  2. 首行是 **header**（run_id + 分组 + 配置指纹），续跑前先比它 —— 指纹不同就拒绝续跑，绝不混结果；
  3. 坏行（写了一半、被手工改坏）只记一条警告并跳过：一份烂文件不能让几小时的结果作废。

文件名与报告同级同目录，`list_runs` 只扫 `*.json`，所以进度文件不会污染历史列表。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from ...core.benchmark.models import BenchmarkTask
from ...core.benchmark.progress import (
    fingerprint,
    header as header_line,
    matches_run,
    same_fingerprint,
)
from .store import require_safe_name

SUFFIX = ".progress.jsonl"


@dataclass
class ProgressRead:
    header: dict | None = None
    records: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    exists: bool = False


class ProgressLog:
    """一份进度文件。同一 `run_id` + 目录只有一份（续跑就是接着写它）。

    `run_id` 与报告共用同一份文件名安全校验（`store.require_safe_name`）：不合格直接抛
    `ValueError` —— 进度文件同样会变成文件名，`../` 这类 id 能把文件写到仓库外面去。
    """

    def __init__(self, directory: str, run_id: str) -> None:
        self._directory = Path(directory)
        self._run_id = require_safe_name(run_id, "run_id")
        self._path = self._directory / f"{self._run_id}{SUFFIX}"

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def path(self) -> Path:
        return self._path

    def exists(self) -> bool:
        return self._path.is_file()

    def write_header(
        self, config: dict, tasks: list[BenchmarkTask], *, ablation_id: str = ""
    ) -> None:
        """首行写一次（已存在就不动）：续跑时这行是"这一轮到底在跑什么"的唯一凭证。"""
        if self.exists():
            return
        self.append(header_line(self._run_id, config, tasks, ablation_id=ablation_id))

    def append(self, record: dict) -> None:
        """追加一行并 flush（每条任务判分后立刻调用）。"""
        self._directory.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()

    def read(self) -> ProgressRead:
        """逐行读；坏行/半行跳过并记警告。文件不存在返回 `exists=False` 的空结果。"""
        if not self._path.is_file():
            return ProgressRead()
        out = ProgressRead(exists=True)
        try:
            text = self._path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            out.warnings.append(f"进度文件读不出来（{self._path}）：{type(e).__name__}: {e}")
            return out
        for number, line in enumerate(text.splitlines(), start=1):
            raw = line.strip()
            if not raw:
                continue
            try:
                item = json.loads(raw)
            except json.JSONDecodeError as e:
                out.warnings.append(f"进度文件第 {number} 行不是合法 JSON，已跳过：{e}")
                continue
            if not isinstance(item, dict):
                out.warnings.append(f"进度文件第 {number} 行不是对象，已跳过")
                continue
            if out.header is None and item.get("kind") == "progress":
                out.header = item
                continue
            out.records.append(item)
        return out


def _headers(directory: str) -> list[tuple[Path, dict]]:
    """扫出所有进度文件的 header（读不出来的文件直接跳过）。"""
    found: list[tuple[Path, dict]] = []
    try:
        paths = sorted(Path(directory).glob(f"*{SUFFIX}"))
    except OSError:
        return found
    for path in paths:
        try:
            first = path.read_text(encoding="utf-8").splitlines()[:1]
        except (OSError, UnicodeDecodeError):
            continue
        if not first:
            continue
        try:
            item = json.loads(first[0])
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict) and item.get("kind") == "progress":
            found.append((path, item))
    return found


def _unfinished(directory: str, identity: str) -> bool:
    """最终报告还没落盘 = 这一轮没跑完（跑完才写最终报告）。"""
    if not identity:
        return False
    return not (Path(directory) / f"{identity}.json").is_file()


def find_resumable_run(
    directory: str, config: dict, tasks: list[BenchmarkTask]
) -> str | None:
    """找一个可以续跑的单轮 run：指纹完全相同且最终报告还没写。

    多个候选时取**最近改动**的那个（人通常就是想接着刚才那条跑）。
    """
    wanted = fingerprint(config, tasks)
    candidates: list[tuple[float, str]] = []
    for path, item in _headers(directory):
        if item.get("ablation_id"):
            continue
        if not same_fingerprint(item.get("fingerprint") or {}, wanted):
            continue
        run_id = str(item.get("run_id") or "")
        if _unfinished(directory, run_id):
            candidates.append((path.stat().st_mtime, run_id))
    if not candidates:
        return None
    return max(candidates)[1]


def find_resumable_ablation(
    directory: str, config: dict, tasks: list[BenchmarkTask]
) -> str | None:
    """找一轮没跑完的消融：按**运行身份**匹配（三组开关本来就不同，所以不比开关）。"""
    wanted = fingerprint(config, tasks)
    candidates: list[tuple[float, str]] = []
    for path, item in _headers(directory):
        ablation_id = str(item.get("ablation_id") or "")
        if not ablation_id:
            continue
        if not matches_run(item.get("fingerprint") or {}, wanted):
            continue
        if _unfinished(directory, ablation_id):
            candidates.append((path.stat().st_mtime, ablation_id))
    if not candidates:
        return None
    return max(candidates)[1]
