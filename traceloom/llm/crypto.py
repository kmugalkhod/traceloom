"""Encryption of stored secrets (LLM keys, webhook secrets) with Fernet.

`keys` is `TRACELOOM_ENCRYPTION_KEY`: one or more urlsafe-base64 Fernet keys separated by commas,
newest first. New data is encrypted with the first key; any listed key decrypts. To rotate, put
the new key in front, re-encrypt at leisure, then drop the old key.
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken, MultiFernet


class CryptoError(Exception):
    """Bad key configuration, or a ciphertext no configured key can read."""


def generate_key() -> str:
    return Fernet.generate_key().decode()


def hint(secret: str) -> str:
    """Masked display form: an ellipsis and the last four characters."""
    return "…" + secret[-4:]


class SecretBox:
    def __init__(self, keys: str) -> None:
        parts = [k.strip() for k in keys.split(",") if k.strip()]
        if not parts:
            raise CryptoError("no encryption key configured")
        try:
            self._fernet = MultiFernet([Fernet(k.encode()) for k in parts])
        except ValueError as exc:
            raise CryptoError("encryption key must be 32 urlsafe-base64 bytes") from exc

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._fernet.decrypt(ciphertext.encode()).decode()
        except InvalidToken as exc:
            raise CryptoError("cannot decrypt: wrong or missing key") from exc

    def rotate(self, ciphertext: str) -> str:
        """Re-encrypt with the newest key."""
        try:
            return self._fernet.rotate(ciphertext.encode()).decode()
        except InvalidToken as exc:
            raise CryptoError("cannot decrypt: wrong or missing key") from exc
