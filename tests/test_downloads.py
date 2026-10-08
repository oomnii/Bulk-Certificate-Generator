"""Certificate and ZIP retrieval."""

from __future__ import annotations

import io
import uuid
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient
from pypdf import PdfReader

from app.config import Settings
from app.services.storage import UnsafePathError, resolve_certificate_path
from tests.conftest import create_job, run_until_idle, sample_payload


def test_download_headers_and_bytes(client: TestClient, settings: Settings) -> None:
    created = create_job(
        client, sample_payload(recipients=[{"name": "Aarav Sharma", "email": "aarav@example.com"}])
    )
    run_until_idle(settings)
    item = client.get(f"/api/v1/jobs/{created['job_id']}/certificates").json()["items"][0]
    pending_like = client.get(f"/api/v1/certificates/{uuid.uuid4()}/download")
    assert pending_like.status_code == 404

    response = client.get(item["download_url"])
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert str(item["recipient_id"]) in disposition
    assert disposition.endswith('.pdf"') or disposition.endswith(".pdf")
    assert response.content.startswith(b"%PDF")
    text = "\n".join(
        page.extract_text() or "" for page in PdfReader(io.BytesIO(response.content)).pages
    )
    assert "Aarav Sharma" in text


def test_pending_and_failed_certificates_cannot_be_downloaded(client: TestClient) -> None:
    created = create_job(
        client,
        sample_payload(
            recipients=[
                {"name": "Aarav Sharma", "email": "aarav@example.com"},
                {"name": "", "email": "invalid-email"},
            ]
        ),
    )
    items = client.get(f"/api/v1/jobs/{created['job_id']}/certificates").json()["items"]
    pending = client.get(f"/api/v1/certificates/{items[0]['recipient_id']}/download")
    failed = client.get(f"/api/v1/certificates/{items[1]['recipient_id']}/download")
    assert pending.status_code == 409
    assert failed.status_code == 409
    assert "not available" in pending.json()["detail"]


def test_missing_file_is_a_not_found_response(client: TestClient, settings: Settings) -> None:
    created = create_job(
        client, sample_payload(recipients=[{"name": "Aarav Sharma", "email": "aarav@example.com"}])
    )
    run_until_idle(settings)
    item = client.get(f"/api/v1/jobs/{created['job_id']}/certificates").json()["items"][0]
    pdfs = list(Path(settings.storage_dir).rglob("*.pdf"))
    assert len(pdfs) == 1
    pdfs[0].unlink()

    missing = client.get(item["download_url"])
    assert missing.status_code == 404
    assert missing.json()["detail"] == "Certificate file not found"
    archive = client.get(f"/api/v1/jobs/{created['job_id']}/download")
    assert archive.status_code == 404


def test_invalid_certificate_id_is_rejected(client: TestClient) -> None:
    response = client.get("/api/v1/certificates/not-a-uuid/download")
    assert response.status_code == 422


def test_zip_contains_only_completed_certificates_with_unique_names(
    client: TestClient,
    settings: Settings,
) -> None:
    created = create_job(
        client,
        sample_payload(
            recipients=[
                {"name": "Alex Kim", "email": "alex1@example.com"},
                {"name": "Alex Kim", "email": "alex2@example.com"},
                {"name": "", "email": "invalid-email"},
            ]
        ),
    )
    empty = client.get(f"/api/v1/jobs/{created['job_id']}/download")
    assert empty.status_code == 404

    run_until_idle(settings)
    response = client.get(f"/api/v1/jobs/{created['job_id']}/download")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert f"certificates-{created['job_id']}.zip" in response.headers["content-disposition"]

    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = archive.namelist()
        assert len(names) == 2
        assert len(set(names)) == 2
        assert all(name.endswith(".pdf") for name in names)
        assert all("/" not in name and "\\" not in name for name in names)
        for name in names:
            text = "\n".join(
                page.extract_text() or ""
                for page in PdfReader(io.BytesIO(archive.read(name))).pages
            )
            assert "Alex Kim" in text
            assert "Python Backend Development" in text


def test_paths_cannot_escape_storage(tmp_path: Path) -> None:
    storage = tmp_path / "storage"
    storage.mkdir()
    job_id = uuid.uuid4()
    recipient_id = uuid.uuid4()
    safe = resolve_certificate_path(storage, f"{job_id}/{recipient_id}.pdf")
    assert safe.is_relative_to(storage.resolve())

    for relative in (
        "../secret.pdf",
        "..\\secret.pdf",
        "/tmp/secret.pdf",
        "C:/secret.pdf",
        "notes.txt",
    ):
        try:
            resolve_certificate_path(storage, relative)
        except UnsafePathError:
            continue
        raise AssertionError(f"path was accepted: {relative}")
