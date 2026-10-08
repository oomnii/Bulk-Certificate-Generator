"""Runtime configuration loaded from environment variables and an optional .env file."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings.

    Defaults are enough to run the API and worker locally without creating a .env file.
    ``database_url`` accepts any SQLAlchemy URL so PostgreSQL can replace SQLite later.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "sqlite:///./certificates.db"
    storage_dir: str = "./storage"
    max_recipients_per_job: int = Field(default=500, ge=1)
    max_request_bytes: int = Field(default=1_048_576, ge=1)
    worker_poll_interval_seconds: float = Field(default=1.0, gt=0)
    processing_lease_seconds: int = Field(default=300, ge=1)
    max_generation_attempts: int = Field(default=3, ge=1)
    log_level: str = "INFO"
    max_name_length: int = Field(default=200, ge=1)
    max_email_length: int = Field(default=254, ge=1)
    max_course_name_length: int = Field(default=200, ge=1)
    max_issued_by_length: int = Field(default=200, ge=1)
    certificates_page_size_default: int = Field(default=100, ge=1)
    certificates_page_size_max: int = Field(default=500, ge=1)

    @property
    def storage_path(self) -> Path:
        return Path(self.storage_dir)

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache
def get_settings() -> Settings:
    return Settings()


def clear_settings_cache() -> None:
    get_settings.cache_clear()
