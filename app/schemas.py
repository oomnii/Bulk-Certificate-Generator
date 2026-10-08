"""API request and response models.

Recipient items are typed as ``Any`` on purpose. A strict nested schema would
reject the entire bulk request when a single recipient is invalid. The service
validates each recipient independently and stores the failures.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class JobCreateRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    course_name: str
    event_date: date
    issued_by: str
    recipients: list[Any]

    @field_validator("course_name", "issued_by", mode="before")
    @classmethod
    def require_text(cls, value: object) -> object:
        if not isinstance(value, str):
            raise ValueError("must be a string")
        return value

    @field_validator("recipients")
    @classmethod
    def require_recipients(cls, value: list[Any]) -> list[Any]:
        if len(value) == 0:
            raise ValueError("recipients must not be empty")
        return value


class JobStatusResponse(BaseModel):
    job_id: uuid.UUID
    status: str
    course_name: str
    event_date: date
    issued_by: str
    total: int
    pending: int
    processing: int
    completed: int
    failed: int
    progress_percent: float
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None


class CertificateResultItem(BaseModel):
    recipient_id: uuid.UUID
    input_index: int
    name: str | None
    email: str | None
    status: str
    error_message: str | None
    certificate_id: uuid.UUID | None
    download_url: str | None


class CertificateListResponse(BaseModel):
    job_id: uuid.UUID
    total: int
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)
    items: list[CertificateResultItem]


class HealthResponse(BaseModel):
    status: str
    database: str
    storage: str
