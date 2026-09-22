from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..deps import get_agent

router = APIRouter()


@router.websocket("/ws/agent/{session_id}")
async def agent_ws(ws: WebSocket, session_id: str) -> None:
    """客户端发 {"type":"task","task":...}，服务端逐条推 AgentEvent，末尾补一条 done。"""
    await ws.accept()
    try:
        while True:
            msg = await ws.receive_json()
            if msg.get("type") != "task":
                continue
            task = str(msg.get("task", "")).strip()
            if not task:
                await ws.send_json(
                    {"event_type": "error", "data": {"step": 0, "message": "empty task"}}
                )
                continue
            agent = get_agent()
            async for ev in agent.run_stream(task):
                await ws.send_json({"event_type": ev.event_type.value, "data": ev.data})
            last = agent.last_result
            await ws.send_json(
                {
                    "event_type": "done",
                    "data": {
                        "final_answer": last.final_answer if last else "",
                        "trace_id": last.trace_id if last else "",
                        "warning": last.warning if last else None,
                        "skills_used": last.skills_used if last else [],
                    },
                }
            )
    except WebSocketDisconnect:
        return
