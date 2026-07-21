"""EarlyBid lead feed client, normalization, and persistence.

The standalone agent normalizer is the single interpretation layer for both
remote feed sync and uploaded CSV files. This module only adapts that typed
result to the current lead projection and applies the source-scoped upsert.
"""
from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterable, Mapping
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from agent.normalization import normalize_lead
from app.config import get_settings
from app.db.models import Lead

settings = get_settings()
EARLYBID_SOURCE_SYSTEM = "earlybid"


class LeadFeedError(RuntimeError):
    """Raised when the EarlyBid feed cannot be fetched or parsed."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def _headers() -> dict[str, str]:
    if not settings.lead_api_key:
        raise LeadFeedError("LEAD_API_KEY is not set")
    return {"Authorization": f"Bearer {settings.lead_api_key}"}


def fetch_manifest(reseller: str, client: str) -> dict:
    url = f"{settings.lead_api_base_url}/v1/feeds/{reseller}/{client}/latest.json"
    try:
        resp = httpx.get(url, headers=_headers(), timeout=30)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPStatusError as exc:
        raise LeadFeedError(
            f"EarlyBid manifest request failed with {exc.response.status_code}",
            exc.response.status_code,
        ) from exc
    except httpx.HTTPError as exc:
        raise LeadFeedError(f"EarlyBid manifest request failed: {exc}") from exc


def fetch_latest_csv(reseller: str, client: str) -> str:
    url = f"{settings.lead_api_base_url}/v1/feeds/{reseller}/{client}/latest.csv"
    try:
        resp = httpx.get(url, headers=_headers(), timeout=60)
        resp.raise_for_status()
        return resp.text
    except httpx.HTTPStatusError as exc:
        raise LeadFeedError(
            f"EarlyBid CSV request failed with {exc.response.status_code}",
            exc.response.status_code,
        ) from exc
    except httpx.HTTPError as exc:
        raise LeadFeedError(f"EarlyBid CSV request failed: {exc}") from exc


def parse_feed_csv(text: str) -> list[dict[str, str | None]]:
    """Parse an EarlyBid CSV without interpreting any field values."""
    reader = csv.DictReader(io.StringIO(text))
    return [dict(row) for row in reader]


def _json_safe_mapping(row: Mapping[str, Any]) -> dict[str, Any]:
    """Return a detached JSON value suitable for the PostgreSQL JSONB column."""
    return json.loads(json.dumps(dict(row), default=str))


def normalized_row_fields(
    row: Mapping[str, Any],
    *,
    source_feed: str,
    source_system: str = EARLYBID_SOURCE_SYSTEM,
) -> dict[str, Any]:
    """Normalize a source row into the persisted current lead projection."""
    normalized = normalize_lead(row)
    contact_email = next(
        (contact.email for contact in normalized.contacts if contact.email),
        None,
    )
    return {
        "source_system": source_system,
        "external_id": normalized.lead_id,
        "section": normalized.section,
        "project": normalized.project,
        "location": normalized.location,
        "state": normalized.state,
        "signal": normalized.signal,
        "intelligence": normalized.intelligence,
        "score": normalized.score,
        "timing": normalized.timing,
        "next_step": normalized.next_step,
        "awarded_to": normalized.awarded_to,
        "priority_reasons": normalized.priority_reasons,
        "summary": normalized.summary,
        "contacts": normalized.contacts_raw,
        "contact_email": contact_email,
        "meeting_date": normalized.meeting_date_raw,
        "tags": list(normalized.tags),
        "url": normalized.url,
        "raw_data": _json_safe_mapping(row),
        "source_feed": source_feed,
        "archived_at": None,
    }


def upsert_feed_rows(
    db: Session,
    rows: Iterable[Mapping[str, Any]],
    *,
    source_feed: str,
    source_system: str = EARLYBID_SOURCE_SYSTEM,
) -> tuple[list[Lead], int, int]:
    """Stage normalized rows and return touched leads and create/update counts."""
    source_system = source_system.strip()
    if not source_system:
        raise ValueError("source_system must not be blank")

    prepared: dict[str, dict[str, Any]] = {}
    for row in rows:
        try:
            fields = normalized_row_fields(
                row,
                source_feed=source_feed,
                source_system=source_system,
            )
        except (TypeError, ValueError):
            continue
        prepared[fields["external_id"]] = fields

    if not prepared:
        return [], 0, 0

    existing_leads = db.scalars(
        select(Lead).where(
            Lead.source_system == source_system,
            Lead.external_id.in_(list(prepared)),
        )
    ).all()
    by_external_id = {lead.external_id: lead for lead in existing_leads}

    touched: list[Lead] = []
    created = 0
    updated = 0
    for external_id, fields in prepared.items():
        lead = by_external_id.get(external_id)
        if lead is None:
            lead = Lead(**fields)
            db.add(lead)
            created += 1
        else:
            for attr, value in fields.items():
                setattr(lead, attr, value)
            updated += 1
        touched.append(lead)
    return touched, created, updated


def sync_feed(db: Session, reseller: str, client: str) -> dict[str, int | str]:
    """Pull and upsert the current EarlyBid lead projection."""
    source_feed = f"{reseller}/{client}"
    rows = parse_feed_csv(fetch_latest_csv(reseller, client))
    _, created, updated = upsert_feed_rows(db, rows, source_feed=source_feed)
    db.commit()
    return {
        "created": created,
        "updated": updated,
        "total": len(rows),
        "feed": source_feed,
    }
