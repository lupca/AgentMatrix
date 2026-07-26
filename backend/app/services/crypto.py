"""Encryption helpers for secrets stored in the application database."""

import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken


_DEVELOPMENT_KEY = b"control-tower-development-key"


def _fernet() -> Fernet:
    """Build a Fernet instance from ``ENCRYPTION_KEY``.

    ``ENCRYPTION_KEY`` may be either a Fernet key or an arbitrary secret. The
    latter is deterministically converted to a Fernet key so local development
    and test environments can use a simple value. Production deployments
    should provide a stable, randomly generated secret through the environment.
    """

    configured_key = os.getenv("ENCRYPTION_KEY")
    key_material = configured_key.encode() if configured_key else _DEVELOPMENT_KEY

    try:
        return Fernet(key_material)
    except (TypeError, ValueError):
        derived_key = base64.urlsafe_b64encode(hashlib.sha256(key_material).digest())
        return Fernet(derived_key)


def encrypt_api_key(key: str) -> str:
    """Encrypt an API key before it is persisted."""

    if not key or not key.strip():
        raise ValueError("API key cannot be empty")
    return _fernet().encrypt(key.encode()).decode()


def decrypt_api_key(encrypted: str) -> str:
    """Decrypt a persisted API key.

    The original cryptography exception is intentionally hidden so callers do
    not receive implementation details or key material in an error response.
    """

    if not encrypted:
        raise ValueError("Encrypted API key cannot be empty")
    try:
        return _fernet().decrypt(encrypted.encode()).decode()
    except (InvalidToken, UnicodeDecodeError, ValueError, TypeError) as exc:
        raise ValueError("Unable to decrypt API key") from exc
