"""Sessions list and the session replay (FR-S1, FR-S2)."""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from traceloom.auth.deps import Access, get_container, require
from traceloom.auth.permissions import Perm
from traceloom.container import Container
from traceloom.db.session import get_db
from traceloom.ports import InvalidCursor, SessionQuery, TraceRow
from traceloom.web import formatting  # noqa: F401  (registers the filters)
from traceloom.web.context import page_context
from traceloom.web.ranges import environment, resolve_range
from traceloom.web.render import is_chat, pretty_json
from traceloom.web.templating import render

router = APIRouter(tags=["sessions"])

SORTS = ("first_trace", "last_trace", "traces", "cost", "duration")
DEFAULT_RANGE = "24h"
PAGE = 50


def _turn_text(value, role: str) -> str:
    """The text of one side of a turn. Plain text: the template escapes it."""
    if isinstance(value, str):
        return value
    if is_chat(value):
        wanted = [m for m in value if m.get("role") == role] or value
        return pretty_json(wanted[-1].get("content"))
    return pretty_json(value)


def turn(trace: TraceRow) -> dict:
    return {
        "trace": trace,
        "user_text": _turn_text(trace.input, "user"),
        "assistant_text": _turn_text(trace.output, "assistant"),
        "failed": trace.max_level == "ERROR",
    }


@router.get("/project/{project_id}/sessions", response_class=HTMLResponse)
def sessions_list(
    request: Request,
    sort: str = "last_trace",
    dir: str = "desc",
    cursor: str | None = None,
    access: Access = Depends(require(Perm.TRACES_READ)),
    container: Container = Depends(get_container),
    db: Session = Depends(get_db),
):
    params = dict(request.query_params)
    sort = sort if sort in SORTS else "last_trace"
    descending = dir != "asc"
    range_key, start, end = resolve_range(params, default=DEFAULT_RANGE)
    env = environment(params)
    query = SessionQuery(
        environment=env, from_time=start, to_time=end, sort=sort, descending=descending,
        cursor=cursor or None, limit=PAGE,
    )  # fmt: skip
    try:
        page = container.trace_store.list_sessions(access.project.id, query)
    except InvalidCursor as exc:
        raise HTTPException(400, "That page link is not valid any more.") from exc

    keep = {k: v for k, v in params.items() if k not in ("cursor", "range")}
    base = f"/project/{access.project.id}/sessions"
    no_cursor = {k: v for k, v in params.items() if k != "cursor"}
    next_href = (
        f"{base}?{urlencode({**no_cursor, 'cursor': page.next_cursor})}"
        if page.next_cursor
        else None
    )

    def sort_href(key: str) -> str:
        flip = "asc" if (key == sort and descending) else "desc"
        q = {k: v for k, v in params.items() if k not in ("cursor", "sort", "dir")}
        return f"{base}?{urlencode({**q, 'sort': key, 'dir': flip})}"

    context = page_context(
        db,
        access,
        active="sessions",
        sessions=page.items,
        sort=sort,
        descending=descending,
        sort_href=sort_href,
        range_key=range_key,
        keep={k: v for k, v in keep.items()},
        environment=env,
        first_page=cursor is None,
        first_href=f"{base}?{urlencode(no_cursor)}",
        next_href=next_href,
        range_label={"1h": "hour", "24h": "24 hours", "7d": "7 days", "30d": "30 days"}[range_key],
    )
    return render(request, "sessions/list.html", context)


@router.get("/project/{project_id}/sessions/{session_id:path}", response_class=HTMLResponse)
def session_replay(
    request: Request,
    session_id: str,
    access: Access = Depends(require(Perm.TRACES_READ)),
    container: Container = Depends(get_container),
    db: Session = Depends(get_db),
):
    detail = container.trace_store.get_session(access.project.id, session_id)
    if detail is None:
        raise HTTPException(404, "Not found.")
    traces = sorted(detail.traces, key=lambda t: (t.timestamp, t.id))
    context = page_context(
        db,
        access,
        active="sessions",
        session=detail.session,
        turns=[turn(t) for t in traces],
        cost_sum=sum(t.total_cost for t in traces),
    )
    return render(request, "sessions/detail.html", context)
