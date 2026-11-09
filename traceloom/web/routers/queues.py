"""Evaluation > Annotation queues: the list, queue detail, create, edit, delete and reopen."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from traceloom.annotation import claim, queues
from traceloom.annotation.queues import QueueError, QueueNotFound, QueueStats
from traceloom.auth.deps import Access, form_data, get_container, require
from traceloom.auth.permissions import Perm
from traceloom.container import Container
from traceloom.db.models import User
from traceloom.db.session import get_db
from traceloom.prompts.service import Actor, NotAllowed
from traceloom.scores.configs import list_configs
from traceloom.web.context import page_context
from traceloom.web.templating import render

router = APIRouter(tags=["queues"])

PAGE = 50
NOTES = {
    "created": "Queue created. Add traces from a list, then start reviewing.",
    "saved": "Queue saved.",
    "deleted": "Queue deleted. The scores already written stay on their traces.",
    "reopened": "Item reopened.",
}
STATUS_LABELS = {"pending": "Pending", "completed": "Completed", "skipped": "Skipped"}


def actor_of(access: Access) -> Actor:
    return Actor.for_user(access.user, access.role)


def base(access: Access) -> str:
    return f"/project/{access.project.id}/queues"


def segments(stats: QueueStats) -> dict[str, float]:
    """Widths (percent) of the completed and skipped parts of a progress bar."""
    if not stats.total:
        return {"d": 0.0, "s": 0.0}
    return {"d": 100 * stats.completed / stats.total, "s": 100 * stats.skipped / stats.total}


def _configs_for_form(db: Session, access: Access, selected: list[str]) -> list[dict[str, Any]]:
    return [
        {
            "id": c.id,
            "name": c.name,
            "type": c.data_type.lower(),
            "checked": c.id in selected,
        }
        for c in list_configs(db, access.project.id)
    ]


def _shell(db: Session, access: Access, **extra: Any) -> dict[str, Any]:
    return page_context(
        db,
        access,
        active="queues",
        can_write=Actor.can(actor_of(access), Perm.QUEUES_WRITE),
        can_delete=Actor.can(actor_of(access), Perm.QUEUES_DELETE),
        **extra,
    )


# --- List --------------------------------------------------------------------------------------


def _list_page(
    request: Request,
    db: Session,
    access: Access,
    *,
    error: str = "",
    form: dict[str, Any] | None = None,
    notice: str = "",
    status: int = 200,
):
    actor = actor_of(access)
    rows = []
    for queue, stats in queues.list_queues(db, access.project, actor):
        activity = queues.last_activity(db, queue.id)
        rows.append(
            {
                "queue": queue,
                "stats": stats,
                "bar": segments(stats),
                "configs": queues.queue_configs(db, access.project, queue),
                "activity": activity,
            }
        )
    form = form or {"name": "", "description": "", "config_ids": []}
    context = _shell(
        db,
        access,
        rows=rows,
        notice=NOTES.get(notice, ""),
        error=error,
        form=form,
        form_configs=_configs_for_form(db, access, form["config_ids"]),
    )
    return render(request, "queues/list.html", context, status=status)


@router.get("/project/{project_id}/queues", response_class=HTMLResponse)
def list_page(
    request: Request,
    notice: str = "",
    access: Access = Depends(require(Perm.EVAL_READ)),
    db: Session = Depends(get_db),
):
    return _list_page(request, db, access, notice=notice)


def _fields(form: FormData) -> dict[str, Any]:
    return {
        "name": str(form.get("name", "")),
        "description": str(form.get("description", "")),
        "config_ids": [str(c) for c in form.getlist("config_id")],
    }


@router.post("/project/{project_id}/queues", response_class=HTMLResponse)
def create_queue(
    request: Request,
    access: Access = Depends(require(Perm.QUEUES_WRITE)),
    form: FormData = Depends(form_data),
    db: Session = Depends(get_db),
):
    values = _fields(form)
    try:
        queue = queues.create_queue(
            db,
            access.project,
            actor_of(access),
            values["name"],
            values["description"],
            values["config_ids"],
        )
    except QueueError as exc:
        db.rollback()
        return _list_page(request, db, access, error=str(exc), form=values, status=400)
    db.commit()
    return RedirectResponse(f"{base(access)}/{queue.id}?notice=created", status_code=303)


# --- Detail ------------------------------------------------------------------------------------


def _item_labels(container: Container, project_id: str, items) -> dict[str, tuple[str, str]]:
    """item id -> (title, small text) from the trace store; the id when the object is gone."""
    wanted: dict[str, list[str]] = {"trace": [], "observation": []}
    for item in items:
        if item.object_type in wanted:
            wanted[item.object_type].append(item.object_id)
    found = container.trace_store.get_existing(project_id, {k: v for k, v in wanted.items() if v})
    out = {}
    for item in items:
        if item.object_type == "session":
            out[item.id] = (f"Session {item.object_id}", "")
            continue
        row = found.get(item.object_type, {}).get(item.object_id)
        out[item.id] = ((row.name if row and row.name else item.object_id), item.object_id)
    return out


def _detail_page(
    request: Request,
    db: Session,
    access: Access,
    container: Container,
    queue_id: str,
    *,
    status_filter: str = "",
    reviewer: str = "",
    page: int = 1,
    notice: str = "",
    error: str = "",
    banner: str = "",
    form: dict[str, Any] | None = None,
    http_status: int = 200,
):
    actor = actor_of(access)
    queue = queues.get_queue(db, access.project, actor, queue_id)
    stats = queues.stats(db, access.project, actor, queue_id)
    try:
        items, total = queues.list_items(
            db,
            access.project,
            actor,
            queue_id,
            status=status_filter or None,
            reviewer=reviewer or None,
            limit=PAGE,
            offset=(max(page, 1) - 1) * PAGE,
        )
    except QueueError:
        items, total = [], 0
    holder_ids = {i.locked_by for i in items if i.locked_by} | {
        i.completed_by for i in items if i.completed_by
    }
    names = {
        u.id: (u.name or u.email)
        for u in db.query(User).filter(User.id.in_(list(holder_ids) or [""]))
    }
    labels = _item_labels(container, access.project.id, items)
    form = form or {
        "name": queue.name,
        "description": queue.description,
        "config_ids": list(queue.config_ids or []),
    }
    reviewers = [(r.user_id, r.name) for r in stats.reviewers]
    context = _shell(
        db,
        access,
        queue=queue,
        stats=stats,
        bar=segments(stats),
        configs=queues.queue_configs(db, access.project, queue),
        items=[
            {
                "item": i,
                "title": labels[i.id][0],
                "small": labels[i.id][1],
                "who": names.get(i.completed_by or i.locked_by or "", ""),
            }
            for i in items
        ],
        total=total,
        page=page,
        pages=max(1, -(-total // PAGE)),
        status_filter=status_filter,
        reviewer=reviewer,
        reviewers=reviewers,
        status_labels=STATUS_LABELS,
        notice=NOTES.get(notice, ""),
        error=error,
        banner=banner,
        now_utc=datetime.now(UTC),
        form=form,
        form_configs=_configs_for_form(db, access, form["config_ids"])
        + [
            {
                "id": c.id,
                "name": c.name,
                "type": c.data_type.lower(),
                "checked": True,
                "archived": True,
            }
            for c in queues.queue_configs(db, access.project, queue)
            if c.archived_at is not None
        ],
    )
    return render(request, "queues/detail.html", context, status=http_status)


@router.get("/project/{project_id}/queues/{queue_id}", response_class=HTMLResponse)
def detail_page(
    request: Request,
    queue_id: str,
    status: str = "",
    reviewer: str = "",
    page: int = 1,
    notice: str = "",
    error: str = "",
    access: Access = Depends(require(Perm.EVAL_READ)),
    container: Container = Depends(get_container),
    db: Session = Depends(get_db),
):
    try:
        return _detail_page(
            request,
            db,
            access,
            container,
            queue_id,
            status_filter=status,
            reviewer=reviewer,
            page=page,
            notice=notice,
            banner=error[:300],
        )
    except QueueNotFound as exc:
        raise HTTPException(404, "Not found.") from exc


@router.post("/project/{project_id}/queues/{queue_id}/edit", response_class=HTMLResponse)
def edit_queue(
    request: Request,
    queue_id: str,
    access: Access = Depends(require(Perm.QUEUES_WRITE)),
    container: Container = Depends(get_container),
    form: FormData = Depends(form_data),
    db: Session = Depends(get_db),
):
    values = _fields(form)
    try:
        queues.update_queue(
            db,
            access.project,
            actor_of(access),
            queue_id,
            values["name"],
            values["description"],
            values["config_ids"],
        )
    except QueueNotFound as exc:
        raise HTTPException(404, "Not found.") from exc
    except QueueError as exc:
        db.rollback()
        return _detail_page(
            request, db, access, container, queue_id, error=str(exc), form=values, http_status=400
        )
    db.commit()
    return RedirectResponse(f"{base(access)}/{queue_id}?notice=saved", status_code=303)


@router.post("/project/{project_id}/queues/{queue_id}/delete")
def delete_queue(
    queue_id: str,
    access: Access = Depends(require(Perm.QUEUES_DELETE)),
    db: Session = Depends(get_db),
):
    try:
        queues.delete_queue(db, access.project, actor_of(access), queue_id)
    except QueueNotFound as exc:
        raise HTTPException(404, "Not found.") from exc
    except NotAllowed as exc:
        raise HTTPException(403, str(exc)) from exc
    db.commit()
    return RedirectResponse(f"{base(access)}?notice=deleted", status_code=303)


@router.post("/project/{project_id}/queues/{queue_id}/items/{item_id}/reopen")
def reopen_item(
    queue_id: str,
    item_id: str,
    access: Access = Depends(require(Perm.QUEUES_WRITE)),
    db: Session = Depends(get_db),
):
    try:
        claim.reopen(db, access.project, actor_of(access), queue_id, item_id)
    except QueueNotFound as exc:
        raise HTTPException(404, "Not found.") from exc
    except claim.ClaimError as exc:
        db.rollback()
        return RedirectResponse(
            f"{base(access)}/{queue_id}?error={quote(str(exc))}", status_code=303
        )
    db.commit()
    return RedirectResponse(f"{base(access)}/{queue_id}?notice=reopened", status_code=303)
