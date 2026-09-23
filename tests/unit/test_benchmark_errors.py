"""provider 错误分类契约（`core/benchmark/errors.py`）。

为什么需要它：真实消融要跑 300 次调用，必然撞限流/超时。不把"provider 抽风"与"agent 做错了"分开，
三组的成功率就是被噪声污染的数字（Phase 7 开跑前检查 D）。

判定是**启发式**（`core` 不能 import openai/httpx）：按异常类名 / MRO / 模块名。认不出的算任务失败 ——
两个计数都在报告里，运维能一眼看出有没有漏网的。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from agent_runtime.core.benchmark.errors import (  # noqa: E402
    PROVIDER_ERROR_MODULES,
    PROVIDER_ERROR_NAMES,
    classify_error,
)


def _fake_class(name: str, module: str = "builtins"):
    return type(name, (Exception,), {"__module__": module})


def test_known_provider_error_names_are_detected() -> None:
    for name in (
        "APITimeoutError",
        "APIConnectionError",
        "RateLimitError",
        "TimeoutError",
        "ConnectError",
        "ReadTimeout",
        "HTTPStatusError",
    ):
        assert classify_error(_fake_class(name)("boom")) == "provider", name


def test_detection_walks_the_mro() -> None:
    """真实 SDK 的具体异常类往往继承自基类（`RateLimitError` 之类），子类也要认出来。"""
    base = _fake_class("RateLimitError")
    child = type("MyRateLimit", (base,), {})
    assert classify_error(child("x")) == "provider"


def test_module_based_detection() -> None:
    assert classify_error(_fake_class("WeirdTransport", module="httpx._exceptions")("x")) == "provider"
    assert classify_error(_fake_class("Nope", module="openai._exceptions")("x")) == "provider"
    assert classify_error(_fake_class("Deep", module="httpcore._sync")("x")) == "provider"


def test_unknown_errors_are_task_errors() -> None:
    for exc in (
        RuntimeError("boom"),
        AttributeError("x"),
        ValueError("y"),
        KeyError("z"),
        TypeError("t"),
        ZeroDivisionError("w"),
    ):
        assert classify_error(exc) == "task", type(exc).__name__


def test_none_and_empty_are_task_errors() -> None:
    assert classify_error(None) == "task"
    assert classify_error("") == "task"
    assert classify_error("   ") == "task"


def test_stored_error_strings_are_classified() -> None:
    """报告里存的是字符串（`f"{type(e).__name__}: {e}"`），重新读取时也要能分类。"""
    assert classify_error("APITimeoutError: Request timed out.") == "provider"
    assert classify_error("RateLimitError: rate limit reached for RPM") == "provider"
    assert classify_error("httpx.ConnectError: connection refused") == "provider"
    assert classify_error("AttributeError: 'BenchmarkTask' object has no attribute 'chat'") == "task"
    assert classify_error("RuntimeError: provider 挂了") == "task"


def test_the_name_and_module_sets_are_documented() -> None:
    assert {"APITimeoutError", "RateLimitError", "TimeoutError"} <= PROVIDER_ERROR_NAMES
    assert {"openai", "httpx", "httpcore"} <= PROVIDER_ERROR_MODULES


def _run_all() -> None:
    failed: list[str] = []
    tests = [
        v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)
    ]
    for t in tests:
        print(f"RUN  {t.__name__}")
        try:
            t()
        except Exception as e:  # noqa: BLE001
            print(f"FAIL {t.__name__}: {type(e).__name__}: {e}")
            failed.append(t.__name__)
        else:
            print(f"PASS {t.__name__}")
    if failed:
        raise SystemExit(f"FAILED: {', '.join(failed)}")
    print("ALL PASS")


if __name__ == "__main__":
    _run_all()
