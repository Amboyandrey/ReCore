"""Password hashing and the random tokens behind sessions and CSRF — the primitives auth is built on."""

import hmac
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

# argon2-cffi's defaults (Argon2id, 64 MiB, 3 passes) already track OWASP's current guidance.
_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    """Hash a plaintext password for storage — never store or log the plaintext itself."""
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Check a plaintext password against a stored hash, without raising on a bad match."""
    try:
        return _hasher.verify(password_hash, password)
    except VerifyMismatchError:
        return False


def needs_rehash(password_hash: str) -> bool:
    """Report whether a stored hash was made with outdated parameters and should be renewed."""
    return _hasher.check_needs_rehash(password_hash)


def generate_token() -> str:
    """Generate a random, URL-safe token with enough entropy for a session id or CSRF token."""
    return secrets.token_urlsafe(32)


def tokens_match(a: str, b: str) -> bool:
    """Compare two tokens in constant time, so a timing attack can't reveal a correct prefix."""
    return hmac.compare_digest(a, b)
