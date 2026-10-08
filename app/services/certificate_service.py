"""Create certificate files and archive them for download."""

from __future__ import annotations

import logging
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from uuid import UUID

from app.config import Settings
from app.models import CertificateRecipient
from app.services.pdf_renderer import render_certificate_pdf
from app.services.storage import (
    relative_certificate_path,
    resolve_certificate_path,
    write_pdf_atomic,
)

logger = logging.getLogger(__name__)

CLIENT_GENERATION_ERROR = "Certificate generation failed"


@dataclass(frozen=True)
class CertificateRenderContext:
    job_id: UUID
    recipient_id: UUID
    recipient_name: str
    course_name: str
    event_date: date
    issued_by: str


def generate_certificate_file(context: CertificateRenderContext, settings: Settings) -> str:
    """Render one certificate and return its storage path relative to STORAGE_DIR."""
    relative_path = relative_certificate_path(context.job_id, context.recipient_id)
    destination = resolve_certificate_path(settings.storage_path, relative_path)
    write_pdf_atomic(
        destination,
        lambda path: render_certificate_pdf(
            path,
            recipient_name=context.recipient_name,
            course_name=context.course_name,
            event_date=context.event_date,
            issued_by=context.issued_by,
            certificate_id=str(context.recipient_id),
        ),
    )
    return relative_path


def remove_certificate_file(settings: Settings, relative_path: str | None) -> None:
    if not relative_path:
        return
    try:
        path = resolve_certificate_path(settings.storage_path, relative_path)
    except ValueError:
        logger.warning("refusing to delete an unsafe certificate path")
        return
    path.unlink(missing_ok=True)


def download_filename(name: str | None, recipient_id: UUID) -> str:
    cleaned = _filename_label(name, fallback="certificate", max_length=80)
    return f"{cleaned}_{recipient_id}.pdf"


def archive_entry_name(recipient: CertificateRecipient) -> str:
    cleaned = _filename_label(recipient.name, fallback="recipient", max_length=60)
    return f"{recipient.input_index:04d}_{recipient.id}_{cleaned}.pdf"


def build_certificate_zip(entries: list[tuple[str, Path]]) -> Path:
    """Write a ZIP to a temporary file so the archive is not held entirely in memory."""
    handle = tempfile.NamedTemporaryFile(prefix="certificates-", suffix=".zip", delete=False)
    handle.close()
    archive_path = Path(handle.name)
    try:
        with zipfile.ZipFile(archive_path, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
            for entry_name, file_path in entries:
                archive.write(file_path, entry_name)
    except Exception:
        archive_path.unlink(missing_ok=True)
        raise
    return archive_path


def _filename_label(value: str | None, *, fallback: str, max_length: int) -> str:
    raw = value or fallback
    cleaned = "".join(
        character if character.isalnum() or character in ("-", "_") else "_" for character in raw
    )
    cleaned = cleaned.strip("_")[:max_length]
    return cleaned or fallback
