"""Application configuration.

P0 §13: secrets and deployment-specific values come from environment variables
only; per-tenant business configuration (currency, unit label, timezone, cycle,
reminder schedule, default price) lives on the ``tenant`` row, never here.

Only the settings P1 actually uses are declared. The reserved names from P0 §13
(``GROQ_API_KEY``, ``SPEECH_PROVIDER``, ``COMMS_PROVIDER`` …) are documented in
``.env.example`` but deliberately not loaded: P1 implements no adapter, and an
unused setting is a claim that something is wired up when it is not.
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
