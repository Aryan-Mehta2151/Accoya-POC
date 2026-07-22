"""Pydantic schemas for the chatbot."""
from datetime import datetime

from pydantic import BaseModel


class ChatRequest(BaseModel):
    session_id: str | None = None
    message: str


class ChatResponse(BaseModel):
    session_id: str
    seq: int
    answer: str
    sources: list[str] = []


class ChatSessionCreate(BaseModel):
    session_id: str


class ChatSessionSummary(BaseModel):
    session_id: str
    message_count: int
    last_message_at: datetime | None = None


class ChatMessageRead(BaseModel):
    seq: int
    role: str
    content: str
    created_at: datetime
