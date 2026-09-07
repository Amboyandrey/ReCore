"""Edge middleware — a request id on every log line, and the security headers every response gets."""

import uuid
from collections.abc import Awaitable, Callable

import structlog
from fastapi import Request, Response

RequestResponseEndpoint = Callable[[Request], Awaitable[Response]]


async def request_context_middleware(request: Request, call_next: RequestResponseEndpoint) -> Response:
    """Bind a request id to structlog's context so every log line in this request carries it."""
    request_id = request.headers.get("x-request-id", str(uuid.uuid4()))
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(request_id=request_id)
    response = await call_next(request)
    response.headers["x-request-id"] = request_id
    return response


async def security_headers_middleware(request: Request, call_next: RequestResponseEndpoint) -> Response:
    """Attach the baseline security headers (CSP, HSTS, etc.) to every response."""
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    response.headers["Content-Security-Policy"] = "default-src 'self'; frame-ancestors 'none'"
    return response
