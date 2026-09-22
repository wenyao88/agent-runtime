"""Demo 1 脚本契约：可测的部分（任务构造、依赖缺失时的降级、参数校验）。

沙箱限制：**带管道的子进程被拒**，所以不能用 subprocess 跑脚本；
改为按路径 import 脚本模块后直接调函数 —— 这也要求脚本顶层只能 import 标准库。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "src"))

_SCRIPT = _ROOT / "scripts" / "run_demo1_github.py"


def _load():
    spec = importlib.util.spec_from_file_location("run_demo1_github", _SCRIPT)
    assert spec and spec.loader, "无法加载 demo 脚本"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_build_task_names_the_repo_and_required_tools() -> None:
    task = _load().build_task("fastapi/fastapi")
    assert "fastapi/fastapi" in task
    for tool in ("github_get_repo", "github_list_dir", "github_read_file"):
        assert tool in task, f"任务描述应明确要求使用 {tool}"
    assert "改进建议" in task
    assert "不要凭印象编造" in task, "必须要求基于真实工具返回，抑制幻觉"


def test_build_task_includes_optional_focus() -> None:
    module = _load()
    assert "错误处理" in module.build_task("a/b", focus="错误处理")
    assert "额外关注点" not in module.build_task("a/b")


def test_missing_deps_message_names_them() -> None:
    module = _load()
    message = module._require_deps()
    if message is None:
        print("     (依赖齐备 —— 跳过该分支)")
        return
    assert "httpx" in message and "openai" in message
    assert "pip install" in message


def test_main_degrades_readably_without_third_party_deps() -> None:
    module = _load()
    if module._require_deps() is None:
        print("     (依赖齐备 —— 跳过该分支)")
        return
    code = module.main(["--repo", "octocat/Hello-World"])
    assert code == 2, "缺依赖时应以可读提示 + 退出码 2 结束，而不是抛异常"


def test_main_rejects_blank_repo_before_touching_deps() -> None:
    module = _load()
    assert module.main(["--repo", "   "]) == 2


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
