"""Prompt detail: versions, compare, preview and the metrics tab container; label moves."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from traceloom.auth.deps import Access, form_data, get_container, require
from traceloom.auth.permissions import Perm
from traceloom.container import Container
from traceloom.db.models import PRODUCTION_LABEL, AuditLog, PromptVersion, User
from traceloom.db.session import get_db
from traceloom.prompts import service
from traceloom.prompts.composition import CompositionError, expand_segments, resolve_content
from traceloom.prompts.diff import diff_versions
from traceloom.prompts.service import (
    Actor,
    NotAllowed,
    PromptConflict,
    PromptError,
    PromptNotFound,
)
from traceloom.prompts.variables import VARIABLE_RE
from traceloom.web import prompt_views
from traceloom.web.context import env_context, page_context
from traceloom.web.envpref import resolve_env, saved_env
from traceloom.web.prompt_paths import prompt_url
from traceloom.web.ranges import range_urls, resolve_range
from traceloom.web.templating import render
from traceloom.webhooks import deliver

router = APIRouter(tags=["prompts"])

TABS = (
    ("versions", "Versions"),
    ("compare", "Compare"),
    ("preview", "Preview"),
    ("metrics", "Metrics"),
)
RAIL_SHORT = 5
HISTORY_LIMIT = 20


def _actor(access: Access) -> Actor:
    return Actor.for_user(access.user, access.role)


def _names(db: Session, ids: set[str | None]) -> dict[str, str]:
    wanted = {i for i in ids if i}
    if not wanted:
        return {}
    return {u.id: (u.name or u.email) for u in db.scalars(select(User).where(User.id.in_(wanted)))}


def _label_pills(labels: dict[str, int], version: int, latest: int) -> list[str]:
    """Labels pointing at `version`: production first, `latest` for the newest."""
    names = sorted(n for n, v in labels.items() if v == version)
    names.sort(key=lambda n: n != PRODUCTION_LABEL)
    if version == latest:
        names.insert(0, service.LATEST)
    return names


def _variables(segments: Any, out: list[str]) -> None:
    for segment in segments:
        if segment.kind == "text":
            out.extend(n for n in VARIABLE_RE.findall(segment.text) if n not in out)
        else:
            _variables(segment.children, out)


@router.get("/project/{project_id}/prompts/{name:promptname}", response_class=HTMLResponse)
def prompt_page(
    request: Request,
    name: str,
    tab: str = "versions",
    version: int | None = Query(None, ge=1),
    older: str = "",
    frm: int | None = Query(None, alias="from", ge=1),
    to: int | None = Query(None, ge=1),
    moved: str = "",
    was: str = "",
    now: str = "",
    err: str = "",
    access: Access = Depends(require(Perm.PROMPTS_READ)),
    container: Container = Depends(get_container),
    db: Session = Depends(get_db),
):
    project, actor = access.project, _actor(access)
    tab = tab if tab in dict(TABS) else "versions"
    try:
        head = service.get(db, project, actor, name, label=service.LATEST, resolve=False)
    except PromptNotFound as exc:
        raise HTTPException(404, "Prompt not found.") from exc
    rows = service.versions(db, project, actor, name)  # newest first
    by_number = {r.version: r for r in rows}
    labels = service.prompt_labels(db, project, head.prompt_id)
    people = _names(db, {r.created_by for r in rows})
    latest = head.latest_version

    selected = by_number.get(version or latest)
    if selected is None:
        raise HTTPException(404, "Version not found.")

    versions = [
        {
            "row": r,
            "labels": _label_pills(labels, r.version, latest),
            "author": people.get(r.created_by, "API key" if r.created_by else ""),
        }
        for r in rows
    ]
    rail = versions if older or len(versions) <= RAIL_SHORT else versions[:RAIL_SHORT]
    if selected.version not in {v["row"].version for v in rail}:
        rail = versions  # the selected version must be visible in the rail

    history_rows = list(
        db.scalars(
            select(AuditLog)
            .where(
                AuditLog.project_id == project.id,
                AuditLog.action == "prompt.label_moved",
                AuditLog.target == name,
            )
            .order_by(AuditLog.created_at.desc(), AuditLog.id)
            .limit(HISTORY_LIMIT)
        )
    )
    actor_names = _names(db, {h.actor_id for h in history_rows if h.actor_type == "user"})
    history = [
        {
            "who": actor_names.get(
                h.actor_id, "API key" if h.actor_type == "api_key" else "System"
            ),
            "label": h.detail.get("label", ""),
            "from": h.detail.get("from_version"),
            "to": h.detail.get("to_version"),
            "at": h.created_at,
        }
        for h in history_rows
    ]

    pid = project.id
    can_write = actor.can(Perm.PROMPTS_WRITE)
    can_protected = actor.can(Perm.PROMPT_LABELS_PROTECTED)
    sel_labels = _label_pills(labels, selected.version, latest)
    context: dict[str, Any] = {
        "name": name,
        "head": head,
        "tab": tab,
        "tabs": TABS,
        "tab_url": lambda key: prompt_url(pid, name, f"?{urlencode({'tab': key})}"),
        "versions": versions,
        "rail": rail,
        "hidden_versions": len(versions) - len(rail),
        "selected": selected,
        "selected_author": people.get(
            selected.created_by, "API key" if selected.created_by else ""
        ),
        "selected_labels": sel_labels,
        "labels": labels,
        "latest": latest,
        "history": history,
        "can_write": can_write,
        "can_protected": can_protected,
        "can_delete": actor.can(Perm.PROMPTS_DELETE),
        "can_playground": actor.can(Perm.PLAYGROUND_RUN),
        "known_labels": sorted(set(labels) - {PRODUCTION_LABEL}),
        "PRODUCTION": PRODUCTION_LABEL,
        "base": prompt_url(pid, name),
        "prompt_url_for": lambda other: prompt_url(pid, other),
        "version_url": lambda n: prompt_url(pid, name, f"?{urlencode({'version': n})}"),
        "moved": moved,
        "moved_was": was,
        "moved_now": now,
        "error": err,
        "config_line": _config_line(selected.config),
        "playground_url": f"/project/{pid}/playground?"
        + urlencode({"prompt": name, "version": selected.version}),
        "new_version_url": f"/project/{pid}/prompts/new?"
        + urlencode({"from": name, "version": selected.version}),
        "sdk_snippet": _sdk_snippet(name, head.type),
    }
    if tab == "compare":
        context.update(_compare(rows, by_number, frm, to, head.type))
    elif tab == "preview":
        context.update(_preview_context(db, access, name, head.type, selected, request))
    elif tab == "metrics":
        params = request.query_params
        range_key, _, _ = resolve_range(params)
        env = resolve_env(params, saved_env(db, access.user.id, pid))
        query = {"version": selected.version, "range": range_key, **({"env": env} if env else {})}
        context["metrics_url"] = prompt_url(pid, name, f"/metrics?{urlencode(query)}")
        context["range_key"] = range_key
        context["range_urls"] = range_urls(request.url.path, params)
        context.update(env_context(db, container.trace_store, access.user, pid, env))
    full = page_context(db, access, active="prompts", **context)
    template = "prompts/_panel.html" if request.headers.get("hx-request") else "prompts/detail.html"
    return render(request, template, full)


def _config_line(config: dict[str, Any]) -> str:
    parts = []
    if config.get("model"):
        parts.append(str(config["model"]))
    if "temperature" in config:
        parts.append(f"temperature {config['temperature']}")
    if "max_tokens" in config:
        parts.append(f"max tokens {config['max_tokens']}")
    return " · ".join(parts)


def _sdk_snippet(name: str, prompt_type: str) -> str:
    call = "messages = prompt.compile()" if prompt_type == "chat" else "text = prompt.compile()"
    return (
        "import traceloom_sdk\n\n"
        f'prompt = traceloom_sdk.get_prompt({json.dumps(name)}, label="production")\n'
        f'{call}  # pass values as keyword arguments, e.g. compile(name="Ann")'
    )


def _compare(
    rows: list[PromptVersion],
    by_number: dict[int, PromptVersion],
    frm: int | None,
    to: int | None,
    prompt_type: str,
) -> dict[str, Any]:
    newest = rows[0].version
    to_v = by_number.get(to or newest) or rows[0]
    default_from = max(1, to_v.version - 1)
    from_v = by_number.get(frm or default_from) or by_number[default_from]
    diff = diff_versions(prompt_type, from_v.content, to_v.content, from_v.config, to_v.config)
    added = removed = 0
    if prompt_type == "chat":
        for message in diff.content:
            added += sum(1 for line in message.lines if line.op == "add")
            removed += sum(1 for line in message.lines if line.op == "remove")
    else:
        added = sum(1 for line in diff.content if line.op == "add")
        removed = sum(1 for line in diff.content if line.op == "remove")
    return {
        "from_v": from_v,
        "to_v": to_v,
        "diff": diff,
        "added": added,
        "removed": removed,
        "config_changed": sum(1 for k in diff.config if k.status != "unchanged"),
    }


def _preview_context(
    db: Session,
    access: Access,
    name: str,
    prompt_type: str,
    row: PromptVersion,
    request: Request,
) -> dict[str, Any]:
    resolver = service.make_resolver(db, access.project)
    root = (name, row.version)
    values = {k[2:]: v for k, v in request.query_params.items() if k.startswith("v_")}

    if prompt_type == "chat":
        sources = [(m["role"], m["content"]) for m in row.content]
    else:
        sources = [("text", row.content)]
    error = ""
    messages: list[dict[str, Any]] = []
    variables: list[str] = []
    missing: list[str] = []
    dependencies: list[dict[str, Any]] = []
    try:
        for role, content in sources:
            if role == "placeholder":
                messages.append({"role": role, "placeholder": content, "parts": []})
                continue
            segments = expand_segments(content, resolver, root=root)
            _variables(segments, variables)
            messages.append(
                {
                    "role": role,
                    "parts": prompt_views.preview_parts(segments, values, missing),
                }
            )
        dependencies = resolve_content(prompt_type, row.content, resolver, root=root)[1]
    except CompositionError as exc:
        error = str(exc)
        messages = []
    return {
        "preview_messages": messages,
        "preview_variables": variables or _raw_variables(sources),
        "preview_values": values,
        "preview_missing": missing,
        "preview_error": error,
        "dependencies": dependencies,
    }


def _raw_variables(sources: list[tuple[str, str]]) -> list[str]:
    names: list[str] = []
    for role, content in sources:
        if role != "placeholder":
            names.extend(n for n in VARIABLE_RE.findall(content) if n not in names)
    return names


# --- label moves and delete -----------------------------------------------------------------


@router.post("/project/{project_id}/prompts/{name:promptname}/labels")
def move_label(
    name: str,
    access: Access = Depends(require(Perm.PROMPTS_WRITE)),
    form: FormData = Depends(form_data),
    container: Container = Depends(get_container),
    db: Session = Depends(get_db),
):
    label = str(form.get("label", "")).strip()
    op = "remove" if form.get("op") == "remove" else "set"
    try:
        version = int(str(form.get("version", "")))
    except ValueError:
        raise HTTPException(400, "Pick a version.") from None
    actor, project = _actor(access), access.project
    try:
        before = service.prompt_labels(db, project, service.get(db, project, actor, name).prompt_id)
        if op == "remove":
            changed = service.remove_label(db, project, actor, name, label)
            to_version: int | None = None
        else:
            changed = service.move_label(db, project, actor, name, label, version)
            to_version = version
    except NotAllowed as exc:
        raise HTTPException(403, str(exc)) from exc
    except PromptNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except PromptConflict as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc
    except PromptError as exc:
        db.rollback()
        return RedirectResponse(
            prompt_url(project.id, name, "?" + urlencode({"version": version, "err": str(exc)})),
            status_code=303,
        )
    db.commit()
    if changed:
        deliver.kick(container)  # label.moved webhooks go out now, not at the next sweep
    query: dict[str, Any] = {"version": version}
    if changed:
        query.update(moved=label, was=before.get(label) or "", now=to_version or "")
    return RedirectResponse(prompt_url(project.id, name, "?" + urlencode(query)), status_code=303)


@router.post("/project/{project_id}/prompts/{name:promptname}/delete")
def delete_prompt(
    name: str,
    access: Access = Depends(require(Perm.PROMPTS_DELETE)),
    db: Session = Depends(get_db),
):
    try:
        service.delete(db, access.project, _actor(access), name)
    except PromptNotFound as exc:
        raise HTTPException(404, "Prompt not found.") from exc
    db.commit()
    return RedirectResponse(f"/project/{access.project.id}/prompts", status_code=303)
