"""The compare panel (FR-L7): up to 4 variant columns, loaded by playground_compare.js.

The fragment carries the project's usable connections and their models as JSON for the column
selectors, and one variant template the script clones. Runs go through the normal
`POST /playground/run`, one request per column.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from traceloom.auth.deps import Access, get_container, require
from traceloom.auth.permissions import Perm
from traceloom.container import Container
from traceloom.db.session import get_db
from traceloom.llm import connections
from traceloom.llm.connections import ConnectionPolicy
from traceloom.prompts.service import Actor
from traceloom.web.context import page_context
from traceloom.web.templating import render

router = APIRouter(tags=["playground"])

MAX_VARIANTS = 4


@router.get("/project/{project_id}/playground/compare-panel", response_class=HTMLResponse)
def compare_panel(
    request: Request,
    access: Access = Depends(require(Perm.PLAYGROUND_RUN)),
    container: Container = Depends(get_container),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    actor = Actor.for_user(access.user, access.role)
    usable = connections.list_usable(
        db, access.project, actor, ConnectionPolicy.from_settings(container.settings)
    )
    context = page_context(
        db,
        access,
        active="playground",
        max_variants=MAX_VARIANTS,
        connections=[
            {"id": u.id, "name": u.name, "provider": u.provider, "models": u.models} for u in usable
        ],
    )
    return render(request, "playground/_compare.html", context)
