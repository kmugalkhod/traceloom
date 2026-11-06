"""The Save as prompt dialog (FR-L6): a fragment the playground page loads on first use.

The dialog lists the project's chat prompts (to add a version to one) or takes a new name, and a
commit message. Submitting posts to `/playground/save` from `playground_save.js`.
"""

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


@router.get("/project/{project_id}/playground/save-dialog", response_class=HTMLResponse)
def save_dialog(
    request: Request,
    access: Access = Depends(require(Perm.PLAYGROUND_RUN)),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    actor = Actor.for_user(access.user, access.role)
    listing = prompts.list_prompts(db, access.project, actor, limit=MAX_LISTED, sort="name")
    chat = [p for p in listing.items if p.type == "chat"]
    context = page_context(db, access, active="playground", chat_prompts=chat)
    return render(request, "playground/_save.html", context)
