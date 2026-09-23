"""文档与代码的一致性（Phase 9）。

为什么值得一个测试：接口表、`.env` 字段表、工具表这三份"清单"分散在 `README.md` / `.env.example`，
改代码时最容易忘了同步 —— 而读者（含面试官）拿到的就是它们。全部走 AST + 正则，**零第三方依赖**，
所以沙箱里也能跑。

四个方向都钉住：
  * README 里写的接口必须真的存在（不能承诺不存在的端点），新增端点也必须写进 README；
  * `.env.example` 与 `settings.py` 的字段**双向**对齐（漏一个就是"文档没写的配置项"）；
  * README 的工具表与 `NATIVE_TOOL_NAMES` 一致，连"8 个"这个数字也对得上；
  * README 引用到的文档真实存在，且 `docs/` 里没有孤儿文档。
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "src"))

_ROUTE_RE = re.compile(r"@router\.(get|post|delete|put|patch|websocket)\(\s*[\"']([^\"']*)[\"']")
_README_API_RE = re.compile(r"\|\s*`(GET|POST|DELETE|PUT|PATCH|WS) ([^`]+)`")
_ENV_RE = re.compile(r"^([A-Z][A-Z0-9_]*)\s*=", re.MULTILINE)


def _readme() -> str:
    return (_ROOT / "README.md").read_text(encoding="utf-8")


def _routes() -> set[tuple[str, str]]:
    """从路由源码里取出 (方法, 完整路径) —— 不 import（沙箱装不上 fastapi）。"""
    found: set[tuple[str, str]] = set()
    for path in sorted((_ROOT / "src" / "agent_runtime" / "api").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        prefix = re.search(r"APIRouter\(\s*(?:prefix\s*=\s*)?[\"']([^\"']*)[\"']", source)
        base = prefix.group(1) if prefix else ""
        for method, route in _ROUTE_RE.findall(source):
            found.add((method.upper(), f"{base}{route}"))
    return found


def _settings_fields() -> set[str]:
    tree = ast.parse((_ROOT / "src" / "agent_runtime" / "config" / "settings.py").read_text("utf-8"))
    fields: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "Settings":
            fields = {
                item.target.id.upper()
                for item in node.body
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)
            }
    return fields


def test_readme_documents_exactly_the_real_endpoints() -> None:
    documented = {("WS" if m == "WS" else m, p) for m, p in _README_API_RE.findall(_readme())}
    real = {(m if m != "WEBSOCKET" else "WS", p) for m, p in _routes()}
    assert not documented - real, f"README 写了不存在的接口：{sorted(documented - real)}"
    assert not real - documented, f"这些接口没写进 README：{sorted(real - documented)}"


def test_env_example_and_settings_are_in_sync() -> None:
    documented = set(_ENV_RE.findall((_ROOT / ".env.example").read_text(encoding="utf-8")))
    fields = _settings_fields()
    assert fields, "没解析到 Settings 字段（AST 逻辑坏了？）"
    assert not fields - documented, f"这些配置项没写进 .env.example：{sorted(fields - documented)}"
    assert not documented - fields, f".env.example 里有 Settings 不认识的名字：{sorted(documented - fields)}"


def test_readme_tool_table_matches_the_native_tools() -> None:
    tree = ast.parse(
        (_ROOT / "src" / "agent_runtime" / "infrastructure" / "tools" / "catalog.py").read_text("utf-8")
    )
    names: tuple[str, ...] = ()
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "NATIVE_TOOL_NAMES":
            names = tuple(ast.literal_eval(node.value))
    assert names, "没解析到 NATIVE_TOOL_NAMES"
    readme = _readme()
    missing = [name for name in names if f"`{name}`" not in readme]
    assert not missing, f"README 的工具表少了：{missing}"
    counts = {int(n) for n in re.findall(r"(\d+)\s*个原生工具", readme)}
    assert counts == {len(names)}, f"README 写的原生工具数量 {counts} 与实际 {len(names)} 不符"


def test_every_documented_file_exists_and_no_orphan_docs() -> None:
    readme = _readme()
    linked = set(re.findall(r"`(docs/[\w./-]+\.md)`", readme))
    for link in linked:
        assert (_ROOT / link).exists(), f"README 引用了不存在的文件：{link}"
    present = {f"docs/{path.name}" for path in (_ROOT / "docs").glob("*.md")}
    assert not present - linked, f"这些文档没被 README 导航到：{sorted(present - linked)}"


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
