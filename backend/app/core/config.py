"""Application configuration.

P0 §13: secrets and deployment-specific values come from environment variables
only; per-tenant business configuration (currency, unit label, timezone, cycle,
reminder schedule, default price) lives on the ``tenant`` row, never here.

Only the settings the code actually uses are declared. A reserved P0 §13 name
that nothing reads stays in ``.env.example`` and out of here: an unused setting
is a claim that something is wired up when it is not.

P7 loads the first two of them. ``COMMS_PROVIDER`` selects a communication
adapter, and ``INTERNAL_JOB_SECRET`` authenticates the host's cron against the
job endpoint. Both have safe postures rather than convenient ones: the provider
defaults to the mock (which sends nothing) and the job secret has **no default**,
so a deployment that forgot to set it gets a refused job rather than an open
"send reminders to everybody" endpoint.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["Settings", "get_settings"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",  # reserved P0 §13 names may be present but unused in P1
        case_sensitive=False,
    )

    database_url: str = Field(
        default="",
        description="postgresql+psycopg://user:pass@host:5432/db",
    )
    jwt_secret: str = Field(default="", description="HS256 signing key")
    access_token_minutes: int = 60  # P0 §3.3
    refresh_token_days: int = 30  # P0 §3.3

    # P7 §7: which communication adapter is wired in. ``mock`` records messages
    # in memory and sends nothing; a real transport is P10.
    comms_provider: str = "mock"
    # P7 §15: the shared secret the host's cron presents to POST
    # /internal/jobs/run-daily. Empty means the endpoint is disabled, never open.
    internal_job_secret: str = Field(default="", description="cron shared secret")

    environment: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    cors_origins: str = ""

    @field_validator("database_url")
    @classmethod
    def _normalise_driver(cls, value: str) -> str:
        """Pin the psycopg3 driver so a bare postgresql:// URL cannot pick psycopg2."""
        if value.startswith("postgresql://"):
            return value.replace("postgresql://", "postgresql+psycopg://", 1)
        return value

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    def require_database_url(self) -> str:
        if not self.database_url:
            raise RuntimeError(
                "DATABASE_URL is not set. Copy backend/.env.example to backend/.env "
                "and fill it in."
            )
        return self.database_url

    def require_internal_job_secret(self) -> str:
        """The cron secret, or a refusal.

        Fails closed, and loudly. An unauthenticated public endpoint that sends
        every customer a dunning message is the one thing this route must never
        become, so a missing secret disables it rather than opening it.
        """
        if not self.internal_job_secret:
            raise RuntimeError(
                "INTERNAL_JOB_SECRET is not set, so the scheduled job endpoint is "
                "disabled. Generate one with: "
                'python -c "import secrets; print(secrets.token_urlsafe(48))"'
            )
        return self.internal_job_secret

    def require_jwt_secret(self) -> str:
        if not self.jwt_secret:
            raise RuntimeError(
                "JWT_SECRET is not set. Generate one with: "
                'python -c "import secrets; print(secrets.token_urlsafe(48))"'
            )
        return self.jwt_secret


@lru_cache
def get_settings() -> Settings:
    return Settings()
