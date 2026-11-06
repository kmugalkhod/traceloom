"""Accounts: sign-up, authentication and password reset (FR-A1, FR-A2)."""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from email_validator import EmailNotValidError, validate_email
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from traceloom.auth.passwords import (
    MIN_LENGTH,
    WeakPassword,
    check_strength,
    hash_password,
    needs_rehash,
    verify_password,
)
from traceloom.config import Settings
from traceloom.db.models import Invite, PasswordReset, User
from traceloom.services.mail import Mailer


class AccountError(ValueError):
    """A problem the user can fix; `field` says which input it belongs to."""

    def __init__(self, message: str, field: str = "") -> None:
        super().__init__(message)
        self.field = field


class SignupClosed(AccountError):
    pass


class ResetInvalid(AccountError):
    pass


_dummy_hash: str | None = None


def normalize_email(email: str) -> str:
    try:
        return validate_email(email.strip(), check_deliverability=False).normalized.lower()
    except EmailNotValidError as exc:
        raise AccountError("Enter a valid email address.", "email") from exc


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def password_error(password: str) -> str | None:
    try:
        check_strength(password)
    except WeakPassword:
        if len(password) < MIN_LENGTH:
            return f"Use at least {MIN_LENGTH} characters. You entered {len(password)}."
        return "That password is too long."
    return None


def has_pending_invite(db: Session, email: str, now: datetime | None = None) -> bool:
    now = now or datetime.now(UTC)
    return (
        db.scalar(
            select(Invite.id).where(
                Invite.email == email, Invite.accepted_at.is_(None), Invite.expires_at > now
            )
        )
        is not None
    )


def register(
    db: Session, settings: Settings, *, email: str, name: str, password: str, invited: bool = False
) -> User:
    """Create an account. With TRACELOOM_SIGNUP_ENABLED=false only invited emails get one.

    `invited` means the caller already verified an invite token for this email.
    """
    email = normalize_email(email)
    if not settings.signup_enabled and not invited and not has_pending_invite(db, email):
        raise SignupClosed(
            "Sign-up is invite-only on this server. Use the address your invite was sent to.",
            "email",
        )
    if error := password_error(password):
        raise AccountError(error, "password")
    user = User(email=email, name=name.strip()[:200], password_hash=hash_password(password))
    db.add(user)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise AccountError(
            "An account with this email already exists. Log in instead.", "email"
        ) from exc
    return user


def authenticate(db: Session, email: str, password: str) -> User | None:
    """The user for these credentials, or None. Unknown emails cost the same hashing time."""
    global _dummy_hash
    try:
        email = normalize_email(email)
    except AccountError:
        email = ""
    user = db.scalar(select(User).where(User.email == email)) if email else None
    if user is None:
        _dummy_hash = _dummy_hash or hash_password("not-a-real-password")
        verify_password(_dummy_hash, password)
        return None
    if not verify_password(user.password_hash, password):
        return None
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
    return user


def request_reset(db: Session, settings: Settings, mailer: Mailer, email: str) -> None:
    """Mail a one-time link if the account exists. Callers answer identically either way."""
    try:
        email = normalize_email(email)
    except AccountError:
        return
    user = db.scalar(select(User).where(User.email == email))
    if user is None:
        return
    token = secrets.token_urlsafe(32)
    db.add(
        PasswordReset(
            token_hash=hash_token(token),
            user_id=user.id,
            expires_at=datetime.now(UTC) + timedelta(minutes=settings.reset_token_minutes),
        )
    )
    db.flush()
    mailer.send(
        user.email,
        "Reset your Traceloom password",
        f"Someone asked to reset the password for this address.\n\n"
        f"Open this link within {settings.reset_token_minutes} minutes to choose a new one:\n"
        f"{settings.base_url.rstrip('/')}/reset/{token}\n\n"
        "It works once. If you did not ask for this, ignore this message.",
    )


def reset_status(db: Session, token: str, now: datetime | None = None) -> str:
    """valid | expired | used | invalid"""
    row = db.get(PasswordReset, hash_token(token))
    if row is None:
        return "invalid"
    if row.used_at is not None:
        return "used"
    if row.expires_at <= (now or datetime.now(UTC)):
        return "expired"
    return "valid"


def reset_password(db: Session, token: str, new_password: str) -> User:
    """Set a new password with a reset token; every existing session stops working."""
    if reset_status(db, token) != "valid":
        raise ResetInvalid("This link has expired or was already used.")
    if error := password_error(new_password):
        raise AccountError(error, "password")
    row = db.get(PasswordReset, hash_token(token))
    user = db.get(User, row.user_id)
    now = datetime.now(UTC)
    user.password_hash = hash_password(new_password)
    user.session_version += 1
    db.execute(
        update(PasswordReset)
        .where(PasswordReset.user_id == user.id, PasswordReset.used_at.is_(None))
        .values(used_at=now)
    )
    db.flush()
    return user
