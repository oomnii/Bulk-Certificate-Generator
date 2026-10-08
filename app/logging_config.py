"""Logging setup shared by the API and the worker."""

import logging

_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


def configure_logging(level: str) -> None:
    normalized = level.upper()
    if normalized not in _LEVELS:
        normalized = "INFO"
    logging.basicConfig(
        level=normalized,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        force=True,
    )
