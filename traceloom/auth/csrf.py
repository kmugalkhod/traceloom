"""CSRF protection: signed double-submit token.

The token lives in the `tl_csrf` cookie (readable by app.js so HTMX can echo it in the
`X-CSRF-Token` header) and must be echoed in that header or a `csrf_token` form field on every
unsafe request. Requests that authenticate with HTTP Basic (the public ingestion API) are
exempt: they carry no ambient cookie credentials.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from http.cookies import SimpleCookie
from urllib.parse import parse_qs

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from traceloom.config import Settings

COOKIE_NAME = "tl_csrf"
HEADER_NAME = "x-csrf-token"
FORM_FIELD = "csrf_token"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}
MAX_FORM_BYTES = 1_000_000


def _sign(secret_key: str, nonce: str) -> str:
    return hmac.new(secret_key.encode(), f"csrf:{nonce}".encode(), hashlib.sha256).hexdigest()


def issue_token(secret_key: str) -> str:
    nonce = secrets.token_urlsafe(16)
    return f"{nonce}.{_sign(secret_key, nonce)}"


def verify_token(secret_key: str, token: str | None) -> bool:
    if not token or "." not in token:
        return False
    nonce, _, sig = token.partition(".")
    return hmac.compare_digest(sig, _sign(secret_key, nonce))


def tokens_match(secret_key: str, cookie_token: str | None, submitted: str | None) -> bool:
    return (
        bool(cookie_token)
        and bool(submitted)
        and hmac.compare_digest(cookie_token, submitted)
        and verify_token(secret_key, cookie_token)
    )


class CSRFMiddleware:
    """Rejects unsafe requests without a matching token; issues the cookie when missing."""

    def __init__(
        self, app: ASGIApp, settings: Settings, exempt_prefixes: tuple[str, ...] = ("/api/public/",)
    ) -> None:
        self.app = app
        self.settings = settings
        self.exempt_prefixes = exempt_prefixes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {k.decode().lower(): v.decode("latin-1") for k, v in scope["headers"]}
        cookies = SimpleCookie(headers.get("cookie", ""))
        cookie_token = cookies[COOKIE_NAME].value if COOKIE_NAME in cookies else None
        valid_cookie = verify_token(self.settings.secret_key, cookie_token)
        token = cookie_token if valid_cookie else issue_token(self.settings.secret_key)
        scope.setdefault("state", {})["csrf_token"] = token

        if scope["method"] not in SAFE_METHODS and not self._exempt(scope, headers):
            submitted = headers.get(HEADER_NAME)
            if submitted is None:
                receive, submitted = await self._form_token(headers, receive)
            if not tokens_match(self.settings.secret_key, cookie_token, submitted):
                await self._reject(send)
                return

        async def send_with_cookie(message: Message) -> None:
            if message["type"] == "http.response.start" and not valid_cookie:
                cookie = SimpleCookie()
                cookie[COOKIE_NAME] = token
                morsel = cookie[COOKIE_NAME]
                morsel["path"] = "/"
                morsel["samesite"] = "Lax"
                if self.settings.secure_cookies:
                    morsel["secure"] = True
                message = {
                    **message,
                    "headers": [
                        *message["headers"],
                        (b"set-cookie", morsel.OutputString().encode("latin-1")),
                    ],
                }
            await send(message)

        await self.app(scope, receive, send_with_cookie)

    def _exempt(self, scope: Scope, headers: dict[str, str]) -> bool:
        if scope["path"].startswith(self.exempt_prefixes):
            return True
        return headers.get("authorization", "").lower().startswith("basic ")

    @staticmethod
    async def _form_token(headers: dict[str, str], receive: Receive):
        """Read `csrf_token` from a urlencoded body and replay the body to the app."""
        if not headers.get("content-type", "").startswith("application/x-www-form-urlencoded"):
            return receive, None
        chunks: list[Message] = []
        body = b""
        while True:
            message = await receive()
            chunks.append(message)
            if message["type"] != "http.request":
                break
            body += message.get("body", b"")
            if len(body) > MAX_FORM_BYTES or not message.get("more_body"):
                break
        values = parse_qs(body.decode("utf-8", "replace")).get(FORM_FIELD)

        async def replay() -> Message:
            return chunks.pop(0) if chunks else await receive()

        return replay, (values[0] if values else None)

    @staticmethod
    async def _reject(send: Send) -> None:
        body = b'{"error":{"code":"csrf_failed","message":"Missing or invalid CSRF token."}}'
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
