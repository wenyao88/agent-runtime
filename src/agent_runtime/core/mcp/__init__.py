"""MCP Server 协议核心（零 IO、零第三方依赖）。

IO 循环在 MCP 的 IO 层；这里只导出协议层，方便入口脚本与测试直接引用，
而不必关心 sys.stdin/stdout。
"""
from .server import (
    DEFAULT_SERVER_NAME,
    DEFAULT_SERVER_VERSION,
    PROTOCOL_VERSION,
    MCPServerCore,
)

__all__ = [
    "MCPServerCore",
    "PROTOCOL_VERSION",
    "DEFAULT_SERVER_NAME",
    "DEFAULT_SERVER_VERSION",
]
