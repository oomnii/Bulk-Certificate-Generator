"""Job creation, status, results, and ZIP download."""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select
from starlette.background import BackgroundTask
from starlette.responses import FileResponse

from app.dependencies import AppSettings, DbSession
from app.models import CertificateRecipient, RecipientStatus
from app.schemas import CertificateListResponse, JobCreateRequest, JobStatusResponse
from app.services.certificate_service import archive_entry_name, build_certificate_zip
from app.services.job_service import create_job, get_job_response, list_certificate_results
from app.services.storage import UnsafePathError, resolve_certificate_path

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.post("", status_code=202, response_model=JobStatusResponse)
def create_certificate_job(
    payload: JobCreateRequest,
    session: DbSession,
    settings: AppSettings,
) -> JobStatusResponse:
    return create_job(session, payload, settings)


@router.get("/{job_id}", response_model=JobStatusResponse)
def get_certificate_job(job_id: uuid.UUID, session: DbSession) -> JobStatusResponse:
    response = get_job_response(session, job_id)
    if response is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return response


@router.get("/{job_id}/certificates", response_model=CertificateListResponse)
def list_job_certificates(
    job_id: uuid.UUID,
    session: DbSession,
    settings: AppSettings,
    limit: int | None = Query(default=None, ge=1),
    offset: int = Query(default=0, ge=0),
) -> CertificateListResponse:
    page_size = settings.certificates_page_size_default if limit is None else limit
    if page_size > settings.certificates_page_size_max:
        raise HTTPException(
            status_code=422,
            detail=f"limit must be at most {settings.certificates_page_size_max}",
        )
    response = list_certificate_results(session, job_id, limit=page_size, offset=offset)
    if response is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return response


@router.get("/{job_id}/download")
def download_job_archive(
    job_id: uuid.UUID, session: DbSession, settings: AppSettings
) -> FileResponse:
    if get_job_response(session, job_id) is None:
        raise HTTPException(status_code=404, detail="Job not found")

    recipients = session.scalars(
        select(CertificateRecipient)
        .where(
            CertificateRecipient.job_id == job_id,
            CertificateRecipient.status == RecipientStatus.COMPLETED,
        )
        .order_by(CertificateRecipient.input_index.asc())
    ).all()

    entries: list[tuple[str, Path]] = []
    for recipient in recipients:
        if not recipient.certificate_path:
            continue
        try:
            path = resolve_certificate_path(settings.storage_path, recipient.certificate_path)
        except UnsafePathError:
            logger.error("skipping unsafe certificate path recipient_id=%s", recipient.id)
            continue
        if path.is_file():
            entries.append((archive_entry_name(recipient), path))

    if not entries:
        raise HTTPException(
            status_code=404, detail="No completed certificates are available for this job"
        )

    archive_path = build_certificate_zip(entries)
    return FileResponse(
        archive_path,
        media_type="application/zip",
        filename=f"certificates-{job_id}.zip",
        background=BackgroundTask(archive_path.unlink, missing_ok=True),
    )
