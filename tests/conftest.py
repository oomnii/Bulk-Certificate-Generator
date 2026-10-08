"""Shared fixtures. Each test gets its own SQLite file and storage directory."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import get_session_factory
from app.main import create_app
from app.workers.certificate_worker import process_next_recipient

ROOT = Path(__file__).resolve().parents[1]
TERMINAL_STATUSES = {"completed", "completed_with_errors", "failed"}


def build_settings(tmp_path: Path, **overrides: object) -> Settings:
    database_path = tmp_path / "test.db"
    storage = tmp_path / "storage"
    values: dict[str, object] = {
        "database_url": "sqlite:///" + database_path.resolve().as_posix(),
        "storage_dir": str(storage),
        "log_level": "WARNING",
        "worker_poll_interval_seconds": 0.05,
        "processing_lease_seconds": 300,
        "max_generation_attempts": 3,
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return build_settings(tmp_path)


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    application = create_app(settings)
    with TestClient(application) as test_client:
        yield test_client


@pytest.fixture
def db_session(client: TestClient) -> Iterator[Session]:
    del client
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def sample_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "course_name": "Python Backend Development",
        "event_date": "2026-10-08",
        "issued_by": "Example Academy",
        "recipients": [
            {"name": "Aarav Sharma", "email": "aarav@example.com"},
            {"name": "Priya Patel", "email": "priya@example.com"},
        ],
    }
    payload.update(overrides)
    return payload


def create_job(client: TestClient, payload: dict[str, object] | None = None) -> dict[str, object]:
    response = client.post("/api/v1/jobs", json=payload or sample_payload())
    assert response.status_code == 202, response.text
    return response.json()


def assert_invariant(body: dict[str, object]) -> None:
    total = int(body["total"])
    pending = int(body["pending"])
    processing = int(body["processing"])
    completed = int(body["completed"])
    failed = int(body["failed"])
    assert total == pending + processing + completed + failed
    if total == 0:
        assert body["progress_percent"] == 0.0
    else:
        expected = round((completed + failed) * 100.0 / total, 2)
        assert body["progress_percent"] == expected


def run_until_idle(settings: Settings, limit: int = 1000) -> None:
    for _ in range(limit):
        if not process_next_recipient(settings):
            return
    raise AssertionError("worker did not finish the available recipients")


def load_example_request() -> dict[str, object]:
    return json.loads((ROOT / "examples" / "sample_request.json").read_text(encoding="utf-8"))
