"""Password hashing and token primitives behave the way every caller downstream depends on."""

from app.core.security import generate_token, hash_password, needs_rehash, tokens_match, verify_password


def test_correct_password_verifies() -> None:
    """A password verifies against its own hash."""
    hashed = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", hashed) is True


def test_wrong_password_fails_without_raising() -> None:
    """A mismatched password returns False rather than raising."""
    hashed = hash_password("correct horse battery staple")
    assert verify_password("wrong password entirely", hashed) is False


def test_fresh_hash_does_not_need_rehash() -> None:
    """A hash made with current parameters isn't flagged for renewal."""
    assert needs_rehash(hash_password("correct horse battery staple")) is False


def test_generated_tokens_are_unique() -> None:
    """Two generated tokens never collide."""
    assert generate_token() != generate_token()


def test_tokens_match_is_exact() -> None:
    """Equal tokens match; a one-character difference does not."""
    token = generate_token()
    assert tokens_match(token, token) is True
    assert tokens_match(token, token[:-1] + "x") is False
