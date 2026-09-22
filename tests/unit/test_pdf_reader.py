"""PDFReaderTool 契约：读取工作区内 PDF 的文本。

沙箱没有 pypdf，因此"页面文本抽取"通过**注入式 page_reader**（鸭子类型）解耦：
  * 测试注入纯标准库假件 → 页面选择、截断、错误处理都能在沙箱内验证；
  * 真实后端（pypdf）惰性导入，缺依赖时给可读错误而不是 ImportError；
    pypdf 本身只能在你机器上验证（本文件不假装验证过它）。
"""
from __future__ import annotations

import asyncio
import importlib.util
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from agent_runtime.infrastructure.tools.pdf_reader import PDFReaderTool

# 测试宿主适配（同 test_file_reader）：本沙箱 mkdtemp 生成的目录拒绝嵌套写入，
# 普通 mkdir 继承工作区 ACL 正常，故 fixture 用普通 mkdir + finally 清理。
_BASE = Path(__file__).resolve().parents[2] / ".testtmp"
_SEQ = 0
PAGES = ["第一页内容", "第二页内容", "第三页内容"]


def _new_root() -> str:
    global _SEQ
    _SEQ += 1
    p = _BASE / f"pdf{_SEQ:02d}"
    p.mkdir(parents=True, exist_ok=True)
    return str(p)


def _make_pdf(root: str, name: str = "doc.pdf") -> str:
    path = Path(root) / name
    path.write_bytes(b"%PDF-1.4 fake bytes")  # 假 reader 不解析内容
    return str(path)


def _fake_reader(page_texts=None, exc: Exception | None = None):
    pages = list(PAGES if page_texts is None else page_texts)
    calls: list[bytes] = []

    def reader(data: bytes) -> list[str]:
        calls.append(data)
        if exc is not None:
            raise exc
        return list(pages)

    reader.calls = calls  # type: ignore[attr-defined]
    return reader


def test_reads_all_pages_by_default() -> None:
    root = _new_root()
    try:
        _make_pdf(root)
        tool = PDFReaderTool(root=root, page_reader=_fake_reader())
        result = asyncio.run(tool.execute(path="doc.pdf"))
        assert result.success is True
        assert "第一页内容" in result.text
        assert "第三页内容" in result.text
        assert result.data["pages"] == 3
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_single_page_selection() -> None:
    root = _new_root()
    try:
        _make_pdf(root)
        tool = PDFReaderTool(root=root, page_reader=_fake_reader())
        result = asyncio.run(tool.execute(path="doc.pdf", pages="2"))
        assert result.success is True
        assert "第二页内容" in result.text
        assert "第一页内容" not in result.text
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_page_range_selection() -> None:
    root = _new_root()
    try:
        _make_pdf(root)
        tool = PDFReaderTool(root=root, page_reader=_fake_reader())
        result = asyncio.run(tool.execute(path="doc.pdf", pages="1-2"))
        assert result.success is True
        assert "第一页内容" in result.text
        assert "第二页内容" in result.text
        assert "第三页内容" not in result.text
        assert result.data["pages"] == 3
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_page_out_of_range_is_readable() -> None:
    root = _new_root()
    try:
        _make_pdf(root)
        tool = PDFReaderTool(root=root, page_reader=_fake_reader())
        result = asyncio.run(tool.execute(path="doc.pdf", pages="9"))
        assert result.success is False
        assert "9" in result.text
        assert "3" in result.text, "错误信息应告知实际页数"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_invalid_pages_format_is_readable() -> None:
    root = _new_root()
    try:
        _make_pdf(root)
        tool = PDFReaderTool(root=root, page_reader=_fake_reader())
        result = asyncio.run(tool.execute(path="doc.pdf", pages="abc"))
        assert result.success is False
        assert "pages" in result.text.lower()
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_file_not_found() -> None:
    root = _new_root()
    try:
        tool = PDFReaderTool(root=root, page_reader=_fake_reader())
        result = asyncio.run(tool.execute(path="missing.pdf"))
        assert result.success is False
        assert "not found" in result.text
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_path_escape_denied() -> None:
    root = _new_root()
    try:
        reader = _fake_reader()
        tool = PDFReaderTool(root=root, page_reader=reader)
        result = asyncio.run(tool.execute(path="../../etc/passwd"))
        assert result.success is False
        assert "escapes" in result.text
        assert reader.calls == [], "越界路径不应触达 reader"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_empty_path_rejected() -> None:
    root = _new_root()
    try:
        tool = PDFReaderTool(root=root, page_reader=_fake_reader())
        result = asyncio.run(tool.execute(path="  "))
        assert result.success is False
        assert "path" in result.text.lower()
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_reader_failure_is_readable() -> None:
    root = _new_root()
    try:
        _make_pdf(root)
        tool = PDFReaderTool(
            root=root, page_reader=_fake_reader(exc=ValueError("file is encrypted"))
        )
        result = asyncio.run(tool.execute(path="doc.pdf"))
        assert result.success is False
        assert "encrypted" in result.text
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_empty_text_gives_readable_error() -> None:
    root = _new_root()
    try:
        _make_pdf(root)
        tool = PDFReaderTool(root=root, page_reader=_fake_reader(page_texts=["", "  "]))
        result = asyncio.run(tool.execute(path="doc.pdf"))
        assert result.success is False
        assert "文本" in result.text or "text" in result.text.lower()
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_missing_pypdf_is_reported_clearly() -> None:
    if importlib.util.find_spec("pypdf") is not None:
        print("     (pypdf installed — 跳过该分支)")
        return
    root = _new_root()
    try:
        _make_pdf(root)
        tool = PDFReaderTool(root=root)  # 不注入 reader → 走真实后端
        result = asyncio.run(tool.execute(path="doc.pdf"))
        assert result.success is False
        assert "pypdf" in result.text
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_truncation_respects_configured_max_chars() -> None:
    root = _new_root()
    try:
        _make_pdf(root)
        tool = PDFReaderTool(
            root=root, page_reader=_fake_reader(page_texts=["a" * 500, "b" * 500]), max_chars=100
        )
        result = asyncio.run(tool.execute(path="doc.pdf"))
        assert result.success is True
        assert result.metadata["truncated"] is True
        assert len(result.text) <= 100 + len("\n...[truncated]")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_schema_declares_path_required() -> None:
    root = _new_root()
    try:
        params = PDFReaderTool(root=root).to_openai_schema()["function"]["parameters"]
        assert params["required"] == ["path"]
        assert set(params["properties"]) == {"path", "pages"}
        assert params["properties"]["path"]["type"] == "string"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _run_all() -> None:
    tests = [
        v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)
    ]
    for t in tests:
        print(f"RUN  {t.__name__}")
        t()
        print(f"PASS {t.__name__}")
    print("ALL PASS")


if __name__ == "__main__":
    _run_all()
