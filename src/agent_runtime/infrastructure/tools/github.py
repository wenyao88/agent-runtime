"""GitHub 工具集：4 个独立工具。

为什么拆成 4 个而不是 1 个带 action 开关的工具：Benchmark 要统计
Tool Selection Accuracy / Tool Argument Accuracy —— 模型"选对工具"和"选对分支"
是两件事，拆开才能度量清楚。

沙箱约束（见 http_base）：不 import httpx，client 鸭子类型可注入，测试零网络。
"""
from __future__ import annotations

import base64
import binascii
from typing import Any

from ...core.tool.base import PropertyDef, ToolResult, ToolSchema
from ...core.tool.registry import ToolRegistry
from .http_base import HttpOutcome, HttpToolBase

DEFAULT_BASE_URL = "https://api.github.com"
API_VERSION = "2022-11-28"


class _GitHubToolBase(HttpToolBase):
    def __init__(
        self,
        token: str = "",
        base_url: str = DEFAULT_BASE_URL,
        **kw: Any,
    ) -> None:
        super().__init__(**kw)
        self._token = token or ""
        self._base_url = base_url.rstrip("/")

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    async def _github(self, path: str, **kw: Any) -> HttpOutcome:
        outcome = await self._get(f"{self._base_url}{path}", headers=self._headers(), **kw)
        if not outcome.ok and outcome.status in (401, 403) and not self._token:
            return HttpOutcome(
                ok=False,
                status=outcome.status,
                text=outcome.text,
                error=f"{outcome.error} —— 该接口可能需要鉴权，请在 .env 配置 GITHUB_TOKEN",
            )
        return outcome

    @staticmethod
    def _repo_path(repo: str) -> str:
        return repo.strip().strip("/")


class GitHubGetRepoTool(_GitHubToolBase):
    name = "github_get_repo"
    description = "获取 GitHub 仓库的基本信息：描述、主语言、star 数、默认分支、topics。"
    parameters = ToolSchema(
        properties={
            "repo": PropertyDef(type="string", description="仓库全名，如 fastapi/fastapi"),
        },
        required=["repo"],
    )

    async def execute(self, repo: str = "", **kwargs: Any) -> ToolResult:
        if not self._repo_path(repo):
            return self._err("repo 不能为空，格式为 owner/name")
        outcome = await self._github(f"/repos/{self._repo_path(repo)}")
        if not outcome.ok:
            return self._err(outcome.error or "GitHub 请求失败")
        data = outcome.json() or {}
        if not isinstance(data, dict):
            return self._err("GitHub 返回了无法解析的仓库信息")
        lines = [
            f"repo: {data.get('full_name', '')}",
            f"description: {data.get('description') or '(none)'}",
            f"language: {data.get('language') or '(unknown)'}",
            f"stars: {data.get('stargazers_count', 0)}",
            f"default_branch: {data.get('default_branch', '')}",
            f"topics: {', '.join(data.get('topics') or []) or '(none)'}",
        ]
        return self._ok("\n".join(lines), data=data)


class GitHubListDirTool(_GitHubToolBase):
    name = "github_list_dir"
    description = "列出 GitHub 仓库中某个目录下的文件与子目录；path 传空字符串表示仓库根目录。"
    parameters = ToolSchema(
        properties={
            "repo": PropertyDef(type="string", description="仓库全名，如 fastapi/fastapi"),
            "path": PropertyDef(type="string", description="目录路径，根目录传空字符串"),
            "ref": PropertyDef(type="string", description="分支/标签/commit，默认仓库默认分支"),
        },
        required=["repo"],
    )

    async def execute(self, repo: str = "", path: str = "", ref: str = "", **kwargs: Any) -> ToolResult:
        if not self._repo_path(repo):
            return self._err("repo 不能为空，格式为 owner/name")
        url_path = f"/repos/{self._repo_path(repo)}/contents/{path.strip().strip('/')}"
        outcome = await self._github(url_path, params={"ref": ref} if ref else None)
        if not outcome.ok:
            return self._err(outcome.error or "GitHub 请求失败")
        payload = outcome.json()
        if not isinstance(payload, list):
            hint = "（这是一个文件，请用 github_read_file）" if isinstance(payload, dict) else ""
            return self._err(f"path 不是目录（not a directory）：{path!r}{hint}")
        entries = [
            {
                "name": item.get("name", ""),
                "type": item.get("type", ""),
                "size": item.get("size", 0),
            }
            for item in payload
            if isinstance(item, dict)
        ]
        lines = [f"{e['type']:<4} {e['size']:>8} {e['name']}" for e in entries] or ["(empty)"]
        return self._ok("\n".join(lines), data=entries)


class GitHubReadFileTool(_GitHubToolBase):
    name = "github_read_file"
    description = "读取 GitHub 仓库中单个文本文件的内容。超大文件会被 GitHub 拒绝，此时返回可读错误。"
    parameters = ToolSchema(
        properties={
            "repo": PropertyDef(type="string", description="仓库全名，如 fastapi/fastapi"),
            "path": PropertyDef(type="string", description="文件路径，如 README.md"),
            "ref": PropertyDef(type="string", description="分支/标签/commit，默认仓库默认分支"),
        },
        required=["repo", "path"],
    )

    async def execute(self, repo: str = "", path: str = "", ref: str = "", **kwargs: Any) -> ToolResult:
        if not self._repo_path(repo) or not path.strip():
            return self._err("repo 与 path 都不能为空")
        url_path = f"/repos/{self._repo_path(repo)}/contents/{path.strip().strip('/')}"
        outcome = await self._github(url_path, params={"ref": ref} if ref else None)
        if not outcome.ok:
            return self._err(outcome.error or "GitHub 请求失败")
        payload = outcome.json()
        if isinstance(payload, list):
            return self._err(
                f"path 是目录（directory）而不是文件：{path!r} —— 请改用 github_list_dir"
            )
        if not isinstance(payload, dict):
            return self._err("GitHub 返回了无法解析的文件内容")
        if payload.get("encoding") != "base64" or not payload.get("content"):
            return self._err(
                f"文件过大或不可内联读取（size={payload.get('size', '?')} bytes）"
                "—— 请指定更小的文件，或改用 raw 链接"
            )
        try:
            text = base64.b64decode(payload["content"]).decode("utf-8", errors="replace")
        except (binascii.Error, ValueError) as e:
            return self._err(f"base64 解码失败：{e}")
        return self._ok(text, data={"path": path, "chars": len(text)})


class GitHubSearchCodeTool(_GitHubToolBase):
    name = "github_search_code"
    description = (
        "在 GitHub 上搜索代码。需要 GITHUB_TOKEN；未配置时直接返回可读错误（该接口未鉴权必然 401）。"
    )
    parameters = ToolSchema(
        properties={
            "query": PropertyDef(
                type="string",
                description="搜索表达式，如 'create_app repo:fastapi/fastapi'",
            ),
            "max_results": PropertyDef(
                type="integer", description="最多返回条数", default=10, minimum=1, maximum=50
            ),
        },
        required=["query"],
    )

    async def execute(self, query: str = "", max_results: int = 10, **kwargs: Any) -> ToolResult:
        if not self._token:
            return self._err(
                "GitHub 代码搜索需要 token：请在 .env 配置 GITHUB_TOKEN"
                "（未配置时不发起请求，因为必然 401）"
            )
        if not query.strip():
            return self._err("query 不能为空")
        outcome = await self._github("/search/code", params={"q": query, "per_page": max_results})
        if not outcome.ok:
            return self._err(outcome.error or "GitHub 请求失败")
        payload = outcome.json() or {}
        rows = [
            {
                "path": item.get("path", ""),
                "repo": (item.get("repository") or {}).get("full_name", ""),
            }
            for item in (payload.get("items") or [])
            if isinstance(item, dict)
        ]
        lines = [f"{r['repo']}:{r['path']}" for r in rows] or ["(no results)"]
        return self._ok(
            f"total_count={payload.get('total_count', 0)}\n" + "\n".join(lines), data=rows
        )


def register_github_tools(
    registry: ToolRegistry,
    token: str = "",
    client: Any = None,
    base_url: str = DEFAULT_BASE_URL,
    **http_kw: Any,
) -> list[str]:
    """把 4 个 GitHub 工具注册进 registry，返回工具名列表。

    `http_kw`（timeout / max_chars 等）原样透传给每个工具 —— 装配层需要能统一配置超时与截断。
    """
    tools = [
        GitHubGetRepoTool(token=token, base_url=base_url, client=client, **http_kw),
        GitHubListDirTool(token=token, base_url=base_url, client=client, **http_kw),
        GitHubReadFileTool(token=token, base_url=base_url, client=client, **http_kw),
        GitHubSearchCodeTool(token=token, base_url=base_url, client=client, **http_kw),
    ]
    for tool in tools:
        registry.register(tool)
    return [t.name for t in tools]
