"""Regressions found by review. These lock behavior the original suite did not prove."""

from __future__ import annotations

import io
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from pypdf import PdfReader
from sqlalchemy import select

from app.config import Settings
from app.database import session_scope
from app.models import CertificateJob, CertificateRecipient
from app.services.job_service import claim_next_recipient
from app.services.pdf_renderer import _fit_lines, register_fonts
from app.workers.certificate_worker import process_next_recipient
from tests.conftest import assert_invariant, create_job, run_until_idle, sample_payload


def test_control_characters_are_not_stored_or_returned(client: TestClient, db_session) -> None:
    created = create_job(
        client,
        sample_payload(
            recipients=[
                {"name": "Safe Person", "email": "safe@example.com"},
                {"name": "Bad\nName", "email": "bad@example.com"},
                {"name": "Bad Email", "email": "bad\nemail@example.com"},
                {"name": "Python\nCourse", "email": "course@example.com"},
            ]
        ),
    )
    assert created["status"] == "pending"
    assert created["pending"] == 1
    assert created["failed"] == 3

    items = client.get(f"/api/v1/jobs/{created['job_id']}/certificates").json()["items"]
    assert items[1]["error_message"] == "Name contains unsupported characters"
    assert items[1]["name"] is None
    assert items[2]["error_message"] == "Email contains unsupported characters"
    assert items[2]["email"] is None
    assert "\n" not in client.get(f"/api/v1/jobs/{created['job_id']}/certificates").text

    stored = db_session.scalars(
        select(CertificateRecipient).order_by(CertificateRecipient.input_index)
    ).all()
    assert stored[1].name is None
    assert stored[2].email is None

    rejected = client.post("/api/v1/jobs", json=sample_payload(course_name="Python\nBackend"))
    assert rejected.status_code == 422
    assert (
        db_session.scalar(select(CertificateJob).where(CertificateJob.course_name.contains("\n")))
        is None
    )


def test_maximum_length_course_and_issuer_stay_within_the_line_budget(
    client: TestClient,
    settings: Settings,
) -> None:
    register_fonts()
    from app.services.pdf_renderer import PAGE_WIDTH

    width = PAGE_WIDTH - 150
    _, _name_size = _fit_lines("N" * 200, "DejaVuBold", 28, 12, width, 4)
    course_lines, _course_size = _fit_lines("C" * 200, "DejaVuBold", 18, 11, width, 3)
    issuer_lines, _issuer_size = _fit_lines("I" * 200, "DejaVuBold", 12, 9, width, 2)
    assert len(course_lines) <= 3
    assert len(issuer_lines) <= 2

    created = create_job(
        client,
        sample_payload(
            course_name="C" * 200,
            issued_by="I" * 200,
            recipients=[{"name": "N" * 200, "email": "long@example.com"}],
        ),
    )
    run_until_idle(settings)
    item = client.get(f"/api/v1/jobs/{created['job_id']}/certificates").json()["items"][0]
    response = client.get(item["download_url"])
    assert response.status_code == 200
    text = "\n".join(
        page.extract_text() or "" for page in PdfReader(io.BytesIO(response.content)).pages
    )
    compact = "".join(text.split())
    assert "N" * 200 in compact
    assert "C" * 200 in compact
    assert "I" * 200 in compact
    assert "8 October 2026" in text


def test_exhausted_crash_removes_the_orphan_pdf_and_blocks_download(
    client: TestClient,
    settings: Settings,
) -> None:
    created = create_job(
        client,
        sample_payload(recipients=[{"name": "Orphan Person", "email": "orphan@example.com"}]),
    )
    with session_scope() as session:
        recipient = session.scalars(select(CertificateRecipient)).one()
        recipient.status = "processing"
        recipient.attempt_count = settings.max_generation_attempts
        recipient.processing_started_at = datetime.now(UTC) - timedelta(days=1)
        relative = f"{created['job_id']}/{recipient.id}.pdf"
        orphan = Path(settings.storage_dir) / relative
        orphan.parent.mkdir(parents=True, exist_ok=True)
        orphan.write_bytes(b"%PDF-1.4\n%orphan")
        recipient_id = recipient.id

    assert process_next_recipient(settings) is False
    finished = client.get(f"/api/v1/jobs/{created['job_id']}").json()
    assert finished["status"] == "failed"
    assert finished["failed"] == 1
    assert not (
        Path(settings.storage_dir) / f"{created['job_id']}" / f"{recipient_id}.pdf"
    ).exists()
    download = client.get(f"/api/v1/certificates/{recipient_id}/download")
    assert download.status_code == 409


def test_two_claimers_cannot_take_the_same_recipient(client: TestClient) -> None:
    create_job(
        client, sample_payload(recipients=[{"name": "Only Person", "email": "only@example.com"}])
    )
    barrier = threading.Barrier(2)
    claimed: list[uuid.UUID | None] = []
    errors: list[BaseException] = []

    def attempt() -> None:
        try:
            barrier.wait(timeout=5)
            with session_scope() as session:
                claimed.append(claim_next_recipient(session))
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    winners = [recipient_id for recipient_id in claimed if recipient_id is not None]
    assert len(winners) == 1
    assert len(set(winners)) == 1


def test_generation_failure_keeps_database_status_aligned_with_the_api(
    client: TestClient,
    settings: Settings,
    db_session,
    monkeypatch,
) -> None:
    def flaky(context, active_settings):
        if context.recipient_name == "Broken Person":
            raise RuntimeError("simulated renderer failure")
        from app.services.certificate_service import generate_certificate_file

        return generate_certificate_file(context, active_settings)

    monkeypatch.setattr("app.workers.certificate_worker.generate_certificate_file", flaky)
    created = create_job(
        client,
        sample_payload(
            recipients=[
                {"name": "Broken Person", "email": "broken@example.com"},
                {"name": "Healthy Person", "email": "healthy@example.com"},
            ]
        ),
    )
    run_until_idle(settings)
    body = client.get(f"/api/v1/jobs/{created['job_id']}").json()
    assert body["status"] == "completed_with_errors"
    assert body["completed"] == 1
    assert body["failed"] == 1
    assert body["pending"] == 0
    assert body["processing"] == 0
    assert_invariant(body)

    stored = db_session.get(CertificateJob, uuid.UUID(str(created["job_id"])))
    assert stored is not None
    assert stored.status == body["status"]
    items = client.get(f"/api/v1/jobs/{created['job_id']}/certificates").json()["items"]
    assert items[0]["status"] == "failed"
    assert (
        client.get(f"/api/v1/certificates/{items[0]['recipient_id']}/download").status_code == 409
    )
    healthy = client.get(items[1]["download_url"])
    assert healthy.status_code == 200
    assert healthy.content.startswith(b"%PDF")
    text = "\n".join(
        page.extract_text() or "" for page in PdfReader(io.BytesIO(healthy.content)).pages
    )
    assert "Healthy Person" in text
    assert "Broken Person" not in text
