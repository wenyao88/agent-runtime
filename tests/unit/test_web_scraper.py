"""WebScraperTool 契约：网页 → 正文文本。

沙箱没有 bs4，因此正文提取用标准库 html.parser 自建：
  * 跳过 script/style/noscript/svg/template（否则代码会被当成正文喂给模型）；
  * <title> 单独抽出做标题；
  * 块级标签转换为换行，再折叠空白。

安全边界：只允许 http/https，file:// 等一律拒绝且不发请求。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from agent_runtime.infrastructure.tools.web_scraper import WebScraperTool

PAGE = """<html><head><title>示例页面</title>
<style>body{color:red}</style>
<script>function evil(){alert('x')}</script>
</head><body>
<h1>标题</h1>
<p>第一段   文本，含   多余空白。</p>
<ul><li>条目一</li><li>条目二</li></ul>
</body></html>"""

SCRIPT_ONLY = "<html><head><script>var x=1;</script></head><body></body></html>"


class FakeResponse:
    def __init__(self, status_code: int = 200, text: str = ""):
        self.status_code = status_code
        self.text = text


class FakeClient:
    def __init__(self, response: FakeResponse | None = None):
        self._response = response or FakeResponse(200, PAGE)
        self.calls: list[tuple[str, str, dict]] = []

    async def request(self, method: str, url: str, **kw):
        self.calls.append((method, url, kw))
        return self._response


def test_extracts_title_and_body() -> None:
    result = asyncio.run(WebScraperTool(client=FakeClient()).execute(url="https://e.test/p"))
    assert result.success is True
    assert "示例页面" in result.text
    assert "标题" in result.text
    assert "条目一" in result.text and "条目二" in result.text
    assert result.data["title"] == "示例页面"


def test_script_and_style_are_stripped() -> None:
    result = asyncio.run(WebScraperTool(client=FakeClient()).execute(url="https://e.test/p"))
    assert "evil" not in result.text
    assert "alert" not in result.text
    assert "color:red" not in result.text


def test_whitespace_is_collapsed() -> None:
    result = asyncio.run(WebScraperTool(client=FakeClient()).execute(url="https://e.test/p"))
    assert "第一段 文本，含 多余空白。" in result.text
    assert "  " not in result.text
    assert "\n\n\n" not in result.text


def test_rejects_non_http_scheme_without_request() -> None:
    client = FakeClient()
    result = asyncio.run(WebScraperTool(client=client).execute(url="file:///etc/passwd"))
    assert result.success is False
    assert "http" in result.text.lower()
    assert client.calls == [], "非法 scheme 不应发起请求"


def test_rejects_empty_url() -> None:
    result = asyncio.run(WebScraperTool(client=FakeClient()).execute(url="   "))
    assert result.success is False
    assert "url" in result.text.lower()


def test_http_error_is_readable() -> None:
    client = FakeClient(FakeResponse(404, "not found"))
    result = asyncio.run(WebScraperTool(client=client).execute(url="https://e.test/missing"))
    assert result.success is False
    assert "404" in result.text


def test_page_without_visible_text_gives_readable_error() -> None:
    client = FakeClient(FakeResponse(200, SCRIPT_ONLY))
    result = asyncio.run(WebScraperTool(client=client).execute(url="https://e.test/js"))
    assert result.success is False
    assert "正文" in result.text or "text" in result.text.lower()


def test_truncation_respects_configured_max_chars() -> None:
    long_page = "<html><body><p>" + ("a" * 2000) + "</p></body></html>"
    client = FakeClient(FakeResponse(200, long_page))
    result = asyncio.run(
        WebScraperTool(client=client, max_chars=200).execute(url="https://e.test/long")
    )
    assert result.success is True
    assert result.metadata["truncated"] is True
    assert len(result.text) <= 200 + len("\n...[truncated]")


def test_schema_requires_url_only() -> None:
    params = WebScraperTool(client=FakeClient()).to_openai_schema()["function"]["parameters"]
    assert params["required"] == ["url"]
    assert set(params["properties"]) == {"url"}


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
