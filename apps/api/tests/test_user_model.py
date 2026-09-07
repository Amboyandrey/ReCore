"""The User model persists and enforces case-insensitive email uniqueness at the database level."""

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User


async def test_user_round_trips(db_session: AsyncSession) -> None:
    """A saved user comes back with its defaults (uuid id, is_superuser false) applied."""
    user = User(email="ada@example.com", password_hash="hashed")
    db_session.add(user)
    await db_session.flush()

    assert user.id is not None
    assert user.is_superuser is False
    assert user.created_at is not None


async def test_email_uniqueness_is_case_insensitive(db_session: AsyncSession) -> None:
    """citext rejects a second user whose email differs only by case."""
    db_session.add(User(email="Ada@Example.com", password_hash="hashed"))
    await db_session.flush()

    db_session.add(User(email="ada@example.com", password_hash="hashed"))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()
