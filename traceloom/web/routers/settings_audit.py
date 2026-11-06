"""Settings > Audit log (Admin+): who did what in this project, newest first."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from traceloom.auth.deps import Access, require
from traceloom.auth.permissions import Perm
from traceloom.db.models import AuditLog, User
from traceloom.db.session import get_db
from traceloom.web.context import page_context, settings_tabs
from traceloom.web.templating import render

router = APIRouter(tags=["audit-log"])

PAGE_SIZE = 50


def _summary(detail: dict) -> str:
    """A short, readable line for the detail column (never secrets: audit rows hold none)."""
    parts = []
    if "count" in detail:
        parts.append(f"{detail['count']} trace{'s' if detail['count'] != 1 else ''}")
    if detail.get("filter"):
        parts.append("filter " + json.dumps(detail["filter"], sort_keys=True, default=str))
    if detail.get("trace_ids"):
        shown = detail["trace_ids"][:3]
        more = len(detail["trace_ids"]) - len(shown)
        parts.append(", ".join(shown) + (f" +{more} more" if more > 0 else ""))
    for key, value in detail.items():
        if key not in ("count", "filter", "trace_ids", "truncated", "query_string"):
            parts.append(f"{key}: {value}")
    return "; ".join(parts)


@router.get("/project/{project_id}/settings/audit", response_class=HTMLResponse)
def audit_page(
    request: Request,
    page: int = 1,
    access: Access = Depends(require(Perm.PROJECT_SETTINGS)),
    db: Session = Depends(get_db),
):
    page = max(1, page)
    scope = or_(
        AuditLog.project_id == access.project.id,
        and_(AuditLog.project_id.is_(None), AuditLog.org_id == access.org.id),
    )
    rows = db.execute(
        select(AuditLog, User)
        .outerjoin(User, User.id == AuditLog.actor_id)
        .where(scope)
        .order_by(AuditLog.created_at.desc(), AuditLog.id)
        .offset((page - 1) * PAGE_SIZE)
        .limit(PAGE_SIZE + 1)
    ).all()
    entries = [
        {
            "when": log.created_at,
            "actor": (user.name or user.email)
            if user
            else (log.actor_type if not log.actor_id else log.actor_id),
            "action": log.action,
            "target": log.target,
            "summary": _summary(log.detail or {}),
        }
        for log, user in rows[:PAGE_SIZE]
    ]
    base = f"/project/{access.project.id}/settings/audit"
    context = page_context(
        db,
        access,
        settings_tabs=settings_tabs(access.project.id, "audit", access.role),
        entries=entries,
        prev_href=f"{base}?page={page - 1}" if page > 1 else None,
        next_href=f"{base}?page={page + 1}" if len(rows) > PAGE_SIZE else None,
        page=page,
    )
    return render(request, "settings/audit.html", context)
