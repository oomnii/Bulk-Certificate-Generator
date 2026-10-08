"""Derive job status and progress from recipient rows.

Counters are always calculated from recipient records. The job row stores the
derived status so it can be queried, but it is not a second source of truth.
"""

from __future__ import annotations

from app.models import JobStatus

RECIPIENT_STATUSES = ("pending", "processing", "completed", "failed")


def empty_counts() -> dict[str, int]:
    counts = {status: 0 for status in RECIPIENT_STATUSES}
    counts["total"] = 0
    return counts


def progress_percent(counts: dict[str, int]) -> float:
    """Processed recipients, including failures, as a percentage of the batch."""
    total = counts["total"]
    if total <= 0:
        return 0.0
    processed = counts["completed"] + counts["failed"]
    return round(processed * 100.0 / total, 2)


def derive_job_status(counts: dict[str, int]) -> str:
    """Map recipient counts to a job status.

    Pending recipients keep the job open even when nobody is actively processing.
    Validation failures alone do not complete the job while valid rows remain.
    """
    pending = counts["pending"]
    processing = counts["processing"]
    completed = counts["completed"]
    failed = counts["failed"]

    if processing > 0 or (pending > 0 and completed > 0):
        return JobStatus.PROCESSING
    if pending > 0:
        return JobStatus.PENDING
    if completed > 0 and failed > 0:
        return JobStatus.COMPLETED_WITH_ERRORS
    if completed > 0:
        return JobStatus.COMPLETED
    return JobStatus.FAILED
