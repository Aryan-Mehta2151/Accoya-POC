"""Durable daily scheduling and lease management for EarlyBid synchronization."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import EarlyBidSyncRun, EarlyBidSyncRunStatus
from app.schemas.earlybid_sync import EarlyBidSyncRunRead, EarlyBidSyncStatusRead
from app.services import lead_feed_service


logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 4
RETRY_DELAYS = (
    timedelta(minutes=5),
    timedelta(minutes=15),
    timedelta(minutes=30),
)

MISSING_CONFIGURATION = "missing_configuration"
UPSTREAM_UNAVAILABLE = "upstream_unavailable"
UPSTREAM_RATE_LIMITED = "upstream_rate_limited"
UPSTREAM_AUTH_ERROR = "upstream_auth_error"
UPSTREAM_REQUEST_ERROR = "upstream_request_error"
INVALID_FEED = "invalid_feed"
PERSISTENCE_ERROR = "persistence_error"
WORKER_LEASE_EXPIRED = "worker_lease_expired"
SUPERSEDED_SCHEDULE = "superseded_schedule"

SAFE_ERROR_CODES = frozenset(
    {
        MISSING_CONFIGURATION,
        UPSTREAM_UNAVAILABLE,
        UPSTREAM_RATE_LIMITED,
        UPSTREAM_AUTH_ERROR,
        UPSTREAM_REQUEST_ERROR,
        INVALID_FEED,
        PERSISTENCE_ERROR,
        SUPERSEDED_SCHEDULE,
        WORKER_LEASE_EXPIRED,
    }
)


class EarlyBidSyncLeaseLostError(RuntimeError):
    """A worker result arrived after ownership of its attempt ended."""


@dataclass(frozen=True)
class ClaimedEarlyBidSync:
    """Detached input for one claimed synchronization attempt."""

    run_id: str
    reseller: str
    client: str
    worker_id: str
    attempt_count: int


@dataclass(frozen=True)
class SyncErrorDisposition:
    """Safe persisted error classification and retry decision."""

    error_code: str
    retryable: bool


def scheduled_midnight_utc(
    schedule_date: date,
    timezone_name: str,
) -> datetime:
    """Return one local calendar midnight as a canonical UTC instant."""

    local_midnight = datetime.combine(
        schedule_date,
        time.min,
        tzinfo=_load_zone(timezone_name),
    )
    return local_midnight.astimezone(timezone.utc)


def ensure_current_daily_run(
    db: Session,
    *,
    reseller: str,
    client: str,
    timezone_name: str,
    now: datetime | None = None,
) -> EarlyBidSyncRun:
    """Durably create today's slot only, collapsing any multi-day downtime."""

    normalized_reseller = reseller.strip()
    normalized_client = client.strip()
    if not normalized_reseller or not normalized_client:
        raise ValueError("EarlyBid feed scope must not be blank")
    instant = _as_utc(now or _utc_now())
    zone = _load_zone(timezone_name)
    schedule_date = instant.astimezone(zone).date()

    existing = db.scalar(
        select(EarlyBidSyncRun).where(
            EarlyBidSyncRun.reseller == normalized_reseller,
            EarlyBidSyncRun.client == normalized_client,
            EarlyBidSyncRun.schedule_date == schedule_date,
        )
    )
    if existing is not None:
        db.rollback()
        return existing

    run = EarlyBidSyncRun(
        id=str(uuid.uuid4()),
        reseller=normalized_reseller,
        client=normalized_client,
        schedule_date=schedule_date,
        scheduled_for=scheduled_midnight_utc(schedule_date, timezone_name),
        status=EarlyBidSyncRunStatus.queued,
        attempt_count=0,
        queued_at=instant,
    )
    db.add(run)
    try:
        db.commit()
        db.refresh(run)
        return run
    except IntegrityError:
        # Another scheduler replica won the unique feed/date slot.
        db.rollback()
        replay = db.scalar(
            select(EarlyBidSyncRun).where(
                EarlyBidSyncRun.reseller == normalized_reseller,
                EarlyBidSyncRun.client == normalized_client,
                EarlyBidSyncRun.schedule_date == schedule_date,
            )
        )
        if replay is None:
            raise
        db.rollback()
        return replay


def claim_next_run(
    db: Session,
    *,
    worker_id: str,
    reseller: str | None = None,
    client: str | None = None,
    schedule_date: date | None = None,
    now: datetime | None = None,
) -> ClaimedEarlyBidSync | None:
    """Claim one due initial/retry attempt using PostgreSQL SKIP LOCKED."""

    normalized_worker_id = worker_id.strip()
    if not normalized_worker_id:
        raise ValueError("worker_id must not be blank")
    instant = _as_utc(now or _utc_now())
    due_at = func.coalesce(
        EarlyBidSyncRun.next_attempt_at,
        EarlyBidSyncRun.scheduled_for,
    )
    statement = select(EarlyBidSyncRun).where(
        or_(
            (
                (EarlyBidSyncRun.status == EarlyBidSyncRunStatus.queued)
                & (EarlyBidSyncRun.scheduled_for <= instant)
            ),
            (
                (EarlyBidSyncRun.status == EarlyBidSyncRunStatus.retry_wait)
                & (EarlyBidSyncRun.next_attempt_at <= instant)
            ),
        )
    )
    if (reseller is None) != (client is None):
        raise ValueError("reseller and client must be provided together")
    if reseller is not None and client is not None:
        statement = statement.where(
            EarlyBidSyncRun.reseller == reseller.strip(),
            EarlyBidSyncRun.client == client.strip(),
        )
    if schedule_date is not None:
        statement = statement.where(
            EarlyBidSyncRun.schedule_date == schedule_date
        )
    run = db.scalar(
        statement
        .order_by(due_at, EarlyBidSyncRun.scheduled_for, EarlyBidSyncRun.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if run is None:
        db.rollback()
        return None

    run.status = EarlyBidSyncRunStatus.running
    run.attempt_count += 1
    run.claimed_by = normalized_worker_id
    run.claimed_at = instant
    run.heartbeat_at = instant
    run.next_attempt_at = None
    run.error_code = None
    stable_run_id = str(run.id)
    attempt_count = run.attempt_count
    reseller = run.reseller
    client = run.client
    db.commit()
    return ClaimedEarlyBidSync(
        run_id=stable_run_id,
        reseller=reseller,
        client=client,
        worker_id=normalized_worker_id,
        attempt_count=attempt_count,
    )


def heartbeat_run(
    db: Session,
    *,
    claim: ClaimedEarlyBidSync,
    now: datetime | None = None,
) -> bool:
    """Renew a lease only while the same worker still owns the same attempt."""

    run = db.scalar(
        select(EarlyBidSyncRun)
        .where(EarlyBidSyncRun.id == claim.run_id)
        .with_for_update()
    )
    if not _claim_owns(run, claim):
        db.rollback()
        return False
    run.heartbeat_at = _as_utc(now or _utc_now())
    db.commit()
    return True


def finalize_success(
    db: Session,
    *,
    claim: ClaimedEarlyBidSync,
    rows: list[dict[str, str | None]],
    timezone_name: str | None = None,
    now: datetime | None = None,
) -> EarlyBidSyncRun:
    """Atomically persist lead/job changes and the successful run outcome."""

    try:
        run = _load_owned_running_run(db, claim)
        guard_at = _as_utc(now) if now is not None else _utc_now()
        if _run_is_superseded(
            db,
            run,
            timezone_name=timezone_name,
            now=guard_at,
        ):
            _commit_claim_as_superseded(db, run=run, claim=claim, now=guard_at)
            raise EarlyBidSyncLeaseLostError(claim.run_id)
        staged = lead_feed_service.stage_feed_sync(
            db,
            rows,
            reseller=claim.reseller,
            client=claim.client,
        )
        completed_at = _as_utc(now) if now is not None else _utc_now()
        if _run_is_superseded(
            db,
            run,
            timezone_name=timezone_name,
            now=completed_at,
        ):
            # Discard all staged lead/job mutations before recording the safe
            # terminal outcome in a new transaction.
            db.rollback()
            run = _load_owned_running_run(db, claim)
            _commit_claim_as_superseded(
                db,
                run=run,
                claim=claim,
                now=completed_at,
            )
            raise EarlyBidSyncLeaseLostError(claim.run_id)
        run.status = EarlyBidSyncRunStatus.succeeded
        run.error_code = None
        run.next_attempt_at = None
        run.completed_at = completed_at
        run.created_count = staged.created
        run.updated_count = staged.updated
        run.total_count = staged.total
        run.generation_queued_count = staged.generation_queued
        duration_ms = _duration_ms(run.claimed_at, completed_at)
        created_count = run.created_count
        updated_count = run.updated_count
        total_count = run.total_count
        generation_queued_count = run.generation_queued_count
        db.commit()
    except Exception:
        db.rollback()
        raise

    logger.info(
        "EarlyBid synchronization succeeded",
        extra={
            "earlybid_sync_run_id": claim.run_id,
            "earlybid_feed": f"{claim.reseller}/{claim.client}",
            "sync_status": EarlyBidSyncRunStatus.succeeded.value,
            "attempt_count": claim.attempt_count,
            "duration_ms": duration_ms,
            "created_count": created_count,
            "updated_count": updated_count,
            "total_count": total_count,
            "generation_queued_count": generation_queued_count,
        },
    )
    return run


def record_attempt_failure(
    db: Session,
    *,
    claim: ClaimedEarlyBidSync,
    error_code: str,
    retryable: bool,
    now: datetime | None = None,
) -> EarlyBidSyncRun:
    """Persist a safe failure and either schedule a retry or terminate the day."""

    _validate_error_code(error_code)
    instant = _as_utc(now or _utc_now())
    run = _load_owned_running_run(db, claim)
    _transition_after_failure(
        run,
        error_code=error_code,
        retryable=retryable,
        now=instant,
    )
    resulting_status = run.status.value
    duration_ms = _duration_ms(run.claimed_at, instant)
    db.commit()
    logger.warning(
        "EarlyBid synchronization attempt failed",
        extra={
            "earlybid_sync_run_id": claim.run_id,
            "earlybid_feed": f"{claim.reseller}/{claim.client}",
            "sync_status": resulting_status,
            "attempt_count": claim.attempt_count,
            "duration_ms": duration_ms,
            "error_code": error_code,
            "will_retry": resulting_status == EarlyBidSyncRunStatus.retry_wait.value,
        },
    )
    return run


def supersede_historical_active_runs(
    db: Session,
    *,
    reseller: str,
    client: str,
    current_schedule_date: date,
    now: datetime | None = None,
) -> int:
    """Terminalize prior-date active rows without fetching or replaying them."""

    normalized_reseller = reseller.strip()
    normalized_client = client.strip()
    if not normalized_reseller or not normalized_client:
        raise ValueError("EarlyBid feed scope must not be blank")
    instant = _as_utc(now or _utc_now())
    runs = list(
        db.scalars(
            select(EarlyBidSyncRun)
            .where(
                EarlyBidSyncRun.reseller == normalized_reseller,
                EarlyBidSyncRun.client == normalized_client,
                EarlyBidSyncRun.schedule_date < current_schedule_date,
                EarlyBidSyncRun.status.in_(
                    (
                        EarlyBidSyncRunStatus.queued,
                        EarlyBidSyncRunStatus.running,
                        EarlyBidSyncRunStatus.retry_wait,
                    )
                ),
            )
            .with_for_update(skip_locked=True)
        ).all()
    )
    superseded_logs: list[dict[str, object]] = []
    for run in runs:
        previous_status = run.status.value
        started_at = run.claimed_at or run.queued_at
        _terminalize_superseded_run(run, now=instant)
        superseded_logs.append(
            {
                "earlybid_sync_run_id": str(run.id),
                "earlybid_feed": run.feed,
                "schedule_date": run.schedule_date.isoformat(),
                "previous_status": previous_status,
                "sync_status": EarlyBidSyncRunStatus.failed.value,
                "attempt_count": run.attempt_count,
                "duration_ms": _duration_ms(started_at, instant),
                "error_code": SUPERSEDED_SCHEDULE,
                "will_retry": False,
            }
        )
    if runs:
        db.commit()
        for log_fields in superseded_logs:
            logger.warning(
                "Historical EarlyBid synchronization was superseded",
                extra=log_fields,
            )
    else:
        db.rollback()
    return len(runs)


def recover_stale_runs(
    db: Session,
    *,
    stale_after_seconds: float,
    reseller: str | None = None,
    client: str | None = None,
    schedule_date: date | None = None,
    now: datetime | None = None,
) -> int:
    """Expire abandoned leases and retry safely within the daily attempt budget."""

    if stale_after_seconds <= 0:
        raise ValueError("stale_after_seconds must be positive")
    instant = _as_utc(now or _utc_now())
    cutoff = instant - timedelta(seconds=stale_after_seconds)
    statement = select(EarlyBidSyncRun).where(
        EarlyBidSyncRun.status == EarlyBidSyncRunStatus.running,
        EarlyBidSyncRun.heartbeat_at < cutoff,
    )
    if (reseller is None) != (client is None):
        raise ValueError("reseller and client must be provided together")
    if reseller is not None and client is not None:
        statement = statement.where(
            EarlyBidSyncRun.reseller == reseller.strip(),
            EarlyBidSyncRun.client == client.strip(),
        )
    if schedule_date is not None:
        statement = statement.where(
            EarlyBidSyncRun.schedule_date == schedule_date
        )
    runs = list(db.scalars(statement.with_for_update(skip_locked=True)).all())
    recovered_logs: list[dict[str, object]] = []
    for run in runs:
        _transition_after_failure(
            run,
            error_code=WORKER_LEASE_EXPIRED,
            retryable=True,
            now=instant,
        )
        recovered_logs.append(
            {
                "earlybid_sync_run_id": str(run.id),
                "earlybid_feed": run.feed,
                "sync_status": run.status.value,
                "attempt_count": run.attempt_count,
                "duration_ms": _duration_ms(run.claimed_at, instant),
                "error_code": WORKER_LEASE_EXPIRED,
                "will_retry": run.status is EarlyBidSyncRunStatus.retry_wait,
            }
        )
    if runs:
        db.commit()
        for log_fields in recovered_logs:
            logger.warning(
                "Expired EarlyBid synchronization lease was recovered",
                extra=log_fields,
            )
    else:
        db.rollback()
    return len(runs)


def classify_feed_error(exc: Exception) -> SyncErrorDisposition:
    """Map fetch/validation failures to safe terminal or retryable outcomes."""

    if isinstance(exc, lead_feed_service.LeadFeedValidationError):
        return SyncErrorDisposition(INVALID_FEED, False)
    if isinstance(exc, lead_feed_service.LeadFeedConfigurationError):
        return SyncErrorDisposition(MISSING_CONFIGURATION, False)
    if isinstance(exc, lead_feed_service.LeadFeedError):
        status_code = exc.status_code
        if status_code == 429:
            return SyncErrorDisposition(UPSTREAM_RATE_LIMITED, True)
        if status_code == 408:
            return SyncErrorDisposition(UPSTREAM_UNAVAILABLE, True)
        if status_code in (401, 403):
            return SyncErrorDisposition(UPSTREAM_AUTH_ERROR, False)
        if status_code is not None and 400 <= status_code < 500:
            return SyncErrorDisposition(UPSTREAM_REQUEST_ERROR, False)
        return SyncErrorDisposition(UPSTREAM_UNAVAILABLE, True)
    return SyncErrorDisposition(UPSTREAM_UNAVAILABLE, True)


def get_sync_status(
    db: Session,
    *,
    reseller: str,
    client: str,
    timezone_name: str,
    stale_after_seconds: float = 300.0,
    now: datetime | None = None,
) -> EarlyBidSyncStatusRead:
    """Return non-mutating status for the configured automatic feed."""

    if stale_after_seconds <= 0:
        raise ValueError("stale_after_seconds must be positive")
    instant = _as_utc(now or _utc_now())
    zone = _load_zone(timezone_name)
    normalized_reseller = reseller.strip()
    normalized_client = client.strip()
    today = instant.astimezone(zone).date()
    latest = db.scalar(
        select(EarlyBidSyncRun)
        .where(
            EarlyBidSyncRun.reseller == normalized_reseller,
            EarlyBidSyncRun.client == normalized_client,
        )
        .order_by(
            EarlyBidSyncRun.schedule_date.desc(),
            EarlyBidSyncRun.queued_at.desc(),
            EarlyBidSyncRun.id.desc(),
        )
        .limit(1)
    )
    today_exists = latest is not None and latest.schedule_date == today
    current_midnight = scheduled_midnight_utc(today, timezone_name)
    # This value is always the next future calendar schedule. A missed current
    # slot is represented separately by ``overdue`` and will be caught up by
    # the worker without making a past timestamp look like the next run.
    next_scheduled_at = scheduled_midnight_utc(
        today + timedelta(days=1),
        timezone_name,
    )
    overdue = (
        instant >= current_midnight
        if not today_exists
        else _active_run_is_overdue(
            latest,
            now=instant,
            stale_after_seconds=stale_after_seconds,
        )
    )

    return EarlyBidSyncStatusRead(
        timezone=zone.key,
        next_scheduled_at=next_scheduled_at,
        overdue=overdue,
        latest_run=(
            EarlyBidSyncRunRead.model_validate(latest) if latest is not None else None
        ),
    )


def _load_owned_running_run(
    db: Session,
    claim: ClaimedEarlyBidSync,
) -> EarlyBidSyncRun:
    run = db.scalar(
        select(EarlyBidSyncRun)
        .where(EarlyBidSyncRun.id == claim.run_id)
        .with_for_update()
    )
    if not _claim_owns(run, claim):
        db.rollback()
        raise EarlyBidSyncLeaseLostError(claim.run_id)
    return run


def _run_is_superseded(
    db: Session,
    run: EarlyBidSyncRun,
    *,
    timezone_name: str | None,
    now: datetime,
) -> bool:
    if timezone_name is not None:
        current_date = now.astimezone(_load_zone(timezone_name)).date()
        if run.schedule_date < current_date:
            return True
    newer_run_id = db.scalar(
        select(EarlyBidSyncRun.id)
        .where(
            EarlyBidSyncRun.reseller == run.reseller,
            EarlyBidSyncRun.client == run.client,
            EarlyBidSyncRun.schedule_date > run.schedule_date,
        )
        .limit(1)
    )
    return newer_run_id is not None


def _commit_claim_as_superseded(
    db: Session,
    *,
    run: EarlyBidSyncRun,
    claim: ClaimedEarlyBidSync,
    now: datetime,
) -> None:
    previous_status = run.status.value
    duration_ms = _duration_ms(run.claimed_at, now)
    schedule_date = run.schedule_date.isoformat()
    _terminalize_superseded_run(run, now=now)
    db.commit()
    logger.warning(
        "Late EarlyBid synchronization was superseded",
        extra={
            "earlybid_sync_run_id": claim.run_id,
            "earlybid_feed": f"{claim.reseller}/{claim.client}",
            "schedule_date": schedule_date,
            "previous_status": previous_status,
            "sync_status": EarlyBidSyncRunStatus.failed.value,
            "attempt_count": claim.attempt_count,
            "duration_ms": duration_ms,
            "error_code": SUPERSEDED_SCHEDULE,
            "will_retry": False,
        },
    )


def _active_run_is_overdue(
    run: EarlyBidSyncRun,
    *,
    now: datetime,
    stale_after_seconds: float,
) -> bool:
    cutoff = now - timedelta(seconds=stale_after_seconds)
    if run.status is EarlyBidSyncRunStatus.queued:
        return _as_utc(run.queued_at) < cutoff
    if run.status is EarlyBidSyncRunStatus.running:
        return run.heartbeat_at is None or _as_utc(run.heartbeat_at) < cutoff
    if run.status is EarlyBidSyncRunStatus.retry_wait:
        return (
            run.next_attempt_at is None
            or _as_utc(run.next_attempt_at) < cutoff
        )
    return False


def _claim_owns(
    run: EarlyBidSyncRun | None,
    claim: ClaimedEarlyBidSync,
) -> bool:
    return (
        run is not None
        and run.status is EarlyBidSyncRunStatus.running
        and run.claimed_by == claim.worker_id
        and run.attempt_count == claim.attempt_count
    )


def _transition_after_failure(
    run: EarlyBidSyncRun,
    *,
    error_code: str,
    retryable: bool,
    now: datetime,
) -> None:
    _validate_error_code(error_code)
    run.error_code = error_code
    if retryable and run.attempt_count < MAX_ATTEMPTS:
        run.status = EarlyBidSyncRunStatus.retry_wait
        run.next_attempt_at = now + RETRY_DELAYS[run.attempt_count - 1]
        run.completed_at = None
    else:
        run.status = EarlyBidSyncRunStatus.failed
        run.next_attempt_at = None
        run.completed_at = now


def _terminalize_superseded_run(
    run: EarlyBidSyncRun,
    *,
    now: datetime,
) -> None:
    run.status = EarlyBidSyncRunStatus.failed
    run.error_code = SUPERSEDED_SCHEDULE
    run.next_attempt_at = None
    run.completed_at = now


def _validate_error_code(error_code: str) -> None:
    if error_code not in SAFE_ERROR_CODES:
        raise ValueError("Unsupported EarlyBid synchronization error code")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _load_zone(timezone_name: str) -> ZoneInfo:
    return ZoneInfo(timezone_name.strip())


def _duration_ms(started_at: datetime | None, completed_at: datetime) -> int | None:
    if started_at is None:
        return None
    return max(
        0,
        int((_as_utc(completed_at) - _as_utc(started_at)).total_seconds() * 1000),
    )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


__all__ = [
    "ClaimedEarlyBidSync",
    "EarlyBidSyncLeaseLostError",
    "INVALID_FEED",
    "MAX_ATTEMPTS",
    "MISSING_CONFIGURATION",
    "PERSISTENCE_ERROR",
    "RETRY_DELAYS",
    "SAFE_ERROR_CODES",
    "SUPERSEDED_SCHEDULE",
    "SyncErrorDisposition",
    "UPSTREAM_AUTH_ERROR",
    "UPSTREAM_RATE_LIMITED",
    "UPSTREAM_REQUEST_ERROR",
    "UPSTREAM_UNAVAILABLE",
    "WORKER_LEASE_EXPIRED",
    "claim_next_run",
    "classify_feed_error",
    "ensure_current_daily_run",
    "finalize_success",
    "get_sync_status",
    "heartbeat_run",
    "record_attempt_failure",
    "recover_stale_runs",
    "scheduled_midnight_utc",
    "supersede_historical_active_runs",
]
