"""Signup, authentication, and session lifecycle — the only layer touching both users and sessions."""

from datetime import UTC, datetime

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import EmailAlreadyRegistered, InvalidCredentials
from app.core.security import hash_password, needs_rehash, verify_password
from app.models import User
from app.services.sessions import create_session, delete_session


async def signup(db: AsyncSession, *, email: str, password: str) -> User:
    """Create a new account, or raise if the email is already registered."""
    existing = await db.scalar(select(User).where(User.email == email))
    if existing is not None:
        raise EmailAlreadyRegistered()
    user = User(email=email, password_hash=hash_password(password))
    db.add(user)
    await db.flush()
    return user


async def authenticate(db: AsyncSession, *, email: str, password: str) -> User:
    """Verify credentials and return the user, raising the same error for any failure reason."""
    user = await db.scalar(select(User).where(User.email == email))
    if user is None or not verify_password(password, user.password_hash):
        raise InvalidCredentials()
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
    user.last_login_at = datetime.now(UTC)
    return user


async def start_session(redis: Redis, user: User) -> tuple[str, str]:
    """Issue a new session for an authenticated user, returning (session_id, csrf_token)."""
    return await create_session(redis, user.id)


async def end_session(redis: Redis, session_id: str) -> None:
    """Terminate a session immediately — the operation behind logout."""
    await delete_session(redis, session_id)
