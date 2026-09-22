"""GitHubTool 契约：4 个独立工具（选择精度 + 参数精度都靠它）。

拆分理由：Benchmark 要统计 Tool Selection Accuracy / Tool Argument Accuracy，
一个带 action 开关的"大工具"会让模型既要选对工具又要选对分支，指标也更糊。
这里 4 个动作 = 4 个 BaseTool，同在一个模块内。

沙箱约束：不 import httpx，client 是鸭子类型，测试用纯标准库假件。
"""
from __future__ import annotations

import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from agent_runtime.core.tool.registry import ToolRegistry
from agent_runtime.infrastructure.tools.github import (
    GitHubGetRepoTool,
    GitHubListDirTool,
    GitHubReadFileTool,
    GitHubSearchCodeTool,
    register_github_tools,
)

REPO_JSON = (
    '{"full_name":"fastapi/fastapi","description":"FastAPI framework",'
    '"language":"Python","stargazers_count":75000,"default_branch":"master",'
    '"topics":["python","api"]}'
)
DIR_JSON = (
    '[{"name":"README.md","type":"file","size":120},'
    '{"name":"docs","type":"dir","size":0}]'
)
CODE_SEARCH_JSON = (
    '{"total_count":1,"items":[{"path":"fastapi/applications.py","repository":'
    '{"full_name":"fastapi/fastapi"}}]}'
)


class FakeResponse:
    def __init__(self, status_code: int = 200, text: str = ""):
        self.status_code = status_code
        self.text = text


class FakeClient:
    """按 URL 路由的假 client；记录每次调用（含 headers）。"""

    def __init__(self, routes: dict[str, FakeResponse], default: FakeResponse | None = None):
        self._routes = routes
        self._default = default or FakeResponse(404, '{"message":"Not Found"}')
        self.calls: list[tuple[str, str, dict]] = []

    async def request(self, method: str, url: str, **kw):
        self.calls.append((method, url, kw))
        for needle, resp in self._routes.items():
            if needle in url:
                return resp
        return self._default

    def header(self, index: int, name: str) -> str | None:
        _, _, kw = self.calls[index]
        return (kw.get("headers") or {}).get(name)


def _file_json(text: str) -> str:
    return (
        '{"type":"file","name":"README.md","path":"README.md","size":%d,'
        '"encoding":"base64","content":"%s"}'
        % (len(text), base64.b64encode(text.encode()).decode())
    )


# ── github_get_repo ──


def test_get_repo_success_returns_readable_summary() -> None:
    client = FakeClient({"api.github.com/repos/fastapi/fastapi": FakeResponse(200, REPO_JSON)})
    result = __import__("asyncio").run(
        GitHubGetRepoTool(client=client).execute(repo="fastapi/fastapi")
    )
    assert result.success is True
    assert "fastapi/fastapi" in result.text
    assert "Python" in result.text
    assert result.data["stargazers_count"] == 75000


def test_get_repo_404_is_readable_failure() -> None:
    client = FakeClient({}, default=FakeResponse(404, '{"message":"Not Found"}'))
    result = __import__("asyncio").run(
        GitHubGetRepoTool(client=client).execute(repo="nope/nope")
    )
    assert result.success is False
    assert "404" in result.text


# ── github_list_dir ──


def test_list_dir_lists_entries() -> None:
    client = FakeClient(
        {"api.github.com/repos/fastapi/fastapi/contents": FakeResponse(200, DIR_JSON)}
    )
    result = __import__("asyncio").run(
        GitHubListDirTool(client=client).execute(repo="fastapi/fastapi", path="")
    )
    assert result.success is True
    assert "README.md" in result.text
    assert "docs" in result.text
    assert len(result.data) == 2


# ── github_read_file ──


def test_read_file_decodes_base64_content() -> None:
    client = FakeClient(
        {"api.github.com/repos/a/b/contents/README.md": FakeResponse(200, _file_json("hello world"))}
    )
    result = __import__("asyncio").run(
        GitHubReadFileTool(client=client).execute(repo="a/b", path="README.md")
    )
    assert result.success is True
    assert "hello world" in result.text
    assert result.data["path"] == "README.md"


def test_read_file_on_directory_gives_readable_error() -> None:
    client = FakeClient(
        {"api.github.com/repos/a/b/contents/docs": FakeResponse(200, DIR_JSON)}
    )
    result = __import__("asyncio").run(
        GitHubReadFileTool(client=client).execute(repo="a/b", path="docs")
    )
    assert result.success is False
    assert "directory" in result.text.lower()


def test_read_file_too_large_has_no_content_field() -> None:
    body = '{"type":"file","name":"big.py","encoding":"none","size":5000000}'
    client = FakeClient({"api.github.com/repos/a/b/contents/big.py": FakeResponse(200, body)})
    result = __import__("asyncio").run(
        GitHubReadFileTool(client=client).execute(repo="a/b", path="big.py")
    )
    assert result.success is False
    assert "5000000" in result.text or "过大" in result.text or "too large" in result.text.lower()


# ── github_search_code ──


def test_search_code_without_token_fails_without_any_request() -> None:
    client = FakeClient({})
    result = __import__("asyncio").run(
        GitHubSearchCodeTool(client=client).execute(query="def create_app")
    )
    assert result.success is False
    assert "token" in result.text.lower()
    assert client.calls == [], "无 token 时不应发起请求（GitHub 代码搜索必然 401）"


def test_search_code_with_token_sends_authorization_and_parses_items() -> None:
    client = FakeClient({"api.github.com/search/code": FakeResponse(200, CODE_SEARCH_JSON)})
    result = __import__("asyncio").run(
        GitHubSearchCodeTool(client=client, token="ghp_test").execute(query="create_app")
    )
    assert result.success is True
    assert "fastapi/applications.py" in result.text
    assert client.header(0, "Authorization") == "Bearer ghp_test"


def test_403_without_token_hints_at_token() -> None:
    client = FakeClient(
        {},
        default=FakeResponse(403, '{"message":"API rate limit exceeded"}'),
    )
    result = __import__("asyncio").run(
        GitHubGetRepoTool(client=client).execute(repo="a/b")
    )
    assert result.success is False
    assert "403" in result.text
    assert "GITHUB_TOKEN" in result.text


def test_read_file_url_encodes_path() -> None:
    """文件名含空格/中文时必须 URL 编码，否则请求 URL 直接坏掉。"""
    client = FakeClient(
        {"api.github.com/repos/a/b/contents": FakeResponse(200, _file_json("hi"))}
    )
    result = __import__("asyncio").run(
        GitHubReadFileTool(client=client).execute(repo="a/b", path="docs/My Notes.md")
    )
    assert result.success is True
    url = client.calls[0][1]
    assert "My%20Notes.md" in url, url
    assert " " not in url, url


# ── 注册与 schema ──


def test_register_github_tools_adds_four_well_formed_tools() -> None:
    registry = ToolRegistry()
    names = register_github_tools(registry, token="")
    assert set(names) == {
        "github_get_repo",
        "github_list_dir",
        "github_read_file",
        "github_search_code",
    }
    assert {t.name for t in registry.list_all()} == set(names)
    for tool in registry.list_all():
        schema = tool.to_openai_schema()
        assert schema["function"]["name"] == tool.name
        assert schema["function"]["parameters"]["required"], tool.name


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
