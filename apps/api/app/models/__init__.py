"""Import every model here so it registers on Base.metadata before Alembic autogenerate runs."""

from app.models.user import User

__all__ = ["User"]
