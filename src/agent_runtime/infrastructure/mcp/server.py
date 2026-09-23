"""MCP Server 的 stdio IO 层：读 stdin 的行 → 协议核心 → 写 stdout。

为什么与协议核心分开：`MCPServerCore` 不认识流，所以协议行为可以脱离 IO 直接验证；
这里只负责"搬运一行"，并把**所有** IO 异常留在循环里（绝不让 server 崩掉）。

流是可注入的：`run_stdio_server(core, stdin, stdout, stderr)` 接受任何
`readline()`（同步或 awaitable）+ `write()`/`flush()` 的对象 —— StringIO、asyncio 流、
进程内队列都可以，因此本模块在沙箱内可完整测试（沙箱拒绝带管道的子进程）。

ponytail: 真实 sys.stdin 的 readline 是**阻塞**调用，会占住事件循环。对"一个进程只服务
一条 stdio 会话"的 MCP server 这是对的取舍（进程只干这件事）；需要并发服务多路输入时，
把 transport 换成 asyncio 流的 `connect_read_pipe` 版本即可，协议层不用动。
"""
from __future__ import annotations

import inspect
import sys
from typing import Any

from ...core.mcp.server import MCPServerCore


def _diagnose(stderr: Any, message: str) -> None:
    """诊断只写 stderr：stdout 是协议帧专用，混进一行日志就会破坏客户端解析。"""
    if stderr is None:
        return
    try:
        stderr.write(message.rstrip("\n") + "\n")
        flush = getattr(stderr, "flush", None)
        if callable(flush):
            flush()
    except Exception:  # noqa: BLE001 —— 连诊断都写不出去时只能放弃，绝不能因此崩掉
        pass


async def run_stdio_server(core: MCPServerCore, stdin: Any, stdout: Any, stderr: Any = None) -> int:
    """逐行服务直到 EOF，返回进程退出码（EOF / 读失败一律 0 = 干净退出）。"""
    while True:
        try:
            line = stdin.readline()
            if inspect.isawaitable(line):
                line = await line
        except Exception as e:  # noqa: BLE001 —— 读失败按 EOF 处理，不能让 traceback 冲出去
            _diagnose(stderr, f"[mcp] stdin 读取失败（按 EOF 处理）：{type(e).__name__}: {e}")
            return 0

        if not line:
            return 0  # EOF：干净退出

        try:
            response = await core.handle_line(line)
        except Exception as e:  # noqa: BLE001 —— 单行出错只跳过这一行，循环必须继续
            _diagnose(stderr, f"[mcp] 处理输入行失败：{type(e).__name__}: {e}")
            continue

        if response is None:
            continue  # 通知 / 空行 / 坏行且无 id：本来就不该有响应

        try:
            stdout.write(response)
            flush = getattr(stdout, "flush", None)
            if callable(flush):
                flush()  # 不 flush 的话管道客户端会一直等（缓冲不落地）
        except Exception as e:  # noqa: BLE001 —— stdout 断了说明对端已走，收摊
            _diagnose(stderr, f"[mcp] 写 stdout 失败：{type(e).__name__}: {e}")
            return 0


class StdioServerTransport:
    """真实 sys.stdin/stdout 的包装（给入口脚本用）；也可注入流以便测试。"""

    def __init__(self, stdin: Any = None, stdout: Any = None, stderr: Any = None) -> None:
        self.stdin = sys.stdin if stdin is None else stdin
        self.stdout = sys.stdout if stdout is None else stdout
        self.stderr = sys.stderr if stderr is None else stderr

    async def run(self, core: MCPServerCore) -> int:
        return await run_stdio_server(core, self.stdin, self.stdout, self.stderr)
