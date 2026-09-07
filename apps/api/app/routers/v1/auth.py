"""Signup, login, logout, and the current-user endpoint — the whole authentication surface."""

from fastapi import APIRouter, Depends, Request, Response
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import get_db
from app.core.errors import RateLimited
from app.core.redis import get_redis
from app.deps.auth import get_current_session, get_current_user, require_csrf
from app.models import User
from app.schemas.auth import LoginRequest, LoginResponse, SignupRequest, UserOut
from app.services.auth import authenticate, end_session, signup, start_session
from app.services.rate_limit import check_rate_limit
from app.services.sessions import SessionData

router = APIRouter(prefix="/auth", tags=["auth"])
settings = get_settings()


def _client_ip(request: Request) -> str:
    """Extract the caller's IP for rate-limit keys, falling back when the test client omits it."""
    return request.client.host if request.client else "unknown"


def _set_auth_cookies(response: Response, session_id: str, csrf_token: str) -> None:
    """Set the session cookie (httpOnly) and its paired CSRF cookie (readable by the frontend)."""
    secure = settings.environment != "development"
    response.set_cookie(
        settings.session_cookie_name,
        session_id,
        max_age=settings.session_ttl_seconds,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        settings.csrf_cookie_name,
        csrf_token,
        max_age=settings.session_ttl_seconds,
        httponly=False,
        secure=secure,
        samesite="lax",
        path="/",
    )


def _clear_auth_cookies(response: Response) -> None:
    """Clear both the session and CSRF cookies — used on logout."""
    response.delete_cookie(settings.session_cookie_name, path="/")
    response.delete_cookie(settings.csrf_cookie_name, path="/")


@router.post("/signup", status_code=201, response_model=UserOut)
async def signup_route(
    body: SignupRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> User:
    """Create a new account. This does not sign the user in — call /auth/login next."""
    allowed = await check_rate_limit(
        redis,
        f"rl:signup:{_client_ip(request)}",
        limit=settings.auth_rate_limit_max,
        window_seconds=settings.auth_rate_limit_window_seconds,
    )
    if not allowed:
        raise RateLimited()
    return await signup(db, email=body.email, password=body.password)


@router.post("/login", response_model=LoginResponse)
async def login_route(
    body: LoginRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> LoginResponse:
    """Verify credentials and start a session, setting the session cookie on success."""
    for key in (f"rl:login:ip:{_client_ip(request)}", f"rl:login:email:{body.email}"):
        allowed = await check_rate_limit(
            redis,
            key,
            limit=settings.auth_rate_limit_max,
            window_seconds=settings.auth_rate_limit_window_seconds,
        )
        if not allowed:
            raise RateLimited()
    user = await authenticate(db, email=body.email, password=body.password)
    session_id, csrf_token = await start_session(redis, user)
    _set_auth_cookies(response, session_id, csrf_token)
    return LoginResponse(user=UserOut.model_validate(user), csrf_token=csrf_token)


@router.post("/logout", status_code=204)
async def logout_route(
    request: Request,
    response: Response,
    redis: Redis = Depends(get_redis),
    _session: SessionData = Depends(get_current_session),
    _csrf: None = Depends(require_csrf),
) -> None:
    """End the current session and clear its cookie — an old cookie is dead the instant this runs."""
    session_id = request.cookies.get(settings.session_cookie_name)
    if session_id is not None:
        await end_session(redis, session_id)
    _clear_auth_cookies(response)


@router.get("/me", response_model=UserOut)
async def me_route(user: User = Depends(get_current_user)) -> User:
    """Return the currently authenticated user."""
    return user
