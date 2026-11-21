"""Add to annotation queue: from a trace, observation or session, from a list selection, and for
every trace matching the current filter (a snapshot, capped by `queue_bulk_max`). The dialog is an
HTMX fragment on the page it opens from and a plain page without JS."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from starlette.datastructures import FormData, QueryParams

from traceloom.annotation import queues
from traceloom.annotation.queues import ItemRef, QueueError, QueueNotFound
from traceloom.auth.deps import Access, form_data, get_container, require
from traceloom.auth.permissions import Perm
from traceloom.container import Container
from traceloom.db.session import get_db
from traceloom.prompts.service import NotAllowed
from traceloom.web import trace_filters
from traceloom.web.context import page_context
from traceloom.web.routers.queues import actor_of
from traceloom.web.routers.trace_delete import _matching
from traceloom.web.templating import render

router = APIRouter(tags=["queue-add"])

MAX_SELECTION = 200  # one request from a selection; "add all matching" covers more


def _options(db: Session, access: Access) -> list[dict[str, Any]]:
    """Queues to pick from. One whose score configs are all archived is listed but disabled."""
    out = []
    for queue, stats in queues.list_queues(db, access.project, actor_of(access)):
        configs = queues.queue_configs(db, access.project, queue)
        usable = any(c.archived_at is None for c in configs)
        out.append({"queue": queue, "pending": stats.pending, "usable": usable})
    return out


def _refs(
    container: Container,
    access: Access,
    trace_ids: list[str],
    observation_id: str,
    session_ids: list[str],
) -> list[ItemRef]:
    store, project_id = container.trace_store, access.project.id
    if len(trace_ids) + len(session_ids) > MAX_SELECTION:
        raise HTTPException(400, f"Add at most {MAX_SELECTION} items at a time.")
    refs: list[ItemRef] = []
    for tid in trace_ids:
        if store.get_trace(project_id, tid) is None:
            raise HTTPException(404, "Not found.")
        if observation_id:
            refs.append(ItemRef("observation", observation_id, tid))
        else:
            refs.append(ItemRef("trace", tid, tid))
    for sid in session_ids:
        if store.get_session(project_id, sid) is None:
            raise HTTPException(404, "Not found.")
        refs.append(ItemRef("session", sid))
    if not refs:
        raise HTTPException(400, "Choose at least one item.")
    return refs


def _subject(trace_ids: list[str], observation_id: str, session_ids: list[str]) -> str:
    if observation_id:
        return "this observation"
    if session_ids:
        return "this session" if len(session_ids) == 1 else f"these {len(session_ids)} sessions"
    return "this trace" if len(trace_ids) == 1 else f"these {len(trace_ids)} traces"


def _render(
    request: Request,
    db: Session,
    container: Container,
    access: Access,
    *,
    trace_ids: list[str],
    observation_id: str,
    session_ids: list[str],
    matching: bool,
    raw_filter: str,
    queue_id: str = "",
    error: str = "",
    result: dict[str, Any] | None = None,
    status: int = 200,
):
    ctx: dict[str, Any] = {
        "options": _options(db, access),
        "queue_id": queue_id,
        "trace_ids": trace_ids,
        "observation_id": observation_id,
        "session_ids": session_ids,
        "matching": matching,
        "raw_filter": raw_filter,
        "error": error,
        "result": result,
        "action": f"/project/{access.project.id}/queues/add",
        "queues_url": f"/project/{access.project.id}/queues",
        "bulk_max": container.settings.queue_bulk_max,
    }
    if matching:
        params = QueryParams(raw_filter)
        query, ids, _ = _matching(db, container, access, raw_filter)
        ctx.update(
            match_count=len(ids),
            over_limit=len(ids) > container.settings.queue_bulk_max,
            chips=trace_filters.describe(query, params),
        )
    else:
        ctx["subject"] = _subject(trace_ids, observation_id, session_ids)
        ctx["count"] = len(trace_ids) + len(session_ids)
    hx = request.headers.get("HX-Request") == "true"
    context = page_context(db, access, active="queues", dialog=hx, **ctx)
    name = "queues/_add_dialog.html" if hx else "queues/add.html"
    return render(request, name, context, status=status)


@router.get("/project/{project_id}/queues/add", response_class=HTMLResponse)
def add_form(
    request: Request,
    trace_id: list[str] = Query(default=[]),
    session_id: list[str] = Query(default=[]),
    observation_id: str = "",
    matching: str = "",
    filter: str = "",
    access: Access = Depends(require(Perm.QUEUES_WRITE)),
    container: Container = Depends(get_container),
    db: Session = Depends(get_db),
):
    trace_ids, session_ids = list(trace_id), list(session_id)
    if not matching:
        _refs(container, access, trace_ids, observation_id, session_ids)  # validates
    return _render(
        request, db, container, access, trace_ids=trace_ids, observation_id=observation_id,
        session_ids=session_ids, matching=bool(matching), raw_filter=filter,
    )  # fmt: skip


@router.post("/project/{project_id}/queues/add", response_class=HTMLResponse)
def add_submit(
    request: Request,
    access: Access = Depends(require(Perm.QUEUES_WRITE)),
    container: Container = Depends(get_container),
    form: FormData = Depends(form_data),
    db: Session = Depends(get_db),
):
    trace_ids = [str(t) for t in form.getlist("trace_id") if str(t)]
    session_ids = [str(s) for s in form.getlist("session_id") if str(s)]
    observation_id = str(form.get("observation_id", ""))
    matching = bool(form.get("matching"))
    raw_filter = str(form.get("filter", ""))
    queue_id = str(form.get("queue_id", ""))
    actor = actor_of(access)
    state: dict[str, Any] = dict(
        trace_ids=trace_ids, observation_id=observation_id, session_ids=session_ids,
        matching=matching, raw_filter=raw_filter, queue_id=queue_id,
    )  # fmt: skip

    def again(message: str, code: int = 400):
        db.rollback()
        return _render(request, db, container, access, error=message, status=code, **state)

    if not queue_id:
        return again("Choose a queue.")
    try:
        queue = queues.get_queue(db, access.project, actor, queue_id)
        configs = queues.queue_configs(db, access.project, queue)
        if not any(c.archived_at is None for c in configs):
            return again("All of this queue's score configs are archived. Pick another queue.")
        if matching:
            query, _, _ = _matching(db, container, access, raw_filter)
            outcome = queues.add_from_filter(
                db, access.project, actor, queue_id, container.trace_store, query,
                container.settings,
            )  # fmt: skip
        else:
            refs = _refs(container, access, trace_ids, observation_id, session_ids)
            outcome = queues.add_items(
                db, access.project, actor, queue_id, refs, container.settings
            )
    except QueueNotFound as exc:
        db.rollback()
        raise HTTPException(404, "Not found.") from exc
    except NotAllowed as exc:
        raise HTTPException(403, str(exc)) from exc
    except QueueError as exc:
        return again(str(exc))
    db.commit()
    result = {
        "added": outcome.added,
        "duplicates": outcome.duplicates,
        "total": outcome.total,
        "capped": outcome.capped,
        "queue_name": queue.name,
        "queue_url": f"/project/{access.project.id}/queues/{queue.id}",
    }
    return _render(request, db, container, access, result=result, **state)
