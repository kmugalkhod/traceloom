"""The review screen for annotation queues: claim the next item, show it read-only beside a score
form, and complete, skip or move on. A lock is held while the page is open (FR-A3, A4)."""

from __future__ import annotations

from datetime import UTC
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from traceloom.annotation import claim, queues
from traceloom.annotation.queues import QueueNotFound
from traceloom.auth.deps import Access, form_data, get_container, require
from traceloom.auth.permissions import Perm
from traceloom.container import Container
from traceloom.db.models import AnnotationItem, AnnotationQueue, ScoreConfig, User
from traceloom.db.session import get_db
from traceloom.ports import ObservationRow, ScoreRow, TraceDetail
from traceloom.web import tree
from traceloom.web.context import page_context
from traceloom.web.formatting import latency, money
from traceloom.web.render import render_value
from traceloom.web.routers.queues import actor_of, base, segments
from traceloom.web.templating import render

router = APIRouter(tags=["queue-review"])

SESSION_TRACES = 10  # traces shown for a session item


# --- The object under review -----------------------------------------------------------------


def _sections(source: Any) -> list[tuple[str, Any]]:
    return [("Input", render_value(source.input)), ("Output", render_value(source.output))]


def _tree_rows(observations: list[ObservationRow], selected: str | None) -> list[dict[str, Any]]:
    rows = []
    for node in tree.flatten(tree.build_tree(observations)):
        o = node.obs
        total = (o.cost_details or {}).get("total")
        rows.append(
            {
                "name": o.name or o.type,
                "type": o.type,
                "depth": node.depth,
                "duration": latency(tree.duration_ms(o)) if o.end_time else "-",
                "cost": money(float(total)) if total else "",
                "selected": o.id == selected,
            }
        )
    return rows


def _chip_class(score: ScoreRow) -> str:
    return {"ANNOTATION": "ann", "EVAL": "eval"}.get(score.source, "api")


def _score_text(score: ScoreRow) -> str:
    if score.string_value is not None:
        return score.string_value
    if score.value is None:
        return ""
    return f"{score.value:g}"


def object_view(container: Container, project_id: str, item: AnnotationItem) -> dict[str, Any]:
    """What the left column shows for the item. `missing` when the object is gone from the store."""
    store = container.trace_store
    view: dict[str, Any] = {"kind": item.object_type, "missing": False, "scores": []}
    if item.object_type == "session":
        detail = store.get_session(project_id, item.object_id)
        if detail is None:
            return {**view, "missing": True, "title": f"Session {item.object_id}"}
        view.update(
            title=f"Session {item.object_id}",
            open_url=f"/project/{project_id}/sessions/{item.object_id}",
            traces=[
                {"id": t.id, "name": t.name or t.id, "sections": _sections(t)}
                for t in detail.traces[:SESSION_TRACES]
            ],
            more=max(0, len(detail.traces) - SESSION_TRACES),
            facts=[f"{detail.session.trace_count} traces"],
        )
        return view
    detail: TraceDetail | None = store.get_trace(project_id, item.trace_id or item.object_id)
    if detail is None:
        return {**view, "missing": True, "title": item.object_id}
    trace = detail.trace
    obs = next((o for o in detail.observations if o.id == item.object_id), None)
    if item.object_type == "observation" and obs is None:
        return {**view, "missing": True, "title": item.object_id}
    source = obs if item.object_type == "observation" else trace
    facts = [
        latency(trace.latency_ms) if trace.latency_ms else "",
        money(trace.total_cost) if trace.total_cost else "",
    ]
    view.update(
        title=(source.name or source.id) if source else item.object_id,
        id=source.id,
        level=(obs.level if obs else trace.max_level),
        facts=[f for f in facts if f],
        open_url=f"/project/{project_id}/traces/{trace.id}" + (f"?obs={obs.id}" if obs else ""),
        sections=_sections(source),
        steps=_tree_rows(detail.observations, obs.id if obs else None),
        scores=[
            s
            for s in detail.scores
            if (s.observation_id == item.object_id if obs else s.observation_id is None)
        ],
    )
    return view


# --- The form ----------------------------------------------------------------------------------


def config_inputs(configs: list[ScoreConfig], values: dict[str, str]) -> list[dict[str, Any]]:
    out = []
    for c in configs:
        labels = [x["label"] for x in c.categories or []]
        if c.data_type == "NUMERIC":
            lo = "" if c.min_value is None else f"{c.min_value:g}"
            hi = "" if c.max_value is None else f"{c.max_value:g}"
            hint = f"Numeric, {lo} to {hi}" if lo and hi else "Numeric"
        elif c.data_type == "BOOLEAN":
            hint = "Boolean. Press T or F."
        else:
            hint = "Categorical. Press 1 to 9."
        out.append(
            {
                "id": c.id,
                "name": c.name,
                "type": c.data_type,
                "labels": labels,
                "hint": hint,
                "value": values.get(c.id, ""),
                "archived": c.archived_at is not None,
            }
        )
    return out


def parse_values(form: FormData, configs: list[ScoreConfig]) -> dict[str, Any]:
    """Typed values for the configs that have one in the form (blank ones are left out)."""
    values: dict[str, Any] = {}
    for c in configs:
        raw = str(form.get(f"score_{c.id}", "")).strip()
        if raw == "":
            continue
        if c.data_type == "NUMERIC":
            try:
                values[c.id] = float(raw)
            except ValueError:
                raise claim.ClaimError(f"{c.name} must be a number.") from None
        elif c.data_type == "BOOLEAN":
            values[c.id] = raw.lower() in ("true", "1")
        else:
            values[c.id] = raw
    return values


def _raw_values(form: FormData, configs: list[ScoreConfig]) -> dict[str, str]:
    return {c.id: str(form.get(f"score_{c.id}", "")) for c in configs}


# --- Page --------------------------------------------------------------------------------------


def _lock_text(db: Session, opened: claim.Opened) -> dict[str, str]:
    item = opened.item
    until = item.locked_until.astimezone(UTC).strftime("%H:%M") if item.locked_until else ""
    return {"until": until, "who": opened.locked_by_name}


def _page(
    request: Request,
    db: Session,
    access: Access,
    container: Container,
    queue: AnnotationQueue,
    opened: claim.Opened,
    *,
    values: dict[str, str] | None = None,
    comment: str = "",
    error: str = "",
    notice: str = "",
    status: int = 200,
):
    item = opened.item
    stats = queues.stats(db, access.project, actor_of(access), queue.id)
    configs = queues.queue_configs(db, access.project, queue)
    done_by = db.get(User, item.completed_by) if item.completed_by else None
    view = object_view(container, access.project.id, item)
    context = page_context(
        db,
        access,
        active="queues",
        queue=queue,
        item=item,
        opened=opened,
        lock=_lock_text(db, opened),
        stats=stats,
        bar=segments(stats),
        position=queues.position(db, item),
        view=view,
        inputs=config_inputs(configs, values or {}),
        comment=comment,
        error=error,
        notice=notice,
        done_by=(done_by.name or done_by.email) if done_by else "",
        can_review=opened.state == "mine",
        back=base(access) + f"/{queue.id}",
        score_text=_score_text,
        chip_class=_chip_class,
        lock_minutes=container.settings.annotation_lock_minutes,
    )
    return render(request, "queues/review.html", context, status=status)


def _empty_page(
    request: Request, db: Session, access: Access, queue: AnnotationQueue, notice: str = ""
):
    stats = queues.stats(db, access.project, actor_of(access), queue.id)
    mine = None
    for r in stats.reviewers:
        if r.user_id == access.user.id:
            mine = r
    context = page_context(
        db,
        access,
        active="queues",
        queue=queue,
        stats=stats,
        bar=segments(stats),
        mine=mine,
        notice=notice,
        back=base(access) + f"/{queue.id}",
    )
    return render(request, "queues/review_done.html", context)


@router.get("/project/{project_id}/queues/{queue_id}/review", response_class=HTMLResponse)
def review_page(
    request: Request,
    queue_id: str,
    item: str = "",
    notice: str = "",
    access: Access = Depends(require(Perm.EVAL_READ)),
    container: Container = Depends(get_container),
    db: Session = Depends(get_db),
):
    """Without `item`: claim the next item and redirect to it (or show the end state). With
    `item`: open that one, read-only for viewers and when someone else holds it."""
    actor = actor_of(access)
    try:
        queue = queues.get_queue(db, access.project, actor, queue_id)
        if not item:
            if not actor.can(Perm.QUEUES_WRITE):
                raise HTTPException(403, "You need the Member role to review queue items.")
            claimed = claim.claim_next(db, access.project, actor, queue_id, container.settings)
            db.commit()
            if claimed is None:
                return _empty_page(request, db, access, queue, notice)
            return RedirectResponse(
                f"{base(access)}/{queue_id}/review?item={claimed.id}", status_code=303
            )
        opened = claim.claim_item(db, access.project, actor, queue_id, item, container.settings)
        db.commit()
    except QueueNotFound as exc:
        raise HTTPException(404, "Not found.") from exc
    return _page(request, db, access, container, queue, opened, notice=notice)


def _redirect_next(access: Access, queue_id: str, notice: str = "") -> RedirectResponse:
    suffix = f"?notice={notice}" if notice else ""
    return RedirectResponse(f"{base(access)}/{queue_id}/review{suffix}", status_code=303)


def _load(db: Session, access: Access, queue_id: str, item_id: str):
    try:
        actor = actor_of(access)
        queue = queues.get_queue(db, access.project, actor, queue_id)
        item = queues.get_item(db, access.project, actor, queue_id, item_id)
    except QueueNotFound as exc:
        raise HTTPException(404, "Not found.") from exc
    return queue, item


def _duration(form: FormData) -> int | None:
    try:
        return int(float(str(form.get("duration_ms", ""))))
    except ValueError:
        return None


@router.post(
    "/project/{project_id}/queues/{queue_id}/review/{item_id}/complete", response_class=HTMLResponse
)
def complete_item(
    request: Request,
    queue_id: str,
    item_id: str,
    access: Access = Depends(require(Perm.QUEUES_WRITE)),
    container: Container = Depends(get_container),
    form: FormData = Depends(form_data),
    db: Session = Depends(get_db),
):
    queue, item = _load(db, access, queue_id, item_id)
    configs = queues.queue_configs(db, access.project, queue)
    try:
        claim.complete(
            db,
            container.trace_store,
            access.project,
            actor_of(access),
            queue_id,
            item_id,
            parse_values(form, configs),
            comment=str(form.get("comment", "")),
            duration_ms=_duration(form),
        )
    except claim.ClaimError as exc:
        db.rollback()
        opened = claim.claim_item(
            db, access.project, actor_of(access), queue_id, item_id, container.settings
        )
        db.commit()
        return _page(
            request, db, access, container, queue, opened,
            values=_raw_values(form, configs), comment=str(form.get("comment", "")),
            error=str(exc), status=400,
        )  # fmt: skip
    db.commit()
    return _redirect_next(access, queue_id)


@router.post("/project/{project_id}/queues/{queue_id}/review/{item_id}/skip")
def skip_item(
    queue_id: str,
    item_id: str,
    access: Access = Depends(require(Perm.QUEUES_WRITE)),
    db: Session = Depends(get_db),
):
    _load(db, access, queue_id, item_id)
    try:
        claim.skip(db, access.project, actor_of(access), queue_id, item_id)
    except claim.ClaimError:
        db.rollback()
        return _redirect_next(access, queue_id)  # not ours any more: move on
    db.commit()
    return _redirect_next(access, queue_id)


@router.post("/project/{project_id}/queues/{queue_id}/review/{item_id}/next")
def next_item(
    queue_id: str,
    item_id: str,
    access: Access = Depends(require(Perm.QUEUES_WRITE)),
    container: Container = Depends(get_container),
    form: FormData = Depends(form_data),
    db: Session = Depends(get_db),
):
    """Save and move on when every score is filled in and valid; otherwise put the item back
    (still pending, unlocked) and open another one."""
    queue, item = _load(db, access, queue_id, item_id)
    actor = actor_of(access)
    configs = queues.queue_configs(db, access.project, queue)
    try:
        values = parse_values(form, configs)
        if configs and all(c.id in values for c in configs):
            claim.complete(
                db, container.trace_store, access.project, actor, queue_id, item_id, values,
                comment=str(form.get("comment", "")), duration_ms=_duration(form),
            )  # fmt: skip
            db.commit()
            return _redirect_next(access, queue_id)
    except claim.ClaimError:
        db.rollback()
    claim.release(db, access.project, actor, queue_id, item_id)
    other = claim.claim_next(
        db, access.project, actor, queue_id, container.settings, exclude=item_id
    )
    db.commit()
    if other is None:
        return RedirectResponse(
            f"{base(access)}/{queue_id}/review?item={item_id}&notice=no-other", status_code=303
        )
    return RedirectResponse(f"{base(access)}/{queue_id}/review?item={other.id}", status_code=303)


@router.post("/project/{project_id}/queues/{queue_id}/review/{item_id}/renew")
def renew_item(
    queue_id: str,
    item_id: str,
    access: Access = Depends(require(Perm.QUEUES_WRITE)),
    container: Container = Depends(get_container),
    db: Session = Depends(get_db),
):
    """Called by the open review page while the reviewer is there; extends their lock."""
    _load(db, access, queue_id, item_id)
    renewed = claim.renew(
        db, access.project, actor_of(access), queue_id, item_id, container.settings
    )
    db.commit()
    until = None
    if renewed:
        row = db.scalar(select(AnnotationItem.locked_until).where(AnnotationItem.id == item_id))
        until = row.astimezone(UTC).strftime("%H:%M") if row else None
    return JSONResponse({"renewed": renewed, "until": until})
