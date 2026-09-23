"""原生工具装配与工具目录（纯逻辑、零第三方依赖）。

为什么放在 infrastructure 而不是 api/deps.py：deps 依赖 pydantic_settings（本环境装不上），
逻辑一旦放那儿，沙箱内就完全无法测试装配正确性。这里全是标准库，可被直接验证。
"""
from __future__ import annotations

from typing import Any

from ...core.tool.registry import ToolRegistry
from .file_reader import FileReaderTool
from .github import register_github_tools
from .pdf_reader import PDFReaderTool
from .web_scraper import WebScraperTool
from .web_search import (
    DEFAULT_SEARCH_PROVIDER,
    SUPPORTED_SEARCH_PROVIDERS,
    WebSearchTool,
)

NATIVE_TOOL_NAMES = (
    "read_file",
    "github_get_repo",
    "github_list_dir",
    "github_read_file",
    "github_search_code",
    "web_search",
    "web_scrape",
    "pdf_read",
)

DEFAULT_HTTP_TIMEOUT = 20.0
DEFAULT_MAX_CHARS = 4000


def search_config_errors(settings: Any) -> list[str]:
    """搜索配置的**致命**错误：这样配下去，每次 `web_search` 都必然失败。

    真实全量实测（2026-09-23）：`WEB_SEARCH_PROVIDER=tavily` 而没填 key 时，
    研究类任务会一路重试到 `max_steps` 才失败 —— 每条白烧 15 轮 LLM 调用。
    所以真实评测**开跑前**就拒绝（mock 不受影响：夹具不调用工具）。

    返回空列表 = 没问题（`duckduckgo` 免 key，是默认值）。
    """
    raw = str(getattr(settings, "web_search_provider", "") or "").strip().lower()
    provider = raw or DEFAULT_SEARCH_PROVIDER
    if provider not in SUPPORTED_SEARCH_PROVIDERS:
        return [
            f"未知的搜索 provider：{provider!r}；可选：{', '.join(SUPPORTED_SEARCH_PROVIDERS)}"
            "（WEB_SEARCH_PROVIDER 拼错会让每次搜索都失败）"
        ]
    if provider == "tavily" and not str(
        getattr(settings, "web_search_api_key", "") or ""
    ).strip():
        return [
            "WEB_SEARCH_PROVIDER=tavily 但没有 WEB_SEARCH_API_KEY："
            "每次搜索都会失败，研究类任务会白跑到 max_steps。"
            "请填 key，或把 WEB_SEARCH_PROVIDER 换回 duckduckgo"
        ]
    return []


def register_native_tools(
    registry: ToolRegistry,
    *,
    root: str,
    github_token: str = "",
    github_base_url: str | None = None,
    web_search_provider: str = "duckduckgo",
    web_search_api_key: str = "",
    http_timeout: float = DEFAULT_HTTP_TIMEOUT,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> list[str]:
    """注册全部原生工具，返回工具名列表。幂等：重复调用不会产生重复项。"""
    http_kw: dict[str, Any] = {"timeout": http_timeout, "max_chars": max_chars}

    registry.register(FileReaderTool(root=root))
    registry.register(
        WebSearchTool(
            provider=web_search_provider, api_key=web_search_api_key, **http_kw
        )
    )
    registry.register(WebScraperTool(**http_kw))
    registry.register(PDFReaderTool(root=root, max_chars=max_chars))

    github_kw: dict[str, Any] = {"token": github_token, **http_kw}
    if github_base_url:
        github_kw["base_url"] = github_base_url
    register_github_tools(registry, **github_kw)

    return list(NATIVE_TOOL_NAMES)


def tool_catalog(registry: ToolRegistry) -> list[dict[str, Any]]:
    """给 GET /api/tools 用的目录：名称/描述/OpenAI 参数 schema，按名称排序。

    原生工具与 MCP 工具在这里形态完全一致 —— 上层（含模型）不感知工具来源。
    """
    entries: list[dict[str, Any]] = []
    for tool in registry.list_all():
        schema = tool.to_openai_schema()
        entries.append(
            {
                "name": tool.name,
                "description": tool.description,
                "parameters": schema["function"]["parameters"],
            }
        )
    entries.sort(key=lambda entry: entry["name"])
    return entries
