"""Durable Microsoft Graph mailbox synchronization and reply correlation."""

from __future__ import annotations

import hmac
import re
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote, unquote, urlencode, urlparse

import httpx
from sqlalchemy import distinct, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import (
    AgentRun,
    Email,
    EmailDeliveryJob,
    EmailReply,
    EmailReplyClassification,
    EmailReplyMatchMethod,
    GraphMailNotification,
    GraphMailboxSyncState,
    GraphMailboxSyncStatus,
    Lead,
    LeadReviewStatus,
)
from app.services import email_service


_GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
_MESSAGE_ID_PATTERN = re.compile(r"<[^<>\s]+>")
_SELECT_FIELDS = ",".join(
    (
        "id",
        "internetMessageId",
        "conversationId",
        "from",
        "sender",
        "replyTo",
        "toRecipients",
        "receivedDateTime",
        "sentDateTime",
        "isRead",
        "isDraft",
        "parentFolderId",
        "internetMessageHeaders",
    )
)


class GraphReplyError(RuntimeError):
    """Safe Microsoft Graph failure used by the reply worker."""

    def __init__(self, code: str, *, reset_delta: bool = False):
        super().__init__(code)
        self.code = code
        self.reset_delta = reset_delta


class GraphMessageMissing(GraphReplyError):
    """The notified message no longer exists in the primary mailbox."""


@dataclass(frozen=True)
class MailboxSyncClaim:
    mailbox_email: str
    worker_id: str
    claimed_at: datetime
    requested_at: datetime | None


@dataclass(frozen=True)
class _MessageMatch:
    lead_id: str
    email_id: str | None
    delivery_job_id: str | None
    method: EmailReplyMatchMethod


@dataclass(frozen=True)
class _NotificationClaim:
    notification_id: str
    graph_message_id: str
    requested_at: datetime


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def reply_configuration_error(settings: Settings) -> str | None:
    """Return a safe configuration code when reply tracking cannot start."""

    if not settings.email_reply_tracking_enabled:
        return "reply_tracking_disabled"
    if not email_service.microsoft_graph_is_configured(settings):
        return "microsoft_graph_not_configured"
    notification_url = urlparse(settings.microsoft_graph_notification_url)
    expected_path = (
        f"{settings.api_prefix.rstrip('/')}/microsoft-graph/mail-notifications"
    )
    if (
        notification_url.scheme != "https"
        or not notification_url.netloc
        or notification_url.hostname is None
        or notification_url.username is not None
        or notification_url.password is not None
        or notification_url.path != expected_path
        or notification_url.params
        or notification_url.query
        or notification_url.fragment
    ):
        return "graph_notification_url_invalid"
    if len(settings.microsoft_graph_client_state.encode("utf-8")) < 32:
        return "graph_client_state_invalid"
    timings = (
        settings.email_reply_worker_poll_seconds,
        settings.email_reply_reconcile_seconds,
        settings.email_reply_heartbeat_seconds,
        settings.email_reply_stale_seconds,
    )
    if min(timings) <= 0:
        return "reply_worker_timing_invalid"
    if settings.email_reply_stale_seconds <= settings.email_reply_heartbeat_seconds:
        return "reply_worker_stale_threshold_invalid"
    if settings.email_reply_backfill_days <= 0:
        return "reply_backfill_days_invalid"
    return None


class GraphMailClient:
    """Small synchronous Graph client that never persists message content."""

    def __init__(
        self,
        settings: Settings,
        *,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self.settings = settings
        try:
            self.token = email_service.graph_access_token(settings)
        except email_service.EmailDeliveryFailure as exc:
            raise GraphReplyError(exc.code) from exc
        self._sleep = sleeper

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "Prefer": 'IdType="ImmutableId"',
        }

    def request(
        self,
        method: str,
        path_or_url: str,
        *,
        json: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        url = (
            path_or_url
            if path_or_url.startswith("https://")
            else f"{_GRAPH_ROOT}{path_or_url}"
        )
        response: httpx.Response | None = None
        last_exception: httpx.HTTPError | None = None
        for attempt in range(3):
            try:
                response = httpx.request(
                    method,
                    url,
                    headers=self.headers,
                    json=json,
                    timeout=self.settings.microsoft_graph_timeout_seconds,
                )
            except httpx.HTTPError as exc:
                last_exception = exc
                if attempt < 2:
                    self._sleep(float(2**attempt))
                    continue
                raise GraphReplyError("microsoft_graph_request_interrupted") from exc
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < 2:
                    self._sleep(_retry_delay(response, attempt))
                    continue
            break
        if response is None:
            raise GraphReplyError("microsoft_graph_request_interrupted") from last_exception
        if response.status_code == 404:
            raise GraphMessageMissing("microsoft_graph_message_missing")
        if response.status_code == 410:
            raise GraphReplyError("microsoft_graph_delta_expired", reset_delta=True)
        if response.status_code in (401, 403):
            raise GraphReplyError("microsoft_graph_authentication_failed")
        if response.status_code == 409:
            raise GraphReplyError("microsoft_graph_subscription_conflict")
        if response.status_code == 429:
            raise GraphReplyError("microsoft_graph_throttled")
        if response.status_code >= 500:
            raise GraphReplyError("microsoft_graph_temporarily_unavailable")
        if not 200 <= response.status_code < 300:
            raise GraphReplyError("microsoft_graph_request_rejected")
        if response.status_code == 204 or not response.content:
            return {}
        try:
            payload = response.json()
        except ValueError as exc:
            raise GraphReplyError("microsoft_graph_response_invalid") from exc
        if not isinstance(payload, dict):
            raise GraphReplyError("microsoft_graph_response_invalid")
        return payload

    def get_message(self, mailbox_email: str, graph_message_id: str) -> dict[str, Any]:
        mailbox = quote(mailbox_email, safe="")
        message_id = quote(graph_message_id, safe="")
        return self.request(
            "GET",
            f"/users/{mailbox}/messages/{message_id}?$select={_SELECT_FIELDS}",
        )


def ensure_mailbox_state(
    db: Session,
    *,
    mailbox_email: str,
    backfill_days: int,
    now: datetime | None = None,
) -> GraphMailboxSyncState:
    """Create the singleton mailbox state without changing an existing cutoff."""

    observed_at = now or _utc_now()
    mailbox = mailbox_email.strip().casefold()
    state = db.get(GraphMailboxSyncState, mailbox)
    if state is None:
        state = GraphMailboxSyncState(
            mailbox_email=mailbox,
            status=GraphMailboxSyncStatus.initializing,
            backfill_cutoff_at=observed_at - timedelta(days=backfill_days),
            next_sync_at=observed_at,
            requested_at=observed_at,
            force_resync=True,
        )
        db.add(state)
        db.commit()
    return state


def record_notifications(
    db: Session,
    *,
    payload: object,
    settings: Settings,
    now: datetime | None = None,
) -> int:
    """Validate and durably coalesce a Graph notification collection."""

    if not settings.email_reply_tracking_enabled or not isinstance(payload, dict):
        return 0
    values = payload.get("value")
    if not isinstance(values, list):
        return 0
    mailbox = str(settings.microsoft_sender_email).strip().casefold()
    state = db.get(GraphMailboxSyncState, mailbox)
    if state is None or not state.subscription_id:
        return 0
    observed_at = now or _utc_now()
    accepted = 0

    expected_subscription_id = state.subscription_id
    pending_changes: dict[str, tuple[str, str]] = {}
    for raw in values:
        if not isinstance(raw, dict):
            continue
        client_state = raw.get("clientState")
        subscription_id = raw.get("subscriptionId")
        tenant_id = raw.get("tenantId")
        if not isinstance(client_state, str) or not hmac.compare_digest(
            client_state,
            settings.microsoft_graph_client_state,
        ):
            continue
        if subscription_id != expected_subscription_id:
            continue
        if (
            not isinstance(tenant_id, str)
            or tenant_id.casefold() != settings.microsoft_tenant_id.casefold()
        ):
            continue

        lifecycle_event = raw.get("lifecycleEvent")
        if lifecycle_event in {
            "missed",
            "subscriptionRemoved",
            "reauthorizationRequired",
        }:
            state.requested_at = observed_at
            if lifecycle_event in {"missed", "subscriptionRemoved"}:
                state.force_resync = True
                state.mailbox_scan_link = None
                state.sent_scan_link = None
                state.sent_backfill_completed_at = None
            if lifecycle_event == "reauthorizationRequired":
                # Force a PATCH even when the provider-reported expiry is still
                # more than 24 hours away.
                state.subscription_expires_at = observed_at
            if lifecycle_event == "subscriptionRemoved":
                state.subscription_id = None
                state.subscription_expires_at = None
            accepted += 1
            continue

        resource_data = raw.get("resourceData")
        message_id = (
            resource_data.get("id") if isinstance(resource_data, dict) else None
        )
        change_type = raw.get("changeType")
        if not isinstance(message_id, str) or not message_id:
            continue
        if change_type not in {"created", "updated", "deleted"}:
            continue
        pending_changes[message_id] = (change_type, str(subscription_id))
        accepted += 1

    for message_id, (change_type, subscription_id) in pending_changes.items():
        notification = db.scalar(
            select(GraphMailNotification).where(
                GraphMailNotification.mailbox_email == mailbox,
                GraphMailNotification.graph_message_id == message_id,
            )
        )
        if notification is None:
            notification = GraphMailNotification(
                id=str(uuid.uuid4()),
                mailbox_email=mailbox,
                graph_message_id=message_id,
                change_type=change_type,
                subscription_id=str(subscription_id),
                requested_at=observed_at,
            )
            try:
                with db.begin_nested():
                    db.add(notification)
                    db.flush()
            except IntegrityError:
                notification = db.scalar(
                    select(GraphMailNotification).where(
                        GraphMailNotification.mailbox_email == mailbox,
                        GraphMailNotification.graph_message_id == message_id,
                    )
                )
                if notification is None:
                    raise
        notification.change_type = change_type
        notification.subscription_id = str(subscription_id)
        notification.requested_at = observed_at
        notification.error_code = None
        state.requested_at = observed_at

    if not accepted:
        db.rollback()
        return 0
    try:
        db.commit()
    except IntegrityError:
        # Concurrent duplicate delivery is harmless. The periodic delta pass
        # remains authoritative and will pick up the same mailbox state.
        db.rollback()
        state = db.get(GraphMailboxSyncState, mailbox)
        if state is not None:
            state.requested_at = observed_at
            state.force_resync = True
            db.commit()
    return accepted


def recover_stale_sync(
    db: Session,
    *,
    mailbox_email: str,
    stale_after_seconds: float,
    now: datetime | None = None,
) -> bool:
    """Release a mailbox lease whose heartbeat has expired."""

    observed_at = now or _utc_now()
    mailbox = mailbox_email.strip().casefold()
    state = db.scalar(
        select(GraphMailboxSyncState)
        .where(GraphMailboxSyncState.mailbox_email == mailbox)
        .with_for_update()
    )
    heartbeat = _as_utc(state.heartbeat_at) if state else None
    stale_before = observed_at - timedelta(seconds=stale_after_seconds)
    mailbox_stale = bool(
        state
        and state.status is GraphMailboxSyncStatus.running
        and heartbeat is not None
        and heartbeat <= stale_before
    )
    notifications = db.scalars(
        select(GraphMailNotification)
        .where(
            GraphMailNotification.mailbox_email == mailbox,
            GraphMailNotification.claimed_by.is_not(None),
            or_(
                GraphMailNotification.heartbeat_at <= stale_before,
                GraphMailNotification.heartbeat_at.is_(None),
            ),
        )
        .with_for_update(skip_locked=True)
    ).all()
    if not mailbox_stale and not notifications:
        db.rollback()
        return False
    if mailbox_stale and state is not None:
        state.status = GraphMailboxSyncStatus.error
        state.error_code = "reply_sync_lease_expired"
        state.claimed_by = None
        state.claimed_at = None
        state.heartbeat_at = None
        state.next_sync_at = observed_at
        state.force_resync = True
        state.mailbox_scan_link = None
        state.sent_scan_link = None
        state.sent_backfill_completed_at = None
    for notification in notifications:
        notification.claimed_by = None
        notification.claimed_at = None
        notification.heartbeat_at = None
        notification.error_code = "reply_notification_lease_expired"
    db.commit()
    return True


def claim_mailbox_sync(
    db: Session,
    *,
    mailbox_email: str,
    worker_id: str,
    now: datetime | None = None,
) -> MailboxSyncClaim | None:
    """Claim due mailbox work with a committed PostgreSQL lease."""

    observed_at = now or _utc_now()
    mailbox = mailbox_email.strip().casefold()
    pending_notification = db.scalar(
        select(GraphMailNotification.id)
        .where(
            GraphMailNotification.mailbox_email == mailbox,
            or_(
                GraphMailNotification.processed_at.is_(None),
                GraphMailNotification.processed_at
                < GraphMailNotification.requested_at,
            ),
        )
        .limit(1)
    )
    state = db.scalar(
        select(GraphMailboxSyncState)
        .where(GraphMailboxSyncState.mailbox_email == mailbox)
        .with_for_update(skip_locked=True)
    )
    if state is None or state.status is GraphMailboxSyncStatus.running:
        db.rollback()
        return None
    expires = _as_utc(state.subscription_expires_at)
    next_sync_at = _as_utc(state.next_sync_at)
    waiting_after_error = bool(
        state.status is GraphMailboxSyncStatus.error
        and next_sync_at is not None
        and next_sync_at > observed_at
    )
    due = not waiting_after_error and (
        next_sync_at is None
        or next_sync_at <= observed_at
        or pending_notification is not None
        or state.subscription_id is None
        or expires is None
        or expires <= observed_at + timedelta(hours=24)
    )
    if not due:
        db.rollback()
        return None
    state.status = GraphMailboxSyncStatus.running
    state.claimed_by = worker_id
    state.claimed_at = observed_at
    state.heartbeat_at = observed_at
    state.last_started_at = observed_at
    claim = MailboxSyncClaim(
        mailbox_email=mailbox,
        worker_id=worker_id,
        claimed_at=observed_at,
        requested_at=_as_utc(state.requested_at),
    )
    db.commit()
    return claim


def heartbeat_mailbox_sync(db: Session, *, claim: MailboxSyncClaim) -> bool:
    state = db.get(GraphMailboxSyncState, claim.mailbox_email)
    if not _claim_is_current(state, claim):
        db.rollback()
        return False
    state.heartbeat_at = _utc_now()
    db.commit()
    return True


def finalize_mailbox_sync(
    db: Session,
    *,
    claim: MailboxSyncClaim,
    reconcile_seconds: float,
    error_code: str | None = None,
    now: datetime | None = None,
) -> bool:
    """Release the lease without losing notifications received during the run."""

    observed_at = now or _utc_now()
    state = db.scalar(
        select(GraphMailboxSyncState)
        .where(GraphMailboxSyncState.mailbox_email == claim.mailbox_email)
        .with_for_update()
    )
    if not _claim_is_current(state, claim):
        db.rollback()
        return False
    state.status = (
        GraphMailboxSyncStatus.error if error_code else GraphMailboxSyncStatus.idle
    )
    state.error_code = error_code
    state.claimed_by = None
    state.claimed_at = None
    state.heartbeat_at = None
    if error_code is None:
        state.last_succeeded_at = observed_at
    else:
        notifications = db.scalars(
            select(GraphMailNotification)
            .where(
                GraphMailNotification.mailbox_email == claim.mailbox_email,
                GraphMailNotification.claimed_by == claim.worker_id,
            )
            .with_for_update(skip_locked=True)
        ).all()
        for notification in notifications:
            notification.claimed_by = None
            notification.claimed_at = None
            notification.heartbeat_at = None
            notification.error_code = error_code
    request_arrived = (
        _as_utc(state.requested_at) is not None
        and (
            claim.requested_at is None
            or _as_utc(state.requested_at) > claim.requested_at
        )
    )
    state.next_sync_at = (
        observed_at
        if error_code is None and request_arrived
        else observed_at + timedelta(seconds=reconcile_seconds)
    )
    db.commit()
    return True


def _claim_is_current(
    state: GraphMailboxSyncState | None,
    claim: MailboxSyncClaim,
) -> bool:
    return bool(
        state
        and state.status is GraphMailboxSyncStatus.running
        and state.claimed_by == claim.worker_id
        and _as_utc(state.claimed_at) == claim.claimed_at
    )


def _lock_current_claim(db: Session, claim: MailboxSyncClaim) -> GraphMailboxSyncState:
    state = db.scalar(
        select(GraphMailboxSyncState)
        .where(GraphMailboxSyncState.mailbox_email == claim.mailbox_email)
        .with_for_update()
    )
    if not _claim_is_current(state, claim):
        raise GraphReplyError("reply_sync_lease_lost")
    return state


def reply_summary(
    db: Session,
    *,
    settings: Settings,
    now: datetime | None = None,
) -> dict[str, object]:
    """Return active-opportunity unread counts plus explicit sync health."""

    counts = db.execute(
        select(
            func.count(EmailReply.id).filter(EmailReply.is_read.is_(False)),
            func.count(distinct(EmailReply.lead_id)),
        )
        .join(Lead, Lead.id == EmailReply.lead_id)
        .where(
            Lead.review_status == LeadReviewStatus.active,
            EmailReply.classification == EmailReplyClassification.human,
            EmailReply.removed_at.is_(None),
        )
    ).one()
    state = db.get(
        GraphMailboxSyncState,
        str(settings.microsoft_sender_email).strip().casefold(),
    )
    last_sync = _as_utc(state.last_succeeded_at) if state else None
    observed_at = now or _utc_now()
    if not settings.email_reply_tracking_enabled:
        sync_status = "disabled"
    elif state is not None and state.status is GraphMailboxSyncStatus.error:
        sync_status = "error"
    elif state is None or last_sync is None:
        sync_status = "initializing"
    elif last_sync < observed_at - timedelta(minutes=5):
        sync_status = "stale"
    else:
        sync_status = "healthy"
    return {
        "unread_reply_count": int(counts[0] or 0),
        "replied_opportunity_count": int(counts[1] or 0),
        "last_synced_at": last_sync,
        "sync_status": sync_status,
    }


def unread_reply_summaries(
    db: Session,
    lead_ids: Iterable[str],
) -> dict[str, tuple[int, datetime | None]]:
    ids = list(lead_ids)
    if not ids:
        return {}
    rows = db.execute(
        select(
            EmailReply.lead_id,
            func.count(EmailReply.id).filter(EmailReply.is_read.is_(False)),
            func.max(EmailReply.received_at),
        )
        .where(
            EmailReply.lead_id.in_(ids),
            EmailReply.classification == EmailReplyClassification.human,
            EmailReply.removed_at.is_(None),
        )
        .group_by(EmailReply.lead_id)
    ).all()
    return {
        str(lead_id): (int(count), last_reply_at)
        for lead_id, count, last_reply_at in rows
        if lead_id is not None
    }


def process_graph_message(
    db: Session,
    *,
    mailbox_email: str,
    payload: dict[str, Any],
    now: datetime | None = None,
) -> EmailReply | None:
    """Reconcile one metadata-only Graph message as sent mail or a reply."""

    observed_at = now or _utc_now()
    mailbox = mailbox_email.strip().casefold()
    graph_id = payload.get("id")
    if not isinstance(graph_id, str) or not graph_id:
        return None
    headers = _headers(payload.get("internetMessageHeaders"))
    sender_email = _address(payload.get("from")) or _address(payload.get("sender"))
    sender_key = sender_email.casefold() if sender_email else None
    app_message_id = _first_header(headers, "x-accoya-message-id")
    if sender_key == mailbox:
        if app_message_id:
            _reconcile_sent_message(
                db,
                mailbox_email=mailbox,
                graph_message_id=graph_id,
                app_message_id=app_message_id,
                internet_message_id=_clean_message_id(payload.get("internetMessageId")),
                conversation_id=_clean_text(payload.get("conversationId")),
                observed_at=observed_at,
            )
        return None

    received_at = _parse_graph_datetime(payload.get("receivedDateTime"))
    if received_at is None:
        return None
    internet_message_id = _clean_message_id(payload.get("internetMessageId"))
    conversation_id = _clean_text(payload.get("conversationId"))
    reference_ids = _reference_ids(headers)
    match, ambiguous = _match_reply(
        db,
        mailbox_email=mailbox,
        reference_ids=reference_ids,
        conversation_id=conversation_id,
    )
    automatic_kind = _automatic_classification(headers, sender_email)
    if automatic_kind is not None:
        classification = automatic_kind
    elif match is not None:
        classification = EmailReplyClassification.human
    elif ambiguous:
        classification = EmailReplyClassification.ambiguous
    else:
        classification = EmailReplyClassification.unmatched

    conditions = [
        EmailReply.mailbox_email == mailbox,
        EmailReply.graph_message_id == graph_id,
    ]
    existing = db.scalar(select(EmailReply).where(*conditions))
    if existing is None and internet_message_id:
        existing = db.scalar(
            select(EmailReply).where(
                EmailReply.mailbox_email == mailbox,
                EmailReply.internet_message_id == internet_message_id,
            )
        )
    reply = existing or EmailReply(
        id=str(uuid.uuid4()),
        mailbox_email=mailbox,
        graph_message_id=graph_id,
        received_at=received_at,
    )
    if existing is None:
        db.add(reply)
    reply.graph_message_id = graph_id
    reply.internet_message_id = internet_message_id
    reply.conversation_id = conversation_id
    reply.reference_message_ids = reference_ids
    reply.sender_email = sender_email
    reply.received_at = received_at
    reply.is_read = bool(payload.get("isRead", False))
    reply.classification = classification
    reply.removed_at = None
    if match is None:
        reply.lead_id = None
        reply.email_id = None
        reply.delivery_job_id = None
        reply.match_method = EmailReplyMatchMethod.none
    else:
        reply.lead_id = match.lead_id
        reply.email_id = match.email_id
        reply.delivery_job_id = match.delivery_job_id
        reply.match_method = match.method
    # Sessions disable autoflush. Make this upsert visible to subsequent
    # occurrences in the same Graph page without committing its checkpoint.
    db.flush()
    return reply


def mark_graph_message_removed(
    db: Session,
    *,
    mailbox_email: str,
    graph_message_id: str,
    now: datetime | None = None,
) -> bool:
    reply = db.scalar(
        select(EmailReply).where(
            EmailReply.mailbox_email == mailbox_email.strip().casefold(),
            EmailReply.graph_message_id == graph_message_id,
        )
    )
    if reply is None:
        return False
    reply.removed_at = now or _utc_now()
    return True


def reconcile_pending_replies(db: Session, *, mailbox_email: str) -> int:
    """Recheck unresolved and conversation matches after sent identities appear."""

    candidates = db.scalars(
        select(EmailReply).where(
            EmailReply.mailbox_email == mailbox_email.strip().casefold(),
            or_(
                EmailReply.lead_id.is_(None),
                EmailReply.match_method == EmailReplyMatchMethod.conversation,
            ),
            EmailReply.removed_at.is_(None),
        )
    ).all()
    matched = 0
    for reply in candidates:
        match, ambiguous = _match_reply(
            db,
            mailbox_email=reply.mailbox_email,
            reference_ids=reply.reference_message_ids,
            conversation_id=reply.conversation_id,
        )
        if match is None:
            reply.lead_id = None
            reply.email_id = None
            reply.delivery_job_id = None
            reply.match_method = EmailReplyMatchMethod.none
            if reply.classification not in {
                EmailReplyClassification.automatic,
                EmailReplyClassification.bounce,
            }:
                reply.classification = (
                    EmailReplyClassification.ambiguous
                    if ambiguous else EmailReplyClassification.unmatched
                )
            continue
        reply.lead_id = match.lead_id
        reply.email_id = match.email_id
        reply.delivery_job_id = match.delivery_job_id
        reply.match_method = match.method
        if reply.classification in {
            EmailReplyClassification.unmatched,
            EmailReplyClassification.ambiguous,
        }:
            reply.classification = EmailReplyClassification.human
        matched += 1
    db.flush()
    return matched


def _reconcile_sent_message(
    db: Session,
    *,
    mailbox_email: str,
    graph_message_id: str,
    app_message_id: str,
    internet_message_id: str | None,
    conversation_id: str | None,
    observed_at: datetime,
) -> None:
    job = db.scalar(
        select(EmailDeliveryJob).where(
            EmailDeliveryJob.sender_email.ilike(mailbox_email),
            EmailDeliveryJob.message_id == app_message_id,
        )
    )
    if job is None:
        return
    if job.graph_message_id and job.graph_message_id != graph_message_id:
        raise GraphReplyError("sent_message_graph_identity_conflict")
    if (
        job.internet_message_id
        and internet_message_id
        and job.internet_message_id != internet_message_id
    ):
        raise GraphReplyError("sent_message_internet_identity_conflict")
    job.graph_message_id = graph_message_id
    job.internet_message_id = internet_message_id
    job.conversation_id = conversation_id
    job.sent_item_observed_at = observed_at
    db.flush()
    reconcile_pending_replies(db, mailbox_email=mailbox_email)


def _match_reply(
    db: Session,
    *,
    mailbox_email: str,
    reference_ids: list[str],
    conversation_id: str | None,
) -> tuple[_MessageMatch | None, bool]:
    if reference_ids:
        rows = _delivery_matches(
            db,
            EmailDeliveryJob.internet_message_id.in_(reference_ids),
        )
        match = _unique_delivery_match(rows, EmailReplyMatchMethod.references)
        if match is not None:
            return match, False
        if len({row[2] for row in rows}) > 1:
            return None, True

    if not conversation_id:
        return None, False
    rows = _delivery_matches(
        db,
        EmailDeliveryJob.conversation_id == conversation_id,
    )
    reply_rows = db.execute(
        select(EmailReply.lead_id, EmailReply.email_id, EmailReply.delivery_job_id)
        .where(
            EmailReply.mailbox_email == mailbox_email,
            EmailReply.conversation_id == conversation_id,
            EmailReply.lead_id.is_not(None),
            # A fallback attribution is not independent evidence: retaining
            # it here would let stale matches perpetuate themselves.
            EmailReply.match_method == EmailReplyMatchMethod.references,
        )
    ).all()
    lead_ids = {row[2] for row in rows}
    lead_ids.update(str(row[0]) for row in reply_rows if row[0] is not None)
    if len(lead_ids) != 1:
        return None, len(lead_ids) > 1
    match = _unique_delivery_match(rows, EmailReplyMatchMethod.conversation)
    if match is not None:
        return match, False
    previous = reply_rows[0]
    return (
        _MessageMatch(
            lead_id=str(previous[0]),
            email_id=str(previous[1]) if previous[1] else None,
            delivery_job_id=str(previous[2]) if previous[2] else None,
            method=EmailReplyMatchMethod.conversation,
        ),
        False,
    )


def _delivery_matches(db: Session, criterion: object) -> list[tuple[Any, str, str]]:
    return list(
        db.execute(
            select(EmailDeliveryJob, Email.id, AgentRun.lead_id)
            .join(Email, Email.id == EmailDeliveryJob.email_id)
            .join(AgentRun, AgentRun.id == Email.agent_run_id)
            .where(criterion)
            .order_by(EmailDeliveryJob.queued_at.desc())
        ).all()
    )


def _unique_delivery_match(
    rows: list[tuple[Any, str, str]],
    method: EmailReplyMatchMethod,
) -> _MessageMatch | None:
    lead_ids = {str(row[2]) for row in rows}
    if len(lead_ids) != 1 or not rows:
        return None
    job, email_id, lead_id = rows[0]
    return _MessageMatch(
        lead_id=str(lead_id),
        email_id=str(email_id),
        delivery_job_id=str(job.id),
        method=method,
    )


def _headers(value: object) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    if not isinstance(value, list):
        return result
    for raw in value:
        if not isinstance(raw, dict):
            continue
        name = raw.get("name")
        header_value = raw.get("value")
        if isinstance(name, str) and isinstance(header_value, str):
            result.setdefault(name.strip().casefold(), []).append(header_value.strip())
    return result


def _first_header(headers: dict[str, list[str]], name: str) -> str | None:
    values = headers.get(name.casefold(), [])
    return values[0] if values else None


def _reference_ids(headers: dict[str, list[str]]) -> list[str]:
    result: list[str] = []
    for name in ("in-reply-to", "references"):
        for value in headers.get(name, []):
            matches = _MESSAGE_ID_PATTERN.findall(value)
            candidates = matches or value.split()
            for candidate in candidates:
                cleaned = _clean_message_id(candidate)
                if cleaned and cleaned not in result:
                    result.append(cleaned)
    return result


def _automatic_classification(
    headers: dict[str, list[str]],
    sender_email: str | None,
) -> EmailReplyClassification | None:
    local_part = (sender_email or "").split("@", 1)[0].casefold()
    content_type = " ".join(headers.get("content-type", [])).casefold()
    if local_part in {"mailer-daemon", "postmaster"} or "multipart/report" in content_type:
        return EmailReplyClassification.bounce
    auto_submitted = " ".join(headers.get("auto-submitted", [])).strip().casefold()
    if auto_submitted and auto_submitted != "no":
        return EmailReplyClassification.automatic
    if any(
        name in headers
        for name in ("x-autoreply", "x-autorespond")
    ):
        return EmailReplyClassification.automatic
    precedence = " ".join(headers.get("precedence", [])).casefold()
    if precedence in {"bulk", "junk", "list"}:
        return EmailReplyClassification.automatic
    return None


def _address(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    email_address = value.get("emailAddress")
    if not isinstance(email_address, dict):
        return None
    address = email_address.get("address")
    return address.strip() if isinstance(address, str) and address.strip() else None


def _clean_text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _clean_message_id(value: object) -> str | None:
    return _clean_text(value)


def _parse_graph_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _as_utc(parsed)


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    raw = response.headers.get("Retry-After")
    if raw:
        try:
            return min(max(float(raw), 0.0), 30.0)
        except ValueError:
            pass
    return float(2**attempt)


def synchronize_mailbox(
    session_factory: Callable[[], Session],
    *,
    claim: MailboxSyncClaim,
    settings: Settings,
) -> None:
    """Execute one claimed mailbox pass with no Graph call under a DB lock."""

    client = GraphMailClient(settings)
    _ensure_subscription(session_factory, client=client, claim=claim, settings=settings)
    _run_sent_rescan(session_factory, client=client, claim=claim)
    _run_delta(
        session_factory,
        client=client,
        claim=claim,
        folder="sentitems",
        state_field="sent_delta_link",
    )
    _run_bounded_rescan(session_factory, client=client, claim=claim)
    _run_delta(
        session_factory,
        client=client,
        claim=claim,
        folder="inbox",
        state_field="inbox_delta_link",
    )
    _process_notification_queue(session_factory, client=client, claim=claim)
    _refresh_unread_replies(session_factory, client=client, claim=claim)
    with session_factory() as db:
        _lock_current_claim(db, claim)
        reconcile_pending_replies(db, mailbox_email=claim.mailbox_email)
        db.commit()


def _ensure_subscription(
    session_factory: Callable[[], Session],
    *,
    client: GraphMailClient,
    claim: MailboxSyncClaim,
    settings: Settings,
) -> None:
    with session_factory() as db:
        state = db.get(GraphMailboxSyncState, claim.mailbox_email)
        if not _claim_is_current(state, claim):
            raise GraphReplyError("reply_sync_lease_lost")
        subscription_id = state.subscription_id
        expires_at = _as_utc(state.subscription_expires_at)
    now = _utc_now()
    if subscription_id and expires_at and expires_at > now + timedelta(hours=24):
        return

    expiration = now + timedelta(days=6)
    notification_url = settings.microsoft_graph_notification_url.strip()
    body: dict[str, object] = {
        "expirationDateTime": expiration.isoformat().replace("+00:00", "Z"),
        "notificationUrl": notification_url,
        "lifecycleNotificationUrl": notification_url,
        "clientState": settings.microsoft_graph_client_state,
    }
    if subscription_id:
        try:
            response = client.request(
                "PATCH",
                f"/subscriptions/{quote(subscription_id, safe='')}",
                json=body,
            )
        except GraphMessageMissing:
            subscription_id = None
        else:
            _persist_subscription(session_factory, claim, response)
            return
    mailbox = quote(claim.mailbox_email, safe="")
    body.update(
        {
            "changeType": "created,updated,deleted",
            "resource": f"/users/{mailbox}/messages",
        }
    )
    try:
        response = client.request("POST", "/subscriptions", json=body)
    except GraphReplyError as exc:
        if exc.code != "microsoft_graph_subscription_conflict":
            raise
        response = _find_existing_subscription(
            client,
            resource=str(body["resource"]),
            change_type=str(body["changeType"]),
            notification_url=notification_url,
        )
        if response is None:
            raise
    _persist_subscription(session_factory, claim, response)


def _find_existing_subscription(
    client: GraphMailClient,
    *,
    resource: str,
    change_type: str,
    notification_url: str,
) -> dict[str, Any] | None:
    """Find the exact subscription created by an ambiguously completed POST."""

    url = "/subscriptions"
    expected_changes = {part.strip() for part in change_type.split(",") if part.strip()}
    expected_resource = unquote(resource.lstrip("/")).casefold()
    while True:
        page = client.request("GET", url)
        values = page.get("value", [])
        if not isinstance(values, list):
            raise GraphReplyError("microsoft_graph_response_invalid")
        for candidate in values:
            if not isinstance(candidate, dict):
                continue
            candidate_resource = unquote(
                str(candidate.get("resource", "")).lstrip("/")
            ).casefold()
            candidate_changes = {
                part.strip()
                for part in str(candidate.get("changeType", "")).split(",")
                if part.strip()
            }
            if (
                candidate_resource == expected_resource
                and candidate_changes == expected_changes
                and candidate.get("notificationUrl") == notification_url
            ):
                return candidate
        next_link = page.get("@odata.nextLink")
        if not isinstance(next_link, str):
            return None
        url = next_link


def _persist_subscription(
    session_factory: Callable[[], Session],
    claim: MailboxSyncClaim,
    payload: dict[str, Any],
) -> None:
    subscription_id = payload.get("id")
    expires_at = _parse_graph_datetime(payload.get("expirationDateTime"))
    if not isinstance(subscription_id, str) or not subscription_id or expires_at is None:
        raise GraphReplyError("microsoft_graph_subscription_response_invalid")
    with session_factory() as db:
        state = _lock_current_claim(db, claim)
        state.subscription_id = subscription_id
        state.subscription_expires_at = expires_at
        db.commit()


def _initial_page_url(
    mailbox_email: str,
    *,
    folder: str | None,
    cutoff: datetime,
    delta: bool,
    filter_field: str = "receivedDateTime",
) -> str:
    mailbox = quote(mailbox_email, safe="")
    if folder:
        path = f"/users/{mailbox}/mailFolders/{folder}/messages"
    else:
        path = f"/users/{mailbox}/messages"
    if delta:
        path += "/delta"
    query = urlencode(
        {
            "$select": _SELECT_FIELDS,
            "$filter": (
                f"{filter_field} ge "
                + cutoff.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            ),
            "$top": "100",
        }
    )
    return f"{path}?{query}"


def _run_delta(
    session_factory: Callable[[], Session],
    *,
    client: GraphMailClient,
    claim: MailboxSyncClaim,
    folder: str,
    state_field: str,
) -> None:
    while True:
        with session_factory() as db:
            state = db.get(GraphMailboxSyncState, claim.mailbox_email)
            if not _claim_is_current(state, claim):
                raise GraphReplyError("reply_sync_lease_lost")
            link = getattr(state, state_field)
            cutoff = _as_utc(state.backfill_cutoff_at)
        url = link or _initial_page_url(
            claim.mailbox_email,
            folder=folder,
            cutoff=cutoff or _utc_now(),
            delta=True,
        )
        try:
            page = client.request("GET", url)
        except GraphReplyError as exc:
            if exc.reset_delta:
                with session_factory() as db:
                    state = db.get(GraphMailboxSyncState, claim.mailbox_email)
                    if _claim_is_current(state, claim):
                        setattr(state, state_field, None)
                        state.force_resync = True
                        state.mailbox_scan_link = None
                        state.sent_scan_link = None
                        state.sent_backfill_completed_at = None
                        db.commit()
            raise
        items = page.get("value", [])
        if not isinstance(items, list):
            raise GraphReplyError("microsoft_graph_response_invalid")
        removed_messages: dict[str, dict[str, Any] | None] = {}
        for raw in items:
            if not isinstance(raw, dict) or "@removed" not in raw:
                continue
            removed_id = raw.get("id")
            if not isinstance(removed_id, str):
                continue
            try:
                removed_messages[removed_id] = client.get_message(
                    claim.mailbox_email,
                    removed_id,
                )
            except GraphMessageMissing:
                removed_messages[removed_id] = None
        next_link = page.get("@odata.nextLink")
        delta_link = page.get("@odata.deltaLink")
        if not isinstance(next_link, str) and not isinstance(delta_link, str):
            raise GraphReplyError("microsoft_graph_delta_checkpoint_missing")
        checkpoint = next_link if isinstance(next_link, str) else delta_link
        with session_factory() as db:
            state = _lock_current_claim(db, claim)
            for raw in items:
                if isinstance(raw, dict) and "@removed" not in raw:
                    process_graph_message(
                        db,
                        mailbox_email=claim.mailbox_email,
                        payload=raw,
                    )
                elif isinstance(raw, dict) and isinstance(raw.get("id"), str):
                    removed_id = raw["id"]
                    current_payload = removed_messages.get(removed_id)
                    if current_payload is None:
                        mark_graph_message_removed(
                            db,
                            mailbox_email=claim.mailbox_email,
                            graph_message_id=removed_id,
                        )
                    else:
                        # Folder delta reports a removal for moves as well as
                        # deletions. Immutable-ID lookup distinguishes them.
                        process_graph_message(
                            db,
                            mailbox_email=claim.mailbox_email,
                            payload=current_payload,
                        )
            setattr(state, state_field, checkpoint)
            db.commit()
        if not isinstance(next_link, str):
            return


def _run_sent_rescan(
    session_factory: Callable[[], Session],
    *,
    client: GraphMailClient,
    claim: MailboxSyncClaim,
) -> None:
    """Backfill sent identities with sentDateTime rather than Inbox semantics."""

    while True:
        with session_factory() as db:
            state = db.get(GraphMailboxSyncState, claim.mailbox_email)
            if not _claim_is_current(state, claim):
                raise GraphReplyError("reply_sync_lease_lost")
            if not state.force_resync or state.sent_backfill_completed_at is not None:
                return
            cutoff = _as_utc(state.backfill_cutoff_at) or _utc_now()
            link = state.sent_scan_link
        page = client.request(
            "GET",
            link
            or _initial_page_url(
                claim.mailbox_email,
                folder="sentitems",
                cutoff=cutoff,
                delta=False,
                filter_field="sentDateTime",
            ),
        )
        items = page.get("value", [])
        if not isinstance(items, list):
            raise GraphReplyError("microsoft_graph_response_invalid")
        next_link = page.get("@odata.nextLink")
        with session_factory() as db:
            state = _lock_current_claim(db, claim)
            for raw in items:
                if isinstance(raw, dict):
                    process_graph_message(
                        db,
                        mailbox_email=claim.mailbox_email,
                        payload=raw,
                    )
            if isinstance(next_link, str):
                state.sent_scan_link = next_link
            else:
                state.sent_scan_link = None
                state.sent_backfill_completed_at = _utc_now()
            db.commit()
        if not isinstance(next_link, str):
            return


def _run_bounded_rescan(
    session_factory: Callable[[], Session],
    *,
    client: GraphMailClient,
    claim: MailboxSyncClaim,
) -> None:
    while True:
        with session_factory() as db:
            state = db.get(GraphMailboxSyncState, claim.mailbox_email)
            if not _claim_is_current(state, claim):
                raise GraphReplyError("reply_sync_lease_lost")
            if not state.force_resync:
                return
            cutoff = _as_utc(state.backfill_cutoff_at) or _utc_now()
            link = state.mailbox_scan_link
        page = client.request(
            "GET",
            link
            or _initial_page_url(
                claim.mailbox_email,
                folder=None,
                cutoff=cutoff,
                delta=False,
            ),
        )
        items = page.get("value", [])
        if not isinstance(items, list):
            raise GraphReplyError("microsoft_graph_response_invalid")
        next_link = page.get("@odata.nextLink")
        with session_factory() as db:
            state = _lock_current_claim(db, claim)
            for raw in items:
                if isinstance(raw, dict):
                    process_graph_message(
                        db,
                        mailbox_email=claim.mailbox_email,
                        payload=raw,
                    )
            if isinstance(next_link, str):
                state.mailbox_scan_link = next_link
            else:
                state.mailbox_scan_link = None
                state.force_resync = False
                state.initial_backfill_completed_at = (
                    state.initial_backfill_completed_at or _utc_now()
                )
            db.commit()
        if not isinstance(next_link, str):
            return


def _claim_notification(
    db: Session,
    *,
    mailbox_email: str,
    worker_id: str,
) -> _NotificationClaim | None:
    row = db.scalar(
        select(GraphMailNotification)
        .where(
            GraphMailNotification.mailbox_email == mailbox_email,
            GraphMailNotification.claimed_by.is_(None),
            or_(
                GraphMailNotification.processed_at.is_(None),
                GraphMailNotification.processed_at
                < GraphMailNotification.requested_at,
            ),
        )
        .order_by(GraphMailNotification.requested_at)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if row is None:
        db.rollback()
        return None
    row.claimed_by = worker_id
    row.claimed_at = _utc_now()
    row.heartbeat_at = row.claimed_at
    row.attempt_count += 1
    claim = _NotificationClaim(
        notification_id=row.id,
        graph_message_id=row.graph_message_id,
        requested_at=_as_utc(row.requested_at) or _utc_now(),
    )
    db.commit()
    return claim


def _process_notification_queue(
    session_factory: Callable[[], Session],
    *,
    client: GraphMailClient,
    claim: MailboxSyncClaim,
) -> None:
    while True:
        with session_factory() as db:
            notification = _claim_notification(
                db,
                mailbox_email=claim.mailbox_email,
                worker_id=claim.worker_id,
            )
        if notification is None:
            return
        missing = False
        try:
            payload = client.get_message(
                claim.mailbox_email,
                notification.graph_message_id,
            )
        except GraphMessageMissing:
            payload = {}
            missing = True
        except GraphReplyError as exc:
            with session_factory() as db:
                row = db.get(GraphMailNotification, notification.notification_id)
                if row and row.claimed_by == claim.worker_id:
                    row.claimed_by = None
                    row.claimed_at = None
                    row.heartbeat_at = None
                    row.error_code = exc.code
                    db.commit()
            raise
        with session_factory() as db:
            _lock_current_claim(db, claim)
            row = db.get(GraphMailNotification, notification.notification_id)
            if row is None or row.claimed_by != claim.worker_id:
                raise GraphReplyError("reply_notification_lease_lost")
            if missing:
                mark_graph_message_removed(
                    db,
                    mailbox_email=claim.mailbox_email,
                    graph_message_id=notification.graph_message_id,
                )
            else:
                process_graph_message(
                    db,
                    mailbox_email=claim.mailbox_email,
                    payload=payload,
                )
            row.processed_at = notification.requested_at
            row.claimed_by = None
            row.claimed_at = None
            row.heartbeat_at = None
            row.error_code = None
            db.commit()


def _refresh_unread_replies(
    session_factory: Callable[[], Session],
    *,
    client: GraphMailClient,
    claim: MailboxSyncClaim,
) -> None:
    with session_factory() as db:
        message_ids = list(
            db.scalars(
                select(EmailReply.graph_message_id).where(
                    EmailReply.mailbox_email == claim.mailbox_email,
                    EmailReply.classification == EmailReplyClassification.human,
                    EmailReply.is_read.is_(False),
                    EmailReply.removed_at.is_(None),
                )
            ).all()
        )
    for message_id in message_ids:
        try:
            payload = client.get_message(claim.mailbox_email, message_id)
        except GraphMessageMissing:
            with session_factory() as db:
                _lock_current_claim(db, claim)
                mark_graph_message_removed(
                    db,
                    mailbox_email=claim.mailbox_email,
                    graph_message_id=message_id,
                )
                db.commit()
            continue
        with session_factory() as db:
            _lock_current_claim(db, claim)
            process_graph_message(
                db,
                mailbox_email=claim.mailbox_email,
                payload=payload,
            )
            db.commit()
