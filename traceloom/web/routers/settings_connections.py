"""Settings > LLM connections: encrypted provider credentials, never shown again (Admin+)."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from traceloom.auth.deps import Access, form_data, get_container, require
from traceloom.auth.permissions import Perm
from traceloom.container import Container
from traceloom.db.session import get_db
from traceloom.llm import connections
from traceloom.llm.connections import ConnectionInvalid, ConnectionNotFound, ConnectionPolicy
from traceloom.llm.models import DEFAULT_MODELS
from traceloom.prompts.service import Actor, NotAllowed
from traceloom.web.context import page_context, settings_tabs
from traceloom.web.templating import render

router = APIRouter(tags=["llm-connections"])

# provider -> (label, tile class, tile text)
PROVIDER_INFO = {
    "anthropic": ("Anthropic", "a", "A"),
    "openai": ("OpenAI", "o", "O"),
    "azure_openai": ("Azure OpenAI", "z", "Az"),
    "bedrock": ("Amazon Bedrock", "b", "B"),
    "google": ("Google", "g", "G"),
    "openai_compatible": ("OpenAI-compatible", "b", "L"),
}
NOTES = {"saved": "Connection saved.", "deleted": "Connection deleted."}
BLANK = {
    "provider": "anthropic",
    "name": "",
    "base_url": "",
    "custom_models": "",
    "region": "",
    "api_version": "",
    "deployments": "",
    "access_key_id": "",
    "headers": "",
}


def _actor(access: Access) -> Actor:
    return Actor.for_user(access.user, access.role)


def _policy(container: Container) -> ConnectionPolicy:
    return ConnectionPolicy.from_settings(container.settings)


def _csv(text: str) -> list[str]:
    return [p.strip() for p in text.replace("\n", ",").split(",") if p.strip()]


def _deployments(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in _csv(text):
        model, sep, deployment = part.partition("=")
        if not sep or not model.strip() or not deployment.strip():
            raise ConnectionInvalid("Deployments are model=deployment pairs, comma separated.")
        out[model.strip()] = deployment.strip()
    return out


def _headers(text: str) -> dict[str, str] | None:
    text = text.strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except ValueError:
        raise ConnectionInvalid("Extra headers must be a JSON object.") from None
    if not isinstance(parsed, dict) or not all(isinstance(v, str) for v in parsed.values()):
        raise ConnectionInvalid("Extra headers must be a JSON object of text values.")
    return parsed


def _read(form: FormData) -> dict[str, Any]:
    """Raw form values (kept for re-rendering; secrets are not kept)."""
    return {
        "provider": str(form.get("provider", "anthropic")),
        "name": str(form.get("name", "")).strip(),
        "base_url": str(form.get("base_url", "")).strip(),
        "custom_models": str(form.get("custom_models", "")),
        "region": str(form.get("region", "")).strip(),
        "api_version": str(form.get("api_version", "")).strip(),
        "deployments": str(form.get("deployments", "")),
        "access_key_id": str(form.get("access_key_id", "")).strip(),
        "headers": str(form.get("headers", "")),
    }


def _secrets(form: FormData) -> dict[str, str]:
    return {k: str(form.get(k, "")) for k in ("api_key", "access_key_id", "session_token")}


def _config(provider: str, values: dict[str, Any]) -> dict[str, Any]:
    if provider == "azure_openai":
        return {
            "api_version": values["api_version"],
            "deployments": _deployments(values["deployments"]),
        }
    if provider == "bedrock":
        return {"region": values["region"]}
    return {}


FIELD_HINTS = (
    ("base url", "base_url"),
    ("endpoint", "base_url"),
    ("api key", "api_key"),
    ("access key", "api_key"),
    ("region", "region"),
    ("api version", "api_version"),
    ("deployment", "deployments"),
    ("header", "headers"),
    ("name", "name"),
)


def _field(message: str) -> str:
    """Which form field an error message is about ("form" when it is about none)."""
    lowered = message.lower()
    return next((field for needle, field in FIELD_HINTS if needle in lowered), "form")


def _page(
    request: Request,
    db: Session,
    access: Access,
    container: Container,
    *,
    notice: str = "",
    result: tuple[str, bool, str] | None = None,
    add: dict[str, Any] | None = None,
    add_error: tuple[str, str] | None = None,
    edit: tuple[str, dict[str, Any], tuple[str, str]] | None = None,
    status: int = 200,
):
    project = access.project
    rows = connections.list_connections(db, project, _actor(access), _policy(container))
    context = page_context(
        db,
        access,
        settings_tabs=settings_tabs(project.id, "connections", access.role),
        rows=rows,
        info=PROVIDER_INFO,
        provider_defaults={p: len(DEFAULT_MODELS.get(p, [])) for p in PROVIDER_INFO},
        notice=NOTES.get(notice, ""),
        result=result,
        add=add or BLANK,
        add_error=add_error,
        edit=edit,
    )
    return render(request, "settings/connections.html", context, status=status)


@router.get("/project/{project_id}/settings/connections", response_class=HTMLResponse)
def connections_page(
    request: Request,
    notice: str = "",
    access: Access = Depends(require(Perm.LLM_CONNECTIONS)),
    container: Container = Depends(get_container),
    db: Session = Depends(get_db),
):
    return _page(request, db, access, container, notice=notice)


@router.post("/project/{project_id}/settings/connections", response_class=HTMLResponse)
def create_connection(
    request: Request,
    access: Access = Depends(require(Perm.LLM_CONNECTIONS)),
    container: Container = Depends(get_container),
    form: FormData = Depends(form_data),
    db: Session = Depends(get_db),
):
    values = _read(form)
    policy = _policy(container)
    try:
        if values["provider"] not in PROVIDER_INFO:
            raise ConnectionInvalid("Unknown provider.")
        row = connections.create(
            db,
            access.project,
            _actor(access),
            policy,
            provider=values["provider"],
            name=values["name"],
            secrets=_secrets(form),
            base_url=values["base_url"] or None,
            config=_config(values["provider"], values),
            custom_models=_csv(values["custom_models"]),
            headers=_headers(values["headers"]),
        )
    except ConnectionInvalid as exc:
        db.rollback()
        return _page(
            request,
            db,
            access,
            container,
            add=values,
            add_error=(_field(str(exc)), str(exc)),
            status=400,
        )
    except NotAllowed as exc:
        raise HTTPException(403, str(exc)) from exc
    db.commit()
    if form.get("then") == "test":
        result = connections.test(db, access.project, _actor(access), policy, row.id)
        return _page(
            request,
            db,
            access,
            container,
            result=(row.name, result.ok, result.message),
            notice="saved",
        )
    return RedirectResponse(
        f"/project/{access.project.id}/settings/connections?notice=saved", status_code=303
    )


@router.post(
    "/project/{project_id}/settings/connections/{connection_id}", response_class=HTMLResponse
)
def update_connection(
    request: Request,
    connection_id: str,
    access: Access = Depends(require(Perm.LLM_CONNECTIONS)),
    container: Container = Depends(get_container),
    form: FormData = Depends(form_data),
    db: Session = Depends(get_db),
):
    values = _read(form)
    policy = _policy(container)
    try:
        row = connections.get(db, access.project, connection_id)
        connections.update(
            db,
            access.project,
            _actor(access),
            policy,
            connection_id,
            name=values["name"],
            secrets=_secrets(form),
            base_url=values["base_url"] or None if row.provider != "anthropic" else None,
            config=_config(row.provider, values)
            if row.provider in ("azure_openai", "bedrock")
            else None,
            custom_models=_csv(values["custom_models"]),
            headers=_headers(values["headers"]),
        )
    except ConnectionNotFound as exc:
        raise HTTPException(404, "Not found.") from exc
    except ConnectionInvalid as exc:
        db.rollback()
        return _page(
            request,
            db,
            access,
            container,
            edit=(connection_id, values, (_field(str(exc)), str(exc))),
            status=400,
        )
    db.commit()
    return RedirectResponse(
        f"/project/{access.project.id}/settings/connections?notice=saved", status_code=303
    )


@router.post(
    "/project/{project_id}/settings/connections/{connection_id}/test", response_class=HTMLResponse
)
def test_connection(
    request: Request,
    connection_id: str,
    access: Access = Depends(require(Perm.LLM_CONNECTIONS)),
    container: Container = Depends(get_container),
    db: Session = Depends(get_db),
):
    try:
        row = connections.get(db, access.project, connection_id)
        result = connections.test(
            db, access.project, _actor(access), _policy(container), connection_id
        )
    except ConnectionNotFound as exc:
        raise HTTPException(404, "Not found.") from exc
    return _page(request, db, access, container, result=(row.name, result.ok, result.message))


@router.post("/project/{project_id}/settings/connections/{connection_id}/delete")
def delete_connection(
    connection_id: str,
    access: Access = Depends(require(Perm.LLM_CONNECTIONS)),
    db: Session = Depends(get_db),
):
    try:
        connections.delete(db, access.project, _actor(access), connection_id)
    except ConnectionNotFound as exc:
        raise HTTPException(404, "Not found.") from exc
    db.commit()
    return RedirectResponse(
        f"/project/{access.project.id}/settings/connections?notice=deleted", status_code=303
    )
