"""分层与"core 只用标准库"的**全仓**约束（审查 M4）。

为什么单独一个文件：这条不是 MCP 的性质，而是整个 `core/**` 的性质。原先它只扫
`core/mcp/*.py` —— 名字写着"core 不得依赖第三方"，实际只覆盖一个子目录，**看起来合规**。
现在扫整个 `core/**` 与 `infrastructure/**`。

判定用 allowlist（`sys.stdlib_module_names`）而不是黑名单：黑名单挡不住 yaml、numpy
这类"谁都可能顺手 import 一下"的包，allowlist 天生挡得住。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

_SRC = Path(__file__).resolve().parents[2] / "src" / "agent_runtime"
_IMPORT_RE = re.compile(r"^\s*(?:from\s+([.\w]+)\s+import|import\s+([.\w]+))")
_FORBIDDEN_LAYERS = {"infrastructure", "api"}

_OPTIONAL_THIRD_PARTY = {"tiktoken"}
"""`core/context/manager.py` 里那个**带 try/except 的可选** import（Phase 5 的决定：装了就用真分词器，
没装退化为字符估算）。它是既成事实、且是"可选能力不阻断启动"的一部分，所以显式放行 ——
但它是**唯一**一个：再冒出来第二个就会被这条测试抓住。要彻底干净就把分词器改成注入（留给后续）。"""


def _imports(path: Path):
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        match = _IMPORT_RE.match(line)
        if match is not None:
            yield number, line.strip(), (match.group(1) or match.group(2))


def test_core_only_imports_stdlib_or_core_itself() -> None:
    files = sorted((_SRC / "core").rglob("*.py"))
    assert len(files) > 20, f"core 文件数看起来不对：{len(files)}"
    violations: list[str] = []
    for path in files:
        for number, line, module in _imports(path):
            segments = [s for s in module.split(".") if s]
            if module.startswith("."):
                if _FORBIDDEN_LAYERS.intersection(segments):
                    violations.append(f"{path.name}:{number} 相对 import 跨层：{line}")
                continue
            if _FORBIDDEN_LAYERS.intersection(segments):
                violations.append(f"{path.name}:{number} 不得 import IO/API 层：{line}")
                continue
            if segments[0] == "agent_runtime":
                if segments[1:2] != ["core"]:
                    violations.append(f"{path.name}:{number} 只能依赖 core 内部：{line}")
                continue
            if segments[0] in sys.stdlib_module_names:
                continue
            if segments[0] in _OPTIONAL_THIRD_PARTY:
                continue
            violations.append(f"{path.name}:{number} core 只允许标准库：{line}")
    assert not violations, "；".join(violations)


def test_infrastructure_never_imports_the_api_layer() -> None:
    """`infrastructure` 可以 import `core`，但**不能**反向依赖 `api`（真实 agent 的装配靠调用方注入）。"""
    files = sorted((_SRC / "infrastructure").rglob("*.py"))
    assert len(files) > 20, f"infrastructure 文件数看起来不对：{len(files)}"
    violations: list[str] = []
    for path in files:
        for number, line, module in _imports(path):
            segments = [s for s in module.split(".") if s]
            if "api" in segments:
                violations.append(f"{path.name}:{number} infrastructure 不得 import api：{line}")
    assert not violations, "；".join(violations)


def test_only_scripts_may_import_the_api_layer() -> None:
    """反过来说：`api` 只能被"入口"用到。

    允许两类地方：
      * `scripts/**`（脚本就是入口，也允许 import api）；
      * `api/**` 自己内部（包内互相 import 天经地义）。
    其它任何地方（core / infrastructure）出现 `agent_runtime.api` 都是反向依赖。
    """
    root = Path(__file__).resolve().parents[2]
    allowed_dirs = [root / "scripts", _SRC / "api"]
    offenders: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        if any(path.is_relative_to(directory) for directory in allowed_dirs):
            continue
        for number, line, module in _imports(path):
            if "api" in [s for s in module.split(".") if s]:
                offenders.append(f"{path.name}:{number} {line}")
    assert not offenders, "；".join(offenders)


def _run_all() -> None:
    failed = []
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"RUN  {name}")
            try:
                fn()
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
