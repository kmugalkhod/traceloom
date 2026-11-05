from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

MIN_LENGTH = 10
MAX_LENGTH = 1024  # bound the hashing work an attacker can request

_hasher = PasswordHasher()


class WeakPassword(ValueError):
    pass


def check_strength(password: str) -> None:
    if len(password) < MIN_LENGTH:
        raise WeakPassword(f"Password must be at least {MIN_LENGTH} characters.")
    if len(password) > MAX_LENGTH:
        raise WeakPassword(f"Password must be at most {MAX_LENGTH} characters.")


def hash_password(password: str) -> str:
    check_strength(password)
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    if len(password) > MAX_LENGTH:
        return False
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)
