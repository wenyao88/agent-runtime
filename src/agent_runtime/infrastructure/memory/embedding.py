"""OpenAI 兼容 Embedding 实现（惰性 httpx + 可注入 client）。

与原生工具的差异（有意为之，spec §5.1）：工具契约是"永不抛异常"，因为它要变成 observation；
而 embedder **抛 `EmbeddingError`** —— 降级决策属于调用方（`MemoryManager` 已逐层捕获），
把失败说清楚比悄悄返回空向量强得多（空向量会污染向量检索结果）。

密钥安全：`Authorization` 头只在本文件内拼装，**绝不写进任何错误信息**（错误会进日志与 trace）。
"""
from __future__ import annotations

import json as _json
from typing import Any

from ...core.memory.embedding import BaseEmbedder


class EmbeddingError(Exception):
    """embedding 调用失败（缺 key / 缺 httpx / 网络错误 / 响应格式不符）。"""


def _describe(exc: Exception, timeout: float) -> str:
    name = type(exc).__name__
    detail = str(exc) or name
    if "Timeout" in name or isinstance(exc, TimeoutError):
        return f"embedding 请求 timeout after {timeout}s: {detail}"
    return f"{name}: {detail}"


class OpenAICompatibleEmbedder(BaseEmbedder):
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        timeout: float = 20.0,
        client: Any = None,
    ) -> None:
        self._api_key = api_key or ""
        self._base_url = (base_url or "").rstrip("/")
        self._model = model or ""
        self._timeout = timeout
        self._client = client

    # ── 传输 ──

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        import httpx  # 惰性导入：未安装时在 embed 里变成可读错误

        self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def aclose(self) -> None:
        client = self._client
        if client is not None and hasattr(client, "aclose"):
            await client.aclose()

    # ── 主流程 ──

    async def embed(self, texts: list[str]) -> list[list[float]]:
        items = list(texts or [])
        if not items:
            return []
        if not self._api_key:
            raise EmbeddingError(
                "缺少 EMBEDDING_API_KEY：无法调用 embedding 接口（请在 .env 中配置）"
            )

        try:
            client = self._get_client()
        except ImportError:
            raise EmbeddingError(
                "httpx 未安装：请先 `pip install httpx`，或给 embedder 注入自定义 client"
            ) from None

        url = f"{self._base_url}/embeddings"
        try:
            resp = await client.request(
                "POST",
                url,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json={"model": self._model, "input": items},
                timeout=self._timeout,
            )
        except Exception as e:  # noqa: BLE001 —— 归一为可读错误（不含密钥）
            raise EmbeddingError(_describe(e, self._timeout)) from None

        status = getattr(resp, "status_code", 0) or 0
        if not (200 <= status < 300):
            raise EmbeddingError(f"embedding 接口返回 HTTP {status}")
        return _extract_vectors(resp, expected=len(items))


def _extract_vectors(resp: Any, *, expected: int) -> list[list[float]]:
    payload: Any = None
    try:
        payload = resp.json()
    except Exception:  # noqa: BLE001 —— 有些实现没有 .json()，退化为解析文本
        try:
            payload = _json.loads(getattr(resp, "text", "") or "")
        except (ValueError, TypeError):
            payload = None
    if not isinstance(payload, dict):
        raise EmbeddingError("embedding 接口返回不是 JSON 对象")

    data = payload.get("data")
    if not isinstance(data, list):
        raise EmbeddingError("embedding 接口返回缺少 data 数组")

    vectors: list[list[float]] = []
    for item in data:
        raw = item.get("embedding") if isinstance(item, dict) else None
        if not isinstance(raw, list) or not raw:
            raise EmbeddingError("embedding 接口返回格式不符：data[].embedding 缺失")
        try:
            vectors.append([float(x) for x in raw])
        except (TypeError, ValueError):
            raise EmbeddingError("embedding 接口返回的向量含非数值元素") from None

    if len(vectors) != expected:
        raise EmbeddingError(
            f"embedding 接口返回条数与输入不符：{len(vectors)} != {expected}"
        )
    return vectors
