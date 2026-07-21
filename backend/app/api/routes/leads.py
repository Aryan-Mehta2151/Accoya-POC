"""Lead ingestion (EarlyBid feed sync + CSV upload) and listing.

One EarlyBid opportunity = one Lead = one card in the UI.
"""
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.database import get_db
from app.db.models import Lead
from app.schemas.lead import LeadRead, SyncResult
from app.services import lead_feed_service
from app.services.lead_feed_service import LeadFeedError

settings = get_settings()

router = APIRouter(prefix="/leads", tags=["leads"])


@router.post("/sync", response_model=SyncResult)
def sync_feed(
    reseller: str | None = None,
    client: str | None = None,
    db: Session = Depends(get_db),
):
    """Pull the latest EarlyBid feed and upsert opportunities on their `id`.

    Defaults to the feed configured in settings when reseller/client are omitted.
    """
    try:
        return lead_feed_service.sync_feed(
            db,
            reseller or settings.lead_feed_reseller,
            client or settings.lead_feed_client,
        )
    except LeadFeedError as exc:
        raise HTTPException(
            status_code=502,
            detail=str(exc),
        ) from exc


@router.post("/upload-csv", response_model=list[LeadRead])
async def upload_csv(file: UploadFile = File(...), db: Session = Depends(get_db)):
    """Upload an EarlyBid-format CSV; each row is upserted as a Lead."""
    text = (await file.read()).decode("utf-8-sig")
    rows = lead_feed_service.parse_feed_csv(text)

    touched, _, _ = lead_feed_service.upsert_feed_rows(
        db,
        rows,
        source_feed=f"upload:{file.filename}",
    )

    db.commit()
    for lead in touched:
        db.refresh(lead)
    return touched


@router.get("", response_model=list[LeadRead])
def list_leads(db: Session = Depends(get_db)):
    return db.scalars(select(Lead).order_by(Lead.score.desc().nullslast())).all()
