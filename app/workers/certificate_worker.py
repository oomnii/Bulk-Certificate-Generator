"""Database-backed certificate worker.

Run one worker from the repository root:

    python -m app.workers.certificate_worker

The worker polls for pending recipients, claims one row, generates that PDF
outside the database transaction, then records the result. A failure for one
recipient is stored on that row and the loop continues.

Only one worker is supported. SQLite has no SKIP LOCKED queue. The claim
update is conditional so two overlapping processes cannot both own the same
recipient, but this is not a distributed worker pool.
"""

from __future__ import annotations

import logging
import signal
import threading

from app.config import Settings, get_settings
from app.database import configure_database, init_db, redact_database_url, session_scope
from app.logging_config import configure_logging
from app.models import CertificateRecipient
from app.services.certificate_service import (
    CLIENT_GENERATION_ERROR,
    generate_certificate_file,
    remove_certificate_file,
)
from app.services.job_service import (
    claim_next_recipient,
    load_render_context,
    mark_completed,
    mark_failed,
    recover_stale_recipients,
    refresh_job_status,
)

logger = logging.getLogger(__name__)


def process_next_recipient(settings: Settings) -> bool:
    """Claim and finish one recipient. Returns False when there is nothing to do."""
    with session_scope() as session:
        recover_stale_recipients(session, settings)
        recipient_id = claim_next_recipient(session)
        if recipient_id is None:
            return False
        recipient = session.get(CertificateRecipient, recipient_id)
        if recipient is None:
            return False
        job_id = recipient.job_id
        try:
            context = load_render_context(session, recipient_id)
        except Exception:
            logger.exception("recipient context could not be loaded recipient_id=%s", recipient_id)
            mark_failed(session, recipient_id, CLIENT_GENERATION_ERROR, settings)
            refresh_job_status(session, job_id)
            return True

    try:
        relative_path = generate_certificate_file(context, settings)
    except Exception:
        logger.exception("certificate generation failed recipient_id=%s", context.recipient_id)
        with session_scope() as session:
            mark_failed(session, context.recipient_id, CLIENT_GENERATION_ERROR, settings)
            refresh_job_status(session, context.job_id)
        return True

    try:
        with session_scope() as session:
            mark_completed(session, context.recipient_id, relative_path, settings)
            refresh_job_status(session, context.job_id)
    except Exception:
        logger.exception("could not save certificate result recipient_id=%s", context.recipient_id)
        remove_certificate_file(settings, relative_path)
        return True
    return True


def serve(
    settings: Settings,
    stop_event: threading.Event,
    *,
    max_iterations: int | None = None,
) -> None:
    """Poll until ``stop_event`` is set. The database must already be configured."""
    init_db()
    settings.storage_path.mkdir(parents=True, exist_ok=True)
    logger.info(
        "certificate worker started database=%s storage=%s",
        redact_database_url(settings.database_url),
        settings.storage_path,
    )
    iterations = 0
    while not stop_event.is_set():
        if max_iterations is not None and iterations >= max_iterations:
            break
        iterations += 1
        try:
            did_work = process_next_recipient(settings)
        except Exception:
            logger.exception("worker iteration failed")
            did_work = False
        if stop_event.is_set() or did_work:
            continue
        stop_event.wait(settings.worker_poll_interval_seconds)
    logger.info("certificate worker stopped")


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    configure_database(settings)
    stop_event = threading.Event()
    _install_signal_handlers(stop_event)
    try:
        serve(settings, stop_event)
    except KeyboardInterrupt:
        logger.info("certificate worker interrupted")


def _install_signal_handlers(stop_event: threading.Event) -> None:
    def request_stop(signum: int, frame: object) -> None:
        del signum, frame
        logger.info("certificate worker shutdown requested")
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    if hasattr(signal, "SIGTERM"):
        try:
            signal.signal(signal.SIGTERM, request_stop)
        except (ValueError, OSError):
            logger.debug("SIGTERM handler was not installed")


if __name__ == "__main__":
    main()
