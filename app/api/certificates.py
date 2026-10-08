"""Download one generated certificate."""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, HTTPException
from starlette.responses import FileResponse

from app.dependencies import AppSettings, DbSession
from app.models import CertificateRecipient, RecipientStatus
from app.services.certificate_service import download_filename
from app.services.storage import UnsafePathError, resolve_certificate_path

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/certificates", tags=["certificates"])


@router.get("/{certificate_id}/download")
def download_certificate(
    certificate_id: uuid.UUID,
    session: DbSession,
    settings: AppSettings,
) -> FileResponse:
    recipient = session.get(CertificateRecipient, certificate_id)
    if recipient is None:
        raise HTTPException(status_code=404, detail="Certificate not found")
    if recipient.status != RecipientStatus.COMPLETED or not recipient.certificate_path:
        raise HTTPException(status_code=409, detail="Certificate is not available for download")

    try:
        path = resolve_certificate_path(settings.storage_path, recipient.certificate_path)
    except UnsafePathError:
        logger.error("unsafe certificate path recipient_id=%s", certificate_id)
        raise HTTPException(status_code=404, detail="Certificate file not found") from None

    if not path.is_file():
        logger.error("certificate file missing recipient_id=%s", certificate_id)
        raise HTTPException(status_code=404, detail="Certificate file not found")

    return FileResponse(
        path,
        media_type="application/pdf",
        filename=download_filename(recipient.name, recipient.id),
    )
