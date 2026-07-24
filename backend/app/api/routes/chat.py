"""QnA chatbot over strategy docs and sent emails.

Retrieval uses the Bedrock Knowledge Base for chunks; answer generation uses
Gemini with the per-session conversation history. Only the current question is
sent to AWS retrieval. The conversation history is provided solely to Gemini.
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.database import get_db
from app.db.models import ChatMessage
from app.schemas.chat import (
    ChatMessageRead,
    ChatRequest,
    ChatResponse,
    ChatSessionCreate,
    ChatSessionSummary,
)
from app.services import rag_service
from app.services.bedrock_service import BedrockKnowledgeBaseError

settings = get_settings()

router = APIRouter(prefix="/chat", tags=["chat"])


def _next_seq(db: Session, session_id: str) -> int:
    """Return the next 1-based message serial number for a session."""
    current = db.scalar(
        select(func.max(ChatMessage.seq)).where(
            ChatMessage.session_id == session_id
        )
    )
    return (current or 0) + 1


@router.post("/session", response_model=ChatSessionCreate)
def create_session() -> ChatSessionCreate:
    """Start a brand-new chat session with its own isolated context."""
    return ChatSessionCreate(session_id=str(uuid.uuid4()))


@router.get("/sessions", response_model=list[ChatSessionSummary])
def list_sessions(db: Session = Depends(get_db)):
    """List chat sessions ordered by most recent activity."""
    rows = db.execute(
        select(
            ChatMessage.session_id,
            func.count().label("message_count"),
            func.max(ChatMessage.created_at).label("last_message_at"),
        )
        .group_by(ChatMessage.session_id)
        .order_by(func.max(ChatMessage.created_at).desc())
    ).all()
    return [
        ChatSessionSummary(
            session_id=row.session_id,
            message_count=row.message_count,
            last_message_at=row.last_message_at,
        )
        for row in rows
    ]


@router.post("", response_model=ChatResponse)
def chat(payload: ChatRequest, db: Session = Depends(get_db)):
    """Answer a question grounded in KB chunks and prior session history."""
    session_id = payload.session_id or str(uuid.uuid4())

    # Load prior turns for this session. History is Gemini context only and is
    # never forwarded to AWS retrieval.
    history_rows = db.scalars(
        select(ChatMessage)
        .where(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.seq.asc())
    ).all()

    max_turns = settings.chat_history_max_turns
    if max_turns > 0:
        # Each turn is a human+ai pair, so keep the last max_turns*2 messages.
        history_rows = history_rows[-(max_turns * 2):]
    history = [{"role": row.role, "content": row.content} for row in history_rows]

    try:
        answer, sources = rag_service.answer_question(payload.message, history)
    except BedrockKnowledgeBaseError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Chat generation failed: {exc}",
        ) from exc

    human_seq = _next_seq(db, session_id)
    db.add(
        ChatMessage(
            session_id=session_id,
            seq=human_seq,
            role="human",
            content=payload.message,
        )
    )
    db.add(
        ChatMessage(
            session_id=session_id,
            seq=human_seq + 1,
            role="ai",
            content=answer,
        )
    )
    db.commit()

    return ChatResponse(
        session_id=session_id,
        seq=human_seq + 1,
        answer=answer,
        sources=sources,
    )


@router.get("/{session_id}", response_model=list[ChatMessageRead])
def get_history(session_id: str, db: Session = Depends(get_db)):
    """Return one session's messages ordered by their serial number."""
    rows = db.scalars(
        select(ChatMessage)
        .where(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.seq.asc())
    ).all()
    return [
        ChatMessageRead(
            seq=row.seq,
            role=row.role,
            content=row.content,
            created_at=row.created_at,
        )
        for row in rows
    ]


@router.delete("/{session_id}")
def delete_session(session_id: str, db: Session = Depends(get_db)):
    """Delete all messages in a session (deletes the session from the database)."""
    db.execute(
        delete(ChatMessage).where(ChatMessage.session_id == session_id)
    )
    db.commit()
    return {"deleted": True, "session_id": session_id}
