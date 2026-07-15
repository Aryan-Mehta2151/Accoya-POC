"""Strategy document upload (S3) and listing.

After upload to S3, trigger a Bedrock Knowledge Base ingestion job (or rely on
scheduled sync) so the doc becomes searchable. Ingestion wiring is a TODO.
"""
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db.models import StrategyDocument
from app.services import s3_service

router = APIRouter(prefix="/documents", tags=["documents"])


@router.post("/upload")
async def upload_document(file: UploadFile = File(...), db: Session = Depends(get_db)):
    file_bytes = await file.read()
    key = f"{uuid.uuid4()}-{file.filename}"
    try:
        s3_service.upload_strategy_doc(file_bytes, key, file.content_type)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"S3 upload failed: {exc}") from exc

    doc = StrategyDocument(
        filename=file.filename,
        s3_key=key,
        content_type=file.content_type,
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)

    # TODO: start a Bedrock KB ingestion job so this doc is indexed for RAG.
    try:
        url = s3_service.presigned_url(key)
    except Exception:
        # Upload succeeded, but URL generation can fail if credentials/policy are incomplete.
        url = None

    return {
        "id": key,
        "s3_key": key,
        "filename": file.filename,
        "url": url,
    }


@router.get("")
def list_documents(db: Session = Depends(get_db)):
    # Source of truth for the UI is the bucket contents.
    try:
        return s3_service.list_strategy_docs()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"S3 list failed: {exc}") from exc


@router.delete("/{doc_id:path}")
def delete_document(doc_id: str, db: Session = Depends(get_db)):
    """Delete a document from S3. `doc_id` is the S3 key."""
    try:
        s3_service.delete_strategy_doc(doc_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to delete from S3: {exc}") from exc

    # Best-effort metadata cleanup for previously inserted records.
    doc = db.scalar(select(StrategyDocument).where(StrategyDocument.s3_key == doc_id))
    if doc:
        db.delete(doc)
        db.commit()

    return {"deleted": True, "s3_key": doc_id}
