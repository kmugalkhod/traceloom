"""Evaluation > Evaluators > Templates: the starter library, project templates, the editor
(every save is a new immutable version) and clone (FR-J1, J2)."""

from __future__ import annotations

from collections.abc import Mapping
from functools import cache
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from traceloom.auth.deps import Access, form_data, get_container, require
from traceloom.auth.permissions import Perm, can
from traceloom.container import Container
from traceloom.db.models import User
from traceloom.db.models_eval import EvalRule
from traceloom.db.session import get_db
from traceloom.evals import templates as tpl
from traceloom.llm.connections import ConnectionPolicy, list_usable
from traceloom.prompts.service import Actor
from traceloom.prompts.variables import find_variables
from traceloom.services import audit
from traceloom.web.context import page_context
from traceloom.web.templating import render

router = APIRouter(tags=["evaluators"])

SCORE_TYPE_LABELS = {"numeric": "Numeric", "boolean": "Boolean", "categorical": "Categorical"}
NOTES = {
    "saved": "Template saved as a new version.",
    "cloned": "Template cloned into this project.",
}


@cache
def _descriptions() -> dict[str, str]:
    return {e["name"]: e.get("description", "") for e in tpl.load_starter()}


# The starter cards shown first (mockup section 7); the rest are listed under "Also built in".
FEATURED_STARTERS = ("hallucination", "relevance", "correctness")


def score_label(schema: Mapping[str, Any]) -> str:
    kind = SCORE_TYPE_LABELS.get(schema.get("type", ""), "")
    if schema.get("type") == "numeric":
        return f"{kind} {schema['min']:g} to {schema['max']:g}"
    return kind


def tab_counts(db: Session, project_id: str) -> dict[str, int]:
    templates = len(tpl.list_templates(db, project_id))
    rules = db.scalar(
        select(func.count()).select_from(EvalRule).where(EvalRule.project_id == project_id)
    )
    return {"templates": templates, "rules": rules or 0}


def tabs(project_id: str, current: str, counts: dict[str, int]) -> list[dict[str, Any]]:
    base = f"/project/{project_id}/evals"
    return [
        {
            "key": "templates",
            "label": "Templates",
            "href": f"{base}/templates",
            "count": counts["templates"],
        },
        {"key": "rules", "label": "Rules", "href": f"{base}/rules", "count": counts["rules"]},
        {"key": "log", "label": "Log", "href": f"{base}/log", "count": None},
    ]


def shell(db: Session, access: Access, current: str, **extra: Any) -> dict[str, Any]:
    counts = tab_counts(db, access.project.id)
    return page_context(
        db,
        access,
        active="evaluators",
        evals_tabs=[
            {**t, "current": t["key"] == current} for t in tabs(access.project.id, current, counts)
        ],
        **extra,
    )


def _connections(db: Session, access: Access, container: Container) -> list[dict[str, Any]]:
    """Connections and their models for the picker; only roles that can edit need it."""
    if not can(access.role, Perm.EVAL_TEMPLATES_WRITE):
        return []
    actor = Actor.for_user(access.user, access.role)
    policy = ConnectionPolicy.from_settings(container.settings)
    return [
        {"id": c.id, "name": c.name, "provider": c.provider, "models": c.models}
        for c in list_usable(db, access.project, actor, policy)
    ]


# --- Pages -------------------------------------------------------------------------------------


@router.get("/project/{project_id}/evals")
def evals_home(access: Access = Depends(require(Perm.EVAL_READ))):
    return RedirectResponse(f"/project/{access.project.id}/evals/templates", status_code=303)


@router.get("/project/{project_id}/evals/templates", response_class=HTMLResponse)
def templates_page(
    request: Request,
    notice: str = "",
    access: Access = Depends(require(Perm.EVAL_READ)),
    db: Session = Depends(get_db),
):
    project_id = access.project.id
    views = tpl.list_templates(db, project_id)
    rule_counts = dict(
        db.execute(
            select(EvalRule.template_id, func.count())
            .where(EvalRule.project_id == project_id)
            .group_by(EvalRule.template_id)
        ).all()
    )
    descriptions = _descriptions()
    cards = [
        {
            "id": v.template.id,
            "name": v.template.name,
            "description": descriptions.get(v.template.name, ""),
            "variables": v.variables,
            "score": score_label(v.latest.output_schema),
        }
        for v in views
        if v.builtin
    ]
    featured = sorted(
        (c for c in cards if c["name"] in FEATURED_STARTERS),
        key=lambda c: FEATURED_STARTERS.index(c["name"]),
    )
    more = [c for c in cards if c["name"] not in FEATURED_STARTERS]
    mine = [
        {
            "id": v.template.id,
            "name": v.template.name,
            "score": SCORE_TYPE_LABELS.get(v.latest.output_schema.get("type", ""), ""),
            "model": v.latest.model,
            "version": v.latest.version,
            "rules": rule_counts.get(v.template.id, 0),
            "edited": v.latest.created_at,
        }
        for v in views
        if not v.builtin
    ]
    context = shell(
        db,
        access,
        "templates",
        cards=featured,
        more_cards=more,
        mine=mine,
        notice=NOTES.get(notice, ""),
    )
    return render(request, "evals/templates.html", context)


def _blank_form() -> dict[str, Any]:
    return {
        "name": "",
        "prompt": "",
        "type": "numeric",
        "min": "0",
        "max": "1",
        "categories": "",
        "connection_id": "",
        "model": "",
        "temperature": "0",
        "max_tokens": "400",
    }


def _form_from_version(version: tpl.EvalTemplateVersion, name: str = "") -> dict[str, Any]:
    schema, params = version.output_schema or {}, version.params or {}
    return {
        "name": name,
        "prompt": version.prompt,
        "type": schema.get("type", "numeric"),
        "min": f"{schema.get('min', 0):g}",
        "max": f"{schema.get('max', 1):g}",
        "categories": "\n".join(schema.get("categories", [])),
        "connection_id": version.connection_id or "",
        "model": version.model or "",
        "temperature": "" if "temperature" not in params else str(params["temperature"]),
        "max_tokens": "" if "max_tokens" not in params else str(params["max_tokens"]),
    }


def _editor(
    request: Request,
    db: Session,
    access: Access,
    container: Container,
    *,
    view: tpl.TemplateView | None,
    form: dict[str, Any],
    shown: tpl.EvalTemplateVersion | None = None,
    error: str = "",
    status: int = 200,
):
    versions = tpl.versions(db, view.template.id) if view else []
    users = (
        {
            u.id: u
            for u in db.scalars(
                select(User).where(User.id.in_([v.created_by for v in versions if v.created_by]))
            )
        }
        if versions
        else {}
    )
    rail = [
        {
            "v": v,
            "by": (users[v.created_by].name or users[v.created_by].email)
            if v.created_by in users
            else "",
        }
        for v in versions
    ]
    builtin = bool(view and view.builtin)
    editable = (
        can(access.role, Perm.EVAL_TEMPLATES_WRITE)
        and not builtin
        and (shown is None or shown is view.latest)
    )
    context = shell(
        db,
        access,
        "templates",
        view=view,
        builtin=builtin,
        editable=editable,
        form=form,
        error=error,
        rail=rail,
        shown=shown,
        variables=find_variables(form["prompt"]),
        connections=_connections(db, access, container),
        can_clone=can(access.role, Perm.EVAL_TEMPLATES_WRITE),
    )
    return render(request, "evals/template_edit.html", context, status=status)


@router.get("/project/{project_id}/evals/templates/new", response_class=HTMLResponse)
def new_template_page(
    request: Request,
    access: Access = Depends(require(Perm.EVAL_TEMPLATES_WRITE)),
    container: Container = Depends(get_container),
    db: Session = Depends(get_db),
):
    return _editor(request, db, access, container, view=None, form=_blank_form())


@router.get("/project/{project_id}/evals/templates/{template_id}", response_class=HTMLResponse)
def template_page(
    request: Request,
    template_id: str,
    v: int | None = None,
    access: Access = Depends(require(Perm.EVAL_READ)),
    container: Container = Depends(get_container),
    db: Session = Depends(get_db),
):
    try:
        template = tpl.get_template(db, access.project.id, template_id)
        latest = tpl.get_version(db, template.id)
        shown = tpl.get_version(db, template.id, v) if v else latest
    except tpl.TemplateNotFound as exc:
        raise HTTPException(404, "Not found.") from exc
    view = tpl.TemplateView(template, latest)
    return _editor(
        request,
        db,
        access,
        container,
        view=view,
        form=_form_from_version(shown, template.name),
        shown=shown if shown is not latest else None,
    )


def _parse(form: FormData) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """(raw values for re-rendering, output schema, params); raises TemplateError."""
    raw = {k: str(form.get(k, "")).strip() for k in _blank_form()}
    raw["prompt"] = str(form.get("prompt", ""))  # keep the author's whitespace
    schema: dict[str, Any] = {"type": raw["type"]}
    try:
        if raw["type"] == "numeric":
            schema |= {"min": float(raw["min"]), "max": float(raw["max"])}
        elif raw["type"] == "categorical":
            schema["categories"] = [c.strip() for c in raw["categories"].splitlines() if c.strip()]
        params: dict[str, Any] = {}
        if raw["temperature"]:
            params["temperature"] = float(raw["temperature"])
            if not 0 <= params["temperature"] <= 2:
                raise tpl.TemplateError("Temperature must be between 0 and 2.")
        if raw["max_tokens"]:
            params["max_tokens"] = int(raw["max_tokens"])
            if not 1 <= params["max_tokens"] <= 100_000:
                raise tpl.TemplateError("Max tokens must be between 1 and 100000.")
    except ValueError:
        raise tpl.TemplateError(
            "Enter numbers for the range, temperature and max tokens."
        ) from None
    return raw, schema, params


def _audit(
    db: Session, access: Access, action: str, template: tpl.EvalTemplate, **detail: Any
) -> None:
    audit.record(
        db,
        action=action,
        actor_type="user",
        actor_id=access.user.id,
        org_id=access.project.org_id,
        project_id=access.project.id,
        target=template.id,
        detail={"name": template.name, **detail},
    )


@router.post("/project/{project_id}/evals/templates", response_class=HTMLResponse)
def create_template(
    request: Request,
    access: Access = Depends(require(Perm.EVAL_TEMPLATES_WRITE)),
    container: Container = Depends(get_container),
    form: FormData = Depends(form_data),
    db: Session = Depends(get_db),
):
    raw = _blank_form() | {k: str(form.get(k, "")) for k in _blank_form()}
    try:
        raw, schema, params = _parse(form)
        view = tpl.create_template(
            db,
            access.project.id,
            raw["name"],
            raw["prompt"],
            schema,
            params=params,
            connection_id=raw["connection_id"] or None,
            model=raw["model"] or None,
            created_by=access.user.id,
        )
    except tpl.TemplateError as exc:
        db.rollback()
        return _editor(
            request, db, access, container, view=None, form=raw, error=str(exc), status=400
        )
    _audit(db, access, "eval_template.create", view.template, version=1)
    db.commit()
    return RedirectResponse(
        f"/project/{access.project.id}/evals/templates/{view.template.id}", status_code=303
    )


@router.post("/project/{project_id}/evals/templates/{template_id}", response_class=HTMLResponse)
def save_template(
    request: Request,
    template_id: str,
    access: Access = Depends(require(Perm.EVAL_TEMPLATES_WRITE)),
    container: Container = Depends(get_container),
    form: FormData = Depends(form_data),
    db: Session = Depends(get_db),
):
    try:
        template = tpl.get_template(db, access.project.id, template_id)
        view = tpl.TemplateView(template, tpl.get_version(db, template.id))
    except tpl.TemplateNotFound as exc:
        raise HTTPException(404, "Not found.") from exc
    try:
        raw, schema, params = _parse(form)
        version = tpl.save_version(
            db,
            access.project.id,
            template_id,
            raw["prompt"],
            schema,
            params=params,
            connection_id=raw["connection_id"] or None,
            model=raw["model"] or None,
            created_by=access.user.id,
        )
    except tpl.BuiltinReadOnly as exc:
        raise HTTPException(403, str(exc)) from exc
    except tpl.TemplateError as exc:
        db.rollback()
        raw = _blank_form() | {k: str(form.get(k, "")) for k in _blank_form()}
        return _editor(
            request, db, access, container, view=view, form=raw, error=str(exc), status=400
        )
    _audit(db, access, "eval_template.version", template, version=version.version)
    db.commit()
    return RedirectResponse(
        f"/project/{access.project.id}/evals/templates?notice=saved", status_code=303
    )


@router.post("/project/{project_id}/evals/templates/{template_id}/clone")
def clone_template(
    template_id: str,
    access: Access = Depends(require(Perm.EVAL_TEMPLATES_WRITE)),
    form: FormData = Depends(form_data),
    db: Session = Depends(get_db),
):
    name = str(form.get("name", "")).strip() or None
    try:
        copy = tpl.clone(db, access.project.id, template_id, name, created_by=access.user.id)
    except tpl.TemplateNotFound as exc:
        raise HTTPException(404, "Not found.") from exc
    except tpl.TemplateError:  # the default "(copy)" name is taken: number it
        db.rollback()
        base = tpl.get_template(db, access.project.id, template_id).name
        taken = {v.template.name for v in tpl.list_templates(db, access.project.id)}
        n = 2
        while f"{base} (copy {n})" in taken:
            n += 1
        copy = tpl.clone(
            db, access.project.id, template_id, f"{base} (copy {n})", created_by=access.user.id
        )
    _audit(db, access, "eval_template.clone", copy.template, source=template_id)
    db.commit()
    return RedirectResponse(
        f"/project/{access.project.id}/evals/templates/{copy.template.id}", status_code=303
    )
