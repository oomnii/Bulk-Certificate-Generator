"""Job creation, status lifecycle, and progress."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.config import Settings
from app.models import CertificateJob, CertificateRecipient
from app.services.status import derive_job_status, empty_counts, progress_percent
from tests.conftest import assert_invariant, create_job, run_until_idle, sample_payload


def test_progress_percent_handles_zero_total() -> None:
    assert progress_percent(empty_counts()) == 0.0


@pytest.mark.parametrize(
    ("counts", "expected"),
    [
        ({"pending": 2, "processing": 0, "completed": 0, "failed": 1, "total": 3}, "pending"),
        ({"pending": 1, "processing": 1, "completed": 0, "failed": 0, "total": 2}, "processing"),
        ({"pending": 1, "processing": 0, "completed": 1, "failed": 0, "total": 2}, "processing"),
        ({"pending": 0, "processing": 0, "completed": 2, "failed": 0, "total": 2}, "completed"),
        (
            {"pending": 0, "processing": 0, "completed": 2, "failed": 1, "total": 3},
            "completed_with_errors",
        ),
        ({"pending": 0, "processing": 0, "completed": 0, "failed": 3, "total": 3}, "failed"),
    ],
)
def test_derive_job_status(counts: dict[str, int], expected: str) -> None:
    assert derive_job_status(counts) == expected


def test_valid_request_persists_job_and_recipients(
    client: TestClient,
    db_session,
) -> None:
    body = create_job(client)
    assert_invariant(body)
    assert body["status"] == "pending"
    assert body["total"] == 2
    assert body["pending"] == 2
    assert body["processing"] == 0
    assert body["completed"] == 0
    assert body["failed"] == 0
    assert body["progress_percent"] == 0.0
    assert body["started_at"] is None
    assert body["completed_at"] is None

    job_id = uuid.UUID(str(body["job_id"]))
    job = db_session.get(CertificateJob, job_id)
    assert job is not None
    assert job.course_name == "Python Backend Development"
    assert job.status == "pending"
    recipients = db_session.scalars(
        select(CertificateRecipient)
        .where(CertificateRecipient.job_id == job_id)
        .order_by(CertificateRecipient.input_index)
    ).all()
    assert [recipient.name for recipient in recipients] == ["Aarav Sharma", "Priya Patel"]
    assert [recipient.status for recipient in recipients] == ["pending", "pending"]


def test_invalid_recipient_does_not_reject_valid_ones(client: TestClient, db_session) -> None:
    body = create_job(
        client,
        sample_payload(
            recipients=[
                {"name": "Aarav Sharma", "email": "aarav@example.com"},
                {"name": "   ", "email": "invalid-email"},
                {"name": "Priya Patel", "email": "priya@example.com"},
            ]
        ),
    )
    assert body["status"] == "pending"
    assert body["total"] == 3
    assert body["pending"] == 2
    assert body["failed"] == 1
    assert body["progress_percent"] == 33.33
    assert_invariant(body)

    failed = db_session.scalar(
        select(CertificateRecipient).where(CertificateRecipient.status == "failed")
    )
    assert failed is not None
    assert failed.input_index == 1
    assert failed.error_message is not None
    assert "Name is required" in failed.error_message
    assert "Email is not valid" in failed.error_message


def test_duplicate_names_and_emails_are_allowed(client: TestClient) -> None:
    body = create_job(
        client,
        sample_payload(
            recipients=[
                {"name": "Alex Kim", "email": "alex@example.com"},
                {"name": "Alex Kim", "email": "alex@example.com"},
            ]
        ),
    )
    assert body["pending"] == 2
    assert body["failed"] == 0


def test_whitespace_around_name_is_trimmed_without_changing_internal_spaces(
    client: TestClient,
    db_session,
) -> None:
    create_job(
        client,
        sample_payload(recipients=[{"name": "  Ana  Maria  ", "email": " ana@example.com "}]),
    )
    recipient = db_session.scalar(select(CertificateRecipient))
    assert recipient is not None
    assert recipient.name == "Ana  Maria"
    assert recipient.email == "ana@example.com"


def test_job_not_found(client: TestClient) -> None:
    missing = uuid.uuid4()
    assert client.get(f"/api/v1/jobs/{missing}").status_code == 404
    assert client.get(f"/api/v1/jobs/{missing}/certificates").status_code == 404
    assert client.get(f"/api/v1/jobs/{missing}/download").status_code == 404


def test_status_tracks_the_full_lifecycle(
    client: TestClient, settings: Settings, db_session
) -> None:
    created = create_job(
        client,
        sample_payload(
            recipients=[
                {"name": "Aarav Sharma", "email": "aarav@example.com"},
                {"name": "Priya Patel", "email": "priya@example.com"},
                {"name": "", "email": "invalid-email"},
            ]
        ),
    )
    job_id = created["job_id"]
    assert created["status"] == "pending"
    assert_invariant(created)

    assert process_one(settings) is True
    processing = client.get(f"/api/v1/jobs/{job_id}").json()
    assert processing["status"] == "processing"
    assert processing["completed"] == 1
    assert processing["pending"] == 1
    assert processing["failed"] == 1
    assert processing["progress_percent"] == 66.67
    assert processing["started_at"] is not None
    assert processing["completed_at"] is None
    assert_invariant(processing)

    run_until_idle(settings)
    finished = client.get(f"/api/v1/jobs/{job_id}").json()
    assert finished["status"] == "completed_with_errors"
    assert finished["pending"] == 0
    assert finished["processing"] == 0
    assert finished["completed"] == 2
    assert finished["failed"] == 1
    assert finished["progress_percent"] == 100.0
    assert finished["completed_at"] is not None
    assert str(finished["created_at"]).endswith(("Z", "+00:00"))
    assert str(finished["completed_at"]).endswith(("Z", "+00:00"))
    assert_invariant(finished)

    stored = db_session.get(CertificateJob, uuid.UUID(str(job_id)))
    assert stored is not None
    assert stored.status == "completed_with_errors"


def test_all_invalid_recipients_fail_the_job_immediately(client: TestClient) -> None:
    body = create_job(
        client,
        sample_payload(
            recipients=[
                {"name": "", "email": "nope"},
                "not-an-object",
            ]
        ),
    )
    assert body["status"] == "failed"
    assert body["failed"] == 2
    assert body["progress_percent"] == 100.0
    assert body["started_at"] is None
    assert body["completed_at"] is not None
    assert_invariant(body)


def test_fully_completed_job(client: TestClient, settings: Settings) -> None:
    created = create_job(client)
    run_until_idle(settings)
    finished = client.get(f"/api/v1/jobs/{created['job_id']}").json()
    assert finished["status"] == "completed"
    assert finished["completed"] == 2
    assert finished["failed"] == 0
    assert finished["progress_percent"] == 100.0
    assert_invariant(finished)


def test_results_keep_submission_order_and_support_pagination(client: TestClient) -> None:
    created = create_job(
        client,
        sample_payload(
            recipients=[
                {"name": "First Person", "email": "first@example.com"},
                {"name": "", "email": "bad"},
                {"name": "Third Person", "email": "third@example.com"},
            ]
        ),
    )
    listed = client.get(f"/api/v1/jobs/{created['job_id']}/certificates").json()
    assert listed["total"] == 3
    assert [item["input_index"] for item in listed["items"]] == [0, 1, 2]
    assert listed["items"][1]["status"] == "failed"
    assert listed["items"][1]["download_url"] is None
    assert listed["items"][0]["status"] == "pending"

    page = client.get(
        f"/api/v1/jobs/{created['job_id']}/certificates",
        params={"limit": 1, "offset": 1},
    ).json()
    assert page["total"] == 3
    assert page["limit"] == 1
    assert page["offset"] == 1
    assert page["items"][0]["name"] is None
    assert page["items"][0]["input_index"] == 1


def test_foreign_keys_are_enforced(db_session) -> None:
    db_session.add(
        CertificateRecipient(
            job_id=uuid.uuid4(),
            input_index=0,
            name="Orphan",
            email="orphan@example.com",
            status="pending",
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()
    assert db_session.scalar(select(func.count()).select_from(CertificateRecipient)) == 0


def test_health(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok", "storage": "ok"}


def process_one(settings: Settings) -> bool:
    from app.workers.certificate_worker import process_next_recipient

    return process_next_recipient(settings)
