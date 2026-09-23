"""网页搜索工具：provider 可切换（默认 DuckDuckGo 免 key，可切 Tavily）。

为什么 DuckDuckGo 走 Instant Answer JSON 接口而不是 HTML 页面：沙箱没有 bs4，
JSON 用标准库就能解析，同时少一个依赖。provider 用字典分派而不是类层次（YAGNI），
但保持了"换 provider 不改工具主体"的可替换性。
"""
from __future__ import annotations

from typing import Any

from ...core.tool.base import PropertyDef, ToolResult, ToolSchema
from .http_base import HttpToolBase

DDG_ENDPOINT = "https://api.duckduckgo.com/"
TAVILY_ENDPOINT = "https://api.tavily.com/search"
DEFAULT_PROVIDER = "duckduckgo"


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def _norm_duckduckgo(payload: Any, max_results: int) -> list[dict]:
    items: list[dict] = []
    if not isinstance(payload, dict):
        return items

    abstract = _clean(payload.get("AbstractText"))
    abstract_url = _clean(payload.get("AbstractURL"))
    if abstract and abstract_url:
        items.append({"title": abstract[:120], "url": abstract_url, "snippet": abstract})

    def walk(topics: Any) -> None:
        if not isinstance(topics, list):
            return
        for topic in topics:
            if not isinstance(topic, dict):
                continue
            url = _clean(topic.get("FirstURL"))
            text = _clean(topic.get("Text"))
            if url and text:
                items.append({"title": text[:120], "url": url, "snippet": text})
            if topic.get("Topics"):
                walk(topic["Topics"])

    walk(payload.get("RelatedTopics"))
    return items[:max_results]


def _norm_tavily(payload: Any, max_results: int) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    results = payload.get("results")
    if not isinstance(results, list):
        return []
    items: list[dict] = []
    for row in results:
        if not isinstance(row, dict):
            continue
        items.append(
            {
                "title": _clean(row.get("title")),
                "url": _clean(row.get("url")),
                "snippet": _clean(row.get("content")),
            }
        )
    return items[:max_results]


async def _run_duckduckgo(tool: "WebSearchTool", query: str, max_results: int):
    outcome = await tool._get(
        DDG_ENDPOINT,
        params={"q": query, "format": "json", "no_html": 1, "skip_disambig": 1},
    )
    if not outcome.ok:
        return None, outcome.error or "DuckDuckGo 请求失败"
    return _norm_duckduckgo(outcome.json(), max_results), None


async def _run_tavily(tool: "WebSearchTool", query: str, max_results: int):
    if not tool.api_key:
        return None, (
            "Tavily 需要 API key：请在 .env 配置 WEB_SEARCH_API_KEY"
            "（未配置时不发起请求）"
        )
    outcome = await tool._post(
        TAVILY_ENDPOINT,
        json={
            "api_key": tool.api_key,
            "query": query,
            "max_results": max_results,
            "search_depth": "basic",
        },
    )
    if not outcome.ok:
        return None, outcome.error or "Tavily 请求失败"
    return _norm_tavily(outcome.json(), max_results), None


_RUNNERS = {"duckduckgo": _run_duckduckgo, "tavily": _run_tavily}

SUPPORTED_SEARCH_PROVIDERS: tuple[str, ...] = tuple(sorted(_RUNNERS))
"""可选 provider（启动校验与错误信息共用同一份名单，避免两处漂移）。"""

DEFAULT_SEARCH_PROVIDER = DEFAULT_PROVIDER
"""默认 provider 的公开别名：空值走它（唯一口径，不在别处硬编码 "duckduckgo"）。"""


class WebSearchTool(HttpToolBase):
    name = "web_search"
    description = (
        "搜索互联网，返回标题/链接/摘要列表。默认使用 DuckDuckGo 的 Instant Answer 接口："
        "免 key，但只覆盖实体/主题摘要，**不是通用网页搜索**（冷门主题可能返回空）；"
        "把 WEB_SEARCH_PROVIDER 配成 tavily 并填 key 后可获得通用网页结果。"
    )
    parameters = ToolSchema(
        properties={
            "query": PropertyDef(type="string", description="搜索关键词"),
            "max_results": PropertyDef(
                type="integer", description="最多返回条数", default=5, minimum=1, maximum=10
            ),
        },
        required=["query"],
    )

    def __init__(self, provider: str = DEFAULT_PROVIDER, api_key: str = "", **kw: Any) -> None:
        super().__init__(**kw)
        self.provider = (provider or DEFAULT_PROVIDER).strip().lower()
        self.api_key = api_key or ""

    async def execute(self, query: str = "", max_results: int = 5, **kwargs: Any) -> ToolResult:
        if not query.strip():
            return self._err("query 不能为空")
        runner = _RUNNERS.get(self.provider)
        if runner is None:
            return self._err(
                f"未知的搜索 provider：{self.provider!r}；可选：{', '.join(sorted(_RUNNERS))}"
            )

        raw = 5 if max_results is None else max_results
        try:
            requested = int(raw)
        except (TypeError, ValueError):
            return self._err(f"max_results 必须是整数，收到 {max_results!r}")
        top_k = max(1, min(requested, 10))
        items, error = await runner(self, query.strip(), top_k)
        if error:
            return self._err(error)
        items = items or []
        if not items:
            return self._ok("(no results) 无结果", data=[])

        lines = [
            f"{i}. {item['title']}\n   {item['url']}\n   {item['snippet']}"
            for i, item in enumerate(items, 1)
        ]
        return self._ok("\n".join(lines), data=items)
