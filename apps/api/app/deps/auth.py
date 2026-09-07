"""Dependency chain that resolves a request's session, its user, and checks CSRF on mutations."""

from fastapi import Depends, Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import get_db
from app.core.errors import CsrfTokenInvalid, SessionInvalid
from app.core.redis import get_redis
from app.core.security import tokens_match
from app.models import User
from app.services.sessions import SessionData, get_session

settings = get_settings()


async def get_current_session(request: Request, redis: Redis = Depends(get_redis)) -> SessionData:
    """Resolve the caller's session from their cookie, or raise if it's missing or expired."""
    session_id = request.cookies.get(settings.session_cookie_name)
    if session_id is None:
        raise SessionInvalid()
    session = await get_session(redis, session_id)
    if session is None:
        raise SessionInvalid()
    return session


async def get_current_user(
    session: SessionData = Depends(get_current_session),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Load the full user row behind the current session."""
    user = await db.get(User, session.user_id)
    if user is None:
        raise SessionInvalid()
    return user


async def require_csrf(
    request: Request, session: SessionData = Depends(get_current_session)
) -> None:
    """Reject the request unless its X-CSRF-Token header matches the current session's token."""
    header_token = request.headers.get("x-csrf-token")
    if header_token is None or not tokens_match(header_token, session.csrf_token):
        raise CsrfTokenInvalid()
