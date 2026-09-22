"""Embedding 抽象（core）：把文本映射成向量。

只定义接口、不碰任何网络库 —— 真实实现是 `infrastructure/memory/embedding.py`。
之所以单独抽一层：`PostgresLongTermMemory` 只依赖这个接口，
测试里可以用"返回固定向量"的假 embedder 驱动它（不必联网、不必装 SDK）。
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class BaseEmbedder(ABC):
    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """把一批文本转成向量；返回顺序必须与输入**一一对应**。"""

    async def aclose(self) -> None:  # pragma: no cover - 默认可选，实现按需覆盖
        return None
