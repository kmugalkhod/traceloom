"""Error envelope `{"error": {"code", "message"}}` and the 401/403/404 handlers.

API clients (paths under /api/ or Accept: application/json) get the envelope. Browser pages
get a redirect to the login page on 401, and a plain HTML error page otherwise.
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    409: "conflict",
    413: "payload_too_large",
    422: "validation_error",
    429: "rate_limited",
    503: "unavailable",
}
DEFAULT_MESSAGES = {
    401: "Authentication required.",
    403: "You do not have permission to do this.",
    404: "Not found.",
}


def error_body(status: int, message: str | None = None, code: str | None = None) -> dict:
    return {
        "error": {
            "code": code or CODES.get(status, "error"),
            "message": message or DEFAULT_MESSAGES.get(status, "Request failed."),
        }
    }


def wants_json(request: Request) -> bool:
    return request.url.path.startswith("/api/") or "application/json" in request.headers.get(
        "accept", ""
    )


def _respond(request: Request, status: int, message: str, headers: dict | None = None) -> Response:
    if wants_json(request):
        return JSONResponse(error_body(status, message), status_code=status, headers=headers)
    if status == 401:
        target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        login = f"/login?next={quote(target, safe='')}"
        if request.headers.get("hx-request"):  # HTMX swaps would nest the login page
            return Response(status_code=401, headers={"HX-Redirect": login})
        return RedirectResponse(login, status_code=303)
    from traceloom.web.pages import error_page  # late import: web depends on this module

    return error_page(request, status, message, headers)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        message = exc.detail if isinstance(exc.detail, str) else None
        return _respond(
            request,
            exc.status_code,
            message or DEFAULT_MESSAGES.get(exc.status_code, "Request failed."),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> Response:
        fields = ", ".join(
            ".".join(str(p) for p in e["loc"] if p != "body") or "body" for e in exc.errors()
        )
        return _respond(request, 422, f"Invalid request: {fields}.")


__all__ = ["HTTPException", "error_body", "install_error_handlers", "wants_json"]
