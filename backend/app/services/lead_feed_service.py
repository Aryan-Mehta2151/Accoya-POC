"""EarlyBid lead feed client, normalization, and persistence.

The standalone agent normalizer is the single interpretation layer for both
remote feed sync and uploaded CSV files. This module only adapts that typed
result to the current lead projection and applies the source-scoped upsert.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy import select, text as sql_text
from sqlalchemy.orm import Session

from agent.normalization import EARLYBID_NATURAL_ID_PREFIX, normalize_lead
from app.config import get_settings
from app.db.models import EmailGenerationTrigger, Lead
from app.services.email_generation_service import enqueue_initial_generations

settings = get_settings()
EARLYBID_SOURCE_SYSTEM = "earlybid"


class LeadFeedError(RuntimeError):
    """Raised when the EarlyBid feed cannot be fetched or parsed."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class LeadFeedConfigurationError(LeadFeedError):
    """Raised when required EarlyBid client configuration is unavailable."""


@dataclass(frozen=True)
class LeadFeedValidationIssue:
    """A safe, row-scoped feed error that contains no lead values."""

    row_number: int
    reason_code: str

    def as_dict(self) -> dict[str, int | str]:
        return {"row": self.row_number, "reason": self.reason_code}


class LeadFeedValidationError(LeadFeedError):
    """Raised after the complete feed fails validation before persistence."""

    code = "invalid_lead_feed"

    def __init__(self, issues: Iterable[LeadFeedValidationIssue]):
        self.issues = tuple(issues)
        super().__init__("Lead feed validation failed")

    def as_detail(self) -> dict[str, Any]:
        """Return a safe HTTP detail without source-row values or exceptions."""
        return {
            "code": self.code,
            "message": str(self),
            "issues": [issue.as_dict() for issue in self.issues],
        }


@dataclass(frozen=True)
class StagedLeadSync:
    """Counts staged by one feed ingestion before the caller commits."""

    created: int
    updated: int
    total: int
    feed: str
    generation_queued: int

    def as_dict(self) -> dict[str, int | str]:
        return {
            "created": self.created,
            "updated": self.updated,
            "total": self.total,
            "feed": self.feed,
            "generation_queued": self.generation_queued,
        }


def earlybid_identity_scope(
    reseller: str,
    client: str,
    *,
    source_system: str = EARLYBID_SOURCE_SYSTEM,
) -> str:
    """Build the stable source scope used for backend-owned lead identities."""
    parts = tuple(part.strip().casefold() for part in (source_system, reseller, client))
    if any(not part for part in parts):
        raise ValueError("source_system, reseller, and client must not be blank")
    # JSON avoids ambiguous boundaries if a source name contains punctuation.
    return json.dumps(parts, ensure_ascii=False, separators=(",", ":"))


def _headers() -> dict[str, str]:
    if not settings.lead_api_key:
        raise LeadFeedConfigurationError("LEAD_API_KEY is not set")
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


def fetch_feed_rows(reseller: str, client: str) -> list[dict[str, str | None]]:
    """Fetch and parse the current feed without opening a database transaction."""

    return parse_feed_csv(fetch_latest_csv(reseller, client))


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
    identity_scope: str | None = None,
) -> dict[str, Any]:
    """Normalize a source row into the persisted current lead projection."""
    normalized = normalize_lead(row, identity_scope=identity_scope)
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


def _validation_reason(exc: TypeError | ValueError) -> str:
    """Classify normalization failures without returning exception text."""
    if isinstance(exc, TypeError):
        return "invalid_row"
    message = str(exc).casefold()
    if "display rank" in message:
        return "invalid_explicit_identity"
    if any(term in message for term in ("project", "location", "identity_scope")):
        return "invalid_natural_identity"
    return "invalid_lead"


def upsert_feed_rows(
    db: Session,
    rows: Iterable[Mapping[str, Any]],
    *,
    source_feed: str,
    source_system: str = EARLYBID_SOURCE_SYSTEM,
    identity_scope: str | None = None,
    created_leads: list[Lead] | None = None,
) -> tuple[list[Lead], int, int]:
    """Validate, stage, and upsert a complete source-scoped feed."""
    source_system = source_system.strip()
    if not source_system:
        raise ValueError("source_system must not be blank")

    prepared: dict[str, tuple[dict[str, Any], int, bool]] = {}
    issues: list[LeadFeedValidationIssue] = []
    for row_number, row in enumerate(rows, start=2):
        try:
            fields = normalized_row_fields(
                row,
                source_feed=source_feed,
                source_system=source_system,
                identity_scope=identity_scope,
            )
        except (TypeError, ValueError) as exc:
            issues.append(
                LeadFeedValidationIssue(
                    row_number=row_number,
                    reason_code=_validation_reason(exc),
                )
            )
            continue

        external_id = fields["external_id"]
        is_derived = external_id.startswith(EARLYBID_NATURAL_ID_PREFIX)
        previous = prepared.get(external_id)
        if previous is not None and (is_derived or previous[2]):
            previous_fields, previous_row_number, _ = previous
            if fields["raw_data"] != previous_fields["raw_data"]:
                issues.extend(
                    (
                        LeadFeedValidationIssue(
                            row_number=previous_row_number,
                            reason_code="derived_identity_collision",
                        ),
                        LeadFeedValidationIssue(
                            row_number=row_number,
                            reason_code="derived_identity_collision",
                        ),
                    )
                )
            continue

        # Explicit source identifiers retain the existing last-row-wins behavior.
        prepared[external_id] = (fields, row_number, is_derived)

    if issues:
        unique_issues = tuple(dict.fromkeys(issues))
        raise LeadFeedValidationError(unique_issues)

    if not prepared:
        return [], 0, 0

    _lock_source_scope(
        db,
        source_system=source_system,
        identity_scope=identity_scope or source_feed,
    )

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
    for external_id, (fields, _, _) in prepared.items():
        lead = by_external_id.get(external_id)
        if lead is None:
            lead = Lead(**fields)
            db.add(lead)
            if created_leads is not None:
                created_leads.append(lead)
            created += 1
        else:
            for attr, value in fields.items():
                setattr(lead, attr, value)
            updated += 1
        touched.append(lead)
    return touched, created, updated


def stage_feed_sync(
    db: Session,
    rows: list[Mapping[str, Any]],
    *,
    reseller: str,
    client: str,
) -> StagedLeadSync:
    """Stage lead upserts and first-draft jobs without committing them."""

    source_feed = f"{reseller}/{client}"
    created_leads: list[Lead] = []
    _, created, updated = upsert_feed_rows(
        db,
        rows,
        source_feed=source_feed,
        identity_scope=earlybid_identity_scope(reseller, client),
        created_leads=created_leads,
    )
    db.flush()
    jobs = enqueue_initial_generations(
        db,
        created_leads,
        trigger=EmailGenerationTrigger.earlybid_sync,
    )
    # Surface uniqueness or queue-persistence failures before a scheduled run
    # is marked successful in the same transaction.
    db.flush()
    return StagedLeadSync(
        created=created,
        updated=updated,
        total=len(rows),
        feed=source_feed,
        generation_queued=len(jobs),
    )


def sync_feed(db: Session, reseller: str, client: str) -> dict[str, int | str]:
    """Fetch outside a transaction, then atomically persist the current feed."""

    rows = fetch_feed_rows(reseller, client)
    try:
        staged = stage_feed_sync(
            db,
            rows,
            reseller=reseller,
            client=client,
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return staged.as_dict()


def _lock_source_scope(
    db: Session,
    *,
    source_system: str,
    identity_scope: str,
) -> None:
    """Serialize one PostgreSQL ingestion scope for race-free create detection."""

    bind = db.get_bind()
    if bind.dialect.name != "postgresql":
        return
    material = f"{source_system}\0{identity_scope}".encode("utf-8")
    lock_key = int.from_bytes(
        hashlib.sha256(material).digest()[:8],
        byteorder="big",
        signed=True,
    )
    db.execute(
        sql_text("SELECT pg_advisory_xact_lock(:lock_key)"),
        {"lock_key": lock_key},
    )
