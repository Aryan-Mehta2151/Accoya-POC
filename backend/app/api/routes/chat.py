"""QnA chatbot over strategy docs and sent emails (RAG via Bedrock + Gemini)."""
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db.models import ChatMessage
from app.schemas.chat import ChatRequest, ChatResponse
from app.services import bedrock_service
from app.services.bedrock_service import BedrockKnowledgeBaseError

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("", response_model=ChatResponse)
def chat(payload: ChatRequest, db: Session = Depends(get_db)):
    requested_session_id = payload.session_id

    try:
        kb_answer = bedrock_service.retrieve_and_generate(
            payload.message,
            session_id=requested_session_id,
        )
    except BedrockKnowledgeBaseError as exc:
        error_text = str(exc)
        invalid_session = (
            "Session with Id" in error_text and "is not valid" in error_text
        )

        # Recover from expired/stale Bedrock session IDs by creating a new session.
        if requested_session_id and invalid_session:
            try:
                kb_answer = bedrock_service.retrieve_and_generate(payload.message, session_id=None)
            except BedrockKnowledgeBaseError as retry_exc:
                raise HTTPException(status_code=502, detail=str(retry_exc)) from retry_exc
        else:
            raise HTTPException(status_code=502, detail=error_text) from exc

    effective_session_id = kb_answer.session_id or requested_session_id or str(uuid.uuid4())

    db.add(ChatMessage(session_id=effective_session_id, role="user", content=payload.message))
    db.add(ChatMessage(session_id=effective_session_id, role="assistant", content=kb_answer.answer))
    db.commit()

    return ChatResponse(
        session_id=effective_session_id,
        answer=kb_answer.answer,
        sources=kb_answer.sources,
    )


@router.get("/{session_id}")
def get_history(session_id: str, db: Session = Depends(get_db)):
    rows = db.scalars(
        select(ChatMessage)
        .where(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.created_at.asc())
    ).all()
    return [{"role": r.role, "content": r.content, "created_at": r.created_at} for r in rows]
