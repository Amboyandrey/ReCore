"""Application settings, loaded once from the environment and shared everywhere via `get_settings`."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed, validated environment configuration for the API process."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "ReCore API"
    environment: str = Field(default="development")
    debug: bool = Field(default=False)

    # Postgres, as an async SQLAlchemy URL (postgresql+asyncpg://...)
    database_url: str = Field(default="postgresql+asyncpg://recore:recore@localhost:5432/recore")

    # Redis connection string used for sessions, rate limits, streams and caching
    redis_url: str = Field(default="redis://localhost:6379/0")

    # Origins allowed to call the API with credentials, from the web app
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])

    # Master key for envelope-encrypting provider credentials (phase 4); base64, 32 bytes
    master_key: str = Field(default="")

    # Cookie name for the opaque session id issued at login
    session_cookie_name: str = "recore_session"


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide Settings instance, constructed once and cached."""
    return Settings()
