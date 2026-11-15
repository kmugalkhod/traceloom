"""Settings > Evaluation: the judge daily cost cap and the public feedback origins (Admin edit)."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from traceloom.auth.deps import Access, form_data, get_container, require
from traceloom.auth.permissions import Perm, can
from traceloom.container import Container
from traceloom.db.models import ApiKey
from traceloom.db.session import get_db
from traceloom.evals import spend
from traceloom.scores import feedback
from traceloom.services import audit
from traceloom.web.context import page_context, settings_tabs
from traceloom.web.templating import render

router = APIRouter(tags=["evaluation-settings"])

NOTES = {
    "cap": "Daily cost cap saved.",
    "origin_added": "Origin added.",
    "origin_removed": "Origin removed.",
}
PLACEHOLDER_KEY = "YOUR_PUBLIC_KEY"


def _snippet(host: str, public_key: str) -> str:
    return (
        f'<script src="{host}/static/feedback.js" defer></script>\n'
        '<button data-traceloom-feedback="thumbs" data-trace-id="TRACE_ID" data-value="true">'
        "\U0001f44d</button>\n"
        '<button data-traceloom-feedback="thumbs" data-trace-id="TRACE_ID" data-value="false">'
        "\U0001f44e</button>\n"
        f'<script>Traceloom.init({{ publicKey: "{public_key}" }})</script>'
    )


def _hours_until_reset(now: datetime) -> int:
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, math.ceil((midnight - now).total_seconds() / 3600))


def _page(
    request: Request,
    db: Session,
    access: Access,
    container: Container,
    *,
    notice: str = "",
    cap_error: str = "",
    cap_value: str | None = None,
    origin_error: str = "",
    origin_value: str = "",
    status: int = 200,
):
    project_id = access.project.id
    now = datetime.now(UTC)
    state = spend.status(db, project_id, container.settings, now)
    used = state.spent + state.reserved
    percent = 100 if state.paused else min(100, round(used / state.cap * 100)) if state.cap else 0
    key = db.scalar(
        select(ApiKey)
        .where(ApiKey.project_id == project_id, ApiKey.revoked_at.is_(None))
        .order_by(ApiKey.created_at.desc())
    )
    host = str(request.base_url).rstrip("/")
    public_key = key.public_key if key else PLACEHOLDER_KEY
    context = page_context(
        db,
        access,
        settings_tabs=settings_tabs(project_id, "evaluation", access.role),
        can_edit=can(access.role, Perm.EVAL_SETTINGS),
        spent=state.spent,
        cap=state.cap,
        remaining=max(0.0, state.cap - used),
        percent=percent,
        paused=state.paused,
        reset_hours=_hours_until_reset(now),
        default_cap=container.settings.eval_daily_cost_cap_usd,
        cap_value=f"{state.cap:.2f}" if cap_value is None else cap_value,
        cap_error=cap_error,
        origins=feedback.feedback_origins(db, project_id),
        origin_error=origin_error,
        origin_value=origin_value,
        snippet=_snippet(host, public_key),
        has_key=key is not None,
        feedback_rate=container.settings.rate_feedback,
        script_kb=_script_kb(),
        notice=NOTES.get(notice, ""),
        base=f"/project/{project_id}/settings/evaluation",
    )
    return render(request, "settings/evaluation.html", context, status=status)


def _script_kb() -> str:
    from pathlib import Path

    size = (Path(__file__).resolve().parents[1] / "static" / "feedback.js").stat().st_size
    return f"{size / 1024:.1f}"


@router.get("/project/{project_id}/settings/evaluation", response_class=HTMLResponse)
def evaluation_page(
    request: Request,
    notice: str = "",
    access: Access = Depends(require(Perm.EVAL_READ)),
    container: Container = Depends(get_container),
    db: Session = Depends(get_db),
):
    return _page(request, db, access, container, notice=notice)


def _record(db: Session, access: Access, action: str, **detail: Any) -> None:
    audit.record(
        db,
        action=action,
        actor_type="user",
        actor_id=access.user.id,
        org_id=access.org.id,
        project_id=access.project.id,
        target=access.project.id,
        detail=detail,
    )


def _redirect(access: Access, notice: str) -> RedirectResponse:
    return RedirectResponse(
        f"/project/{access.project.id}/settings/evaluation?notice={notice}", 303
    )


@router.post("/project/{project_id}/settings/evaluation/cap", response_class=HTMLResponse)
def save_cap(
    request: Request,
    access: Access = Depends(require(Perm.EVAL_SETTINGS)),
    container: Container = Depends(get_container),
    form: FormData = Depends(form_data),
    db: Session = Depends(get_db),
):
    text = str(form.get("cap", "")).strip().lstrip("$")
    try:
        new = None if not text else float(text)
        before = spend.get_cap(db, access.project.id, container.settings)
        spend.set_cap(db, access.project.id, new)
    except ValueError:
        db.rollback()
        message = "Enter the cap in USD, zero or more."
        return _page(request, db, access, container, cap_error=message, cap_value=text, status=400)
    after = spend.get_cap(db, access.project.id, container.settings)
    _record(db, access, "eval.cap.change", before=before, after=after, default=new is None)
    db.commit()
    return _redirect(access, "cap")


@router.post("/project/{project_id}/settings/evaluation/origins", response_class=HTMLResponse)
def add_origin(
    request: Request,
    access: Access = Depends(require(Perm.EVAL_SETTINGS)),
    container: Container = Depends(get_container),
    form: FormData = Depends(form_data),
    db: Session = Depends(get_db),
):
    text = str(form.get("origin", ""))
    current = feedback.feedback_origins(db, access.project.id)
    try:
        updated = feedback.set_feedback_origins(db, access.project.id, [*current, text])
    except feedback.FeedbackError as exc:
        db.rollback()
        return _page(
            request, db, access, container, origin_error=_origin_message(str(exc)),
            origin_value=text, status=400,
        )  # fmt: skip
    _record(db, access, "feedback.origins.change", added=sorted(set(updated) - set(current)))
    db.commit()
    return _redirect(access, "origin_added")


def _origin_message(raw: str) -> str:
    return (
        "Use an origin only: scheme and host, like https://app.example. No path."
        if "not an origin" in raw or "valid origin" in raw
        else raw
    )


@router.post("/project/{project_id}/settings/evaluation/origins/remove")
def remove_origin(
    access: Access = Depends(require(Perm.EVAL_SETTINGS)),
    form: FormData = Depends(form_data),
    db: Session = Depends(get_db),
):
    origin = str(form.get("origin", ""))
    current = feedback.feedback_origins(db, access.project.id)
    if origin not in current:
        raise HTTPException(404, "Not found.")
    feedback.set_feedback_origins(db, access.project.id, [o for o in current if o != origin])
    _record(db, access, "feedback.origins.change", removed=[origin])
    db.commit()
    return _redirect(access, "origin_removed")
