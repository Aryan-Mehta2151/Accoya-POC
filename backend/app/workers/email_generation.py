"""Poll and execute durable email-generation jobs.

Run from the backend directory with ``python -m app.workers.email_generation``.
"""

from __future__ import annotations

import logging
import signal
import socket
import threading
import uuid

from app.config import Settings, get_settings
from app.db.database import SessionLocal
from app.services import email_generation_service, email_generator


logger = logging.getLogger(__name__)


class _Heartbeat:
    """Renew one worker lease without sharing SQLAlchemy sessions across threads."""

    def __init__(self, *, job_id: str, worker_id: str, interval: float) -> None:
        self._job_id = job_id
        self._worker_id = worker_id
        self._interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=f"email-generation-heartbeat-{job_id}",
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
                    renewed = email_generation_service.heartbeat_job(
                        db,
                        job_id=self._job_id,
                        worker_id=self._worker_id,
                    )
                except Exception:
                    db.rollback()
                    logger.error(
                        "Email generation heartbeat failed",
                        extra={
                            "email_generation_job_id": self._job_id,
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
    """Run until interrupted; return without claiming when Gemini is unavailable."""

    configured = settings or get_settings()
    if not configured.gemini_api_key.strip() or not configured.gemini_model.strip():
        logger.error(
            "Email generation worker is disabled because Gemini is not configured"
        )
        return 2
    if min(
        configured.email_generation_worker_poll_seconds,
        configured.email_generation_heartbeat_seconds,
        configured.email_generation_stale_seconds,
        configured.gemini_request_timeout_seconds,
    ) <= 0:
        logger.error("Email generation worker timing settings must be positive")
        return 2
    if (
        configured.email_generation_stale_seconds
        <= configured.email_generation_heartbeat_seconds
    ):
        logger.error("Email generation stale threshold must exceed heartbeat interval")
        return 2

    stopper = stop_event or threading.Event()
    worker_id = f"{socket.gethostname()}:{uuid.uuid4()}"
    agent = email_generator.get_accoya_email_agent()
    logger.info("Email generation worker started", extra={"worker_id": worker_id})

    while not stopper.is_set():
        claim = _claim_one(configured, worker_id)
        if claim is None:
            stopper.wait(configured.email_generation_worker_poll_seconds)
            continue

        heartbeat = _Heartbeat(
            job_id=claim.job_id,
            worker_id=worker_id,
            interval=configured.email_generation_heartbeat_seconds,
        )
        heartbeat.start()
        try:
            with SessionLocal() as db:
                email_generation_service.execute_claimed_job(
                    db,
                    claim=claim,
                    agent=agent,
                )
        except email_generation_service.EmailGenerationJobNotRunningError:
            logger.warning(
                "Email generation result ignored after its lease ended",
                extra={
                    "email_generation_job_id": claim.job_id,
                    "agent_run_id": claim.run_id,
                },
            )
        except Exception:
            logger.error(
                "Email generation worker could not finalize a job",
                extra={
                    "email_generation_job_id": claim.job_id,
                    "agent_run_id": claim.run_id,
                },
            )
        finally:
            heartbeat.stop()

    logger.info("Email generation worker stopped", extra={"worker_id": worker_id})
    return 0


def _claim_one(
    settings: Settings,
    worker_id: str,
) -> email_generation_service.ClaimedEmailGeneration | None:
    with SessionLocal() as db:
        try:
            email_generation_service.recover_stale_jobs(
                db,
                stale_after_seconds=settings.email_generation_stale_seconds,
            )
            return email_generation_service.claim_next_job(
                db,
                worker_id=worker_id,
            )
        except Exception:
            db.rollback()
            logger.error(
                "Email generation worker could not claim a job",
                extra={"worker_id": worker_id},
            )
            return None


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    stopper = threading.Event()

    def request_stop(signum: int, _: object) -> None:
        logger.info(
            "Email generation worker shutdown requested",
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
