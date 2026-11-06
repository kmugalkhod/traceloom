"""Jinja environment and render helpers shared by every web router."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from traceloom.auth.permissions import Perm, Role
from traceloom.web.formatting import FILTERS

WEB_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))  # autoescape is on for .html


def _date(value: datetime | None, fmt: str = "%b %-d, %Y") -> str:
    return value.astimezone(UTC).strftime(fmt) if value else ""


def _ago(value: datetime | None, now: datetime | None = None) -> str:
    if value is None:
        return "never"
    seconds = int(((now or datetime.now(UTC)) - value.astimezone(UTC)).total_seconds())
    if seconds < 0:
        days = -seconds // 86400
        return "in " + (f"{days} day{'s' if days != 1 else ''}" if days else "under a day")
    if seconds < 60:
        return "just now"
    for size, unit in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if seconds >= size:
            n = seconds // size
            return f"{n} {unit}{'s' if n != 1 else ''} ago"
    return "just now"


templates.env.filters["date"] = _date
templates.env.filters["ago"] = _ago
templates.env.filters["ord"] = ord
templates.env.filters.update(FILTERS)
templates.env.globals["now_utc"] = lambda: datetime.now(UTC)
templates.env.globals["Perm"] = Perm
templates.env.globals["Role_ADMIN"] = Role.ADMIN


def render(
    request: Request,
    name: str,
    context: dict[str, Any] | None = None,
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
) -> HTMLResponse:
    """Render a template with the request-scoped basics (CSRF token) merged in."""
    path = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    ctx = {
        "csrf_token": getattr(request.state, "csrf_token", ""),
        "request_path": path,
        **(context or {}),
    }
    return templates.TemplateResponse(request, name, ctx, status_code=status, headers=headers)
