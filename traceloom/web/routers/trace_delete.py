"""Delete one trace, or every trace matching the current filter (Admin+), with an audit row."""

from __future__ import annotations

from urllib.parse import parse_qsl

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from traceloom.auth.deps import Access, get_container, require
from traceloom.auth.permissions import Perm
from traceloom.container import Container
from traceloom.db.session import get_db
from traceloom.ports import TraceQuery
from traceloom.services import audit
from traceloom.services import comments as comment_service
from traceloom.web import trace_filters
from traceloom.web.context import page_context
from traceloom.web.templating import render

router = APIRouter(tags=["trace-delete"])

AUDIT_ID_LIMIT = 100  # trace ids kept in one audit row; the count is always exact


def _delete(
    db: Session, container: Container, access: Access, ids: list[str], action: str, **detail
) -> int:
    """Delete `ids` from the trace store and their comments from the meta DB, then audit.

    Comments live in the meta database in both modes, so they are removed here, not in the
    store (the ClickHouse store cannot reach them). Safe to repeat: the lite store also
    removes them and deleting nothing is not an error.
    """
    project_id = access.project.id
    removed = container.trace_store.delete_traces(project_id, ids)
    comment_service.delete_for_traces(db, project_id, ids)
    audit.record(
        db,
        action=action,
        actor_type="user",
        actor_id=access.user.id,
        org_id=access.org.id,
        project_id=project_id,
        target=ids[0] if len(ids) == 1 else "filter",
        detail={
            "count": removed,
            "trace_ids": ids[:AUDIT_ID_LIMIT],
            "truncated": len(ids) > AUDIT_ID_LIMIT,
            **detail,
        },
    )
    db.commit()
    return removed


def _matching(container: Container, access: Access, raw: str) -> tuple[TraceQuery, list[str]]:
    pairs = parse_qsl(raw, keep_blank_values=False)
    params = dict(pairs)
    multi: dict[str, list[str]] = {}
    for key, value in pairs:
        multi.setdefault(key, []).append(value)
    query = trace_filters.parse_trace_query(params, multi)
    return query, container.trace_store.matching_trace_ids(access.project.id, query)


def _dialog_page(request, db, access, raw, query, ids, *, error="", status=200):
    params = dict(parse_qsl(raw))
    context = page_context(
        db,
        access,
        active="traces",
        raw_filter=raw,
        match_count=len(ids),
        chips=trace_filters.describe(query, params),
        error=error,
        list_href=f"/project/{access.project.id}/traces" + (f"?{raw}" if raw else ""),
    )
    return render(request, "traces/delete_matching.html", context, status=status)


@router.get("/project/{project_id}/traces-delete-matching", response_class=HTMLResponse)
def delete_matching_dialog(
    request: Request,
    access: Access = Depends(require(Perm.TRACES_DELETE)),
    container: Container = Depends(get_container),
    db: Session = Depends(get_db),
):
    raw = request.url.query
    query, ids = _matching(container, access, raw)
    return _dialog_page(request, db, access, raw, query, ids)


@router.post("/project/{project_id}/traces-delete-matching", response_class=HTMLResponse)
async def delete_matching(
    request: Request,
    access: Access = Depends(require(Perm.TRACES_DELETE)),
    container: Container = Depends(get_container),
    db: Session = Depends(get_db),
):
    form = await request.form()
    raw = str(form.get("filter", ""))
    query, ids = _matching(container, access, raw)
    if not ids:
        return _dialog_page(
            request, db, access, raw, query, ids, error="No traces match.", status=400
        )
    if str(form.get("confirm", "")).strip() != str(len(ids)):
        return _dialog_page(
            request, db, access, raw, query, ids,
            error=f"Type {len(ids)} to confirm. The number of matching traces is {len(ids)} now.",
            status=400,
        )  # fmt: skip
    removed = _delete(
        db, container, access, ids, "traces.deleted_by_filter",
        filter=trace_filters.to_jsonable(query), query_string=raw[:1000],
    )  # fmt: skip
    return RedirectResponse(
        f"/project/{access.project.id}/traces?notice=deleted&count={removed}", status_code=303
    )


@router.post("/project/{project_id}/traces-delete/{trace_id}")
def delete_one(
    request: Request,
    trace_id: str,
    access: Access = Depends(require(Perm.TRACES_DELETE)),
    container: Container = Depends(get_container),
    db: Session = Depends(get_db),
):
    if container.trace_store.get_trace(access.project.id, trace_id) is None:
        raise HTTPException(404, "Not found.")
    _delete(db, container, access, [trace_id], "trace.deleted")
    return RedirectResponse(f"/project/{access.project.id}/traces?notice=deleted", status_code=303)
