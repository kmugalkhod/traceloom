"""Users list and the user page (FR-S3, FR-S4)."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from traceloom.auth.deps import Access, get_container, require
from traceloom.auth.permissions import Perm
from traceloom.container import Container
from traceloom.db.session import get_db
from traceloom.ports import InvalidCursor, UserQuery
from traceloom.web import pagefmt  # noqa: F401  (registers the filters)
from traceloom.web.context import initials, page_context
from traceloom.web.ranges import environment, resolve_range
from traceloom.web.templating import WEB_DIR, render

router = APIRouter(tags=["users"])

SORTS = ("last_seen", "first_seen", "traces", "tokens", "cost")
DEFAULT_RANGE = "30d"
PAGE = 50


def charts_available() -> bool:
    """The chart initializer ships with the dashboard; until then pages show a table."""
    return (Path(WEB_DIR) / "static" / "charts.js").exists()


@router.get("/project/{project_id}/users", response_class=HTMLResponse)
def users_list(
    request: Request,
    sort: str = "last_seen",
    dir: str = "desc",
    cursor: str | None = None,
    access: Access = Depends(require(Perm.TRACES_READ)),
    container: Container = Depends(get_container),
    db: Session = Depends(get_db),
):
    params = dict(request.query_params)
    sort = sort if sort in SORTS else "last_seen"
    descending = dir != "asc"
    range_key, start, end = resolve_range(params, default=DEFAULT_RANGE)
    env = environment(params)
    query = UserQuery(
        environment=env, from_time=start, to_time=end, sort=sort, descending=descending,
        cursor=cursor or None, limit=PAGE,
    )  # fmt: skip
    try:
        page = container.trace_store.list_users(access.project.id, query)
    except InvalidCursor as exc:
        raise HTTPException(400, "That page link is not valid any more.") from exc

    base = f"/project/{access.project.id}/users"
    no_cursor = {k: v for k, v in params.items() if k != "cursor"}

    def sort_href(key: str) -> str:
        flip = "asc" if (key == sort and descending) else "desc"
        q = {k: v for k, v in no_cursor.items() if k not in ("sort", "dir")}
        return f"{base}?{urlencode({**q, 'sort': key, 'dir': flip})}"

    context = page_context(
        db,
        access,
        active="users",
        users=[{"row": u, "initials": initials(u.user_id)} for u in page.items],
        sort=sort,
        sort_href=sort_href,
        range_key=range_key,
        keep={k: v for k, v in params.items() if k not in ("cursor", "range")},
        environment=env,
        first_page=cursor is None,
        first_href=f"{base}?{urlencode(no_cursor)}",
        next_href=(
            f"{base}?{urlencode({**no_cursor, 'cursor': page.next_cursor})}"
            if page.next_cursor
            else None
        ),
        range_label={"1h": "hour", "24h": "24 hours", "7d": "7 days", "30d": "30 days"}[range_key],
    )
    return render(request, "users/list.html", context)


@router.get("/project/{project_id}/users/{user_id:path}", response_class=HTMLResponse)
def user_page(
    request: Request,
    user_id: str,
    access: Access = Depends(require(Perm.TRACES_READ)),
    container: Container = Depends(get_container),
    db: Session = Depends(get_db),
):
    summary = container.trace_store.get_user_summary(access.project.id, user_id)
    if summary is None:
        raise HTTPException(404, "Not found.")
    daily = summary.daily[-14:]
    chart = {
        "kind": "bar",
        "title": "Traces per day",
        "labels": [d["day"].isoformat() for d in daily],
        "series": [{"name": "Traces", "values": [d["traces"] for d in daily]}],
    }
    names = {}
    from traceloom.db.models import User

    author_ids = {s.author_id for s in summary.recent_scores if s.author_id}
    if author_ids:
        from sqlalchemy import select

        names = {
            u.id: (u.name or u.email)
            for u in db.scalars(select(User).where(User.id.in_(author_ids)))
        }
    context = page_context(
        db,
        access,
        active="users",
        summary=summary,
        daily=daily,
        chart=chart,
        charts_available=charts_available(),
        author_names=names,
        traces_href=f"/project/{access.project.id}/traces?{urlencode({'user_id': user_id})}",
    )
    return render(request, "users/detail.html", context)
