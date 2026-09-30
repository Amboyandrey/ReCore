"""The SSRF guard blocks internal and malformed base URLs without touching the network."""

from types import SimpleNamespace

import pytest

from app.core.ssrf import UnsafeBaseUrlError, assert_safe_base_url


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8000",
        "http://localhost:6379",
        "http://169.254.169.254/latest/meta-data/",  # the AWS/GCP cloud metadata address
        "http://10.0.0.5",
        "http://192.168.1.1",
        "ftp://example.com",
        "not-a-url",
        "http://",
    ],
)
def test_disallowed_or_malformed_urls_are_rejected(url: str) -> None:
    """Loopback, private, link-local, and non-http(s) URLs are all refused."""
    with pytest.raises(UnsafeBaseUrlError):
        assert_safe_base_url(url)


def test_a_public_address_is_allowed() -> None:
    """A literal public IP passes without needing a real DNS lookup, or raising at all."""
    assert_safe_base_url("https://8.8.8.8/v1")


def test_an_allowlisted_host_passes_even_on_a_private_address(monkeypatch: pytest.MonkeyPatch) -> None:
    """An operator-allowlisted hostname skips the address check, matched case-insensitively."""
    monkeypatch.setattr(
        "app.core.ssrf.get_settings", lambda: SimpleNamespace(ssrf_allowed_hosts=["LOCALHOST"])
    )
    assert_safe_base_url("http://localhost:5678/mcp/lead-tools")


def test_the_allowlist_only_covers_exact_hostnames(monkeypatch: pytest.MonkeyPatch) -> None:
    """Allowlisting one host leaves every other private address blocked."""
    monkeypatch.setattr("app.core.ssrf.get_settings", lambda: SimpleNamespace(ssrf_allowed_hosts=["n8n"]))
    with pytest.raises(UnsafeBaseUrlError):
        assert_safe_base_url("http://127.0.0.1:5678")
