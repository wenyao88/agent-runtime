"""provider/网络错误 vs 任务失败（纯逻辑，零第三方依赖）。

真实消融要跑 300 次调用，**必然**撞限流/超时。不把"provider 抽风"与"agent 做错了"分开，
三组的成功率就是被噪声污染的数字（Phase 7 开跑前检查 D）。

判定是**启发式**：`core` 不能 import openai/httpx，所以按**类名 / MRO / 模块名**认。
认不出的算任务失败 —— 报告里两个计数都在，有没有漏网能一眼看出来。
升级路径：真正需要精确判定时，在 infrastructure 层用 `isinstance` 对着 SDK 的异常树做一次。
匹配是**大小写敏感**的：agent 自己的错误信息里出现小写 "timeout" 不该被算成 provider 的问题。
"""
from __future__ import annotations

PROVIDER_ERROR_NAMES: frozenset[str] = frozenset(
    {
        # openai SDK
        "APITimeoutError",
        "APIConnectionError",
        "APIError",
        "APIStatusError",
        "RateLimitError",
        "InternalServerError",
        "ServiceUnavailableError",
        # httpx / httpcore
        "TimeoutException",
        "ConnectError",
        "ConnectTimeout",
        "ReadTimeout",
        "WriteTimeout",
        "PoolTimeout",
        "ReadError",
        "WriteError",
        "RemoteProtocolError",
        "LocalProtocolError",
        "TransportError",
        "NetworkError",
        "HTTPStatusError",
        "ProxyError",
        "UnsupportedProtocol",
        # 标准库超时（3.11 起 asyncio.TimeoutError 就是 TimeoutError）
        "TimeoutError",
        "Timeout",
    }
)
"""异常**类名**白名单（按 MRO 逐级比对，所以 SDK 的子类也认得出）。"""

PROVIDER_ERROR_MODULES: frozenset[str] = frozenset(
    {"openai", "httpx", "httpcore", "aiohttp", "urllib3"}
)
"""异常所在的**顶层模块**白名单（类名没命中时的兜底）。"""

_STRING_HINTS: tuple[str, ...] = tuple(sorted(PROVIDER_ERROR_NAMES)) + tuple(
    sorted(PROVIDER_ERROR_MODULES)
)


def classify_error(error: object) -> str:
    """返回 `"provider"`（限流/超时/连接）或 `"task"`（其余，含"无法判断"）。

    既接受异常对象，也接受报告里存下来的字符串（`f"{type(e).__name__}: {e}"`）。
    """
    if error is None:
        return "task"
    if isinstance(error, str):
        text = error.strip()
        if not text:
            return "task"
        return "provider" if any(hint in text for hint in _STRING_HINTS) else "task"
    if isinstance(error, BaseException):
        for cls in type(error).__mro__:
            if cls.__name__ in PROVIDER_ERROR_NAMES:
                return "provider"
            if (cls.__module__ or "").split(".")[0] in PROVIDER_ERROR_MODULES:
                return "provider"
    return "task"
