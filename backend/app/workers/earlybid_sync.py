"""Schedule and execute the configured EarlyBid feed once per local day.

Run from the backend directory with ``python -m app.workers.earlybid_sync``.
"""

from __future__ import annotations

import logging
import signal
import socket
import threading
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config import Settings, get_settings
from app.db.database import SessionLocal, check_database_schema
from app.services import earlybid_sync_service, lead_feed_service


logger = logging.getLogger(__name__)


class _Heartbeat:
    """Renew one synchronization lease from an independent session."""

    def __init__(
        self,
        *,
        claim: earlybid_sync_service.ClaimedEarlyBidSync,
        interval: float,
    ) -> None:
        self._claim = claim
        self._interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=f"earlybid-sync-heartbeat-{claim.run_id}",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=min(self._interval, 5.0))

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            with SessionLocal() as db:
                try:
                    renewed = earlybid_sync_service.heartbeat_run(
                        db,
                        claim=self._claim,
                    )
                except Exception:
                    db.rollback()
                    logger.error(
                        "EarlyBid synchronization heartbeat failed",
                        extra={"earlybid_sync_run_id": self._claim.run_id},
                    )
                    continue
            if not renewed:
                return


def run_worker(
    *,
    settings: Settings | None = None,
    stop_event: threading.Event | None = None,
) -> int:
    """Run the scheduler until interrupted, without using FastAPI lifecycle."""

    configured = settings or get_settings()
    if not _configuration_is_valid(configured):
        return 2
    try:
        check_database_schema()
    except RuntimeError:
        logger.error("EarlyBid scheduler database schema validation failed")
        return 2

    stopper = stop_event or threading.Event()
    worker_id = f"{socket.gethostname()}:{uuid.uuid4()}"
    logger.info(
        "EarlyBid synchronization worker started",
        extra={
            "worker_id": worker_id,
            "earlybid_feed": (
                f"{configured.lead_feed_reseller}/{configured.lead_feed_client}"
            ),
            "schedule_timezone": configured.lead_auto_sync_timezone,
        },
    )

    while not stopper.is_set():
        claim = _schedule_recover_and_claim(configured, worker_id)
        if claim is None:
            stopper.wait(configured.lead_auto_sync_poll_seconds)
            continue
        _execute_claim(configured, claim)

    logger.info(
        "EarlyBid synchronization worker stopped",
        extra={"worker_id": worker_id},
    )
    return 0


def _configuration_is_valid(settings: Settings) -> bool:
    required = (
        settings.lead_api_base_url,
        settings.lead_api_key or "",
        settings.lead_feed_reseller,
        settings.lead_feed_client,
        settings.lead_auto_sync_timezone,
    )
    if any(not value.strip() for value in required):
        logger.error("EarlyBid scheduler configuration is incomplete")
        return False
    timings = (
        settings.lead_auto_sync_poll_seconds,
        settings.lead_auto_sync_heartbeat_seconds,
        settings.lead_auto_sync_stale_seconds,
    )
    if min(timings) <= 0:
        logger.error("EarlyBid scheduler timing settings must be positive")
        return False
    if (
        settings.lead_auto_sync_stale_seconds
        <= settings.lead_auto_sync_heartbeat_seconds
    ):
        logger.error("EarlyBid scheduler stale threshold must exceed heartbeat interval")
        return False
    try:
        ZoneInfo(settings.lead_auto_sync_timezone.strip())
    except (ValueError, ZoneInfoNotFoundError):
        logger.error("EarlyBid scheduler timezone is unavailable")
        return False
    return True


def _schedule_recover_and_claim(
    settings: Settings,
    worker_id: str,
) -> earlybid_sync_service.ClaimedEarlyBidSync | None:
    with SessionLocal() as db:
        try:
            # One clock value keeps the local date and all due/stale decisions
            # coherent if a polling iteration straddles Pacific midnight.
            now = datetime.now(timezone.utc)
            daily_run = earlybid_sync_service.ensure_current_daily_run(
                db,
                reseller=settings.lead_feed_reseller,
                client=settings.lead_feed_client,
                timezone_name=settings.lead_auto_sync_timezone,
                now=now,
            )
            schedule_date = daily_run.schedule_date
            earlybid_sync_service.supersede_historical_active_runs(
                db,
                reseller=settings.lead_feed_reseller,
                client=settings.lead_feed_client,
                current_schedule_date=schedule_date,
                now=now,
            )
            earlybid_sync_service.recover_stale_runs(
                db,
                stale_after_seconds=settings.lead_auto_sync_stale_seconds,
                reseller=settings.lead_feed_reseller,
                client=settings.lead_feed_client,
                schedule_date=schedule_date,
                now=now,
            )
            return earlybid_sync_service.claim_next_run(
                db,
                worker_id=worker_id,
                reseller=settings.lead_feed_reseller,
                client=settings.lead_feed_client,
                schedule_date=schedule_date,
                now=now,
            )
        except Exception:
            db.rollback()
            logger.error(
                "EarlyBid scheduler could not create or claim daily work",
                extra={"worker_id": worker_id},
            )
            return None


def _execute_claim(
    settings: Settings,
    claim: earlybid_sync_service.ClaimedEarlyBidSync,
) -> None:
    heartbeat = _Heartbeat(
        claim=claim,
        interval=settings.lead_auto_sync_heartbeat_seconds,
    )
    heartbeat.start()
    try:
        try:
            rows = lead_feed_service.fetch_feed_rows(claim.reseller, claim.client)
        except Exception as exc:
            disposition = earlybid_sync_service.classify_feed_error(exc)
            _record_failure(claim, disposition)
            return

        try:
            with SessionLocal() as db:
                earlybid_sync_service.finalize_success(
                    db,
                    claim=claim,
                    rows=rows,
                    timezone_name=settings.lead_auto_sync_timezone,
                )
        except earlybid_sync_service.EarlyBidSyncLeaseLostError:
            logger.warning(
                "Late EarlyBid synchronization result was ignored",
                extra={"earlybid_sync_run_id": claim.run_id},
            )
        except lead_feed_service.LeadFeedValidationError as exc:
            _record_failure(
                claim,
                earlybid_sync_service.classify_feed_error(exc),
            )
        except Exception:
            _record_failure(
                claim,
                earlybid_sync_service.SyncErrorDisposition(
                    earlybid_sync_service.PERSISTENCE_ERROR,
                    True,
                ),
            )
    finally:
        # Keep ownership live through the atomic lead/job/run commit.
        heartbeat.stop()


def _record_failure(
    claim: earlybid_sync_service.ClaimedEarlyBidSync,
    disposition: earlybid_sync_service.SyncErrorDisposition,
) -> None:
    with SessionLocal() as db:
        try:
            earlybid_sync_service.record_attempt_failure(
                db,
                claim=claim,
                error_code=disposition.error_code,
                retryable=disposition.retryable,
            )
        except earlybid_sync_service.EarlyBidSyncLeaseLostError:
            logger.warning(
                "Late EarlyBid synchronization failure was ignored",
                extra={"earlybid_sync_run_id": claim.run_id},
            )
        except Exception:
            db.rollback()
            logger.error(
                "EarlyBid synchronization failure state could not be persisted",
                extra={"earlybid_sync_run_id": claim.run_id},
            )


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    stopper = threading.Event()

    def request_stop(signum: int, _: object) -> None:
        logger.info(
            "EarlyBid synchronization worker shutdown requested",
            extra={"signal": signum},
        )
        stopper.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    try:
        return run_worker(stop_event=stopper)
    except KeyboardInterrupt:
        stopper.set()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
