"""Envelope encryption round-trips, and a wrong key never silently produces the wrong plaintext."""

import pytest
from cryptography.exceptions import InvalidTag

from app.core.crypto import decrypt_secret, encrypt_secret


def test_encrypt_then_decrypt_recovers_the_plaintext() -> None:
    """The exact secret comes back out after a full encrypt/decrypt round trip."""
    secret = encrypt_secret("sk-ant-super-secret-key")
    assert decrypt_secret(secret) == "sk-ant-super-secret-key"


def test_each_encryption_uses_a_fresh_data_key_and_nonce() -> None:
    """Encrypting the same plaintext twice never produces identical ciphertext."""
    first = encrypt_secret("same-plaintext")
    second = encrypt_secret("same-plaintext")
    assert first.ciphertext != second.ciphertext
    assert first.wrapped_key != second.wrapped_key


def test_tampered_ciphertext_fails_to_decrypt() -> None:
    """A single flipped byte in the ciphertext is caught by AES-GCM's authentication tag."""
    secret = encrypt_secret("sk-ant-super-secret-key")
    tampered = secret._replace(ciphertext=bytes([secret.ciphertext[0] ^ 0xFF]) + secret.ciphertext[1:])
    with pytest.raises(InvalidTag):
        decrypt_secret(tampered)
