"""Settings > Webhooks: endpoints, a delivery log and Send test (Admin+)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from traceloom.auth.deps import Access, form_data, get_container, require
from traceloom.auth.permissions import Perm
from traceloom.container import Container
from traceloom.db.models import Webhook, WebhookDelivery
from traceloom.db.session import get_db
from traceloom.prompts.service import Actor, NotAllowed
from traceloom.web.context import page_context, settings_tabs
from traceloom.web.templating import render
from traceloom.webhooks import deliver, events
from traceloom.webhooks import service as webhooks

router = APIRouter(tags=["webhooks"])

EVENT_LABELS = {
    events.LABEL_MOVED: "Label moved",
    events.VERSION_CREATED: "Version created",
    webhooks.TEST_EVENT: "Test",
}
NOTES = {
    "tested": "Test delivery queued. It appears in Recent deliveries below.",
    "saved": "Webhook saved.",
    "deleted": "Webhook deleted.",
}


def _actor(access: Access) -> Actor:
    return Actor.for_user(access.user, access.role)


def _policy(container: Container) -> webhooks.WebhookPolicy:
    return webhooks.WebhookPolicy.from_settings(container.settings)


def _status(hook: Webhook, last: WebhookDelivery | None) -> dict[str, str]:
    """The pill for a webhook row: its latest delivery, or Disabled."""
    if not hook.enabled:
        return {"css": "", "text": "Disabled"}
    if last is None:
        return {"css": "", "text": "No deliveries yet"}
    return delivery_status(last)


def delivery_status(d: WebhookDelivery) -> dict[str, str]:
    if d.status == "succeeded":
        return {"css": "ok", "icon": "i-check", "text": "Delivered"}
    if d.status == "failed":
        return {"css": "ERROR", "icon": "i-error", "text": "Failed"}
    if d.attempts:
        return {
            "css": "WARNING",
            "icon": "i-warn",
            "text": f"Retrying {d.attempts} of {deliver.MAX_ATTEMPTS}",
        }
    return {"css": "", "icon": "i-clock", "text": "Queued"}


def _detail(d: WebhookDelivery) -> str:
    attempts = f"{d.attempts} attempt{'' if d.attempts == 1 else 's'}"
    if d.status == "succeeded":
        return f"{d.last_status_code or ''} · {attempts}".strip(" ·")
    if d.status == "failed":
        return f"{d.last_error or d.last_status_code or 'Failed'} · {attempts}"
    if d.attempts and d.next_attempt_at:
        seconds = max(0, int((d.next_attempt_at - datetime.now(UTC)).total_seconds()))
        code = f"{d.last_status_code} · " if d.last_status_code else ""
        return f"{code}next try in {seconds} s"
    return "Waiting to be sent"


def _page(
    request: Request,
    db: Session,
    access: Access,
    *,
    created: tuple[Webhook, str] | None = None,
    error: str = "",
    form: dict[str, Any] | None = None,
    edit_error: tuple[str, str] | None = None,
    notice: str = "",
    status: int = 200,
):
    project, actor = access.project, _actor(access)
    hooks = webhooks.list_webhooks(db, project, actor)
    recent = webhooks.recent_deliveries(db, project, actor)
    last = {}
    for d in recent:  # newest first, so the first seen per webhook is its latest
        last.setdefault(d.webhook_id, d)
    by_id = {h.id: h for h in hooks}
    rows = [
        {
            "hook": h,
            "status": _status(h, last.get(h.id)),
            "events": ", ".join(EVENT_LABELS.get(e, e) for e in h.events),
        }
        for h in hooks
    ]
    deliveries = [
        {
            "d": d,
            "host": _host(by_id[d.webhook_id].url) if d.webhook_id in by_id else "",
            "event": EVENT_LABELS.get(d.event, d.event),
            "status": delivery_status(d),
            "detail": _detail(d),
        }
        for d in recent
    ]
    context = page_context(
        db,
        access,
        settings_tabs=settings_tabs(project.id, "webhooks", access.role),
        rows=rows,
        deliveries=deliveries,
        all_events=[(e, EVENT_LABELS.get(e, e)) for e in events.EVENTS],
        created=created,
        error=error,
        form=form or {"url": "", "events": list(events.EVENTS), "enabled": True},
        edit_error=edit_error,
        notice=NOTES.get(notice, ""),
    )
    # A page that shows a secret must not be cached or kept in history snapshots.
    headers = {"Cache-Control": "no-store"} if created else None
    return render(request, "settings/webhooks.html", context, status=status, headers=headers)


def _host(url: str) -> str:
    return urlsplit(url).hostname or url


@router.get("/project/{project_id}/settings/webhooks", response_class=HTMLResponse)
def webhooks_page(
    request: Request,
    notice: str = "",
    access: Access = Depends(require(Perm.WEBHOOKS)),
    db: Session = Depends(get_db),
):
    return _page(request, db, access, notice=notice)


def _fields(form: FormData) -> dict[str, Any]:
    return {
        "url": str(form.get("url", "")).strip(),
        "events": [str(e) for e in form.getlist("events")],
        "enabled": form.get("enabled") is not None,
    }


@router.post("/project/{project_id}/settings/webhooks", response_class=HTMLResponse)
def create_webhook(
    request: Request,
    access: Access = Depends(require(Perm.WEBHOOKS)),
    container: Container = Depends(get_container),
    form: FormData = Depends(form_data),
    db: Session = Depends(get_db),
):
    values = _fields(form)
    try:
        hook, secret = webhooks.create(
            db,
            access.project,
            _actor(access),
            _policy(container),
            url=values["url"],
            event_names=values["events"],
            enabled=values["enabled"],
        )
    except webhooks.WebhookError as exc:
        db.rollback()
        return _page(request, db, access, error=str(exc), form=values, status=400)
    except NotAllowed as exc:
        raise HTTPException(403, str(exc)) from exc
    db.commit()
    return _page(request, db, access, created=(hook, secret), status=201)


@router.post("/project/{project_id}/settings/webhooks/{webhook_id}", response_class=HTMLResponse)
def update_webhook(
    request: Request,
    webhook_id: str,
    access: Access = Depends(require(Perm.WEBHOOKS)),
    container: Container = Depends(get_container),
    form: FormData = Depends(form_data),
    db: Session = Depends(get_db),
):
    values = _fields(form)
    try:
        webhooks.update(
            db,
            access.project,
            _actor(access),
            _policy(container),
            webhook_id,
            url=values["url"],
            event_names=values["events"],
            enabled=values["enabled"],
        )
    except webhooks.WebhookNotFound as exc:
        raise HTTPException(404, "Not found.") from exc
    except webhooks.WebhookError as exc:
        db.rollback()
        return _page(request, db, access, edit_error=(webhook_id, str(exc)), status=400)
    db.commit()
    return RedirectResponse(
        f"/project/{access.project.id}/settings/webhooks?notice=saved", status_code=303
    )


@router.post("/project/{project_id}/settings/webhooks/{webhook_id}/toggle")
def toggle_webhook(
    webhook_id: str,
    access: Access = Depends(require(Perm.WEBHOOKS)),
    container: Container = Depends(get_container),
    db: Session = Depends(get_db),
):
    try:
        hook = webhooks.get(db, access.project, webhook_id)
        webhooks.update(
            db,
            access.project,
            _actor(access),
            _policy(container),
            webhook_id,
            enabled=not hook.enabled,
        )
    except webhooks.WebhookNotFound as exc:
        raise HTTPException(404, "Not found.") from exc
    db.commit()
    return RedirectResponse(
        f"/project/{access.project.id}/settings/webhooks?notice=saved", status_code=303
    )


@router.post("/project/{project_id}/settings/webhooks/{webhook_id}/test")
def test_webhook(
    webhook_id: str,
    access: Access = Depends(require(Perm.WEBHOOKS)),
    container: Container = Depends(get_container),
    db: Session = Depends(get_db),
):
    try:
        webhooks.send_test(db, access.project, _actor(access), webhook_id)
    except webhooks.WebhookNotFound as exc:
        raise HTTPException(404, "Not found.") from exc
    db.commit()
    deliver.kick(container)  # send it now, not at the next sweep
    return RedirectResponse(
        f"/project/{access.project.id}/settings/webhooks?notice=tested", status_code=303
    )


@router.post("/project/{project_id}/settings/webhooks/{webhook_id}/delete")
def delete_webhook(
    webhook_id: str,
    access: Access = Depends(require(Perm.WEBHOOKS)),
    db: Session = Depends(get_db),
):
    try:
        webhooks.delete(db, access.project, _actor(access), webhook_id)
    except webhooks.WebhookNotFound as exc:
        raise HTTPException(404, "Not found.") from exc
    db.commit()
    return RedirectResponse(
        f"/project/{access.project.id}/settings/webhooks?notice=deleted", status_code=303
    )
