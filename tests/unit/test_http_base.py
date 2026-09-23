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


class SequenceClient:
    """按顺序返回预设响应，并记录每次请求的头 —— 用来验证重试与请求头。"""

    def __init__(self, *responses: FakeResponse, exc: Exception | None = None):
        self._responses = list(responses) or [FakeResponse()]
        self._exc = exc
        self.calls = 0
        self.sent_headers: list[dict] = []

    async def request(self, method: str, url: str, **kw):
        self.calls += 1
        self.sent_headers.append(dict(kw.get("headers") or {}))
        if self._exc is not None:
            raise self._exc
        return self._responses[min(self.calls - 1, len(self._responses) - 1)]


# ── 请求头：真实站点会按头把裸请求判成机器人（403） ──


def test_default_headers_identify_a_real_client_with_a_contact_ua() -> None:
    client = SequenceClient()
    asyncio.run(DummyTool(client=client)._get("https://example.test/x"))
    sent = client.sent_headers[0]
    assert "Mozilla" in sent["User-Agent"], sent
    assert "agent-runtime" in sent["User-Agent"], "UA 里要留可联系的项目标识"
    assert sent["Accept"].startswith("text/html"), sent
    assert sent["Accept-Language"].startswith("zh"), sent


def test_a_tool_specific_header_wins_over_the_default() -> None:
    client = SequenceClient()
    asyncio.run(
        DummyTool(client=client)._get(
            "https://example.test/x", headers={"Accept": "application/json"}
        )
    )
    assert client.sent_headers[0]["Accept"] == "application/json"


# ── 重试：只对"对方明确说稍后再试"的响应重试一次 ──


def test_a_transient_429_is_retried_once_and_can_succeed() -> None:
    client = SequenceClient(FakeResponse(429, "slow down"), FakeResponse(200, "ok"))
    outcome = asyncio.run(DummyTool(client=client)._get("https://example.test/x"))
    assert client.calls == 2
    assert outcome.ok is True and outcome.text == "ok"


def test_a_transient_500_is_retried_once() -> None:
    client = SequenceClient(FakeResponse(500, "boom"), FakeResponse(200, "ok"))
    outcome = asyncio.run(DummyTool(client=client)._get("https://example.test/x"))
    assert client.calls == 2
    assert outcome.ok is True


def test_retry_happens_at_most_once_and_says_so() -> None:
    client = SequenceClient(FakeResponse(503, "nope"))
    outcome = asyncio.run(DummyTool(client=client)._get("https://example.test/x"))
    assert client.calls == 2, "最多重试一次（不许无限重试）"
    assert outcome.ok is False
    assert "503" in (outcome.error or "")
    assert "重试" in (outcome.error or ""), "重试过就要说出来，否则排障时看不见"


def test_a_403_is_not_retried() -> None:
    """403 是"对方不打算给你看"：换一次同样的头再问一遍只是白等（也费对方资源）。"""
    client = SequenceClient(FakeResponse(403, "forbidden"))
    outcome = asyncio.run(DummyTool(client=client)._get("https://example.test/x"))
    assert client.calls == 1
    assert outcome.ok is False and "403" in (outcome.error or "")


def test_a_404_is_not_retried() -> None:
    client = SequenceClient(FakeResponse(404, "missing"))
    asyncio.run(DummyTool(client=client)._get("https://example.test/x"))
    assert client.calls == 1


def test_a_transport_error_is_not_retried_inside_the_tool() -> None:
    """超时/连接错误**不**在工具内重试：外层 `asyncio.wait_for(agent_tool_timeout)` 只有 30s，
    工具内 20s 超时再重试一次会被外层掐掉，连可读错误都拿不到（诊断价值归零）。"""
    client = SequenceClient(exc=TimeoutError("模拟超时"))
    outcome = asyncio.run(DummyTool(client=client)._get("https://example.test/x"))
    assert client.calls == 1
    assert outcome.ok is False
    assert "timeout" in (outcome.error or "").lower()


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
    assert "Mozilla" in headers.get("User-Agent", ""), "裸 UA 会被很多站点直接 403"
    assert "agent-runtime" in headers.get("User-Agent", ""), "要留可联系的项目标识"


def test_a_custom_user_agent_wins() -> None:
    client = FakeClient()
    asyncio.run(
        DummyTool(client=client, user_agent="my-bot/1.0")._get("https://example.test/x")
    )
    assert client.last_call is not None
    assert (client.last_call[2].get("headers") or {})["User-Agent"] == "my-bot/1.0"


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
