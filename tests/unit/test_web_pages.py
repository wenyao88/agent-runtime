"""前端的最小真实验证（审查 M5）。

沙箱没有浏览器、`web/` 也没有测试框架，所以前端的"验收"一直是 `tsc -b` + `vite build` ——
但**构建通过证明不了任何一条产品约定**：页面可以完全不 import 组件库、主题变量可以塞进 `.dark`、
`null` 可以被渲染成 `0`，构建照样 exit 0。

spec §6 早就写明了该断言什么（"grep 断言页面确实 import 了 `@/components/ui/*`"、plan T1 的
"`index.css` 里只有 light 变量"），只是没人写。这里把它写成能跑的测试：**只做静态断言**，
不假装能看渲染效果（那仍然只能由人验收）。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

_WEB = Path(__file__).resolve().parents[2] / "web"
_PAGES = ("ChatPage", "TracePage", "InspectorPage", "BenchmarkPage")


def _page_source(name: str) -> str:
    path = _WEB / "src" / "pages" / f"{name}.tsx"
    assert path.exists(), f"页面不存在：{path}"
    return path.read_text(encoding="utf-8")


def test_every_page_is_built_from_the_shadcn_components() -> None:
    missing = [
        name for name in _PAGES if "@/components/ui/" not in _page_source(name)
    ]
    assert not missing, f"这些页面没有用 shadcn 组件：{missing}"


def test_the_theme_is_light_only() -> None:
    """浅色单主题是用户明确要求的：`.dark` 一出现就说明有人偷偷加了深色。"""
    css = (_WEB / "src" / "index.css").read_text(encoding="utf-8")
    assert ".dark" not in css, "不要引入深色主题"
    for variable in ("--background", "--foreground", "--primary", "--border"):
        assert variable in css, f"缺少浅色变量 {variable}"
    assert "@theme inline" in css, "Tailwind v4 的 CSS-first 映射不能丢"


def test_the_shared_formatter_keeps_null_apart_from_zero() -> None:
    """`None ≠ 0` 在前端只有一个实现（`lib/format.ts`），别让四个页面各写一遍。"""
    source = (_WEB / "src" / "lib" / "format.ts").read_text(encoding="utf-8")
    assert '"—"' in source, "缺失值必须渲染成 —"
    for page in _PAGES:
        assert "lib/format" in _page_source(page) or page == "ChatPage", (
            f"{page} 应当复用共享格式化（ChatPage 只展示事件流，可以不用）"
        )


def test_the_components_json_alias_points_at_a_real_module() -> None:
    """审查 m3：`components.json` 承诺 `@/lib/utils`，但那个文件不存在 ——
    下一次 `shadcn add` 生成的组件会 import 一个不存在的模块。"""
    import json

    aliases = json.loads((_WEB / "components.json").read_text(encoding="utf-8"))["aliases"]
    utils = aliases["utils"]
    relative = utils.replace("@/", "")
    assert (_WEB / "src" / f"{relative}.ts").exists(), f"{utils} 指向的文件不存在"


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
