"""WebSearchTool 契约：provider 可切换（默认 DuckDuckGo 免 key，可切 Tavily）。

设计说明：DuckDuckGo 走 Instant Answer JSON 接口而非 HTML 页面 —— 沙箱没有 bs4，
JSON 用标准库即可解析，也少一个依赖。provider 用字典分派，不做类层次（YAGNI）。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from agent_runtime.infrastructure.tools.web_search import WebSearchTool

DDG_JSON = (
    '{"AbstractText":"FastAPI is a modern web framework",'
    '"AbstractURL":"https://fastapi.tiangolo.com",'
    '"RelatedTopics":[{"Text":"FastAPI - Wikipedia",'
    '"FirstURL":"https://en.wikipedia.org/wiki/FastAPI"},'
    '{"Topics":[{"Text":"Tiangolo","FirstURL":"https://tiangolo.com"}]}]}'
)
TAVILY_JSON = (
    '{"results":[{"title":"T1","url":"https://a.example","content":"snippet one"},'
    '{"title":"T2","url":"https://b.example","content":"snippet two"}]}'
)


class FakeResponse:
    def __init__(self, status_code: int = 200, text: str = ""):
        self.status_code = status_code
        self.text = text


class FakeClient:
    def __init__(self, response: FakeResponse | None = None):
        self._response = response or FakeResponse(200, "{}")
        self.calls: list[tuple[str, str, dict]] = []

    async def request(self, method: str, url: str, **kw):
        self.calls.append((method, url, kw))
        return self._response


def test_duckduckgo_normalizes_abstract_and_related_topics() -> None:
    client = FakeClient(FakeResponse(200, DDG_JSON))
    result = asyncio.run(
        WebSearchTool(client=client).execute(query="fastapi", max_results=5)
    )
    assert result.success is True
    urls = [item["url"] for item in result.data]
    assert "https://fastapi.tiangolo.com" in urls
    assert "https://en.wikipedia.org/wiki/FastAPI" in urls
    assert "https://tiangolo.com" in urls, "嵌套 Topics 也要展开"
    assert all({"title", "url", "snippet"} <= set(item) for item in result.data)
    assert "fastapi.tiangolo.com" in result.text


def test_max_results_is_respected() -> None:
    client = FakeClient(FakeResponse(200, DDG_JSON))
    result = asyncio.run(WebSearchTool(client=client).execute(query="fastapi", max_results=1))
    assert len(result.data) == 1


def test_empty_results_is_success_not_error() -> None:
    client = FakeClient(FakeResponse(200, "{}"))
    result = asyncio.run(WebSearchTool(client=client).execute(query="zzzz", max_results=5))
    assert result.success is True
    assert result.data == []
    assert "no results" in result.text.lower() or "无结果" in result.text


def test_tavily_without_key_fails_readably() -> None:
    client = FakeClient()
    result = asyncio.run(
        WebSearchTool(client=client, provider="tavily").execute(query="fastapi", max_results=3)
    )
    assert result.success is False
    assert "key" in result.text.lower()
    assert client.calls == [], "没有 key 时不应发请求"


def test_tavily_with_key_posts_and_normalizes() -> None:
    client = FakeClient(FakeResponse(200, TAVILY_JSON))
    result = asyncio.run(
        WebSearchTool(client=client, provider="tavily", api_key="tvly-x").execute(
            query="fastapi", max_results=3
        )
    )
    assert result.success is True
    assert result.data[0]["url"] == "https://a.example"
    assert result.data[0]["snippet"] == "snippet one"
    method, url, kw = client.calls[0]
    assert method.upper() == "POST"
    assert "tavily" in url
    assert kw["json"]["api_key"] == "tvly-x"


def test_unknown_provider_fails_readably() -> None:
    result = asyncio.run(
        WebSearchTool(client=FakeClient(), provider="bing").execute(query="x")
    )
    assert result.success is False
    assert "bing" in result.text
    assert "duckduckgo" in result.text.lower()


def test_http_failure_is_a_readable_failure() -> None:
    client = FakeClient(FakeResponse(503, "upstream down"))
    result = asyncio.run(WebSearchTool(client=client).execute(query="x"))
    assert result.success is False
    assert "503" in result.text


def test_empty_query_is_rejected() -> None:
    result = asyncio.run(WebSearchTool(client=FakeClient()).execute(query="   "))
    assert result.success is False
    assert "query" in result.text.lower()


def test_schema_declares_query_required_and_bounds() -> None:
    schema = WebSearchTool(client=FakeClient()).to_openai_schema()
    params = schema["function"]["parameters"]
    assert params["required"] == ["query"]
    assert params["properties"]["query"]["type"] == "string"
    assert params["properties"]["max_results"]["type"] == "integer"
    assert params["properties"]["max_results"]["maximum"] == 10


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
