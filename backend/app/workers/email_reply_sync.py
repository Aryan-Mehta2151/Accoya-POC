"""Maintain Microsoft Graph subscriptions and synchronize reply metadata.

Run from the backend directory with ``python -m app.workers.email_reply_sync``.
"""

from __future__ import annotations

import logging
import signal
import socket
import threading
import uuid

from app.config import Settings, get_settings
from app.db.database import SessionLocal, check_database_schema
from app.services import email_reply_service


logger = logging.getLogger(__name__)


class _Heartbeat:
    def __init__(
        self,
        *,
        claim: email_reply_service.MailboxSyncClaim,
        interval: float,
    ) -> None:
        self._claim = claim
        self._interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=f"email-reply-heartbeat-{claim.mailbox_email}",
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
                    renewed = email_reply_service.heartbeat_mailbox_sync(
                        db,
                        claim=self._claim,
                    )
                except Exception:
                    db.rollback()
                    logger.error(
                        "Reply synchronization heartbeat failed",
                        extra={"mailbox": self._claim.mailbox_email},
                    )
                    continue
            if not renewed:
                return


def run_worker(
    *,
    settings: Settings | None = None,
    stop_event: threading.Event | None = None,
) -> int:
    """Run the durable mailbox synchronizer until interrupted."""

    configured = settings or get_settings()
    configuration_error = email_reply_service.reply_configuration_error(configured)
    if configuration_error:
        logger.error(
            "Email reply worker is disabled or invalid",
            extra={"error_code": configuration_error},
        )
        return 2
    try:
        check_database_schema()
    except RuntimeError:
        logger.error("Email reply worker database schema validation failed")
        return 2

    mailbox = str(configured.microsoft_sender_email).strip().casefold()
    with SessionLocal() as db:
        email_reply_service.ensure_mailbox_state(
            db,
            mailbox_email=mailbox,
            backfill_days=configured.email_reply_backfill_days,
        )

    stopper = stop_event or threading.Event()
    worker_id = f"{socket.gethostname()}:{uuid.uuid4()}"
    logger.info(
        "Email reply worker started",
        extra={"worker_id": worker_id, "mailbox": mailbox},
    )
    while not stopper.is_set():
        claim = _recover_and_claim(configured, mailbox, worker_id)
        if claim is None:
            stopper.wait(configured.email_reply_worker_poll_seconds)
            continue
        heartbeat = _Heartbeat(
            claim=claim,
            interval=configured.email_reply_heartbeat_seconds,
        )
        heartbeat.start()
        error_code: str | None = None
        try:
            email_reply_service.synchronize_mailbox(
                SessionLocal,
                claim=claim,
                settings=configured,
            )
        except email_reply_service.GraphReplyError as exc:
            error_code = exc.code
            logger.warning(
                "Email reply synchronization did not complete",
                extra={"mailbox": mailbox, "error_code": error_code},
            )
        except Exception:
            error_code = "reply_sync_system_error"
            logger.exception(
                "Email reply synchronization failed",
                extra={"mailbox": mailbox},
            )
        finally:
            heartbeat.stop()
        with SessionLocal() as db:
            email_reply_service.finalize_mailbox_sync(
                db,
                claim=claim,
                reconcile_seconds=configured.email_reply_reconcile_seconds,
                error_code=error_code,
            )

    logger.info("Email reply worker stopped", extra={"worker_id": worker_id})
    return 0


def _recover_and_claim(
    settings: Settings,
    mailbox: str,
    worker_id: str,
) -> email_reply_service.MailboxSyncClaim | None:
    with SessionLocal() as db:
        try:
            email_reply_service.recover_stale_sync(
                db,
                mailbox_email=mailbox,
                stale_after_seconds=settings.email_reply_stale_seconds,
            )
            return email_reply_service.claim_mailbox_sync(
                db,
                mailbox_email=mailbox,
                worker_id=worker_id,
            )
        except Exception:
            db.rollback()
            logger.exception(
                "Email reply worker could not claim mailbox work",
                extra={"worker_id": worker_id, "mailbox": mailbox},
            )
            return None


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    stop_event = threading.Event()

    def request_stop(signum: int, _frame: object) -> None:
        logger.info("Email reply worker shutdown requested", extra={"signal": signum})
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    return run_worker(stop_event=stop_event)


if __name__ == "__main__":
    raise SystemExit(main())
