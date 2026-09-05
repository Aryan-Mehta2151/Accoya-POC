"""Authenticated read-only reply summary API."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.database import get_db
from app.schemas.email_reply import EmailReplySummaryRead
from app.services import email_reply_service


router = APIRouter(prefix="/email-replies", tags=["email replies"])
settings = get_settings()


@router.get("/summary", response_model=EmailReplySummaryRead)
def get_reply_summary(db: Session = Depends(get_db)):
    """Return active-opportunity unread replies and mailbox sync health."""

    return email_reply_service.reply_summary(db, settings=settings)
