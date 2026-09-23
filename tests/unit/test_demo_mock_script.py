"""`run_demo_mock.py` 的契约：它演示的每一步都必须能在**当前仓库**里真的跑出来。

清理仓库时踩到的坑：这个"零依赖演示"里硬编码了 `读取 计划.md`，而 `计划.md` 属于开发过程文件、
已从仓库移除 —— 于是演示的第一步就在读一个不存在的文件，输出全是 `file not found`，
而它本该演示"读文件成功 → 读另一个文件失败 → 模型自行纠正"。

路径是写死在脚本里的字符串，没有任何东西盯着它。这里把它钉住：
  1. 要读的文件必须真实存在于仓库中；
  2. 故意读不到的那个文件必须**真的**不存在（否则那条失败分支永远走不到，演示失去意义）。
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "scripts" / "run_demo_mock.py"


def _load():
    spec = importlib.util.spec_from_file_location("run_demo_mock", _SCRIPT)
    assert spec and spec.loader, "无法加载 mock 演示脚本"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_mock_reads_a_file_that_exists() -> None:
    module = _load()
    target = _ROOT / module.TARGET_FILE
    assert target.is_file(), f"演示要读的文件不在仓库里：{module.TARGET_FILE}"


def test_mock_missing_file_is_really_missing() -> None:
    module = _load()
    assert not (_ROOT / module.MISSING_FILE).exists(), (
        f"故意读不到的文件必须真的不存在，否则失败分支走不到：{module.MISSING_FILE}"
    )


def _run_all() -> None:
    failed: list[str] = []
    tests = [
        v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)
    ]
    for t in tests:
        print(f"RUN  {t.__name__}")
        try:
            t()
        except unittest.SkipTest as e:
            print(f"SKIP {t.__name__}: {e}")
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
