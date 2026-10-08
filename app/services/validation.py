"""Job envelope and per-recipient validation."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from fastapi.exceptions import RequestValidationError

from app.config import Settings

# Practical ASCII email check. Internationalized domain names are not accepted.
_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9._%+\-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?)*"
    r"\.[A-Za-z]{2,}$"
)


@dataclass(frozen=True)
class RecipientValidation:
    input_index: int
    name: str | None
    email: str | None
    is_valid: bool
    error_message: str | None


def request_validation_error(field: str, message: str) -> RequestValidationError:
    return RequestValidationError(
        [
            {
                "type": "value_error",
                "loc": ("body", field),
                "msg": message,
                "input": None,
            }
        ]
    )


def has_disallowed_characters(value: str) -> bool:
    """Reject control and line-separator characters that would break rendering."""
    return any(unicodedata.category(character) in {"Cc", "Zl", "Zp"} for character in value)


def clean_job_field(value: str, field: str, label: str, max_length: int) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise request_validation_error(field, f"{label} must not be blank")
    if len(cleaned) > max_length:
        raise request_validation_error(field, f"{label} must be at most {max_length} characters")
    if has_disallowed_characters(cleaned):
        raise request_validation_error(field, f"{label} contains unsupported characters")
    return cleaned


def validate_recipient(raw: Any, index: int, settings: Settings) -> RecipientValidation:
    if not isinstance(raw, dict):
        return RecipientValidation(
            input_index=index,
            name=None,
            email=None,
            is_valid=False,
            error_message="Recipient must be a JSON object",
        )

    name, name_error = _validate_name(raw, settings)
    email, email_error = _validate_email(raw, settings)
    errors = [message for message in (name_error, email_error) if message]
    if errors:
        return RecipientValidation(
            input_index=index,
            name=name,
            email=email,
            is_valid=False,
            error_message="; ".join(errors),
        )
    return RecipientValidation(
        input_index=index,
        name=name,
        email=email,
        is_valid=True,
        error_message=None,
    )


def _validate_name(raw: dict[str, Any], settings: Settings) -> tuple[str | None, str | None]:
    if "name" not in raw or raw["name"] is None:
        return None, "Name is required"
    value = raw["name"]
    if not isinstance(value, str):
        return None, "Name must be a string"
    cleaned = value.strip()
    if not cleaned:
        return None, "Name is required"
    if has_disallowed_characters(cleaned):
        return None, "Name contains unsupported characters"
    if len(cleaned) > settings.max_name_length:
        return None, f"Name must be at most {settings.max_name_length} characters"
    return cleaned, None


def _validate_email(raw: dict[str, Any], settings: Settings) -> tuple[str | None, str | None]:
    if "email" not in raw or raw["email"] is None:
        return None, "Email is required"
    value = raw["email"]
    if not isinstance(value, str):
        return None, "Email must be a string"
    cleaned = value.strip()
    if not cleaned:
        return None, "Email is required"
    if has_disallowed_characters(cleaned):
        return None, "Email contains unsupported characters"
    if len(cleaned) > settings.max_email_length:
        return None, f"Email must be at most {settings.max_email_length} characters"
    if _EMAIL_RE.fullmatch(cleaned) is None:
        return cleaned, "Email is not valid"
    return cleaned, None
