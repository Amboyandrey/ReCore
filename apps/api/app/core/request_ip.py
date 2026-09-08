"""The one place a caller's IP is pulled off a request — for rate-limit keys and audit rows."""

from fastapi import Request


def client_ip(request: Request) -> str:
    """Extract the caller's IP, falling back when the test client (or a proxy misconfig) omits it."""
    return request.client.host if request.client else "unknown"
