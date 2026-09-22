"""网页抓取工具：HTML → 正文文本。

沙箱没有 bs4，因此用标准库 html.parser 自建提取器：
  * 跳过 script/style/noscript/svg/template —— 否则 JS/CSS 会被当成正文喂给模型；
  * <title> 单独抽出作为标题；
  * 块级标签转成换行，再折叠空白（模型的 token 预算很宝贵）。

安全边界：只允许 http/https。file://、data:、javascript: 一律拒绝且不发请求。
ponytail: 只做"正文提取"的粗粒度启发式，不做 Readability 级别的正文识别；
若抓取质量成为瓶颈，升级路径是引入 readability-lxml 或直接接 MCP 的 fetch server。
"""
from __future__ import annotations

from html.parser import HTMLParser
from typing import Any

from ...core.tool.base import PropertyDef, ToolResult, ToolSchema
from .http_base import HttpToolBase

SKIP_TAGS = {"script", "style", "noscript", "svg", "template", "iframe"}
BLOCK_TAGS = {
    "p", "div", "br", "li", "tr", "section", "article", "header", "footer",
    "h1", "h2", "h3", "h4", "h5", "h6", "table", "ul", "ol", "blockquote", "pre",
}
ALLOWED_SCHEMES = ("http", "https")


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._in_title = False
        self.title_parts: list[str] = []
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        tag = tag.lower()
        if tag in SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: Any) -> None:
        if tag.lower() in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in SKIP_TAGS:
            if self._skip_depth > 0:
                self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title_parts.append(data)
            return
        self.parts.append(data)


def extract_text(html_text: str) -> tuple[str, str]:
    """返回 (title, text)；对畸形 HTML 保持宽容，绝不抛异常。"""
    parser = _TextExtractor()
    try:
        parser.feed(html_text)
        parser.close()
    except Exception:  # noqa: BLE001 —— HTMLParser 偶有畸形输入，兜底为已有内容
        pass

    raw = "".join(parser.parts)
    lines = [" ".join(line.split()) for line in raw.splitlines()]
    text = "\n".join(line for line in lines if line)
    title = " ".join("".join(parser.title_parts).split())
    return title, text


class WebScraperTool(HttpToolBase):
    name = "web_scrape"
    description = (
        "抓取一个网页并提取其中的正文文本（自动去掉脚本、样式与标签）。"
        "只支持 http/https 地址。"
    )
    parameters = ToolSchema(
        properties={
            "url": PropertyDef(type="string", description="要抓取的 http(s) 页面地址"),
        },
        required=["url"],
    )

    async def execute(self, url: str = "", **kwargs: Any) -> ToolResult:
        target = (url or "").strip()
        if not target:
            return self._err("url 不能为空")
        scheme = target.split(":", 1)[0].lower() if ":" in target else ""
        if scheme not in ALLOWED_SCHEMES:
            return self._err(f"只允许 http/https 地址（拒绝 {target!r}）")

        outcome = await self._get(target)
        if not outcome.ok:
            return self._err(outcome.error or "抓取失败")

        title, text = extract_text(outcome.text)
        if not text:
            return self._err("未能从页面提取正文（可能是非 HTML 内容，或页面依赖 JS 渲染）")

        body = f"# {title}\n{text}" if title else text
        return self._ok(body, data={"url": target, "title": title, "chars": len(text)})
