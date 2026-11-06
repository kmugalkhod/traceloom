"""Prompts: the list (folders, search, filters, sort) and the new prompt / new version editor."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from traceloom.auth.deps import Access, form_data, require
from traceloom.auth.permissions import Perm
from traceloom.db.models import PRODUCTION_LABEL, User
from traceloom.db.session import get_db
from traceloom.prompts import service
from traceloom.prompts.service import Actor, NotAllowed, PromptError, PromptNotFound
from traceloom.prompts.variables import find_message_variables, find_variables
from traceloom.web.context import page_context
from traceloom.web.prompt_paths import prompt_url
from traceloom.web.templating import render

router = APIRouter(tags=["prompts"])

PAGE_SIZE = 50
SORTS = {"updated": "Updated", "name": "Name"}
# Labels offered as checkboxes in the editor; anything else goes in "Other labels".
COMMON_LABELS = (PRODUCTION_LABEL, "staging")


def _actor(access: Access) -> Actor:
    return Actor.for_user(access.user, access.role)


def _list_url(project_id: str, **params: Any) -> str:
    query = urlencode({k: v for k, v in params.items() if v not in (None, "", 1)})
    return f"/project/{project_id}/prompts" + (f"?{query}" if query else "")


@router.get("/project/{project_id}/prompts", response_class=HTMLResponse)
def prompt_list(
    request: Request,
    q: str = "",
    tag: str = "",
    label: str = "",
    folder: str = "",
    sort: str = "updated",
    page: int = Query(1, ge=1),
    access: Access = Depends(require(Perm.PROMPTS_READ)),
    db: Session = Depends(get_db),
):
    project, actor = access.project, _actor(access)
    sort = sort if sort in SORTS else "updated"
    folder = folder.strip("/")
    try:
        result = service.list_prompts(
            db,
            project,
            actor,
            folder=folder,
            search=q,
            tag=tag or None,
            label=label or None,
            sort=sort,
            descending=sort == "updated",
            page=page,
            limit=PAGE_SIZE,
        )
        everything = service.list_prompts(db, project, actor, limit=1)
    except PromptError as exc:
        raise HTTPException(400, str(exc)) from exc
    tags, labels = service.facets(db, project, actor)
    root = service.list_prompts(db, project, actor, folder="", direct_only=True, limit=1)
    inside = (
        service.list_prompts(db, project, actor, folder=folder, direct_only=True, limit=1)
        if folder
        else root
    )

    people = {
        u.id: (u.name or u.email)
        for u in db.scalars(
            select(User).where(User.id.in_({s.updated_by for s in result.items if s.updated_by}))
        )
    }
    keep = {"q": q, "tag": tag, "label": label, "sort": sort if sort != "updated" else ""}
    pid = project.id
    context = page_context(
        db,
        access,
        active="prompts",
        items=result.items,
        total=result.total,
        everything=everything.total,
        folder=folder,
        root_folders=root.folders,
        child_folders=inside.folders if folder else [],
        crumbs=_crumbs(folder),
        people=people,
        tags=tags,
        labels=labels,
        q=q,
        tag=tag,
        label=label,
        sort=sort,
        sorts=SORTS,
        filtered=bool(q or tag or label),
        folder_url=lambda f: _list_url(pid, folder=f, **keep),
        clear_url=_list_url(pid, folder=folder),
        chip_urls={
            "q": _list_url(pid, folder=folder, **{**keep, "q": ""}),
            "tag": _list_url(pid, folder=folder, **{**keep, "tag": ""}),
            "label": _list_url(pid, folder=folder, **{**keep, "label": ""}),
        },
        prompt_url=lambda name: prompt_url(pid, name),
        page=page,
        prev_url=_list_url(pid, folder=folder, page=page - 1, **keep) if page > 1 else "",
        next_url=(
            _list_url(pid, folder=folder, page=page + 1, **keep)
            if page * PAGE_SIZE < result.total
            else ""
        ),
        can_write=actor.can(Perm.PROMPTS_WRITE),
        new_url=f"/project/{pid}/prompts/new",
        PRODUCTION=PRODUCTION_LABEL,
    )
    return render(request, "prompts/list.html", context)


def _crumbs(folder: str) -> list[tuple[str, str]]:
    parts = folder.split("/") if folder else []
    return [("/".join(parts[: i + 1]), part) for i, part in enumerate(parts)]


# --- editor ---------------------------------------------------------------------------------


def _blank_state() -> dict[str, Any]:
    return {
        "mode": "prompt",
        "name": "",
        "type": "text",
        "tags": "",
        "text": "",
        "messages": [
            {"role": "system", "content": ""},
            {"role": "user", "content": ""},
        ],
        "config": "",
        "cfg_model": "",
        "cfg_temperature": "",
        "cfg_max_tokens": "",
        "commit_message": "",
        "labels": [],
        "other_labels": "",
        "from_version": None,
    }


def _editor_page(
    request: Request,
    db: Session,
    access: Access,
    state: dict[str, Any],
    errors: dict[str, str] | None = None,
    status: int = 200,
):
    actor = _actor(access)
    can_protected = actor.can(Perm.PROMPT_LABELS_PROTECTED)
    protected = set(service.protected_labels(db, access.project))
    if state["type"] == "chat":
        variables = find_message_variables(state["messages"])
    else:
        variables = find_variables(state["text"])
    context = page_context(
        db,
        access,
        active="prompts",
        state=state,
        errors=errors or {},
        variables=variables,
        can_protected=can_protected,
        protected=protected,
        common_labels=COMMON_LABELS,
        cancel_url=(
            prompt_url(access.project.id, state["name"])
            if state["mode"] == "version"
            else f"/project/{access.project.id}/prompts"
        ),
        roles=service.ROLES,
    )
    return render(request, "prompts/new.html", context, status=status)


@router.get("/project/{project_id}/prompts/new", response_class=HTMLResponse)
def new_prompt(
    request: Request,
    source: str = Query("", alias="from"),
    version: int | None = Query(None, ge=1),
    access: Access = Depends(require(Perm.PROMPTS_WRITE)),
    db: Session = Depends(get_db),
):
    state = _blank_state()
    if source:
        try:
            view = service.get(
                db,
                access.project,
                _actor(access),
                source,
                version=version,
                label=None if version else service.LATEST,
                resolve=False,
            )
        except PromptNotFound as exc:
            raise HTTPException(404, "Prompt not found.") from exc
        state.update(
            mode="version",
            name=view.name,
            type=view.type,
            tags=", ".join(view.tags),
            from_version=view.version,
            **_split_config(view.config),
        )
        if view.type == "chat":
            state["messages"] = [dict(m) for m in view.prompt]
        else:
            state["text"] = view.prompt
    return _editor_page(request, db, access, state)


COMMON_CONFIG = ("model", "temperature", "max_tokens")


def _split_config(config: dict[str, Any]) -> dict[str, str]:
    """A saved config as the editor's fields: three common keys plus the rest as JSON."""
    rest = {k: v for k, v in config.items() if k not in COMMON_CONFIG}
    return {
        "cfg_model": str(config.get("model", "")),
        "cfg_temperature": str(config.get("temperature", "")),
        "cfg_max_tokens": str(config.get("max_tokens", "")),
        "config": json.dumps(rest, indent=2) if rest else "",
    }


def _merge_config(state: dict[str, Any]) -> dict[str, Any]:
    """The editor's config fields as one JSON object. ValueError carries a message to show."""
    config: dict[str, Any] = {}
    if state["config"].strip():
        try:
            parsed = json.loads(state["config"])
        except ValueError:
            raise ValueError("Config must be valid JSON.") from None
        if not isinstance(parsed, dict):
            raise ValueError("Config must be a JSON object.")
        config = parsed
    if state["cfg_model"]:
        config["model"] = state["cfg_model"]
    try:
        if state["cfg_temperature"]:
            config["temperature"] = float(state["cfg_temperature"])
        if state["cfg_max_tokens"]:
            config["max_tokens"] = int(state["cfg_max_tokens"])
    except ValueError:
        raise ValueError(
            "Config temperature must be a number and max tokens a whole number."
        ) from None
    return config


def _read_form(form: FormData) -> dict[str, Any]:
    roles, bodies = form.getlist("role"), form.getlist("msg")
    other = [x.strip() for x in str(form.get("other_labels", "")).replace(",", " ").split()]
    return {
        "mode": "version" if form.get("mode") == "version" else "prompt",
        "name": str(form.get("name", "")).strip(),
        "type": "chat" if form.get("type") == "chat" else "text",
        "tags": str(form.get("tags", "")),
        "text": str(form.get("text", "")),
        "messages": [
            {"role": str(r), "content": str(b)} for r, b in zip(roles, bodies, strict=False)
        ],
        "config": str(form.get("config", "")),
        "cfg_model": str(form.get("cfg_model", "")).strip(),
        "cfg_temperature": str(form.get("cfg_temperature", "")).strip(),
        "cfg_max_tokens": str(form.get("cfg_max_tokens", "")).strip(),
        "commit_message": str(form.get("commit_message", "")),
        "labels": [str(x) for x in form.getlist("label")] + other,
        "other_labels": " ".join(other),
        "from_version": None,
    }


def _field_for(message: str) -> str:
    if message.startswith("Prompt names"):
        return "name"
    if message.startswith("A commit message"):
        return "commit_message"
    if message.startswith("Config"):
        return "config"
    if message.startswith("Tags") or "tag" in message.lower():
        return "tags"
    if "label" in message.lower():
        return "labels"
    return "form"


@router.post("/project/{project_id}/prompts/new", response_class=HTMLResponse)
def create_prompt_version(
    request: Request,
    access: Access = Depends(require(Perm.PROMPTS_WRITE)),
    form: FormData = Depends(form_data),
    db: Session = Depends(get_db),
):
    state = _read_form(form)
    errors: dict[str, str] = {}
    config: dict[str, Any] = {}
    try:
        config = _merge_config(state)
    except ValueError as exc:
        errors["config"] = str(exc)
    if not errors:
        content = state["messages"] if state["type"] == "chat" else state["text"]
        tags = [t.strip() for t in state["tags"].split(",") if t.strip()]
        try:
            row = service.create_version(
                db,
                access.project,
                _actor(access),
                name=state["name"],
                type=state["type"],
                content=content,
                config=config,
                commit_message=state["commit_message"],
                tags=tags if state["mode"] == "prompt" else None,  # a new version keeps the tags
                labels=state["labels"],
            )
        except NotAllowed as exc:
            raise HTTPException(403, str(exc)) from exc
        except PromptError as exc:
            errors[_field_for(str(exc))] = str(exc)
        else:
            db.commit()
            return RedirectResponse(
                prompt_url(access.project.id, state["name"], f"?version={row.version}"),
                status_code=303,
            )
    db.rollback()
    return _editor_page(request, db, access, state, errors, status=400)
