"""Persist jobs and move recipient rows through the status lifecycle."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import (
    TERMINAL_JOB_STATUSES,
    CertificateJob,
    CertificateRecipient,
    JobStatus,
    RecipientStatus,
    utcnow,
)
from app.schemas import (
    CertificateListResponse,
    CertificateResultItem,
    JobCreateRequest,
    JobStatusResponse,
)
from app.services.certificate_service import CertificateRenderContext, remove_certificate_file
from app.services.status import derive_job_status, empty_counts, progress_percent
from app.services.storage import relative_certificate_path, resolve_certificate_path
from app.services.validation import clean_job_field, request_validation_error, validate_recipient

logger = logging.getLogger(__name__)

INTERRUPTED_ERROR = "Certificate generation was interrupted too many times"


def create_job(
    session: Session, payload: JobCreateRequest, settings: Settings
) -> JobStatusResponse:
    if len(payload.recipients) > settings.max_recipients_per_job:
        raise request_validation_error(
            "recipients",
            f"Recipient count exceeds the maximum of {settings.max_recipients_per_job}",
        )

    course_name = clean_job_field(
        payload.course_name,
        "course_name",
        "Course name",
        settings.max_course_name_length,
    )
    issued_by = clean_job_field(
        payload.issued_by,
        "issued_by",
        "Issuer",
        settings.max_issued_by_length,
    )

    job = CertificateJob(
        course_name=course_name,
        event_date=payload.event_date,
        issued_by=issued_by,
        status=JobStatus.PENDING,
    )
    session.add(job)
    session.flush()

    for index, raw in enumerate(payload.recipients):
        result = validate_recipient(raw, index, settings)
        session.add(
            CertificateRecipient(
                job_id=job.id,
                input_index=result.input_index,
                name=result.name,
                email=result.email,
                status=RecipientStatus.FAILED if not result.is_valid else RecipientStatus.PENDING,
                error_message=result.error_message,
                processed_at=utcnow() if not result.is_valid else None,
            )
        )

    session.flush()
    refresh_job_status(session, job.id)
    response = build_job_response(session, job.id)
    session.commit()
    logger.info(
        "created job_id=%s status=%s total=%s pending=%s failed=%s",
        response.job_id,
        response.status,
        response.total,
        response.pending,
        response.failed,
    )
    return response


def get_job_response(session: Session, job_id: uuid.UUID) -> JobStatusResponse | None:
    job = session.get(CertificateJob, job_id)
    if job is None:
        return None
    return build_job_response(session, job_id)


def list_certificate_results(
    session: Session,
    job_id: uuid.UUID,
    *,
    limit: int,
    offset: int,
) -> CertificateListResponse | None:
    job = session.get(CertificateJob, job_id)
    if job is None:
        return None
    total = session.scalar(
        select(func.count())
        .select_from(CertificateRecipient)
        .where(CertificateRecipient.job_id == job_id)
    )
    recipients = session.scalars(
        select(CertificateRecipient)
        .where(CertificateRecipient.job_id == job_id)
        .order_by(CertificateRecipient.input_index.asc())
        .offset(offset)
        .limit(limit)
    ).all()
    return CertificateListResponse(
        job_id=job_id,
        total=int(total or 0),
        limit=limit,
        offset=offset,
        items=[_result_item(recipient) for recipient in recipients],
    )


def count_recipients(session: Session, job_id: uuid.UUID) -> dict[str, int]:
    rows = session.execute(
        select(CertificateRecipient.status, func.count())
        .where(CertificateRecipient.job_id == job_id)
        .group_by(CertificateRecipient.status)
    ).all()
    counts = empty_counts()
    for status, count in rows:
        counts[str(status)] = int(count)
    counts["total"] = sum(
        counts[status] for status in ("pending", "processing", "completed", "failed")
    )
    return counts


def build_job_response(session: Session, job_id: uuid.UUID) -> JobStatusResponse:
    job = session.get(CertificateJob, job_id)
    if job is None:
        raise LookupError(str(job_id))
    counts = count_recipients(session, job_id)
    return JobStatusResponse(
        job_id=job.id,
        status=derive_job_status(counts),
        course_name=job.course_name,
        event_date=job.event_date,
        issued_by=job.issued_by,
        total=counts["total"],
        pending=counts["pending"],
        processing=counts["processing"],
        completed=counts["completed"],
        failed=counts["failed"],
        progress_percent=progress_percent(counts),
        created_at=_as_utc(job.created_at),
        started_at=_as_utc(job.started_at),
        completed_at=_as_utc(job.completed_at),
    )


def refresh_job_status(session: Session, job_id: uuid.UUID) -> CertificateJob:
    job = session.get(CertificateJob, job_id)
    if job is None:
        raise LookupError(str(job_id))
    counts = count_recipients(session, job_id)
    new_status = derive_job_status(counts)
    now = utcnow()
    if job.started_at is None and new_status == JobStatus.PROCESSING:
        job.started_at = now
    if new_status in TERMINAL_JOB_STATUSES:
        if job.completed_at is None:
            job.completed_at = now
    else:
        job.completed_at = None
    job.status = new_status
    return job


def claim_next_recipient(session: Session) -> uuid.UUID | None:
    """Claim one pending recipient.

    The conditional update is the guard against a second worker claiming the
    same row. One worker is the supported way to run this project.
    """
    for _ in range(3):
        recipient = session.scalars(
            select(CertificateRecipient)
            .join(CertificateJob, CertificateRecipient.job_id == CertificateJob.id)
            .where(CertificateRecipient.status == RecipientStatus.PENDING)
            .order_by(CertificateJob.created_at.asc(), CertificateRecipient.input_index.asc())
            .limit(1)
        ).first()
        if recipient is None:
            return None

        recipient_id = recipient.id
        job_id = recipient.job_id
        now = utcnow()
        result = session.execute(
            update(CertificateRecipient)
            .where(
                CertificateRecipient.id == recipient_id,
                CertificateRecipient.status == RecipientStatus.PENDING,
            )
            .values(
                status=RecipientStatus.PROCESSING,
                processing_started_at=now,
                attempt_count=CertificateRecipient.attempt_count + 1,
            )
        )
        if result.rowcount != 1:
            session.expire_all()
            continue

        session.expire(recipient)
        job = session.get(CertificateJob, job_id)
        if job is not None and job.started_at is None:
            job.started_at = now
        refresh_job_status(session, job_id)
        logger.info("claimed recipient_id=%s job_id=%s", recipient_id, job_id)
        return recipient_id
    return None


def load_render_context(session: Session, recipient_id: uuid.UUID) -> CertificateRenderContext:
    recipient = session.get(CertificateRecipient, recipient_id)
    if recipient is None or not recipient.name or not recipient.name.strip():
        raise RuntimeError("pending recipient is missing a name")
    job = session.get(CertificateJob, recipient.job_id)
    if job is None:
        raise RuntimeError("certificate job is missing")
    return CertificateRenderContext(
        job_id=job.id,
        recipient_id=recipient.id,
        recipient_name=recipient.name,
        course_name=job.course_name,
        event_date=job.event_date,
        issued_by=job.issued_by,
    )


def mark_completed(
    session: Session, recipient_id: uuid.UUID, relative_path: str, settings: Settings
) -> None:
    path = resolve_certificate_path(settings.storage_path, relative_path)
    if not path.is_file() or path.stat().st_size <= 0:
        raise RuntimeError("completed certificate file is missing")

    recipient = session.get(CertificateRecipient, recipient_id)
    if recipient is None:
        raise RuntimeError("recipient disappeared before completion")
    if recipient.status == RecipientStatus.COMPLETED:
        return
    recipient.status = RecipientStatus.COMPLETED
    recipient.certificate_path = relative_path
    recipient.error_message = None
    recipient.processed_at = utcnow()
    recipient.processing_started_at = None
    logger.info("completed recipient_id=%s job_id=%s", recipient.id, recipient.job_id)


def mark_failed(
    session: Session,
    recipient_id: uuid.UUID,
    message: str,
    settings: Settings | None = None,
) -> None:
    recipient = session.get(CertificateRecipient, recipient_id)
    if recipient is None or recipient.status == RecipientStatus.COMPLETED:
        return
    job_id = recipient.job_id
    recipient.status = RecipientStatus.FAILED
    recipient.error_message = message
    recipient.certificate_path = None
    recipient.processed_at = utcnow()
    recipient.processing_started_at = None
    if settings is not None:
        remove_certificate_file(settings, relative_certificate_path(job_id, recipient_id))
    logger.info("failed recipient_id=%s job_id=%s", recipient.id, recipient.job_id)


def recover_stale_recipients(session: Session, settings: Settings) -> int:
    """Return crashed in-progress rows to the queue, or fail them after repeated crashes."""
    cutoff = utcnow() - timedelta(seconds=settings.processing_lease_seconds)
    stale_rows = session.scalars(
        select(CertificateRecipient).where(
            CertificateRecipient.status == RecipientStatus.PROCESSING
        )
    ).all()

    recovered = 0
    affected_jobs: set[uuid.UUID] = set()
    for recipient in stale_rows:
        started = recipient.processing_started_at
        if started is not None and _as_utc(started) >= cutoff:
            continue
        recovered += 1
        affected_jobs.add(recipient.job_id)
        if recipient.attempt_count >= settings.max_generation_attempts:
            recipient.status = RecipientStatus.FAILED
            recipient.error_message = INTERRUPTED_ERROR
            recipient.certificate_path = None
            recipient.processed_at = utcnow()
            recipient.processing_started_at = None
            remove_certificate_file(
                settings, relative_certificate_path(recipient.job_id, recipient.id)
            )
            logger.warning("gave up on interrupted recipient_id=%s", recipient.id)
        else:
            recipient.status = RecipientStatus.PENDING
            recipient.processing_started_at = None
            logger.info("requeued interrupted recipient_id=%s", recipient.id)

    for job_id in affected_jobs:
        refresh_job_status(session, job_id)
    return recovered


def _result_item(recipient: CertificateRecipient) -> CertificateResultItem:
    download_url = None
    certificate_id = None
    if recipient.status == RecipientStatus.COMPLETED and recipient.certificate_path:
        certificate_id = recipient.id
        download_url = f"/api/v1/certificates/{recipient.id}/download"
    return CertificateResultItem(
        recipient_id=recipient.id,
        input_index=recipient.input_index,
        name=recipient.name,
        email=recipient.email,
        status=recipient.status,
        error_message=recipient.error_message,
        certificate_id=certificate_id,
        download_url=download_url,
    )


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
