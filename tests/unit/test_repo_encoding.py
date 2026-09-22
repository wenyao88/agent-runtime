"""仓库编码约定（把"哪类文件必须 ASCII"这条界线锁死）。

为什么单独一个文件：本项目"编码"这一族问题已经发作三次，而且每次症状都很隐蔽 ——
  ① Phase 3：非 UTF-8 的技能文件（GBK 存的 `.md`）让**整个技能目录**加载失败，好文件被连带丢掉；
  ② Phase 4：我用 PowerShell `Set-Content`（Windows PowerShell 5.1 默认 ANSI/GBK）改源文件，
     把 UTF-8 源码写成乱码、不可恢复；
  ③ Phase 4 本机实测：`alembic.ini` 里的中文注释让 `alembic upgrade head` 在中文 Windows（locale=GBK）上
     直接 `UnicodeDecodeError` —— 因为 **Alembic 用 `configparser.read(encoding="locale")` 读它**。

根因不是"中文有罪"，而是**谁读这个文件、用什么编码读**：
  * `.py` 由 Python 按 UTF-8 读（PEP 3120）→ **中文安全**，注释和文档字符串照写；
  * `.md`/`.json`/`.yml`/`.toml` 由我们或明确按 UTF-8 读的工具处理 → 中文安全；
  * **被外部工具按 locale 编码读的配置**（`*.ini` / `*.cfg` / `*.conf` / `*.mako`）→ **必须纯 ASCII**。
所以本文件分别钉住这两类，而不是一刀切禁止中文。
"""
from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]

_SKIP_DIRS = {"node_modules", ".git", ".testtmp", "__pycache__", ".venv", "dist", "build"}

# 被外部工具按 **locale 编码** 读取的配置类文件：必须纯 ASCII
LOCALE_READ_SUFFIXES = (".ini", ".cfg", ".conf", ".mako")

# Python 源码：必须是合法 UTF-8（中文字符串/注释照常写，但不允许出现 GBK 乱码）
PY_SUFFIXES = (".py",)

# 我们自己按 UTF-8 读的文本资产
UTF8_ASSET_SUFFIXES = (".md", ".json", ".yml", ".yaml", ".toml")


def _walk(suffixes: tuple[str, ...]) -> list[Path]:
    found: list[Path] = []
    for path in _ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() in suffixes:
            found.append(path)
    return sorted(found)


def test_locale_read_configs_are_pure_ascii() -> None:
    offenders = []
    for path in _walk(LOCALE_READ_SUFFIXES):
        raw = path.read_bytes()
        if any(b > 127 for b in raw):
            offenders.append(str(path.relative_to(_ROOT)))
    assert not offenders, (
        "以下文件含非 ASCII 字节，但它们是被外部工具按 locale 编码读的，"
        f"中文 Windows 上会解码失败：{offenders}。请把注释改成英文。"
    )


def test_python_sources_are_valid_utf8() -> None:
    """抓的是 ② 那类事故：源文件被写成 GBK/乱码。"""
    offenders = []
    for path in _walk(PY_SUFFIXES):
        try:
            path.read_bytes().decode("utf-8")
        except UnicodeDecodeError as e:
            offenders.append(f"{path.relative_to(_ROOT)}: {e}")
    assert not offenders, f"以下 Python 源文件不是合法 UTF-8：{offenders}"


def test_text_assets_are_valid_utf8() -> None:
    offenders = []
    for path in _walk(UTF8_ASSET_SUFFIXES):
        try:
            path.read_bytes().decode("utf-8")
        except UnicodeDecodeError as e:
            offenders.append(f"{path.relative_to(_ROOT)}: {e}")
    assert not offenders, f"以下文本资产不是合法 UTF-8：{offenders}"


def test_chinese_comments_are_allowed_in_python() -> None:
    """反向断言：不是"禁用中文"。`.py` 里的中文注释/文档字符串是**允许且期望**的。

    这条防止后来者把"必须 ASCII"过度推广成"代码里不许出现中文"。
    """
    react = (_ROOT / "src" / "agent_runtime" / "core" / "agent" / "react.py").read_text(
        encoding="utf-8"
    )
    assert any(ord(ch) > 127 for ch in react), "本项目 .py 文件按 UTF-8 读，中文注释是允许的"


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
