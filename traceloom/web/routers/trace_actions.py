"""Trace actions for Member+ (comments, scores, bookmark, tags) and the share/delete controls.

The trace detail page loads two fragments with HTMX: `/actions` (header controls) and
`/discussion` (comments and scores). Every write returns the refreshed fragment for HTMX, or
redirects to the detail page for a plain form post.
"""

from __future__ import annotations

import math
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from traceloom.auth.deps import Access, form_data, get_container, require
from traceloom.auth.permissions import Perm, can
from traceloom.container import Container
from traceloom.db.models import Comment, User
from traceloom.db.session import get_db
from traceloom.ports import ScoreRow, TraceDetail
from traceloom.services import audit
from traceloom.services import comments as comment_service
from traceloom.web.context import initials
from traceloom.web.render import render_markdown
from traceloom.web.templating import render, templates

router = APIRouter(tags=["trace-actions"])

MAX_TAGS = 50


def score_value(score: Any) -> str:
    """The value shown in a `name: value` chip."""
    if score.data_type == "CATEGORICAL":
        return score.string_value or ""
    if score.data_type == "BOOLEAN":
        return "true" if score.value else "false"
    if score.value is None:
        return score.string_value or ""
    return f"{score.value:g}" if abs(score.value) < 1e15 else str(score.value)


templates.env.filters["score_value"] = score_value


def _detail(container: Container, project_id: str, trace_id: str) -> TraceDetail:
    detail = container.trace_store.get_trace(project_id, trace_id)
    if detail is None:
        raise HTTPException(404, "Not found.")
    return detail


def _base_context(access: Access, detail: TraceDetail) -> dict[str, Any]:
    role = access.role
    return {
        "project": access.project,
        "trace": detail.trace,
        "role": role,
        "can": lambda perm: can(role, Perm(perm)),
    }


def _comment_views(db: Session, access: Access, detail: TraceDetail) -> list[dict[str, Any]]:
    project = access.project
    members = comment_service.project_members(db, project)
    names = {o.id: (o.name or o.type) for o in detail.observations}
    views = []
    for c in comment_service.list_comments(db, project.id, detail.trace.id):
        views.append(
            {
                "id": c.id,
                "author_name": c.author_name,
                "author_email": c.author_email,
                "initials": initials(c.author_name),
                "created_at": c.created_at,
                "object_type": c.object_type,
                "target": names.get(c.object_id, c.object_id),
                "html": _highlight(render_markdown(c.body), members),
                "can_delete": comment_service.can_delete(access.role, access.user.id, c.author_id),
            }
        )
    return views


def _highlight(html, members):
    """Wrap `@name` / `@email` of project members in a mention chip. Operates on rendered
    (already escaped) HTML, and the replacement text is the same escaped text."""
    from markupsafe import Markup, escape

    text = str(html)
    for user in members:
        for handle in filter(None, (user.name, user.email)):
            token = str(escape("@" + handle))
            text = text.replace(token, f'<span class="mention">{token}</span>')
    return Markup(text)


def _counts(db: Session, detail: TraceDetail, project_id: str) -> dict[str, int]:
    n_comments = db.scalar(
        select(func.count())
        .select_from(Comment)
        .where(Comment.project_id == project_id, Comment.trace_id == detail.trace.id)
    )
    return {
        "observations": len(detail.observations),
        "scores": len(detail.scores),
        "comments": n_comments or 0,
    }


def _actions_response(
    request: Request, db, container, access, detail, *, error: str = "", status: int = 200
) -> HTMLResponse:
    ctx = _base_context(access, detail)
    ctx.update(
        error=error,
        counts=_counts(db, detail, access.project.id),
        share_path=f"{container.settings.base_url.rstrip('/')}/share/{access.project.id}/{detail.trace.id}",
    )
    return render(request, "traces/_actions_body.html", ctx, status=status)


def _tags_response(
    request: Request, db, container, access, detail, *, error: str = "", status: int = 200
) -> HTMLResponse:
    ctx = _base_context(access, detail)
    ctx["error"] = error
    return render(request, "traces/_tags_body.html", ctx, status=status)


def _discussion_response(
    request: Request,
    db,
    container,
    access,
    detail,
    *,
    comment_error: str = "",
    comment_draft: str = "",
    score_error: str = "",
    score_form: dict | None = None,
    status: int = 200,
) -> HTMLResponse:
    ctx = _base_context(access, detail)
    author_ids = {s.author_id for s in detail.scores if s.author_id}
    users = db.scalars(select(User).where(User.id.in_(author_ids))) if author_ids else []
    ctx.update(
        comments=_comment_views(db, access, detail),
        scores=detail.scores,
        author_names={u.id: (u.name or u.email) for u in users},
        comment_error=comment_error,
        comment_draft=comment_draft,
        score_error=score_error,
        score_form=score_form or {"name": "", "data_type": "NUMERIC", "value": "", "comment": ""},
    )
    return render(request, "traces/_panels.html", ctx, status=status)


def _done(
    request: Request, access: Access, trace_id: str, fragment, refresh: bool = False
) -> Response:
    """HTMX gets the refreshed fragment; a plain form post goes back to the detail page.

    `refresh` asks HTMX to reload the whole page: bookmark, share and tag changes also change
    the header the detail page renders itself (flag chips, tag chips).
    """
    if request.headers.get("hx-request"):
        response = fragment()
        if refresh:
            response.headers["HX-Refresh"] = "true"
        return response
    return RedirectResponse(f"/project/{access.project.id}/traces/{trace_id}", status_code=303)


BASE = "/project/{project_id}/traces/{trace_id}"


@router.get(BASE + "/actions", response_class=HTMLResponse)
def actions_fragment(
    request: Request,
    trace_id: str,
    access: Access = Depends(require(Perm.TRACES_READ)),
    container: Container = Depends(get_container),
    db: Session = Depends(get_db),
):
    detail = _detail(container, access.project.id, trace_id)
    return _actions_response(request, db, container, access, detail)


@router.get(BASE + "/tags", response_class=HTMLResponse)
def tags_fragment(
    request: Request,
    trace_id: str,
    access: Access = Depends(require(Perm.TRACES_READ)),
    container: Container = Depends(get_container),
    db: Session = Depends(get_db),
):
    detail = _detail(container, access.project.id, trace_id)
    return _tags_response(request, db, container, access, detail)


@router.get(BASE + "/discussion", response_class=HTMLResponse)
def discussion_fragment(
    request: Request,
    trace_id: str,
    access: Access = Depends(require(Perm.TRACES_READ)),
    container: Container = Depends(get_container),
    db: Session = Depends(get_db),
):
    detail = _detail(container, access.project.id, trace_id)
    return _discussion_response(request, db, container, access, detail)


# --- comments -----------------------------------------------------------------------------


@router.post(BASE + "/comments", response_class=HTMLResponse)
def add_comment(
    request: Request,
    trace_id: str,
    access: Access = Depends(require(Perm.ANNOTATE)),
    container: Container = Depends(get_container),
    db: Session = Depends(get_db),
    form: FormData = Depends(form_data),
):
    detail = _detail(container, access.project.id, trace_id)
    body = str(form.get("body", ""))
    observation_id = str(form.get("observation_id", "")) or None
    object_type = "observation" if observation_id else "trace"
    if observation_id and observation_id not in {o.id for o in detail.observations}:
        raise HTTPException(404, "Not found.")
    try:
        comment = comment_service.add_comment(
            db,
            project=access.project,
            trace_id=trace_id,
            author=access.user,
            body=body,
            object_type=object_type,
            object_id=observation_id,
        )
    except comment_service.CommentError as exc:
        db.rollback()
        return _discussion_response(
            request, db, container, access, detail, comment_error=str(exc), comment_draft=body,
            status=400,
        )  # fmt: skip
    db.commit()
    comment_service.notify_mentions(
        db, container.mailer, container.settings, project=access.project, trace_id=trace_id,
        author=access.user, body=comment.body,
    )  # fmt: skip
    return _done(
        request, access, trace_id,
        lambda: _discussion_response(request, db, container, access, detail),
    )  # fmt: skip


@router.post(BASE + "/comments/{comment_id}/delete", response_class=HTMLResponse)
def delete_comment(
    request: Request,
    trace_id: str,
    comment_id: str,
    access: Access = Depends(require(Perm.ANNOTATE)),
    container: Container = Depends(get_container),
    db: Session = Depends(get_db),
):
    detail = _detail(container, access.project.id, trace_id)
    try:
        found = comment_service.delete_comment(
            db, project_id=access.project.id, trace_id=trace_id, comment_id=comment_id,
            user=access.user, role=access.role,
        )  # fmt: skip
    except comment_service.NotAllowed as exc:
        raise HTTPException(403, str(exc)) from exc
    if not found:
        raise HTTPException(404, "Not found.")
    db.commit()
    return _done(
        request, access, trace_id,
        lambda: _discussion_response(request, db, container, access, detail),
    )  # fmt: skip


# --- scores -------------------------------------------------------------------------------


def parse_score(data_type: str, raw: str) -> tuple[float | None, str | None, str]:
    """(value, string_value, data_type) for a form submission, or ValueError with a message."""
    raw = raw.strip()
    if not raw:
        raise ValueError("Enter a value.")
    if data_type == "NUMERIC":
        try:
            number = float(raw)
        except ValueError:
            raise ValueError("Enter a number.") from None
        if not math.isfinite(number):
            raise ValueError("Enter a finite number.")
        return number, None, "NUMERIC"
    if data_type == "BOOLEAN":
        lowered = raw.lower()
        if lowered in ("true", "1", "yes"):
            return 1.0, None, "BOOLEAN"
        if lowered in ("false", "0", "no"):
            return 0.0, None, "BOOLEAN"
        raise ValueError("Enter true or false.")
    if data_type == "CATEGORICAL":
        return None, raw, "CATEGORICAL"
    raise ValueError("Choose a score type.")


@router.post(BASE + "/scores", response_class=HTMLResponse)
def add_score(
    request: Request,
    trace_id: str,
    access: Access = Depends(require(Perm.ANNOTATE)),
    container: Container = Depends(get_container),
    db: Session = Depends(get_db),
    form: FormData = Depends(form_data),
):
    detail = _detail(container, access.project.id, trace_id)
    fields = {
        "name": str(form.get("name", "")).strip(),
        "data_type": str(form.get("data_type", "NUMERIC")),
        "value": str(form.get("value", "")),
        "comment": str(form.get("comment", "")).strip(),
    }
    observation_id = str(form.get("observation_id", "")) or None
    if observation_id and observation_id not in {o.id for o in detail.observations}:
        raise HTTPException(404, "Not found.")
    try:
        if not fields["name"]:
            raise ValueError("Give the score a name.")
        value, string_value, data_type = parse_score(fields["data_type"], fields["value"])
    except ValueError as exc:
        return _discussion_response(
            request, db, container, access, detail, score_error=str(exc), score_form=fields,
            status=400,
        )  # fmt: skip
    container.trace_store.add_score(
        access.project.id,
        ScoreRow(
            id=uuid.uuid4().hex,
            name=fields["name"][:200],
            timestamp=datetime.now(UTC),
            trace_id=trace_id,
            observation_id=observation_id,
            value=value,
            string_value=string_value,
            data_type=data_type,
            comment=fields["comment"] or None,
            source="ANNOTATION",
            author_id=access.user.id,
            environment=detail.trace.environment,
        ),
    )
    detail = _detail(container, access.project.id, trace_id)
    return _done(
        request, access, trace_id,
        lambda: _discussion_response(request, db, container, access, detail),
    )  # fmt: skip


# --- bookmark, tags -----------------------------------------------------------------------


@router.post(BASE + "/bookmark", response_class=HTMLResponse)
def set_bookmark(
    request: Request,
    trace_id: str,
    access: Access = Depends(require(Perm.ANNOTATE)),
    container: Container = Depends(get_container),
    db: Session = Depends(get_db),
    form: FormData = Depends(form_data),
):
    detail = _detail(container, access.project.id, trace_id)
    value = str(form.get("value", "1")) in ("1", "true", "on")
    container.trace_store.update_trace_flags(access.project.id, trace_id, bookmarked=value)
    detail = _detail(container, access.project.id, trace_id)
    return _done(
        request, access, trace_id,
        lambda: _actions_response(request, db, container, access, detail),
        refresh=True,
    )  # fmt: skip


def _clean_tag(raw: str) -> str:
    return " ".join(raw.split())[:200]


@router.post(BASE + "/tags", response_class=HTMLResponse)
def add_tag(
    request: Request,
    trace_id: str,
    access: Access = Depends(require(Perm.ANNOTATE)),
    container: Container = Depends(get_container),
    db: Session = Depends(get_db),
    form: FormData = Depends(form_data),
):
    detail = _detail(container, access.project.id, trace_id)
    tag = _clean_tag(str(form.get("tag", "")))
    if not tag:
        return _tags_response(
            request, db, container, access, detail, error="Enter a tag.", status=400
        )
    tags = list(detail.trace.tags)
    if tag not in tags:
        if len(tags) >= MAX_TAGS:
            return _tags_response(
                request, db, container, access, detail,
                error=f"A trace can have at most {MAX_TAGS} tags.", status=400,
            )  # fmt: skip
        tags.append(tag)
        container.trace_store.update_trace_flags(access.project.id, trace_id, tags=tags)
        detail = _detail(container, access.project.id, trace_id)
    return _done(
        request, access, trace_id,
        lambda: _tags_response(request, db, container, access, detail),
        refresh=True,
    )  # fmt: skip


@router.post(BASE + "/tags/remove", response_class=HTMLResponse)
def remove_tag(
    request: Request,
    trace_id: str,
    access: Access = Depends(require(Perm.ANNOTATE)),
    container: Container = Depends(get_container),
    db: Session = Depends(get_db),
    form: FormData = Depends(form_data),
):
    detail = _detail(container, access.project.id, trace_id)
    tag = str(form.get("tag", ""))
    tags = [t for t in detail.trace.tags if t != tag]
    if len(tags) != len(detail.trace.tags):
        container.trace_store.update_trace_flags(access.project.id, trace_id, tags=tags)
        detail = _detail(container, access.project.id, trace_id)
    return _done(
        request, access, trace_id,
        lambda: _tags_response(request, db, container, access, detail),
        refresh=True,
    )  # fmt: skip


# --- public share (Admin+) ------------------------------------------------------------------


@router.post(BASE + "/share", response_class=HTMLResponse)
def set_share(
    request: Request,
    trace_id: str,
    access: Access = Depends(require(Perm.SHARE_TOGGLE)),
    container: Container = Depends(get_container),
    db: Session = Depends(get_db),
    form: FormData = Depends(form_data),
):
    _detail(container, access.project.id, trace_id)
    enabled = str(form.get("enabled", "0")) in ("1", "true", "on")
    container.trace_store.update_trace_flags(access.project.id, trace_id, public=enabled)
    audit.record(
        db,
        action="trace.share_enabled" if enabled else "trace.share_disabled",
        actor_type="user",
        actor_id=access.user.id,
        org_id=access.org.id,
        project_id=access.project.id,
        target=trace_id,
    )
    db.commit()
    detail = _detail(container, access.project.id, trace_id)
    return _done(
        request, access, trace_id,
        lambda: _actions_response(request, db, container, access, detail),
        refresh=True,
    )  # fmt: skip
