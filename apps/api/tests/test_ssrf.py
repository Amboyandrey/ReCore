"""The SSRF guard blocks internal and malformed base URLs without touching the network."""

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
