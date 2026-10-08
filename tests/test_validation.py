"""Request and recipient validation."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import ROOT, build_settings, create_job, load_example_request, sample_payload


def test_example_request_file_is_accepted(client: TestClient) -> None:
    response = client.post("/api/v1/jobs", json=load_example_request())
    assert response.status_code == 202
    body = response.json()
    assert body["total"] == 3
    assert body["pending"] == 2
    assert body["failed"] == 1
    assert body["status"] == "pending"


def test_empty_recipients(client: TestClient) -> None:
    response = client.post("/api/v1/jobs", json=sample_payload(recipients=[]))
    assert response.status_code == 422
    assert "recipients" in response.text


def test_missing_recipients(client: TestClient) -> None:
    payload = sample_payload()
    del payload["recipients"]
    response = client.post("/api/v1/jobs", json=payload)
    assert response.status_code == 422
    assert "recipients" in response.text


def test_missing_job_metadata(client: TestClient) -> None:
    payload = sample_payload()
    del payload["course_name"]
    response = client.post("/api/v1/jobs", json=payload)
    assert response.status_code == 422
    assert "course_name" in response.text


def test_incorrect_top_level_types(client: TestClient) -> None:
    response = client.post(
        "/api/v1/jobs",
        json=sample_payload(course_name=12, issued_by=False, recipients="Priya"),
    )
    assert response.status_code == 422


def test_invalid_event_date(client: TestClient) -> None:
    response = client.post("/api/v1/jobs", json=sample_payload(event_date="not-a-date"))
    assert response.status_code == 422
    assert "event_date" in response.text


def test_blank_course_name_and_issuer(client: TestClient) -> None:
    blank_course = client.post("/api/v1/jobs", json=sample_payload(course_name="   "))
    blank_issuer = client.post("/api/v1/jobs", json=sample_payload(issued_by="\n\t"))
    assert blank_course.status_code == 422
    assert "Course name must not be blank" in blank_course.text
    assert blank_issuer.status_code == 422
    assert "Issuer" in blank_issuer.text


def test_course_name_length_limit(client: TestClient) -> None:
    response = client.post("/api/v1/jobs", json=sample_payload(course_name="A" * 201))
    assert response.status_code == 422
    assert "at most 200 characters" in response.text


def test_excessive_recipient_count(tmp_path) -> None:
    settings = build_settings(tmp_path, max_recipients_per_job=2)
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/v1/jobs",
            json=sample_payload(
                recipients=[
                    {"name": "One Person", "email": "one@example.com"},
                    {"name": "Two Person", "email": "two@example.com"},
                    {"name": "Three Person", "email": "three@example.com"},
                ]
            ),
        )
        assert response.status_code == 422
        assert "maximum of 2" in response.text
        follow_up = client.post("/api/v1/jobs", json=sample_payload())
        assert follow_up.status_code == 202


def test_request_body_limit(tmp_path) -> None:
    settings = build_settings(tmp_path, max_request_bytes=120)
    with TestClient(create_app(settings)) as client:
        response = client.post("/api/v1/jobs", json=sample_payload())
        assert response.status_code == 413


def test_recipient_validation_cases(client: TestClient) -> None:
    created = create_job(
        client,
        sample_payload(
            recipients=[
                {"name": "", "email": "valid@example.com"},
                {"name": "   ", "email": "valid@example.com"},
                {"name": "No Email"},
                {},
                {"name": "Bad Email", "email": "invalid-email"},
                {"name": 15, "email": "valid@example.com"},
                {"name": "A" * 201, "email": "long@example.com"},
                "not-an-object",
                {"name": "José García", "email": "jose@example.com"},
                {"name": "Иван Петров", "email": "ivan@example.com"},
                {"name": "Ann <Smith>", "email": "ann@example.com"},
            ]
        ),
    )
    assert created["status"] == "pending"
    assert created["pending"] == 3
    assert created["failed"] == 8

    items = client.get(f"/api/v1/jobs/{created['job_id']}/certificates").json()["items"]
    messages = [item["error_message"] for item in items]
    assert messages[0] == "Name is required"
    assert messages[1] == "Name is required"
    assert messages[2] == "Email is required"
    assert messages[3] == "Name is required; Email is required"
    assert messages[4] == "Email is not valid"
    assert messages[5] == "Name must be a string"
    assert "at most 200 characters" in messages[6]
    assert messages[7] == "Recipient must be a JSON object"
    assert messages[8] is None
    assert items[8]["name"] == "José García"
    assert items[9]["name"] == "Иван Петров"
    assert items[10]["name"] == "Ann <Smith>"
    assert items[8]["status"] == "pending"


def test_sample_file_lives_in_the_repository() -> None:
    payload = load_example_request()
    assert payload["course_name"] == "Python Backend Development"
    assert (ROOT / "examples" / "sample_request.json").is_file()
