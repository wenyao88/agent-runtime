from pydantic import BaseModel


class ChatRequest(BaseModel):
    task: str
    session_id: str | None = None


class ChatResponse(BaseModel):
    final_answer: str
    steps: int
    total_tokens: dict
    total_latency_ms: int
    trace_id: str
    warning: str | None = None
    skills_used: list[str] = []  # 本次命中的技能名（可见性：REST 调用方也要能看见）
