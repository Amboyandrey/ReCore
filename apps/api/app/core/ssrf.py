"""Rejects provider base URLs that resolve to addresses outside the public internet.

"Any OpenAI-compatible endpoint" is a request-forgery primitive if unguarded — a workspace admin
(malicious or just careless) could point base_url at an internal service (a cloud metadata
endpoint, an internal admin panel, the API's own Redis) and use ReCore's outbound requests to
reach it. This checks the resolved address at credential-save and validation time.

It does not pin the resolved address for the request that follows — a DNS answer could change
between this check and the actual HTTP call (a "TOCTOU" gap, closed via rebinding). Closing that
fully means resolving once and connecting to the pinned IP for every request through this
credential, which is meaningful extra plumbing left for the hardening phase; this check still
stops the overwhelmingly common case, a URL that's simply misconfigured or maliciously chosen.
"""

import ipaddress
import socket
from urllib.parse import urlparse

from app.core.errors import AppError


class UnsafeBaseUrlError(AppError):
    """Raised when a base URL is malformed or resolves to a disallowed address range."""

    status_code = 400
    detail = "This base URL isn't allowed."


def _is_disallowed(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Report whether an address falls in a private, loopback, link-local, or reserved range."""
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def assert_safe_base_url(url: str) -> None:
    """Resolve the URL's host and raise if any of its addresses are outside the public internet."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeBaseUrlError("Base URL must start with http:// or https://.")
    if not parsed.hostname:
        raise UnsafeBaseUrlError("Base URL must include a host.")

    try:
        addrinfo = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror as exc:
        raise UnsafeBaseUrlError(f"Could not resolve host: {parsed.hostname}") from exc

    for _family, _type, _proto, _canonname, sockaddr in addrinfo:
        ip = ipaddress.ip_address(sockaddr[0])
        if _is_disallowed(ip):
            raise UnsafeBaseUrlError(f"Base URL resolves to a disallowed address: {ip}")
