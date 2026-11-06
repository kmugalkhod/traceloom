"""HTML error pages. 403 and 404 render inside the app shell when the visitor is signed in."""

from __future__ import annotations

import logging
from html import escape

from fastapi import Request
from fastapi.responses import HTMLResponse

from traceloom.auth.sessions import SessionData
from traceloom.db.models import User
from traceloom.web.context import build_shell
from traceloom.web.templating import render

log = logging.getLogger("traceloom.web")

TEMPLATES = {403: "errors/403.html", 404: "errors/404.html"}


def error_page(
    request: Request, status: int, message: str, headers: dict | None = None
) -> HTMLResponse:
    template = TEMPLATES.get(status)
    if template is None:
        page = f"<!doctype html><title>{status}</title><h1>{status}</h1><p>{escape(message)}</p>"
        return HTMLResponse(page, status_code=status, headers=headers)
    context: dict = {"message": message, "layout": "base_auth.html"}
    try:
        container = request.app.state.container
        session: SessionData | None = getattr(request.state, "session", None)
        with container.session_factory() as db:
            user = db.get(User, session.user_id) if session else None
            if user is not None and user.session_version == session.session_version:
                shell = build_shell(
                    db,
                    user,
                    org_id=request.path_params.get("org_id"),
                    project_id=request.path_params.get("project_id"),
                )
                context.update(shell.context(), layout="base.html")
    except Exception:  # an error page must never fail on its own
        log.exception("could not build the shell for an error page")
        context["layout"] = "base_auth.html"
    return render(request, template, context, status=status, headers=headers)
