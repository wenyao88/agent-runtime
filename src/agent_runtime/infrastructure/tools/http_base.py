"""网络类工具的公共基座。

沙箱现实（重要）：本环境没有任何第三方包，连 pip 都没有。因此：
  * 顶层**不** import httpx —— 只在真正要发请求时惰性导入；
  * `client` 可注入（任何提供 `async def request(method, url, **kw)` 的对象），
    测试用纯标准库的假实现，所以这些工具在沙箱内也能 TDD；
  * `_request` **返回** HttpOutcome 而不抛异常 —— "错误一律变成 observation"
    由结构保证，4 个真实工具不必各自写 try/except。
"""
from __future__ import annotations

import json as _json
from dataclasses import dataclass
from typing import Any

from ...core.tool.base import BaseTool, ToolResult
from ._common import truncate_for_context

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (compatible; agent-runtime/0.1; +https://github.com/wenyao88/agent-runtime)"
)
"""默认 UA：既像正常客户端（很多站点对裸 `agent-runtime/0.1` 直接 403），又留了项目地址可联系。

**不要**冒充具体浏览器版本号：那是伪装，且与"工具结果可信"的项目立场相反。
"""

DEFAULT_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}
"""请求头默认值。工具可用同名 header 覆盖（`setdefault` 语义）。"""

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
"""只对"对方明确说稍后再试"的响应重试：429 限流 + 5xx 服务端抖动。

4xx（403/404 等）不重试 —— 同样的请求再问一遍不会变，只是白等。
传输异常（超时/连接失败）也不在这里重试：外层 `agent_tool_timeout` 只有 30s，
工具内 20s 超时再重试一次会被外层掐掉连错误都读不到（见 `tests/unit/test_http_base.py`）。
"""

MAX_ATTEMPTS = 2
"""总尝试次数（首次 + 一次重试）。"""


@dataclass
class HttpOutcome:
    ok: bool
    status: int = 0
    text: str = ""
    error: str | None = None

    def json(self) -> Any:
        """尽力解析 JSON；失败返回 None，由调用方决定如何降级。"""
        try:
            return _json.loads(self.text)
        except (ValueError, TypeError):
            return None


class HttpToolBase(BaseTool):
    def __init__(
        self,
        client: Any = None,
        timeout: float = 20.0,
        max_chars: int = 4000,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        self._client = client
        self._timeout = timeout
        self._max_chars = max_chars
        self._user_agent = user_agent

    # ── 传输 ──

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        import httpx  # 惰性导入：未安装时在 _request 里变成可读错误

        self._client = httpx.AsyncClient(
            timeout=self._timeout,
            headers={"User-Agent": self._user_agent},
            # httpx 默认**不**跟随重定向；GitHub 对 http→https 或改名仓库回 301，
            # 不跟随会让四个 GitHub 工具全部失败（Demo 1 实测踩到）。
            follow_redirects=True,
        )
        return self._client

    async def _get(self, url: str, **kw) -> HttpOutcome:
        return await self._request("GET", url, **kw)

    async def _post(self, url: str, **kw) -> HttpOutcome:
        return await self._request("POST", url, **kw)

    async def _request(self, method: str, url: str, **kw) -> HttpOutcome:
        headers = dict(kw.pop("headers", None) or {})
        headers.setdefault("User-Agent", self._user_agent)
        # 真实站点会按这些头判断"这是不是一个正常客户端"：缺了它们，403 是常态
        for name, value in DEFAULT_HEADERS.items():
            headers.setdefault(name, value)

        try:
            client = self._get_client()
        except ImportError:
            return HttpOutcome(
                ok=False,
                error="httpx 未安装：请先 `pip install httpx`，或给工具注入自定义 client",
            )

        outcome = HttpOutcome(ok=False)
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                resp = await client.request(
                    method, url, headers=headers, timeout=self._timeout, **kw
                )
            except Exception as e:  # noqa: BLE001 —— 网络异常一律归一为失败结果
                name = type(e).__name__
                detail = str(e) or name
                if "Timeout" in name or isinstance(e, TimeoutError):
                    return HttpOutcome(ok=False, error=f"timeout after {self._timeout}s: {detail}")
                return HttpOutcome(ok=False, error=f"{name}: {detail}")

            status = getattr(resp, "status_code", 0) or 0
            text = getattr(resp, "text", "") or ""
            if 200 <= status < 300:
                return HttpOutcome(ok=True, status=status, text=text)
            retried = attempt - 1
            suffix = f"（已重试 {retried} 次）" if retried else ""
            outcome = HttpOutcome(
                ok=False,
                status=status,
                text=text,
                error=f"HTTP {status} for {method} {url}{suffix}",
            )
            if status not in RETRY_STATUSES:
                return outcome
        # 重试用尽：返回最后一次的失败（里面带着"已重试 N 次"，排障时看得见）
        return outcome

    async def aclose(self) -> None:
        client = self._client
        if client is not None and hasattr(client, "aclose"):
            await client.aclose()

    # ── 结果构造 ──

    def _ok(self, text: str, data: Any = None) -> ToolResult:
        out, truncated = truncate_for_context(text, self._max_chars)
        return ToolResult(
            tool_name=self.name,
            success=True,
            text=out,
            data=data,
            metadata={"truncated": truncated},
        )

    def _err(self, message: str) -> ToolResult:
        return ToolResult(
            tool_name=self.name, success=False, text=f"Error: {message}", error=message
        )
