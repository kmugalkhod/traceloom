"""The Load prompt dialog: pick a chat prompt and a version, then open the playground on it
(`?prompt=<name>&version=<n>`). A fragment the playground page loads on first use."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from traceloom.auth.deps import Access, require
from traceloom.auth.permissions import Perm
from traceloom.db.session import get_db
from traceloom.prompts import service as prompts
from traceloom.prompts.service import Actor
from traceloom.web.context import page_context
from traceloom.web.templating import render

router = APIRouter(tags=["playground"])

MAX_LISTED = 200


@router.get("/project/{project_id}/playground/load-dialog", response_class=HTMLResponse)
def load_dialog(
    request: Request,
    access: Access = Depends(require(Perm.PLAYGROUND_RUN)),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    actor = Actor.for_user(access.user, access.role)
    listing = prompts.list_prompts(db, access.project, actor, limit=MAX_LISTED, sort="name")
    chat = [p for p in listing.items if p.type == "chat"]
    context = page_context(db, access, active="playground", chat_prompts=chat)
    return render(request, "playground/_load.html", context)
