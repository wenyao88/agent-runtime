import asyncio, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from agent_runtime.core.llm.types import FunctionCall, LLMResponse, Message
from agent_runtime.infrastructure.llm.mock import MockLLMProvider

_MS = [Message(role="user", content="hi")]


def _tool_call_resp() -> LLMResponse:
    return LLMResponse(content=None, tool_calls=[
        FunctionCall(id="c1", name="read_file", arguments='{"path": "a.md"}'),
    ])


async def test_script_returns_in_order():
    # ① 按序弹出：第一响应带 tool_call，第二响应纯文本
    provider = MockLLMProvider(script=[_tool_call_resp(), LLMResponse(content="done")])
    first = await provider.chat(list(_MS))
    second = await provider.chat(list(_MS))
    assert first.content is None
    assert first.tool_calls and first.tool_calls[0].name == "read_file"
    assert second.content == "done"
    assert not second.tool_calls
    assert provider.calls == 2


async def test_exhausted_repeats_last_and_counts():
    # ② 耗尽后永久重复最后一条；calls 计数正确
    provider = MockLLMProvider(script=[LLMResponse(content="one"), LLMResponse(content="last")])
    a = await provider.chat(list(_MS))
    b = await provider.chat(list(_MS))
    c = await provider.chat(list(_MS))
    assert a.content == "one"
    assert b.content == "last"
    assert c.content == "last"
    assert c is not b
    assert provider.calls == 3


async def test_returns_are_copies():
    # ③ 返回的是副本：对副本做属性赋值，不影响内部
    #    （replace 为浅拷贝：共享子对象的“原地修改”不在保证范围，测试只做属性赋值）
    provider = MockLLMProvider(script=[LLMResponse(
        content="original",
        tool_calls=[FunctionCall(id="c1", name="t", arguments="{}")],
    )])
    r1 = await provider.chat(list(_MS))
    r1.content = "tampered"
    r1.tool_calls = []
    r2 = await provider.chat(list(_MS))
    assert r2.content == "original"
    assert r2.tool_calls and r2.tool_calls[0].id == "c1"
    assert r1 is not r2


async def test_stream_single_chunk():
    # ④ stream 产出单 chunk：内容 / 无 tool_call / finish_reason=stop
    provider = MockLLMProvider(script=[LLMResponse(content="hello")])
    chunks = [c async for c in provider.stream(list(_MS))]
    assert len(chunks) == 1
    assert chunks[0].delta_content == "hello"
    assert chunks[0].delta_tool_call is None
    assert chunks[0].finish_reason == "stop"
    assert provider.calls == 1


if __name__ == "__main__":
    _failed = []
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            print(f"RUN  {_name}")
            try:
                asyncio.run(_fn())
            except Exception as _e:
                print(f"FAIL {_name}: {type(_e).__name__}: {_e}")
                _failed.append(_name)
            else:
                print(f"PASS {_name}")
    if _failed:
        raise SystemExit(f"FAILED: {', '.join(_failed)}")
    print("ALL PASS")
