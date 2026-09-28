"""Read-only aggregate metrics for the Overview dashboard."""

from datetime import datetime

from sqlalchemy import distinct, func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import (
    AgentRun,
    Email,
    EmailDeliveryJob,
    EmailDeliveryJobStatus,
    EmailReply,
    EmailReplyClassification,
    EmailStatus,
    EmailStatusEvent,
    Lead,
    LeadReviewStatus,
)
from app.services import email_reply_service


def _in_range(statement, column, start_at: datetime | None, end_before: datetime | None):
    if start_at is not None:
        statement = statement.where(column >= start_at)
    if end_before is not None:
        statement = statement.where(column < end_before)
    return statement


def overview_summary(
    db: Session,
    *,
    settings: Settings,
    start_at: datetime | None = None,
    end_before: datetime | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    """Return current active-workspace totals filtered by each activity date."""

    opportunity_statement = select(func.count(Lead.id)).where(
        Lead.review_status == LeadReviewStatus.active,
    )
    opportunity_statement = _in_range(
        opportunity_statement,
        Lead.created_at,
        start_at,
        end_before,
    )
    opportunities = db.scalar(opportunity_statement) or 0

    ranked_emails = (
        select(
            Email.id.label("email_id"),
            AgentRun.lead_id.label("lead_id"),
            Email.status.label("email_status"),
            Email.created_at.label("email_created_at"),
            func.row_number()
            .over(
                partition_by=AgentRun.lead_id,
                order_by=(Email.created_at.desc(), Email.id.desc()),
            )
            .label("email_rank"),
        )
        .join(AgentRun, AgentRun.id == Email.agent_run_id)
        .subquery()
    )
    current_emails = (
        select(
            ranked_emails.c.email_id,
            ranked_emails.c.lead_id,
            ranked_emails.c.email_status,
            ranked_emails.c.email_created_at,
        )
        .where(ranked_emails.c.email_rank == 1)
        .subquery()
    )

    pending_events = (
        select(
            EmailStatusEvent.email_id.label("email_id"),
            func.max(EmailStatusEvent.created_at).label("pending_at"),
        )
        .where(EmailStatusEvent.new_status == EmailStatus.pending_review)
        .group_by(EmailStatusEvent.email_id)
        .subquery()
    )
    pending_at = func.coalesce(
        pending_events.c.pending_at,
        current_emails.c.email_created_at,
    )
    needs_review_statement = (
        select(func.count(current_emails.c.email_id))
        .join(Lead, Lead.id == current_emails.c.lead_id)
        .outerjoin(
            pending_events,
            pending_events.c.email_id == current_emails.c.email_id,
        )
        .where(
            Lead.review_status == LeadReviewStatus.active,
            current_emails.c.email_status == EmailStatus.pending_review,
        )
    )
    needs_review_statement = _in_range(
        needs_review_statement,
        pending_at,
        start_at,
        end_before,
    )
    needs_review = db.scalar(needs_review_statement) or 0

    sent_statement = (
        select(func.count(distinct(current_emails.c.email_id)))
        .join(Lead, Lead.id == current_emails.c.lead_id)
        .join(
            EmailDeliveryJob,
            EmailDeliveryJob.email_id == current_emails.c.email_id,
        )
        .where(
            Lead.review_status == LeadReviewStatus.active,
            current_emails.c.email_status == EmailStatus.sent,
            EmailDeliveryJob.status == EmailDeliveryJobStatus.succeeded,
            EmailDeliveryJob.accepted_at.is_not(None),
        )
    )
    sent_statement = _in_range(
        sent_statement,
        EmailDeliveryJob.accepted_at,
        start_at,
        end_before,
    )
    sent = db.scalar(sent_statement) or 0

    replies_statement = (
        select(func.count(EmailReply.id))
        .join(Lead, Lead.id == EmailReply.lead_id)
        .where(
            Lead.review_status == LeadReviewStatus.active,
            EmailReply.classification == EmailReplyClassification.human,
            EmailReply.removed_at.is_(None),
        )
    )
    replies_statement = _in_range(
        replies_statement,
        EmailReply.received_at,
        start_at,
        end_before,
    )
    replies = db.scalar(replies_statement) or 0

    reply_health = email_reply_service.reply_sync_health(
        db,
        settings=settings,
        now=now,
    )
    return {
        "opportunities": int(opportunities),
        "needs_review": int(needs_review),
        "sent": int(sent),
        "replies": int(replies),
        "reply_sync_status": reply_health["sync_status"],
        "reply_last_synced_at": reply_health["last_synced_at"],
    }
