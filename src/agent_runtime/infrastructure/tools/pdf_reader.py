"""读取工作区内 PDF 的文本。

沙箱没有 pypdf，因此：
  * 顶层不 import pypdf —— 只在真要用默认后端时惰性导入；
  * 页面文本抽取通过注入式 `page_reader`（鸭子类型：`bytes -> list[str]`）解耦，
    测试用纯标准库假件，页面选择/截断/错误处理都能在沙箱内验证；
  * 缺依赖时返回可读错误而不是 ImportError。

ponytail: 只支持本地工作区内文件。远程 PDF 需要二进制响应支持（HttpOutcome 目前
只有 text），升级路径是给 HttpOutcome 加 `content: bytes`；扫描件/图片型 PDF 不做
OCR，直接给可读错误。
"""
from __future__ import annotations

import os
from typing import Any, Callable

from ...core.tool.base import BaseTool, PropertyDef, ToolResult, ToolSchema
from ._common import resolve_within, truncate_for_context

MAX_CHARS = 4000
PageReader = Callable[[bytes], list[str]]


def _pypdf_pages(data: bytes) -> list[str]:
    """默认后端：pypdf。惰性导入，缺依赖时抛 ImportError 由上层转成可读错误。"""
    import io

    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    return [page.extract_text() or "" for page in reader.pages]


class PDFReaderTool(BaseTool):
    name = "pdf_read"
    description = (
        "读取工作区内 PDF 文件的文本内容，可按页范围读取（如 pages='1-5'）。"
        "扫描件/图片型 PDF 没有文本层，会返回可读错误。"
    )
    parameters = ToolSchema(
        properties={
            "path": PropertyDef(
                type="string", description="工作区内 PDF 相对路径，如 docs/paper.pdf"
            ),
            "pages": PropertyDef(
                type="string", description="页范围，如 '3' 或 '1-5'；留空表示读取全部页"
            ),
        },
        required=["path"],
    )

    def __init__(
        self,
        root: str,
        page_reader: PageReader | None = None,
        max_chars: int = MAX_CHARS,
    ) -> None:
        self._root = os.path.abspath(root)
        self._page_reader = page_reader
        self._max_chars = max_chars

    def _reader(self) -> PageReader:
        return self._page_reader or _pypdf_pages

    @staticmethod
    def _parse_pages(spec: str, total: int) -> tuple[list[int] | None, str | None]:
        """返回 (0-based 页索引列表, 错误信息)；空 spec 表示全部页。"""
        text = (spec or "").strip()
        if not text:
            return list(range(total)), None
        try:
            if "-" in text:
                start_text, end_text = text.split("-", 1)
                start, end = int(start_text), int(end_text)
            else:
                start = end = int(text)
        except ValueError:
            return None, f"无法解析 pages 参数：{spec!r}；请使用 '3' 或 '1-5' 形式"
        if start < 1 or end < start:
            return None, f"pages 范围非法：{spec!r}（页码从 1 开始，起始不得大于结束）"
        if start > total:
            return None, f"pages 超出范围：请求第 {start} 页，但该文档只有 {total} 页"
        return list(range(start - 1, min(end, total))), None

    async def execute(self, path: str = "", pages: str = "", **kwargs: Any) -> ToolResult:
        if not (path or "").strip():
            return self._err("path 不能为空")
        target = resolve_within(self._root, path)
        if target is None:
            return self._err(f"path escapes workspace root: {path!r}")
        if not os.path.isfile(target):
            return self._err(f"file not found: {path!r}")

        try:
            with open(target, "rb") as fh:
                data = fh.read()
        except OSError as e:
            return self._err(f"读取文件失败：{e}")

        try:
            page_texts = list(self._reader()(data))
        except ImportError:
            return self._err(
                "缺少 pypdf：请先 `pip install pypdf`，或给工具注入自定义 page_reader"
            )
        except Exception as e:  # noqa: BLE001 —— 加密/损坏 PDF 一律转可读错误
            return self._err(f"PDF 解析失败：{type(e).__name__}: {e}")

        total = len(page_texts)
        selected, error = self._parse_pages(pages, total)
        if error:
            return self._err(error)
        if not selected:
            return self._err("该 PDF 没有任何页面")

        chunks: list[str] = []
        for index in selected:
            text = (page_texts[index] or "").strip()
            if text:
                chunks.append(f"[page {index + 1}]\n{text}")
        if not chunks:
            return self._err(
                "未从该 PDF 提取到任何文本（可能是扫描件/图片型 PDF，需要 OCR 才能读取）"
            )

        out, truncated = truncate_for_context("\n\n".join(chunks), self._max_chars)
        return ToolResult(
            tool_name=self.name,
            success=True,
            text=out,
            data={
                "path": path,
                "pages": total,
                "read_pages": [index + 1 for index in selected],
            },
            metadata={"truncated": truncated},
        )

    def _err(self, message: str) -> ToolResult:
        return ToolResult(
            tool_name=self.name, success=False, text=f"Error: {message}", error=message
        )
