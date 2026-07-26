"""Poll and execute durable SMTP delivery jobs.

Run from the backend directory with python -m app.workers.email_delivery.
"""

from __future__ import annotations

import logging
import signal
import socket
import threading
import uuid

from app.config import Settings, get_settings
from app.db.database import SessionLocal
from app.services import email_delivery_service


logger = logging.getLogger(__name__)


class _Heartbeat:
    """Renew one delivery lease without sharing SQLAlchemy sessions."""

    def __init__(self, *, job_id: str, worker_id: str, interval: float) -> None:
        self._job_id = job_id
        self._worker_id = worker_id
        self._interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=f"email-delivery-heartbeat-{job_id}",
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
                    renewed = email_delivery_service.heartbeat_job(
                        db,
                        job_id=self._job_id,
                        worker_id=self._worker_id,
                    )
                except Exception:
                    db.rollback()
                    logger.error(
                        "Email delivery heartbeat failed",
                        extra={
                            "email_delivery_job_id": self._job_id,
                            "worker_id": self._worker_id,
                        },
                    )
                    continue
            if not renewed:
                return


def run_worker(
    *,
    settings: Settings | None = None,
    stop_event: threading.Event | None = None,
) -> int:
    """Run until interrupted, validating configuration before queue access."""

    configured = settings or get_settings()
    configuration_error = email_delivery_service.delivery_configuration_error(
        configured
    )
    if configuration_error is not None:
        logger.error(
            "Email delivery worker is disabled because SMTP is not configured",
            extra={"configuration_error": configuration_error},
        )
        return 2
    if min(
        configured.email_delivery_worker_poll_seconds,
        configured.email_delivery_heartbeat_seconds,
        configured.email_delivery_stale_seconds,
    ) <= 0:
        logger.error("Email delivery worker timing settings must be positive")
        return 2
    if (
        configured.email_delivery_stale_seconds
        <= configured.email_delivery_heartbeat_seconds
    ):
        logger.error("Email delivery stale threshold must exceed heartbeat interval")
        return 2

    stopper = stop_event or threading.Event()
    worker_id = f"{socket.gethostname()}:{uuid.uuid4()}"
    logger.info("Email delivery worker started", extra={"worker_id": worker_id})

    while not stopper.is_set():
        claim = _claim_one(configured, worker_id)
        if claim is None:
            stopper.wait(configured.email_delivery_worker_poll_seconds)
            continue

        heartbeat = _Heartbeat(
            job_id=claim.job_id,
            worker_id=worker_id,
            interval=configured.email_delivery_heartbeat_seconds,
        )
        heartbeat.start()
        try:
            with SessionLocal() as db:
                email_delivery_service.execute_claimed_job(
                    db,
                    claim=claim,
                    settings=configured,
                )
        except email_delivery_service.EmailDeliveryJobNotRunningError:
            logger.warning(
                "Email delivery result ignored after its lease ended",
                extra={"email_delivery_job_id": claim.job_id},
            )
        except Exception:
            logger.error(
                "Email delivery worker could not finalize a job",
                extra={"email_delivery_job_id": claim.job_id},
            )
        finally:
            heartbeat.stop()

    logger.info("Email delivery worker stopped", extra={"worker_id": worker_id})
    return 0


def _claim_one(
    settings: Settings,
    worker_id: str,
) -> email_delivery_service.ClaimedEmailDelivery | None:
    with SessionLocal() as db:
        try:
            email_delivery_service.recover_stale_jobs(
                db,
                stale_after_seconds=settings.email_delivery_stale_seconds,
            )
            return email_delivery_service.claim_next_job(
                db,
                worker_id=worker_id,
            )
        except Exception:
            db.rollback()
            logger.error(
                "Email delivery worker could not claim a job",
                extra={"worker_id": worker_id},
            )
            return None


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    stopper = threading.Event()

    def request_stop(signum: int, _: object) -> None:
        logger.info(
            "Email delivery worker shutdown requested",
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
