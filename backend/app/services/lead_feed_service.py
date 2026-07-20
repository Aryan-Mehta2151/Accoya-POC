"""EarlyBid lead feed client: fetch, parse, and upsert opportunities.

Feed: GET /v1/feeds/{reseller}/{client}/latest.csv  (Bearer auth)
Schema: earlystack_client_feed_v1 (column order pinned).
"""
import csv
import io
import json
import re

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import Lead

settings = get_settings()


class LeadFeedError(RuntimeError):
    """Raised when the EarlyBid feed cannot be fetched or parsed."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code

# Maps CSV header -> Lead attribute.
_COLUMN_MAP = {
    "id": "external_id",
    "Section": "section",
    "Project": "project",
    "Location": "location",
    "State": "state",
    "Signal": "signal",
    "Intelligence": "intelligence",
    "Score": "score",
    "Timing": "timing",
    "Awarded To": "awarded_to",
    "Priority Reasons": "priority_reasons",
    "Summary": "summary",
    "Contacts": "contacts",
    "Meeting Date": "meeting_date",
    "Tags": "tags",
    "URL": "url",
}

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


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


def parse_feed_csv(text: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(text))
    return [row for row in reader]


def _extract_email(contacts: str | None) -> str | None:
    if not contacts:
        return None
    match = _EMAIL_RE.search(contacts)
    return match.group(0) if match else None


def _row_to_fields(row: dict, source_feed: str) -> dict:
    fields: dict = {}
    for column, attr in _COLUMN_MAP.items():
        value = row.get(column)
        if value == "":
            value = None
        if attr == "score" and value is not None:
            try:
                value = int(value)
            except (TypeError, ValueError):
                value = None
        fields[attr] = value
    fields["contact_email"] = _extract_email(row.get("Contacts"))
    fields["raw_data"] = json.dumps(row)
    fields["source_feed"] = source_feed
    return fields


def sync_feed(db: Session, reseller: str, client: str) -> dict:
    """Pull the latest feed and upsert leads on `external_id`.

    Returns a summary: {created, updated, total}.
    """
    source_feed = f"{reseller}/{client}"
    rows = parse_feed_csv(fetch_latest_csv(reseller, client))

    created = 0
    updated = 0
    for row in rows:
        external_id = row.get("id")
        if not external_id:
            continue
        fields = _row_to_fields(row, source_feed)

        existing = db.scalar(select(Lead).where(Lead.external_id == external_id))
        if existing:
            for attr, value in fields.items():
                setattr(existing, attr, value)
            updated += 1
        else:
            db.add(Lead(**fields))
            created += 1

    db.commit()
    return {"created": created, "updated": updated, "total": len(rows), "feed": source_feed}
