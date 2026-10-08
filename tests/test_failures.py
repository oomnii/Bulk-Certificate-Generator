"""One recipient's failure must not stop the rest of the job."""

from __future__ import annotations

from datetime import UTC
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.services.certificate_service import generate_certificate_file
from tests.conftest import assert_invariant, create_job, run_until_idle, sample_payload


def test_pdf_failure_is_isolated(client: TestClient, settings: Settings, monkeypatch) -> None:
    original = generate_certificate_file

    def flaky(context, active_settings):
        if context.recipient_name == "Broken Person":
            raise RuntimeError("simulated renderer failure")
        return original(context, active_settings)

    monkeypatch.setattr("app.workers.certificate_worker.generate_certificate_file", flaky)
    created = create_job(
        client,
        sample_payload(
            recipients=[
                {"name": "Broken Person", "email": "broken@example.com"},
                {"name": "Healthy Person", "email": "healthy@example.com"},
                {"name": "", "email": "invalid-email"},
            ]
        ),
    )
    run_until_idle(settings)
    finished = client.get(f"/api/v1/jobs/{created['job_id']}").json()
    assert finished["status"] == "completed_with_errors"
    assert finished["completed"] == 1
    assert finished["failed"] == 2
    assert finished["pending"] == 0
    assert finished["processing"] == 0
    assert finished["progress_percent"] == 100.0
    assert_invariant(finished)
    assert "Traceback" not in client.get(f"/api/v1/jobs/{created['job_id']}").text

    items = client.get(f"/api/v1/jobs/{created['job_id']}/certificates").json()["items"]
    assert items[0]["status"] == "failed"
    assert items[0]["error_message"] == "Certificate generation failed"
    assert items[0]["download_url"] is None
    assert items[1]["status"] == "completed"
    assert items[2]["status"] == "failed"

    failed_download = client.get(f"/api/v1/certificates/{items[0]['recipient_id']}/download")
    assert failed_download.status_code == 409
    healthy = client.get(items[1]["download_url"])
    assert healthy.status_code == 200
    assert b"Healthy Person" not in failed_download.content
    assert healthy.content.startswith(b"%PDF")

    pdfs = list(Path(settings.storage_dir).rglob("*.pdf"))
    assert len(pdfs) == 1
    assert items[0]["recipient_id"] not in pdfs[0].name


def test_renderer_exception_removes_temporary_files(
    client: TestClient,
    settings: Settings,
    monkeypatch,
) -> None:
    def explode(path: Path, **kwargs: object) -> None:
        del kwargs
        path.write_bytes(b"partial")
        raise RuntimeError("renderer exploded")

    monkeypatch.setattr("app.services.certificate_service.render_certificate_pdf", explode)
    created = create_job(client)
    run_until_idle(settings)

    finished = client.get(f"/api/v1/jobs/{created['job_id']}").json()
    assert finished["status"] == "failed"
    assert finished["failed"] == 2
    assert finished["completed"] == 0
    storage = Path(settings.storage_dir)
    assert list(storage.rglob("*.pdf")) == []
    temporary = [path for path in storage.rglob("*") if path.name.startswith(".tmp-")]
    assert temporary == []


def test_database_write_failure_does_not_block_the_next_recipient(
    client: TestClient,
    settings: Settings,
    monkeypatch,
) -> None:
    calls = {"count": 0}

    def flaky_complete(session, recipient_id, relative_path, active_settings):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("simulated database write failure")
        from app.services.job_service import mark_completed

        return mark_completed(session, recipient_id, relative_path, active_settings)

    monkeypatch.setattr("app.workers.certificate_worker.mark_completed", flaky_complete)
    created = create_job(client)
    assert process_available(settings) is True
    assert process_available(settings) is True

    midway = client.get(f"/api/v1/jobs/{created['job_id']}").json()
    assert midway["completed"] == 1
    assert midway["processing"] == 1
    assert midway["status"] == "processing"
    assert_invariant(midway)
    assert len(list(Path(settings.storage_dir).rglob("*.pdf"))) == 1

    monkeypatch.undo()
    from datetime import datetime, timedelta

    from sqlalchemy import select

    from app.database import session_scope
    from app.models import CertificateRecipient

    with session_scope() as session:
        stuck = session.scalars(
            select(CertificateRecipient).where(CertificateRecipient.status == "processing")
        ).one()
        stuck.processing_started_at = datetime.now(UTC) - timedelta(hours=1)

    run_until_idle(settings)
    finished = client.get(f"/api/v1/jobs/{created['job_id']}").json()
    assert finished["status"] == "completed"
    assert finished["completed"] == 2
    assert len(list(Path(settings.storage_dir).rglob("*.pdf"))) == 2


def process_available(settings: Settings) -> bool:
    from app.workers.certificate_worker import process_next_recipient

    return process_next_recipient(settings)
