"""`/api/context` 背后的逻辑（纯逻辑，零第三方依赖 → 沙箱可测）。

为什么要这一层而不是直接在路由里调 `manager.snapshot()`：
  * 路由依赖 fastapi（装不上），一放那儿就不可验证；
  * "拿不到上下文"有三种互不相同的原因（还没聊过 / 管理器不支持快照 / 读的时候炸了），
    必须各自给出人读得懂的原因，而不是一律 500。

**HTTP 仍回 200**：`available: false` + `reason` 由前端展示。用 500 装死会和"服务器真的坏了"
分不清（与 memories 的 `enabled: false` 同一态度）。
"""
from __future__ import annotations

from typing import Any

REASON_NO_SESSION = (
    "还没有跑过聊天会话：先在 Chat 页发一句话（或检查 LLM key 是否配好），再看当前上下文。"
)


def context_view(manager: Any, *, errors: list[str] | None = None) -> dict[str, Any]:
    """把最近一次会话的上下文管理器折成一份只读快照视图；**绝不抛**。"""
    assembly_errors = list(errors or [])
    if manager is None:
        return {"available": False, "reason": REASON_NO_SESSION, "errors": assembly_errors}

    snapshot = getattr(manager, "snapshot", None)
    if not callable(snapshot):
        return {
            "available": False,
            "reason": f"{type(manager).__name__} 不支持快照（缺 snapshot()）",
            "errors": assembly_errors,
        }

    try:
        data = dict(snapshot())
    except Exception as e:  # noqa: BLE001 —— 对外接口不抛，如实回报
        return {
            "available": False,
            "reason": f"读取上下文失败：{type(e).__name__}: {e}",
            "errors": assembly_errors,
        }

    # `**data` 在前：快照自己就是那份事实，available/reason/errors 只是它外面的一层包装，
    # 不允许被快照里的同名键覆盖（真被覆盖就是快照越权改了对外契约）。
    return {**data, "available": True, "reason": "", "errors": assembly_errors}
