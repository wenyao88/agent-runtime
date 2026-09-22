from fastapi import APIRouter, Depends

from ...core.agent.react import ReActLoop
from ..deps import get_agent_dep
from ..schemas.chat import ChatRequest, ChatResponse

router = APIRouter(prefix="/api", tags=["chat"])


@router.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, agent: ReActLoop = Depends(get_agent_dep)) -> ChatResponse:
    """跑一次完整的 ReAct 循环，返回最终答案与用量统计。"""
    result = await agent.run(req.task)
    return ChatResponse(
        final_answer=result.final_answer,
        steps=len(result.steps),
        total_tokens={
            "prompt": result.total_tokens.prompt_tokens,
            "completion": result.total_tokens.completion_tokens,
            "total": result.total_tokens.total_tokens,
        },
        total_latency_ms=result.total_latency_ms,
        trace_id=result.trace_id,
        warning=result.warning,
        skills_used=result.skills_used,
    )
