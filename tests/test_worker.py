"""Worker claiming, crash recovery, and the documented worker command."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings
from app.database import session_scope
from app.models import CertificateJob, CertificateRecipient
from app.workers.certificate_worker import process_next_recipient, serve
from tests.conftest import (
    ROOT,
    TERMINAL_STATUSES,
    assert_invariant,
    create_job,
    run_until_idle,
    sample_payload,
)


def test_idle_worker_does_nothing(settings: Settings, client: TestClient) -> None:
    del client
    assert process_next_recipient(settings) is False


def test_stale_processing_recipient_is_requeued_and_completed(
    client: TestClient,
    settings: Settings,
) -> None:
    created = create_job(
        client, sample_payload(recipients=[{"name": "Retry Person", "email": "retry@example.com"}])
    )
    with session_scope() as session:
        recipient = session.scalars(select(CertificateRecipient)).one()
        recipient.status = "processing"
        recipient.processing_started_at = datetime.now(UTC) - timedelta(hours=2)
        recipient.attempt_count = 1
        job = session.get(CertificateJob, uuid.UUID(str(created["job_id"])))
        assert job is not None
        job.status = "processing"

    assert process_next_recipient(settings) is True
    finished = client.get(f"/api/v1/jobs/{created['job_id']}").json()
    assert finished["status"] == "completed"
    assert finished["completed"] == 1
    assert_invariant(finished)


def test_repeated_crashes_fail_the_recipient(client: TestClient, settings: Settings) -> None:
    created = create_job(
        client, sample_payload(recipients=[{"name": "Stuck Person", "email": "stuck@example.com"}])
    )
    with session_scope() as session:
        recipient = session.scalars(select(CertificateRecipient)).one()
        recipient.status = "processing"
        recipient.attempt_count = settings.max_generation_attempts
        recipient.processing_started_at = datetime.now(UTC) - timedelta(days=1)

    assert process_next_recipient(settings) is False
    finished = client.get(f"/api/v1/jobs/{created['job_id']}").json()
    assert finished["status"] == "failed"
    assert finished["failed"] == 1
    items = client.get(f"/api/v1/jobs/{created['job_id']}/certificates").json()["items"]
    assert items[0]["error_message"] == "Certificate generation was interrupted too many times"
    assert list(Path(settings.storage_dir).rglob("*.pdf")) == []


def test_completed_work_is_not_regenerated(client: TestClient, settings: Settings) -> None:
    created = create_job(
        client, sample_payload(recipients=[{"name": "Once Person", "email": "once@example.com"}])
    )
    run_until_idle(settings)
    pdf = next(Path(settings.storage_dir).rglob("*.pdf"))
    contents = pdf.read_bytes()
    modified = pdf.stat().st_mtime_ns

    assert process_next_recipient(settings) is False
    assert pdf.read_bytes() == contents
    assert pdf.stat().st_mtime_ns == modified
    finished = client.get(f"/api/v1/jobs/{created['job_id']}").json()
    assert finished["completed"] == 1


def test_recent_processing_lease_is_left_alone(client: TestClient, settings: Settings) -> None:
    created = create_job(
        client, sample_payload(recipients=[{"name": "Busy Person", "email": "busy@example.com"}])
    )
    with session_scope() as session:
        recipient = session.scalars(select(CertificateRecipient)).one()
        recipient.status = "processing"
        recipient.processing_started_at = datetime.now(UTC)
        recipient.attempt_count = 1

    assert process_next_recipient(settings) is False
    current = client.get(f"/api/v1/jobs/{created['job_id']}").json()
    assert current["processing"] == 1
    assert current["completed"] == 0
    assert current["status"] == "processing"


def test_serve_stops_after_requested_iterations(client: TestClient, settings: Settings) -> None:
    created = create_job(client)
    serve(settings, threading.Event(), max_iterations=1)
    body = client.get(f"/api/v1/jobs/{created['job_id']}").json()
    assert body["completed"] == 1
    assert body["pending"] == 1
    assert body["processing"] == 0
    assert body["status"] == "processing"
    assert_invariant(body)


def test_worker_module_processes_a_job(
    client: TestClient, settings: Settings, tmp_path: Path
) -> None:
    created = create_job(
        client,
        sample_payload(
            recipients=[
                {"name": "Worker One", "email": "one@example.com"},
                {"name": "Worker Two", "email": "two@example.com"},
            ]
        ),
    )
    log_path = tmp_path / "worker.log"
    log_file = log_path.open("w", encoding="utf-8")
    env = os.environ.copy()
    env["DATABASE_URL"] = settings.database_url
    env["STORAGE_DIR"] = settings.storage_dir
    env["WORKER_POLL_INTERVAL_SECONDS"] = "0.1"
    env["LOG_LEVEL"] = "INFO"
    process = subprocess.Popen(
        [sys.executable, "-m", "app.workers.certificate_worker"],
        cwd=ROOT,
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.time() + 30
        finished: dict[str, object] | None = None
        while time.time() < deadline:
            response = client.get(f"/api/v1/jobs/{created['job_id']}")
            body = response.json()
            if (
                body["status"] in TERMINAL_STATUSES
                and body["pending"] == 0
                and body["processing"] == 0
            ):
                finished = body
                break
            time.sleep(0.2)
        log_file.flush()
        assert finished is not None, log_path.read_text(encoding="utf-8")
        assert finished["status"] == "completed"
        assert finished["completed"] == 2
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        log_file.close()
