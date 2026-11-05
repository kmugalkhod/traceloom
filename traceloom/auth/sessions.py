"""Signed session cookie carrying the user id and the user's session_version.

Bumping `users.session_version` (password reset, "log out everywhere") invalidates every
cookie issued before it.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Response
from itsdangerous import BadData, URLSafeTimedSerializer

from traceloom.config import Settings

COOKIE_NAME = "tl_session"
_SALT = "traceloom.session.v1"


@dataclass(frozen=True)
class SessionData:
    user_id: str
    session_version: int


def _serializer(secret_key: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret_key, salt=_SALT)


def issue_session(settings: Settings, user_id: str, session_version: int) -> str:
    return _serializer(settings.secret_key).dumps([user_id, session_version])


def read_session(
    settings: Settings, token: str | None, current_version: int | None = None
) -> SessionData | None:
    """Return the session if the signature and age are valid.

    When `current_version` is given (the user's stored session_version), a cookie issued
    under any other version is rejected.
    """
    if not token:
        return None
    try:
        user_id, version = _serializer(settings.secret_key).loads(
            token, max_age=settings.session_hours * 3600
        )
    except BadData:
        return None
    if not isinstance(user_id, str) or not isinstance(version, int):
        return None
    if current_version is not None and version != current_version:
        return None
    return SessionData(user_id, version)


def set_session_cookie(response: Response, settings: Settings, token: str) -> None:
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=settings.session_hours * 3600,
        httponly=True,
        secure=settings.secure_cookies,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        COOKIE_NAME, httponly=True, secure=settings.secure_cookies, samesite="lax", path="/"
    )
