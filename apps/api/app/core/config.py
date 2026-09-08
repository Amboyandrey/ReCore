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

    # Postgres, as an async SQLAlchemy URL (postgresql+asyncpg://...) — owns the schema, and the
    # only URL Alembic ever uses. Migrations always run as the table owner, which Postgres row-
    # level security exempts by default; that's deliberate (see app_database_url below).
    database_url: str = Field(default="postgresql+asyncpg://recore:recore@localhost:5432/recore")

    # The URL the running API process (not Alembic) actually serves requests through. Left unset,
    # it falls back to `database_url` — same as always. Set to a distinct, low-privilege role
    # (`recore_app`, granted CRUD but no DDL — see the RLS migration) and row-level security
    # policies become real for the app's own traffic instead of silently bypassed by the owner.
    app_database_url: str | None = Field(default=None)

    # SQLAlchemy's own defaults (5 + 10 overflow = 15) are quickly exhausted here: a streaming
    # chat response holds its request's connection open for the SSE response's whole duration
    # (FastAPI only tears down a `yield` dependency once the response body is fully sent), not
    # just for the brief query that starts it — found by actually load-testing concurrent
    # streams, not by guessing a number. Tune alongside Postgres's own max_connections.
    db_pool_size: int = 20
    db_max_overflow: int = 20

    # Redis connection string used for sessions, rate limits, streams and caching
    redis_url: str = Field(default="redis://localhost:6379/0")

    # Origins allowed to call the API with credentials, from the web app
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])

    # Master key for envelope-encrypting provider credentials (phase 4); base64, 32 bytes
    master_key: str = Field(default="")

    # Cookie name for the opaque session id issued at login
    session_cookie_name: str = "recore_session"

    # Cookie name for the CSRF token — deliberately NOT httpOnly, so the frontend can read it
    # and echo it back in a header (the "double submit cookie" pattern)
    csrf_cookie_name: str = "recore_csrf"

    # How long a session stays valid without activity (sliding — refreshed on every use)
    session_ttl_seconds: int = 60 * 60 * 24 * 14

    # Login/signup attempts allowed per window, per IP and per email, before a 429
    auth_rate_limit_max: int = 5
    auth_rate_limit_window_seconds: int = 300

    # How long a resolved flag snapshot stays cached in Redis before it's recomputed from Postgres
    flag_cache_ttl_seconds: int = 30

    # Where uploaded attachments are written — a bind-mounted volume in compose, a tmp dir in tests
    storage_dir: str = "/app/uploads"
    max_attachment_size_bytes: int = 10 * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide Settings instance, constructed once and cached."""
    return Settings()
