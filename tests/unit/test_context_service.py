"""`/api/context` 背后的纯逻辑契约（沙箱可测）。

路由依赖 fastapi（装不上），所以逻辑在 `infrastructure/context/service.py`；api 层只做序列化。
这一层要回答的只有两件事：
  * 能拿到真实上下文 → `available: true` + 快照（分段 token / 预算 / 最近压缩）；
  * 拿不到（还没聊过、管理器不支持、读的时候炸了）→ `available: false` + **人读得懂的原因**，
    HTTP 仍 200（前端展示原因，不要用 500 装死 —— 那和"服务器坏了"分不清）。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from agent_runtime.core.context.budget import TokenBudget
from agent_runtime.core.context.manager import ContextManager
from agent_runtime.infrastructure.context.service import context_view


def _manager() -> ContextManager:
    async def run():
        cm = ContextManager(
            budget=TokenBudget(model_max_tokens=10000, reserved_output=4096),
            keep_recent=1,
        )
        await cm.build(task="看看上下文", system_prompt="SYSTEM")
        return cm

    return asyncio.run(run())


def test_no_agent_is_reported_with_a_reason() -> None:
    body = context_view(None)
    assert body["available"] is False, body
    assert body["reason"], "拿不到 agent 必须给出原因，不能空着"
    assert "sections" not in body, body


def test_available_context_returns_the_snapshot() -> None:
    body = context_view(_manager())
    assert body["available"] is True, body
    assert body["reason"] == "", body
    assert [s["name"] for s in body["sections"]] == ["system", "memory", "task", "messages"], body
    assert body["budget"]["available"] == 5313, body
    assert body["used_tokens"] > 0, body
    assert body["last_compaction"] is None, "一次都没压过 → None，不是 0"


def test_a_manager_without_snapshot_is_reported_not_raised() -> None:
    """Inspector 拿到的 manager 不一定是 `ContextManager`（鸭子类型的假件在测试里很常见）。"""
    body = context_view(object())
    assert body["available"] is False, body
    assert "快照" in body["reason"] or "snapshot" in body["reason"], body


def test_a_broken_snapshot_is_reported_not_raised() -> None:
    class Broken:
        def snapshot(self):
            raise RuntimeError("budget exploded")

    body = context_view(Broken())
    assert body["available"] is False, body
    assert "budget exploded" in body["reason"], body


def test_assembly_errors_are_carried_through() -> None:
    """装配期问题（摘要器缺 key 等）要能一起看到，否则 Inspector 上"没有压缩"看不出为什么。"""
    body = context_view(_manager(), errors=["摘要器未启用：缺 OPENAI_API_KEY"])
    assert body["available"] is True, body
    assert body["errors"] == ["摘要器未启用：缺 OPENAI_API_KEY"], body


def test_the_view_never_mutates_the_manager() -> None:
    manager = _manager()
    before = manager.token_count()
    context_view(manager)
    assert manager.token_count() == before, "快照必须只读"


def _run_all() -> None:
    failed = []
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"RUN  {name}")
            try:
                fn()
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
