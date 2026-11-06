"""The project traces list page, plus saved views and per-user display preferences."""

from __future__ import annotations

import dataclasses
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from traceloom.auth.deps import Access, current_project, form_data, get_container, require
from traceloom.auth.permissions import Perm
from traceloom.container import Container
from traceloom.db.session import get_db
from traceloom.ports import LEVELS, InvalidCursor, TraceQuery
from traceloom.services import views
from traceloom.services.orgs import NotAllowed, OrgError
from traceloom.web.context import env_context, page_context
from traceloom.web.formatting import percent
from traceloom.web.templating import render
from traceloom.web.trace_list import (
    COLUMN_KEYS,
    COLUMNS,
    PAGE_SIZE,
    canonical_params,
    chip_url,
    parse_columns,
    parse_state,
    row_model,
)

router = APIRouter(tags=["traces"])


def _local_next(project_id: str, target: str) -> str:
    """Only paths inside this project's own pages: no other site, no other project."""
    prefix = f"/project/{project_id}/"
    if target.startswith(prefix) and not target.startswith("//") and "\\" not in target:
        return target
    return f"/project/{project_id}/traces"


@router.get("/project/{project_id}/traces", response_class=HTMLResponse)
def traces_page(
    request: Request,
    access: Access = Depends(require(Perm.TRACES_READ)),
    container: Container = Depends(get_container),
    db: Session = Depends(get_db),
):
    project_id = access.project.id
    base = f"/project/{project_id}/traces"
    tidy = canonical_params(request.query_params)
    if tidy != [(k, v) for k, v in request.query_params.multi_items()]:
        return RedirectResponse(f"{base}?{urlencode(tidy)}" if tidy else base, status_code=303)

    store = container.trace_store
    state = parse_state(
        request.query_params, base, views.get_pref(db, access.user.id, project_id, "env")
    )
    columns = parse_columns(views.get_pref(db, access.user.id, project_id, "columns"))
    cursor = request.query_params.get("cursor") or None
    shown_before = (
        int(request.query_params.get("shown", "0"))
        if (request.query_params.get("shown") or "0").isdigit()
        else 0
    )

    error = None
    try:
        page = store.list_traces(project_id, dataclasses.replace(state.query, cursor=cursor))
        rows = [row_model(project_id, t) for t in page.items]
    except InvalidCursor:
        raise HTTPException(
            400, "That page link is not valid any more. Go back to the first page."
        ) from None
    except Exception:  # a failing store must not take the page down; filters stay in the URL
        page, rows, error = (
            None,
            [],
            "Could not load traces. Your filters are unchanged. Try again.",
        )

    shown = shown_before + len(rows)
    total = store.trace_stats(project_id, state.query).count if page is not None else 0
    next_url = state.with_cursor(page.next_cursor, shown) if page and page.next_cursor else None
    pager = {"shown": shown, "total": total, "page_size": PAGE_SIZE, "next_url": next_url}

    if request.headers.get("hx-request") and cursor:  # "Load more": new rows plus the new footer
        context = {"rows": rows, "columns": columns, "oob": True, **pager}
        html = render(request, "traces/_rows.html", context).body.decode()
        footer = render(request, "traces/_pager.html", context).body.decode()
        return HTMLResponse(html + footer)

    # Tab counts and the KPI row come from the same filters with the tab swapped in.
    base_state = parse_state(
        type(request.query_params)(
            [(k, v) for k, v in request.query_params.multi_items() if k != "tab"]
        ),
        base,
        views.get_pref(db, access.user.id, project_id, "env"),
    )
    q = base_state.query
    warn_level = q.min_level if q.min_level in LEVELS[LEVELS.index("WARNING") :] else "WARNING"
    per_tab = {
        "all": store.trace_stats(project_id, q),
        "errors": store.trace_stats(project_id, dataclasses.replace(q, has_error=True)),
        "warnings": store.trace_stats(project_id, dataclasses.replace(q, min_level=warn_level)),
        "bookmarked": store.trace_stats(project_id, dataclasses.replace(q, bookmarked=True)),
    }
    stats = per_tab[state.tab]
    tabs = [
        ("all", "All", per_tab["all"].count),
        ("errors", "Errors", per_tab["errors"].count),
        ("warnings", "Warnings", per_tab["warnings"].count),
        ("bookmarked", "Bookmarked", per_tab["bookmarked"].count),
    ]

    empty_project = False
    if not rows and error is None and not state.is_filtered:
        empty_project = not store.list_traces(project_id, TraceQuery(limit=1)).items

    context = page_context(
        db,
        access,
        active="traces",
        state=state,
        rows=rows,
        columns=columns,
        all_columns=COLUMNS,
        stats=stats,
        tabs=tabs,
        error_rate=percent(stats.error_count, stats.count),
        error=error,
        empty_project=empty_project,
        views=views.list_views(db, project_id),
        chip_urls=[chip_url(state, c) for c in state.chips],
        base_url=container.settings.base_url.rstrip("/"),
        oob=False,
        **pager,
        **env_context(db, store, access.user, project_id, state.environment),
    )
    return render(request, "traces/list.html", context)


# --- display preferences (any member, including Viewers: these are their own settings) -----------


@router.post("/project/{project_id}/prefs/env")
def set_environment(
    env: str = Form(""),
    next: str = Form(""),
    access: Access = Depends(current_project),
    db: Session = Depends(get_db),
):
    project_id = access.project.id
    if env in ("", "all"):
        views.clear_pref(db, access.user.id, project_id, "env")
    else:
        views.set_pref(db, access.user.id, project_id, "env", env[:100])
    db.commit()
    target = _local_next(project_id, next)
    # The saved choice applies when the URL does not name an environment.
    return RedirectResponse(_without_param(target, "env"), status_code=303)


@router.post("/project/{project_id}/prefs/columns")
def set_columns(
    request: Request,
    access: Access = Depends(current_project),
    db: Session = Depends(get_db),
    form: FormData = Depends(form_data),
):
    chosen = [c for c in form.getlist("col") if c in COLUMN_KEYS]
    project_id = access.project.id
    if chosen:
        views.set_pref(db, access.user.id, project_id, "columns", ",".join(chosen))
    else:
        views.clear_pref(db, access.user.id, project_id, "columns")
    db.commit()
    return RedirectResponse(_local_next(project_id, str(form.get("next", ""))), status_code=303)


def _without_param(url: str, name: str) -> str:
    from urllib.parse import parse_qsl, urlsplit

    parts = urlsplit(url)
    pairs = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != name]
    return parts.path + (f"?{urlencode(pairs)}" if pairs else "")


# --- saved views ------------------------------------------------------------------------------


def _fail(db: Session, exc: OrgError) -> HTTPException:
    db.rollback()
    return HTTPException(403 if isinstance(exc, NotAllowed) else 400, str(exc))


@router.post("/project/{project_id}/views")
def create_view(
    name: str = Form(""),
    query: str = Form(""),
    access: Access = Depends(require(Perm.ANNOTATE)),
    db: Session = Depends(get_db),
):
    project_id = access.project.id
    try:
        view = views.create_view(db, project_id, access.user, access.role, name, query)
    except OrgError as exc:
        raise _fail(db, exc) from exc
    db.commit()
    return RedirectResponse(
        f"/project/{project_id}/traces" + (f"?{view.query}" if view.query else ""), status_code=303
    )


@router.post("/project/{project_id}/views/{view_id}")
def update_view(
    view_id: str,
    name: str = Form(""),
    query: str | None = Form(None),
    next: str = Form(""),
    access: Access = Depends(require(Perm.ANNOTATE)),
    db: Session = Depends(get_db),
):
    project_id = access.project.id
    try:
        views.update_view(db, project_id, access.user, access.role, view_id, name, query)
    except OrgError as exc:
        raise _fail(db, exc) from exc
    db.commit()
    return RedirectResponse(_local_next(project_id, next), status_code=303)


@router.post("/project/{project_id}/views/{view_id}/delete")
def delete_view(
    view_id: str,
    next: str = Form(""),
    access: Access = Depends(require(Perm.ANNOTATE)),
    db: Session = Depends(get_db),
):
    project_id = access.project.id
    try:
        views.delete_view(db, project_id, access.user, access.role, view_id)
    except OrgError as exc:
        raise _fail(db, exc) from exc
    db.commit()
    return RedirectResponse(_local_next(project_id, next), status_code=303)
