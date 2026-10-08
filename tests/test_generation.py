"""Real PDF generation."""

from __future__ import annotations

import io
import uuid
from pathlib import Path

from fastapi.testclient import TestClient
from pypdf import PdfReader
from sqlalchemy import select

from app.config import Settings
from app.models import CertificateRecipient
from tests.conftest import assert_invariant, create_job, run_until_idle, sample_payload


def pdf_text(data: bytes) -> str:
    reader = PdfReader(io.BytesIO(data))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def test_worker_generates_valid_recipient_pdfs(
    client: TestClient, settings: Settings, db_session
) -> None:
    created = create_job(
        client,
        sample_payload(
            recipients=[
                {"name": "Aarav Sharma", "email": "aarav@example.com"},
                {"name": "José García", "email": "jose@example.com"},
                {"name": "Иван Петров", "email": "ivan@example.com"},
            ]
        ),
    )
    run_until_idle(settings)
    finished = client.get(f"/api/v1/jobs/{created['job_id']}").json()
    assert finished["status"] == "completed"
    assert finished["completed"] == 3
    assert_invariant(finished)

    items = client.get(f"/api/v1/jobs/{created['job_id']}/certificates").json()["items"]
    seen_text: list[str] = []
    storage = Path(settings.storage_dir)
    for item in items:
        recipient = db_session.get(CertificateRecipient, uuid.UUID(item["recipient_id"]))
        assert recipient is not None
        assert recipient.certificate_path == f"{created['job_id']}/{recipient.id}.pdf"
        assert item["name"] not in recipient.certificate_path
        path = storage / recipient.certificate_path
        assert path.is_file()
        assert path.stat().st_size > 0
        assert path.read_bytes().startswith(b"%PDF")
        assert path.resolve().is_relative_to(storage.resolve())

        response = client.get(item["download_url"])
        assert response.status_code == 200
        text = pdf_text(response.content)
        assert item["name"] in text
        assert "Python Backend Development" in text
        assert "Example Academy" in text
        assert "8 October 2026" in text
        assert "CERTIFICATE OF COMPLETION" in text
        assert str(recipient.id) in text
        seen_text.append(text)

    assert "Aarav Sharma" in seen_text[0]
    assert "Aarav Sharma" not in seen_text[1]
    assert "José García" not in seen_text[0]
    assert "Иван Петров" not in seen_text[0]


def test_long_name_and_markup_do_not_break_rendering(
    client: TestClient, settings: Settings
) -> None:
    long_name = "Alexandria " + ("Longname" * 20)
    assert len(long_name) <= 200
    unbroken = "B" * 200
    created = create_job(
        client,
        sample_payload(
            recipients=[
                {"name": long_name, "email": "long@example.com"},
                {"name": unbroken, "email": "unbroken@example.com"},
                {"name": "Ann <Smith> & Co", "email": "ann@example.com"},
            ]
        ),
    )
    run_until_idle(settings)
    finished = client.get(f"/api/v1/jobs/{created['job_id']}").json()
    assert finished["status"] == "completed"
    items = client.get(f"/api/v1/jobs/{created['job_id']}/certificates").json()["items"]
    texts = [pdf_text(client.get(item["download_url"]).content) for item in items]
    assert "Alexandria" in texts[0]
    assert "Longname" in texts[0]
    assert "B" * 40 in texts[1]
    assert "Ann <Smith> & Co" in texts[2]


def test_batch_of_one_hundred_recipients(
    client: TestClient, settings: Settings, db_session
) -> None:
    recipients = [
        {"name": f"Recipient {index:03d}", "email": f"user{index:03d}@example.com"}
        for index in range(100)
    ]
    created = create_job(client, sample_payload(recipients=recipients))
    run_until_idle(settings, limit=120)
    finished = client.get(f"/api/v1/jobs/{created['job_id']}").json()
    assert finished["status"] == "completed"
    assert finished["completed"] == 100
    assert finished["failed"] == 0
    assert finished["progress_percent"] == 100.0
    assert_invariant(finished)

    files = list(Path(settings.storage_dir).rglob("*.pdf"))
    assert len(files) == 100
    stored = db_session.scalars(select(CertificateRecipient)).all()
    assert len(stored) == 100
    assert all(recipient.status == "completed" for recipient in stored)

    first = client.get(f"/api/v1/jobs/{created['job_id']}/certificates", params={"limit": 1}).json()
    last = client.get(
        f"/api/v1/jobs/{created['job_id']}/certificates",
        params={"limit": 1, "offset": 99},
    ).json()
    first_text = pdf_text(client.get(first["items"][0]["download_url"]).content)
    last_text = pdf_text(client.get(last["items"][0]["download_url"]).content)
    assert "Recipient 000" in first_text
    assert "Recipient 099" in last_text
    assert "Recipient 099" not in first_text
