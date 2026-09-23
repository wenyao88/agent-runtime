"""Embedding 客户端契约（OpenAI 兼容 /embeddings）。

本层与原生工具的区别（spec §5.1 的决定）：工具契约是"永不抛"，而 embedder **抛 `EmbeddingError`** ——
因为降级决策属于调用方（`MemoryManager` 已逐层捕获异常），把失败说清楚比悄悄返回空向量更有用。

沙箱适配：注入纯标准库桩 client。**并且本沙箱真的没有 httpx**，所以"未安装 → 可读错误"这条路径
是**真实跑出来的**，不是模拟的。
"""
from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from agent_runtime.core.memory.embedding import BaseEmbedder
from agent_runtime.infrastructure.memory.embedding import (
    EmbeddingError,
    OpenAICompatibleEmbedder,
)


class StubResponse:
    def __init__(self, *, status: int = 200, payload=None, text: str = "") -> None:
        self.status_code = status
        self._payload = payload
        self.text = text if text else json.dumps(payload if payload is not None else {})

    def json(self):
        return self._payload


class StubClient:
    """最小 httpx 鸭子类型：只要 `async def request(method, url, **kw)`。"""

    def __init__(self, *, status: int = 200, payload=None, raises: Exception | None = None,
                 text: str = "") -> None:
        self.status = status
        self.payload = payload
        self.raises = raises
        self.text = text
        self.calls: list[dict] = []
        self.closed = False

    async def request(self, method, url, **kw):
        self.calls.append({"method": method, "url": url, **kw})
        if self.raises:
            raise self.raises
        return StubResponse(status=self.status, payload=self.payload, text=self.text)

    async def aclose(self) -> None:
        self.closed = True


class FakeTimeout(Exception):
    """名字里带 Timeout 的异常，用于覆盖超时分支（无需引入 httpx）。"""


def _embedder(client=None, **kw) -> OpenAICompatibleEmbedder:
    params = {
        "api_key": "sk-secret-value",
        "base_url": "https://api.example.com/v1",
        "model": "BAAI/bge-large-zh-v1.5",
        "timeout": 7.0,
    }
    params.update(kw)
    return OpenAICompatibleEmbedder(client=client, **params)


def _payload(*vectors) -> dict:
    return {"data": [{"embedding": list(v)} for v in vectors]}


def _embed(embedder, texts):
    return asyncio.run(embedder.embed(texts))


# ── 正常路径 ──


def test_is_a_base_embedder() -> None:
    assert isinstance(_embedder(StubClient()), BaseEmbedder)


def test_embed_posts_model_and_input() -> None:
    client = StubClient(payload=_payload([0.1, 0.2]))
    _embed(_embedder(client), ["你好"])
    call = client.calls[0]
    assert call["method"] == "POST"
    assert call["url"].endswith("/embeddings")
    assert call["json"]["model"] == "BAAI/bge-large-zh-v1.5"
    assert call["json"]["input"] == ["你好"]
    assert call["headers"]["Authorization"] == "Bearer sk-secret-value"
    assert call["timeout"] == 7.0


def test_base_url_trailing_slash_is_normalized() -> None:
    client = StubClient(payload=_payload([0.1]))
    _embed(_embedder(client, base_url="https://api.example.com/v1/"), ["x"])
    assert client.calls[0]["url"] == "https://api.example.com/v1/embeddings"


def test_embed_returns_vectors_in_order() -> None:
    client = StubClient(payload=_payload([1.0, 2.0], [3.0, 4.0]))
    assert _embed(_embedder(client), ["a", "b"]) == [[1.0, 2.0], [3.0, 4.0]]


def test_embed_empty_input_makes_no_request() -> None:
    client = StubClient(payload=_payload())
    assert _embed(_embedder(client), []) == []
    assert client.calls == []


# ── 失败路径：一律 EmbeddingError ──


def test_missing_api_key_raises_before_requesting() -> None:
    client = StubClient(payload=_payload())
    try:
        _embed(_embedder(client, api_key=""), ["x"])
    except EmbeddingError as e:
        assert "EMBEDDING_API_KEY" in str(e)
    else:
        raise AssertionError("缺 key 必须报可读错误")
    assert client.calls == [], "缺 key 时不该发请求"


def test_http_error_status_raises() -> None:
    client = StubClient(status=503, payload={})
    try:
        _embed(_embedder(client), ["x"])
    except EmbeddingError as e:
        assert "503" in str(e)
    else:
        raise AssertionError("非 2xx 必须报错")


def test_timeout_raises_readable_error() -> None:
    client = StubClient(raises=FakeTimeout("timed out"))
    try:
        _embed(_embedder(client), ["x"])
    except EmbeddingError as e:
        assert "timeout" in str(e).lower()
    else:
        raise AssertionError("超时必须报错")


def test_count_mismatch_raises() -> None:
    client = StubClient(payload=_payload([0.1], [0.2]))
    try:
        _embed(_embedder(client), ["only one"])
    except EmbeddingError as e:
        assert "条数" in str(e)
    else:
        raise AssertionError("返回条数与输入不符必须报错")


def test_malformed_body_raises() -> None:
    client = StubClient(payload={"data": [{"no_embedding": True}]})
    try:
        _embed(_embedder(client), ["x"])
    except EmbeddingError:
        pass
    else:
        raise AssertionError("缺 embedding 字段必须报错")


def test_non_numeric_vector_raises() -> None:
    client = StubClient(payload={"data": [{"embedding": ["abc"]}]})
    try:
        _embed(_embedder(client), ["x"])
    except EmbeddingError as e:
        assert "数值" in str(e)
    else:
        raise AssertionError("向量元素非数值必须报错")


def test_error_message_never_leaks_api_key() -> None:
    """密钥绝不进错误信息 —— 错误会被写进日志与 trace。

    注意必须**断言确实抛了错**：否则实现一旦退化成"静默返回空向量"，这条用例会因为
    "没有错误信息可以检查"而假绿（Phase 4 审查指出的弱断言）。
    """
    raised = 0
    for client in (
        StubClient(status=500, payload={}),
        StubClient(raises=FakeTimeout("boom")),
        StubClient(payload=_payload([0.1], [0.2])),
    ):
        try:
            _embed(_embedder(client), ["x"])
        except EmbeddingError as e:
            raised += 1
            assert "sk-secret-value" not in str(e), str(e)
    assert raised == 3, "三条失败路径都必须抛 EmbeddingError（否则本用例什么都没验证）"


def test_missing_httpx_gives_readable_error() -> None:
    """真实路径（本沙箱确实没装 httpx）：惰性导入失败要变成可读错误，而不是 ImportError 栈。

    装了 httpx 的环境（用户机器）必须**跳过**而不是 `return` —— 后者会打印 PASS 但什么都没断言，
    等于"测试在撒谎"（Phase 4 审查指出的弱断言）。
    """
    try:
        import httpx  # noqa: F401
    except ImportError:
        pass
    else:
        raise unittest.SkipTest("httpx 已安装：该用例只在缺依赖时有意义")
    try:
        _embed(_embedder(None), ["x"])
    except EmbeddingError as e:
        assert "httpx" in str(e)
    else:
        raise AssertionError("缺 httpx 必须给可读错误")


def test_aclose_closes_injected_client() -> None:
    client = StubClient(payload=_payload())
    embedder = _embedder(client)
    asyncio.run(embedder.aclose())
    assert client.closed is True


def _run_all() -> None:
    failed = []
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"RUN  {name}")
            try:
                fn()
            except unittest.SkipTest as e:
                print(f"SKIP {name}: {e}")
            except Exception as e:  # noqa: BLE001
                print(f"FAIL {name}: {type(e).__name__}: {e}")
                failed.append(name)
            else:
                print(f"PASS {name}")
    if failed:
        raise SystemExit(f"FAILED: {', '.join(failed)}")
    print("ALL PASS")


if __name__ == "__main__":
    _run_all()
