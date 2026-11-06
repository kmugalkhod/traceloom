"""The playground page: editor, streamed single run, deep links (the run API is playground_api)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from traceloom.auth.deps import Access, get_container, require
from traceloom.auth.permissions import Perm, Role
from traceloom.container import Container
from traceloom.db.models import Membership, User
from traceloom.db.session import get_db
from traceloom.llm import connections
from traceloom.llm.connections import ConnectionPolicy
from traceloom.llm.models import supported_params
from traceloom.prompts import service as prompts
from traceloom.prompts.service import Actor, PromptError, PromptNotFound
from traceloom.prompts.variables import find_message_variables, find_variables
from traceloom.web.context import page_context
from traceloom.web.templating import render

router = APIRouter(tags=["playground"])

BLANK_MESSAGES = [
    {"role": "system", "content": ""},
    {"role": "user", "content": ""},
]
PARAM_KEYS = ("temperature", "top_p", "max_tokens", "stop")


def _admins(db: Session, org_id: str) -> list[str]:
    """Names of the org's Admins and Owners, so a Member knows who to ask."""
    rows = db.scalars(
        select(User)
        .join(Membership, Membership.user_id == User.id)
        .where(
            Membership.org_id == org_id,
            Membership.role.in_([Role.ADMIN.label, Role.OWNER.label]),
        )
        .order_by(User.name, User.email)
    )
    return [u.name or u.email for u in rows]


def _initial(
    db: Session, access: Access, actor: Actor, name: str, version: int | None
) -> dict[str, Any]:
    """Editor state for `?prompt=<name>&version=<n>`: a saved prompt's messages and config."""
    view = prompts.get(
        db,
        access.project,
        actor,
        name,
        version=version,
        label=None if version else prompts.LATEST,
        resolve=False,
    )
    if view.type == "chat":
        messages = [
            {"role": m["role"], "content": m["content"]}
            for m in view.prompt
            if m["role"] in ("system", "user", "assistant")
        ]
        variables = find_message_variables(view.prompt)
    else:
        messages = [{"role": "user", "content": view.prompt}]
        variables = find_variables(view.prompt)
    config = view.config or {}
    params = dict(config.get("params") or {})
    params.update({k: config[k] for k in PARAM_KEYS if k in config})
    return {
        "messages": messages or [dict(m) for m in BLANK_MESSAGES],
        "variables": dict.fromkeys(variables, ""),
        "model": config.get("model") or "",
        "params": params,
        "tools": config.get("tools") or [],
        "promptName": view.name,
        "promptVersion": view.version,
    }


@router.get("/project/{project_id}/playground", response_class=HTMLResponse)
def playground_page(
    request: Request,
    prompt: str = "",
    version: int | None = Query(None, ge=1),
    access: Access = Depends(require(Perm.PLAYGROUND_RUN)),
    container: Container = Depends(get_container),
    db: Session = Depends(get_db),
):
    project = access.project
    actor = Actor.for_user(access.user, access.role)
    usable = connections.list_usable(
        db, project, actor, ConnectionPolicy.from_settings(container.settings)
    )
    initial: dict[str, Any] = {"messages": [dict(m) for m in BLANK_MESSAGES]}
    notice = ""
    if prompt:
        try:
            initial.update(_initial(db, access, actor, prompt, version))
        except (PromptNotFound, PromptError):
            notice = f"Prompt {prompt!r} was not found, so the editor starts empty."
    connection_list = [
        {
            "id": u.id,
            "name": u.name,
            "provider": u.provider,
            "models": u.models,
            "params": {m: supported_params(u.provider, m) for m in u.models},
            "defaultParams": supported_params(u.provider, ""),
        }
        for u in usable
    ]
    base = f"/project/{project.id}/playground"
    config = {
        "projectId": project.id,
        "runUrl": f"{base}/run",
        "saveUrl": f"{base}/save",
        "observationUrl": f"{base}/from-observation/",
        "connections": connection_list,
        "canWrite": actor.can(Perm.PROMPTS_WRITE),
        "initial": initial,
    }
    context = page_context(
        db,
        access,
        active="playground",
        pg_config=config,
        has_connections=bool(connection_list),
        can_manage=actor.can(Perm.LLM_CONNECTIONS),
        admins=[] if connection_list else _admins(db, access.org.id),
        settings_url=f"/project/{project.id}/settings/connections",
        can_write=actor.can(Perm.PROMPTS_WRITE),
        notice=notice,
    )
    return render(request, "playground/index.html", context)
