import asyncio, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

# 测试宿主适配（不触碰被测代码）：本沙箱下 tempfile 的安全目录创建
# （mkdtemp / TemporaryDirectory）生成的目录拒绝嵌套写入（已双点复现：
# 平台 %TEMP% 与工作区内均 WinError 5，且 icacls 亦被拒），而普通
# Path.mkdir 继承工作区 ACL、嵌套读写正常。故 fixture 用普通 mkdir。
import shutil

from agent_runtime.infrastructure.tools.file_reader import FileReaderTool

_BASE = Path(__file__).resolve().parents[2] / ".testtmp"
_SEQ = 0


def _new_root() -> str:
    """新建一次性 fixture 目录（普通 mkdir，继承沙箱可写 ACL）。"""
    global _SEQ
    _SEQ += 1
    p = _BASE / f"case{_SEQ:02d}"
    p.mkdir(parents=True, exist_ok=True)
    return str(p)


async def test_normal_read():
    # ① 正常读取：嵌套子目录文件
    root = _new_root()
    try:
        notes = Path(root) / "notes"
        notes.mkdir()
        (notes / "a.txt").write_text("hello world", encoding="utf-8")
        tool = FileReaderTool(root=root)
        r = await tool.execute(path="notes/a.txt")
        assert r.success is True
        assert r.text == "hello world"
        assert r.data["path"] == "notes/a.txt"
        assert r.data["chars"] == 11
        assert r.data["truncated"] is False
    finally:
        shutil.rmtree(root, ignore_errors=True)


async def test_file_not_found():
    # ② 文件不存在 → success=False，text 含 "not found"
    root = _new_root()
    try:
        tool = FileReaderTool(root=root)
        r = await tool.execute(path="nope/missing.txt")
        assert r.success is False
        assert "not found" in r.text
    finally:
        shutil.rmtree(root, ignore_errors=True)


async def test_path_escape_denied():
    # ③ `..` 逃逸与绝对路径（真实存在于 fixture 根之外的文件）均拒绝
    root = _new_root()
    other = _new_root()
    try:
        tool = FileReaderTool(root=root)
        outside = Path(other) / "x.txt"
        outside.write_text("secret", encoding="utf-8")
        r1 = await tool.execute(path="../x.txt")
        r2 = await tool.execute(path=str(outside))
        for r in (r1, r2):
            assert r.success is False
            assert "escapes" in r.text
    finally:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(other, ignore_errors=True)


async def test_truncation_at_4000():
    # ④ 超长文件截断：5000 字符 → 前 4000 + 截断标记
    #    plan 原定 ≤4013 与其后缀 "\n...[truncated]"（15 字符）矛盾：
    #    4000+15=4015 恒大于 4013，最小修正为 ≤4015（实现保持 plan 逐字版）
    root = _new_root()
    try:
        payload = "x" * 5000
        (Path(root) / "big.txt").write_text(payload, encoding="utf-8")
        tool = FileReaderTool(root=root)
        r = await tool.execute(path="big.txt")
        assert r.success is True
        assert "[truncated]" in r.text
        assert len(r.text) <= 4015
        assert r.text.startswith(payload[:4000])
        assert r.data["truncated"] is True
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    _failed = []
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            print(f"RUN  {_name}")
            try:
                asyncio.run(_fn())
            except Exception as _e:
                print(f"FAIL {_name}: {type(_e).__name__}: {_e}")
                _failed.append(_name)
            else:
                print(f"PASS {_name}")
    if _failed:
        raise SystemExit(f"FAILED: {', '.join(_failed)}")
    print("ALL PASS")
