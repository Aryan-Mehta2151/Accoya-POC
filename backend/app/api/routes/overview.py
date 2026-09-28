"""Authenticated read-only Overview dashboard API."""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.database import get_db
from app.schemas.overview import OverviewSummaryRead
from app.services import overview_service


router = APIRouter(prefix="/overview", tags=["overview"])
settings = get_settings()


@router.get("/summary", response_model=OverviewSummaryRead)
def get_overview_summary(
    start_at: datetime | None = None,
    end_before: datetime | None = None,
    db: Session = Depends(get_db),
):
    """Return all-time totals or totals within one half-open UTC range."""

    if (start_at is None) != (end_before is None):
        raise HTTPException(
            status_code=422,
            detail="start_at and end_before must be provided together",
        )
    if start_at is not None and end_before is not None:
        if start_at.utcoffset() is None or end_before.utcoffset() is None:
            raise HTTPException(
                status_code=422,
                detail="Date range timestamps must include a UTC offset",
            )
        start_at = start_at.astimezone(timezone.utc)
        end_before = end_before.astimezone(timezone.utc)
        if start_at >= end_before:
            raise HTTPException(
                status_code=422,
                detail="start_at must be earlier than end_before",
            )

    return overview_service.overview_summary(
        db,
        settings=settings,
        start_at=start_at,
        end_before=end_before,
    )
