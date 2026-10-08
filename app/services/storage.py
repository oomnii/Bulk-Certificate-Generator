"""Safe certificate paths under the configured storage directory."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from uuid import UUID


class UnsafePathError(ValueError):
    """A stored path would leave the storage directory or is not a PDF."""


def relative_certificate_path(job_id: UUID, recipient_id: UUID) -> str:
    return f"{job_id}/{recipient_id}.pdf"


def resolve_certificate_path(storage_dir: Path, relative_path: str) -> Path:
    """Resolve a stored relative path and refuse anything outside storage."""
    if not relative_path or relative_path.startswith(("/", "\\")):
        raise UnsafePathError("absolute paths are not allowed")
    if len(relative_path) >= 2 and relative_path[1] == ":":
        raise UnsafePathError("absolute paths are not allowed")

    storage_root = storage_dir.resolve()
    candidate = (storage_root / relative_path).resolve()
    if not candidate.is_relative_to(storage_root):
        raise UnsafePathError("path escapes the storage directory")
    if candidate.suffix.lower() != ".pdf":
        raise UnsafePathError("certificate path must be a PDF")
    return candidate


def write_pdf_atomic(destination: Path, render: Callable[[Path], None]) -> None:
    """Write a PDF to a temporary file in the same directory, then rename it.

    ``os.replace`` is atomic on the platforms this project supports, so clients
    never observe a partially written certificate.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temp_name = tempfile.mkstemp(
        prefix=".tmp-",
        suffix=".pdf",
        dir=destination.parent,
    )
    os.close(file_descriptor)
    temp_path = Path(temp_name)
    try:
        render(temp_path)
        if not temp_path.is_file() or temp_path.stat().st_size <= 0:
            raise RuntimeError("PDF generation produced an empty file")
        with temp_path.open("rb") as handle:
            # The signature is the four bytes %PDF. The next byte is a hyphen.
            header = handle.read(4)
        if header != b"%PDF":
            raise RuntimeError("PDF generation produced a file that is not a PDF")
        os.replace(temp_path, destination)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    finally:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)
