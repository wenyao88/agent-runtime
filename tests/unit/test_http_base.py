"""HttpToolBase 契约：网络类工具的公共基座。

设计约束（沙箱现实）：本环境**没有任何第三方包**（httpx 也没有），因此
  * 该模块不得在顶层 import httpx —— 只在真正需要时惰性导入；
  * 测试只依赖标准库的假 client（FakeClient），所以沙箱内可 TDD；
  * _request 返回 HttpOutcome 而不是抛异常 —— 4 个真实工具不必各自 try/except，
    "错误一律变成 observation" 由基座结构性地保证。
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from agent_runtime.infrastructure.tools.http_base import HttpOutcome, HttpToolBase


class FakeResponse:
    def __init__(self, status_code: int = 200, text: str = "hello"):
        self.status_code = status_code
        self.text = text
        self.headers: dict[str, str] = {}


class FakeClient:
    """记录最后一次请求；可按需抛异常或返回预设响应。"""

    def __init__(self, response: FakeResponse | None = None, exc: Exception | None = None):
        self._response = response or FakeResponse()
        self._exc = exc
        self.last_call: tuple[str, str, dict] | None = None
        self.calls = 0

    async def request(self, method: str, url: str, **kw):
        self.calls += 1
        self.last_call = (method, url, kw)
        if self._exc is not None:
            raise self._exc
        return self._response


class DummyTool(HttpToolBase):
    name = "dummy"
    description = "只用来测基座"

    async def execute(self, **kwargs):
        outcome = await self._get("https://example.test/x")
        if not outcome.ok:
            return self._err(outcome.error or "unknown")
        return self._ok(outcome.text)


def test_ok_response_returns_success_outcome() -> None:
    outcome = asyncio.run(DummyTool(client=FakeClient())._get("https://example.test/x"))
    assert isinstance(outcome, HttpOutcome)
    assert outcome.ok is True
    assert outcome.status == 200
    assert outcome.text == "hello"
    assert outcome.error is None


def test_client_receives_method_url_and_user_agent() -> None:
    client = FakeClient()
    asyncio.run(DummyTool(client=client)._get("https://example.test/x"))
    assert client.last_call is not None
    method, url, kw = client.last_call
    assert method.upper() == "GET"
    assert url == "https://example.test/x"
    headers = kw.get("headers") or {}
    assert headers.get("User-Agent") == "agent-runtime/0.1"


def test_404_becomes_failure_outcome() -> None:
    outcome = asyncio.run(
        DummyTool(client=FakeClient(FakeResponse(404, "nope")))._get("https://example.test/x")
    )
    assert outcome.ok is False
    assert outcome.status == 404
    assert "404" in (outcome.error or "")


def test_500_becomes_failure_outcome() -> None:
    outcome = asyncio.run(
        DummyTool(client=FakeClient(FakeResponse(500, "boom")))._get("https://example.test/x")
    )
    assert outcome.ok is False
    assert "500" in (outcome.error or "")


def test_exception_becomes_failure_outcome_not_raised() -> None:
    outcome = asyncio.run(
        DummyTool(client=FakeClient(exc=RuntimeError("connection reset")))._get(
            "https://example.test/x"
        )
    )
    assert outcome.ok is False
    assert "connection reset" in (outcome.error or "")


def test_timeout_exception_is_reported_as_timeout() -> None:
    outcome = asyncio.run(
        DummyTool(client=FakeClient(exc=TimeoutError()))._get("https://example.test/x")
    )
    assert outcome.ok is False
    assert "timeout" in (outcome.error or "").lower()


def test_ok_truncates_long_text_and_flags_metadata() -> None:
    tool = DummyTool(client=FakeClient(), max_chars=10)
    result = tool._ok("x" * 50)
    assert result.success is True
    assert len(result.text) <= 10 + len("\n...[truncated]")
    assert result.text.endswith("[truncated]")
    assert result.metadata.get("truncated") is True


def test_ok_keeps_short_text_intact() -> None:
    result = DummyTool(client=FakeClient(), max_chars=100)._ok("short")
    assert result.text == "short"
    assert result.metadata.get("truncated") is False


def test_err_builds_failed_tool_result() -> None:
    result = DummyTool(client=FakeClient())._err("no token")
    assert result.success is False
    assert result.text.startswith("Error: ")
    assert "no token" in result.text
    assert result.tool_name == "dummy"


def test_execute_never_raises_on_network_failure() -> None:
    tool = DummyTool(client=FakeClient(exc=RuntimeError("dns fail")))
    result = asyncio.run(tool.execute())
    assert result.success is False
    assert "dns fail" in result.text


def test_missing_httpx_is_reported_clearly() -> None:
    """没有注入 client 且环境无 httpx 时，必须给出可读错误而不是 ImportError。"""
    if importlib.util.find_spec("httpx") is not None:
        print("     (httpx installed — 跳过该分支)")
        return
    outcome = asyncio.run(DummyTool()._get("https://example.test/x"))
    assert outcome.ok is False
    assert "httpx" in (outcome.error or "")


def test_default_client_follows_redirects() -> None:
    """httpx 默认**不**跟随重定向；GitHub 对 http→https 或改名仓库回 301，
    四个 GitHub 工具会因此全部失败。用桩 httpx 模块锁住这个构造契约。"""
    import sys
    import types

    recorded: dict = {}

    class StubAsyncClient:
        def __init__(self, **kw):
            recorded.update(kw)

    stub = types.ModuleType("httpx")
    stub.AsyncClient = StubAsyncClient  # type: ignore[attr-defined]
    original = sys.modules.get("httpx")
    sys.modules["httpx"] = stub
    try:
        tool = DummyTool()  # 不注入 client → 走默认构造
        client = tool._get_client()
        assert isinstance(client, StubAsyncClient)
        assert recorded.get("follow_redirects") is True, recorded
    finally:
        if original is None:
            sys.modules.pop("httpx", None)
        else:
            sys.modules["httpx"] = original


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
