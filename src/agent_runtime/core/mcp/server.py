"""MCP Server 协议核心（纯逻辑：零 IO、零第三方依赖）。

设计取舍：
  * 本模块只做"消息 → 消息"的翻译，**不认识任何流**：读 stdin / 写 stdout 的循环
    在 MCP 的 IO 层（同名模块的另一侧），因此协议行为可以在沙箱内 100% 直接验证；
  * 与仓库里已有的 MCP **客户端**严格对称：换行分隔 JSON-RPC 2.0、
    `initialize` / `notifications/initialized` / `tools/list` / `tools/call`、
    结果 `content:[{type:"text",text}]` + `isError`；
  * `inputSchema` 直接取 `BaseTool.to_openai_schema()["function"]["parameters"]`，
    让 MCP 侧与 API 侧看到的是**同一份** JSON Schema，不做二次转写；
  * 工具抛异常一律转成 `isError: true` + `Error: {type}: {msg}`，绝不抛穿
    （`BaseTool.execute()` 自己也不抛，这里是第二道防线）。
"""
from __future__ import annotations

import json
import re
from typing import Any

PROTOCOL_VERSION = "2024-11-05"
DEFAULT_SERVER_NAME = "agent-runtime"
DEFAULT_SERVER_VERSION = "0.1.0"

# JSON-RPC 2.0 标准错误码
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

_NOTIFICATION_PREFIX = "notifications/"

# 坏 JSON 时尝试把行里**真实存在**的顶层 id 捞出来（用于回一条带 id 的错误响应）。
# 注意：捞不到就返回 None，绝不凭空造一个 id —— 凭空造 id 会让客户端把错误响应
# 错配到某次真实请求上。
_ID_PATTERN = re.compile(r'"id"\s*:\s*(-?\d+|"(?:[^"\\]|\\.)*"|null)')
_NO_ID = object()


class MCPServerCore:
    """把一个 ToolRegistry 暴露成 MCP Server。"""

    def __init__(
        self,
        registry: Any = None,
        *,
        server_name: str = DEFAULT_SERVER_NAME,
        server_version: str = DEFAULT_SERVER_VERSION,
        protocol_version: str = PROTOCOL_VERSION,
    ) -> None:
        self._registry = registry
        self._server_name = server_name
        self._server_version = server_version
        self._protocol_version = protocol_version

    # ── 响应构造 ──

    @staticmethod
    def _ok(request_id: Any, result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    @staticmethod
    def _text_result(text: str, is_error: bool) -> dict:
        return {"content": [{"type": "text", "text": text}], "isError": is_error}

    # ── 消息分发 ──

    async def handle(self, message: dict | None) -> dict | None:
        """处理一条已解析的消息。**任何输入都不抛异常**：内部错误变成 -32603。"""
        if not isinstance(message, dict):
            return None
        request_id = message.get("id")
        try:
            return await self._dispatch(message)
        except Exception as e:  # noqa: BLE001 —— 分发层兜底，绝不能抛穿到 IO 循环
            if "id" not in message:
                return None
            return self._error(request_id, INTERNAL_ERROR, f"{type(e).__name__}: {e}")

    async def _dispatch(self, message: dict) -> dict | None:
        method = message.get("method")
        has_id = "id" in message
        request_id = message.get("id")

        if not has_id:
            return None  # 没有 id 的消息按通知处理：JSON-RPC 通知不回响应
        if not isinstance(method, str) or not method:
            return self._error(request_id, INVALID_REQUEST, "invalid request: missing method")
        if method.startswith(_NOTIFICATION_PREFIX):
            # 通知不回响应 —— 即使它反常地带了 id 也不回：MCP 的通知语义由方法名决定，
            # 回一条响应反而会让严格按 id 匹配的客户端把响应对到别的请求上。
            return None

        if method == "initialize":
            return self._ok(request_id, self._initialize_result())
        if method == "tools/list":
            return self._ok(request_id, self._list_result())
        if method == "tools/call":
            return await self._call_result(request_id, message.get("params"))
        return self._error(request_id, METHOD_NOT_FOUND, f"method not found: {method}")

    def _initialize_result(self) -> dict:
        return {
            "protocolVersion": self._protocol_version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": self._server_name, "version": self._server_version},
        }

    def _list_result(self) -> dict:
        """registry 为 None / 没有工具时返回空数组，而不是报错。"""
        tools: list[dict] = []
        if self._registry is not None:
            for tool in self._registry.list_all():
                schema = tool.to_openai_schema()
                tools.append(
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "inputSchema": schema["function"]["parameters"],
                    }
                )
        return {"tools": tools}

    async def _call_result(self, request_id: Any, params: Any) -> dict:
        if not isinstance(params, dict):
            return self._error(request_id, INVALID_PARAMS, "invalid params: params must be an object")
        name = params.get("name")
        if not isinstance(name, str) or not name:
            return self._error(request_id, INVALID_PARAMS, "invalid params: name must be a string")
        # 缺省 arguments 视为 {}（真实客户端常省略它）；**显式**给了非对象（含 null）
        # 一律 -32602 —— "没传"与"传了个错东西"是两回事，不该混为一谈。
        if "arguments" not in params:
            raw_args: Any = {}
        else:
            raw_args = params["arguments"]
            if not isinstance(raw_args, dict):
                return self._error(
                    request_id, INVALID_PARAMS, "invalid params: arguments must be an object"
                )

        tool = None
        if self._registry is not None:
            tool = self._registry.get(name)
        if tool is None:
            return self._error(request_id, INVALID_PARAMS, f"unknown tool: {name}")

        try:
            result = await tool.execute(**raw_args)
        except Exception as e:  # noqa: BLE001 —— 工具异常 = 工具执行失败，不是 server 崩溃
            return self._ok(
                request_id,
                self._text_result(f"Error: {type(e).__name__}: {e}", True),
            )

        text = getattr(result, "text", "")
        success = bool(getattr(result, "success", False))
        return self._ok(request_id, self._text_result(str(text), not success))

    # ── 线格式 ──

    async def handle_line(self, line: str) -> str | None:
        """处理一行 stdin 文本，返回要写回 stdout 的行（**恰好一个结尾 \\n**）或 None。

        None 的三种含义（客户端不需要区分，但必须都成立）：
          * 空行 / 纯空白；
          * 通知；
          * 坏 JSON 且行内**没有** id（没有 id 就没法回响应，也绝不猜一个）。
        """
        if isinstance(line, (bytes, bytearray)):
            line = bytes(line).decode("utf-8", errors="replace")
        if not isinstance(line, str):
            return None
        text = line.strip()
        if not text:
            return None

        try:
            message = json.loads(text)
        except ValueError:
            request_id = _salvage_id(text)
            if request_id is _NO_ID:
                return None
            return self._dump(self._error(request_id, PARSE_ERROR, "parse error: invalid JSON"))

        response = await self.handle(message)
        return None if response is None else self._dump(response)

    @staticmethod
    def _dump(payload: dict) -> str:
        return json.dumps(payload, ensure_ascii=False) + "\n"


def _salvage_id(text: str) -> Any:
    """从坏 JSON 行里捞出**顶层**的 id；捞不到返回 `_NO_ID`。

    只认 depth==1 的 `"id"`：嵌套的 id（例如 `params.id`）不是这条消息的请求 id，
    拿它当 id 回错误响应会把错误错配到客户端另一次在途请求上 —— 那比干脆不回响应更糟
    （客户端会把别人的响应当成自己的结果，静默出错）。

    行内字符串要跳过，否则 `{"a":"id":9"}` 这种碎片会被误判；`null` 是合法的
    JSON-RPC id，必须认（否则客户端永远等不到响应）。
    """
    depth = 0
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            if depth == 1 and _at_key_position(text, index):
                match = _ID_PATTERN.match(text, index)
                if match is not None:
                    return _parse_id(match.group(1))
            in_string = True
        elif char in "{[":
            depth += 1
        elif char in "}]":
            depth -= 1
    return _NO_ID


def _at_key_position(text: str, index: int) -> bool:
    """`index` 处的引号是否处在"键"的位置（前面只可能是 `{` 或 `,`）。

    用于排除字符串**值**里的假 id：`{"a":"id":9",...}` 里的 `id":9` 是值的一部分，
    不是键 —— 认它会造出一个假的请求 id。
    """
    for position in range(index - 1, -1, -1):
        char = text[position]
        if char in " \t\r\n":
            continue
        return char in "{,"
    return False


def _parse_id(raw: str) -> Any:
    try:
        return json.loads(raw)
    except ValueError:  # pragma: no cover —— 正则已保证 raw 是合法 JSON 片段
        return raw
