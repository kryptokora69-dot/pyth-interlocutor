from fastapi import FastAPI
from pydantic import BaseModel

from .pyth_core import run_agent  # assumes pyth_core.py is in agent/

app = FastAPI()


class ChatRequest(BaseModel):
    message: str
    mode: str | None = None  # matches your GUI's "mode" field


class ChatResponse(BaseModel):
    reply: str


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.post("/api/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    user_input = req.message
    mode = req.mode or "chat"

    # This is where your modern agent/router runs
    reply,_ = run_agent(user_input, mode)

    return ChatResponse(reply=reply)
