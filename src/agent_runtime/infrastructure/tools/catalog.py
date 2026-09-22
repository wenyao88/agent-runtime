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
from .web_search import WebSearchTool

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
