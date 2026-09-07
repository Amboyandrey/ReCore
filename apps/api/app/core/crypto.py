"""Envelope encryption for provider API keys.

A random 256-bit data key encrypts the secret; the data key itself is encrypted ("wrapped") by
the app's master key. Rotating the master key only means rewrapping every data key — the
ciphertext of the secrets themselves never has to change.
"""

import base64
import os
from typing import NamedTuple

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import get_settings

_NONCE_SIZE = 12  # bytes — the standard, recommended nonce length for AES-GCM


class EncryptedSecret(NamedTuple):
    """The three values persisted alongside a credential — never the plaintext itself."""

    ciphertext: bytes
    nonce: bytes
    wrapped_key: bytes


def _master_key() -> bytes:
    """Decode the base64 master key from settings, raising clearly if it isn't configured."""
    settings = get_settings()
    if not settings.master_key:
        raise RuntimeError("MASTER_KEY is not set — see apps/api/.env.example.")
    return base64.b64decode(settings.master_key)


def encrypt_secret(plaintext: str) -> EncryptedSecret:
    """Generate a fresh data key, encrypt the secret with it, then wrap the data key itself."""
    data_key = AESGCM.generate_key(bit_length=256)
    nonce = os.urandom(_NONCE_SIZE)
    ciphertext = AESGCM(data_key).encrypt(nonce, plaintext.encode(), None)

    wrap_nonce = os.urandom(_NONCE_SIZE)
    wrapped_key = wrap_nonce + AESGCM(_master_key()).encrypt(wrap_nonce, data_key, None)

    return EncryptedSecret(ciphertext=ciphertext, nonce=nonce, wrapped_key=wrapped_key)


def decrypt_secret(secret: EncryptedSecret) -> str:
    """Unwrap the data key with the master key, then decrypt the secret with it."""
    wrap_nonce, wrapped = secret.wrapped_key[:_NONCE_SIZE], secret.wrapped_key[_NONCE_SIZE:]
    data_key = AESGCM(_master_key()).decrypt(wrap_nonce, wrapped, None)
    return AESGCM(data_key).decrypt(secret.nonce, secret.ciphertext, None).decode()
