"""The users table — one row per person who can sign in, independent of any workspace."""

from datetime import datetime

from sqlalchemy import DateTime
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class User(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A ReCore account — the authentication identity behind one or more workspace memberships."""

    __tablename__ = "users"

    # citext gives us case-insensitive uniqueness (Alice@x.com and alice@x.com are the same login).
    email: Mapped[str] = mapped_column(CITEXT, unique=True, index=True)
    password_hash: Mapped[str]
    is_superuser: Mapped[bool] = mapped_column(default=False, server_default="false")
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
