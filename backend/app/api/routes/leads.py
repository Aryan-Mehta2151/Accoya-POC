"""Lead ingestion (EarlyBid feed sync + CSV upload) and listing.

One EarlyBid opportunity = one Lead = one card in the UI.
"""
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.database import get_db
from app.db.models import EmailGenerationTrigger, Lead
from app.schemas.email_generation import (
    EmailGenerationJobRead,
    EmailGenerationRequest,
)
from app.schemas.earlybid_sync import EarlyBidSyncStatusRead
from app.schemas.lead import (
    LeadListRead,
    LeadRead,
    LeadUploadResult,
    LeadWorkspaceRead,
    SyncResult,
)
from app.services import (
    earlybid_sync_service,
    email_generation_service,
    lead_feed_service,
)
from app.services.lead_feed_service import LeadFeedError, LeadFeedValidationError

settings = get_settings()

router = APIRouter(prefix="/leads", tags=["leads"])


@router.post("/sync", response_model=SyncResult)
def sync_feed(
    reseller: str | None = None,
    client: str | None = None,
    db: Session = Depends(get_db),
):
    """Pull and upsert EarlyBid opportunities using source-scoped identities.

    Explicit source IDs remain supported; ID-less rows use the configured feed
    scope plus their normalized project and location natural key. Defaults to
    the configured reseller/client when those query parameters are omitted.
    """
    try:
        return lead_feed_service.sync_feed(
            db,
            reseller or settings.lead_feed_reseller,
            client or settings.lead_feed_client,
        )
    except LeadFeedValidationError as exc:
        raise HTTPException(
            status_code=502,
            detail=exc.as_detail(),
        ) from exc
    except LeadFeedError as exc:
        raise HTTPException(
            status_code=502,
            detail=str(exc),
        ) from exc


@router.get("/sync-status", response_model=EarlyBidSyncStatusRead)
def get_automatic_sync_status(db: Session = Depends(get_db)):
    """Return the configured feed's latest durable automatic-sync state."""

    return earlybid_sync_service.get_sync_status(
        db,
        reseller=settings.lead_feed_reseller,
        client=settings.lead_feed_client,
        timezone_name=settings.lead_auto_sync_timezone,
        stale_after_seconds=settings.lead_auto_sync_stale_seconds,
    )


@router.post("/upload-csv", response_model=LeadUploadResult)
async def upload_csv(file: UploadFile = File(...), db: Session = Depends(get_db)):
    """Upload an EarlyBid-format CSV; each row is upserted as a Lead."""
    text = (await file.read()).decode("utf-8-sig")
    rows = lead_feed_service.parse_feed_csv(text)

    created_leads: list[Lead] = []
    try:
        touched, created, updated = lead_feed_service.upsert_feed_rows(
            db,
            rows,
            source_feed=f"upload:{file.filename}",
            identity_scope=lead_feed_service.earlybid_identity_scope(
                settings.lead_feed_reseller,
                settings.lead_feed_client,
            ),
            created_leads=created_leads,
        )
    except LeadFeedValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail=exc.as_detail(),
        ) from exc

    try:
        db.flush()
        jobs = email_generation_service.enqueue_initial_generations(
            db,
            created_leads,
            trigger=EmailGenerationTrigger.csv_upload,
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    for lead in touched:
        db.refresh(lead)
    return LeadUploadResult(
        items=touched,
        created=created,
        updated=updated,
        total=len(rows),
        generation_queued=len(jobs),
    )


@router.get("", response_model=list[LeadListRead])
def list_leads(db: Session = Depends(get_db)):
    leads = db.scalars(
        select(Lead).order_by(Lead.score.desc().nullslast())
    ).all()
    return [
        LeadListRead(
            **LeadRead.model_validate(lead).model_dump(),
            current_email=email_generation_service.current_email_for_lead(
                db, lead.id
            ),
            latest_generation=email_generation_service.latest_generation_job(
                db, lead.id
            ),
        )
        for lead in leads
    ]


@router.get("/{lead_id}/workspace", response_model=LeadWorkspaceRead)
def get_lead_workspace(lead_id: str, db: Session = Depends(get_db)):
    """Return opportunity details, email history, and current generation state."""

    lead = db.get(Lead, _canonical_lead_id(lead_id))
    if lead is None:
        raise HTTPException(status_code=404, detail="Lead not found")
    emails = email_generation_service.emails_for_lead(db, lead.id)
    current_email = emails[0] if emails else None
    return LeadWorkspaceRead(
        lead=lead,
        emails=emails,
        current_email_id=current_email.id if current_email else None,
        current_email_is_stale=email_generation_service.current_email_is_stale(
            lead, current_email
        ),
        latest_generation=email_generation_service.latest_generation_job(
            db, lead.id
        ),
    )


@router.post(
    "/{lead_id}/email-generations",
    response_model=EmailGenerationJobRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def request_email_generation(
    lead_id: str,
    payload: EmailGenerationRequest,
    db: Session = Depends(get_db),
):
    """Idempotently queue a manual first draft, regeneration, or retry."""

    try:
        return email_generation_service.enqueue_generation(
            db,
            lead_id=lead_id,
            idempotency_key=str(payload.idempotency_key),
        )
    except email_generation_service.LeadNotFoundError:
        raise HTTPException(status_code=404, detail="Lead not found") from None
    except email_generation_service.IdempotencyKeyConflictError:
        raise HTTPException(
            status_code=409,
            detail="Idempotency key is already in use",
        ) from None
    except email_generation_service.EmailGenerationPersistenceError:
        raise HTTPException(
            status_code=500,
            detail="Email generation could not be queued",
        ) from None


def _canonical_lead_id(lead_id: str) -> str:
    try:
        return str(UUID(lead_id))
    except (AttributeError, ValueError):
        raise HTTPException(status_code=404, detail="Lead not found") from None
