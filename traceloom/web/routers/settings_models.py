"""Settings > Models: list built-in and project model definitions, edit the project's own."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from traceloom.auth.deps import Access, get_container, require
from traceloom.auth.permissions import Perm
from traceloom.container import Container
from traceloom.cost import admin
from traceloom.db.models import ModelDefinition
from traceloom.db.session import get_db
from traceloom.services import audit
from traceloom.web.context import page_context, settings_tabs
from traceloom.web.templating import render, templates

router = APIRouter(tags=["models"])

PRICE_KEYS = (
    ("input", "Input"),
    ("output", "Output"),
    ("cache_read", "Cache read"),
    ("cache_write", "Cache write"),
)
PER_MILLION = Decimal(1_000_000)
NOTES = {"saved": "Model definition saved.", "deleted": "Model definition deleted."}


def per_million(price: float | None) -> str:
    """USD per token -> a readable USD per 1M tokens (no float noise, no trailing zeros)."""
    if price is None:
        return ""
    value = (Decimal(repr(price)) * PER_MILLION).quantize(Decimal("0.0001")).normalize()
    return f"{value:f}"


templates.env.filters["per_million"] = per_million


def _price(text: str, label: str) -> float | None:
    text = text.strip().lstrip("$")
    if not text:
        return None
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise admin.DefinitionError(f"{label} must be a number.") from exc
    if not value.is_finite() or value < 0:
        raise admin.DefinitionError(f"{label} must be zero or more.")
    return float(value / PER_MILLION)


def _prices(values: dict[str, str], prefix: str = "") -> dict[str, float]:
    out = {}
    for key, label in PRICE_KEYS:
        price = _price(values.get(key, ""), f"{prefix}{label} price")
        if price is not None:
            out[key] = price
    return out


def _parse_form(form) -> dict:
    """Turn the submitted form (USD per 1M tokens) into admin.create/update fields."""
    fields: dict = {
        "model_name": form.get("model_name", ""),
        "match_pattern": form.get("match_pattern", ""),
        "tokenizer": (form.get("tokenizer") or "").strip() or None,
        "prices": _prices({k: form.get(f"price_{k}", "") for k, _ in PRICE_KEYS}),
    }
    day = (form.get("valid_from") or "").strip()
    if day:
        try:
            fields["valid_from"] = datetime.fromisoformat(day).replace(tzinfo=UTC)
        except ValueError as exc:
            raise admin.DefinitionError("Valid from must be a date like 2026-11-01.") from exc
    else:
        fields["valid_from"] = None
    tiers = []
    thresholds = form.getlist("tier_threshold")
    for i, threshold in enumerate(thresholds):
        row = {k: (form.getlist(f"tier_{k}") + [""] * len(thresholds))[i] for k, _ in PRICE_KEYS}
        prices = _prices(row, "Tier ")
        if not threshold.strip() and not prices:
            continue  # an empty spare row
        try:
            tiers.append({"threshold": int(threshold.replace(",", "").strip()), "prices": prices})
        except ValueError as exc:
            raise admin.DefinitionError(
                "A tier threshold must be a whole number of input tokens."
            ) from exc
    fields["tiers"] = tiers
    return fields


def _form_values(row: ModelDefinition | None, submitted=None) -> dict:
    """What the edit form shows: the stored definition, or the user's last input on error."""
    if submitted is not None:
        tiers = [
            {
                "threshold": t,
                **{k: (submitted.getlist(f"tier_{k}") + [""] * 9)[i] for k, _ in PRICE_KEYS},
            }
            for i, t in enumerate(submitted.getlist("tier_threshold"))
        ]
        return {
            "model_name": submitted.get("model_name", ""),
            "match_pattern": submitted.get("match_pattern", ""),
            "tokenizer": submitted.get("tokenizer", ""),
            "valid_from": submitted.get("valid_from", ""),
            "prices": {k: submitted.get(f"price_{k}", "") for k, _ in PRICE_KEYS},
            "tiers": tiers,
        }
    if row is None:
        return {
            "model_name": "",
            "match_pattern": "",
            "tokenizer": "",
            "valid_from": "",
            "prices": {k: "" for k, _ in PRICE_KEYS},
            "tiers": [],
        }
    return {
        "model_name": row.model_name,
        "match_pattern": row.match_pattern,
        "tokenizer": row.tokenizer or "",
        "valid_from": row.valid_from.date().isoformat() if row.valid_from else "",
        "prices": {k: per_million((row.prices or {}).get(k)) for k, _ in PRICE_KEYS},
        "tiers": [
            {
                "threshold": t["threshold"],
                **{k: per_million(t["prices"].get(k)) for k, _ in PRICE_KEYS},
            }
            for t in (row.tiers or [])
        ],
    }


def _matches_any(pattern: str, names: list[str]) -> bool:
    try:
        regex = re.compile(pattern)
    except re.error:
        return False
    return any(regex.search(n) for n in names)


def _page(request, db, access, *, editing=None, values=None, error=None, notice="", status=200):
    """The list, with the edit form open when `values` is given. `editing` is the row being
    edited (None means a new definition)."""
    project_id = access.project.id
    rows = admin.list_definitions(db, project_id)
    builtin_names = [r.model_name for r in rows if r.project_id is None]
    overrides = {
        r.id
        for r in rows
        if r.project_id == project_id and _matches_any(r.match_pattern, builtin_names)
    }
    context = page_context(
        db,
        access,
        settings_tabs=settings_tabs(project_id, "models", access.role),
        definitions=rows,
        overrides=overrides,
        price_keys=PRICE_KEYS,
        form=values,
        editing=editing,
        error=error,
        notice=NOTES.get(notice, ""),
    )
    return render(request, "settings/models.html", context, status=status)


@router.get("/project/{project_id}/settings/models", response_class=HTMLResponse)
def models_page(
    request: Request,
    edit: str = "",
    new: int = 0,
    override: str = "",
    notice: str = "",
    access: Access = Depends(require(Perm.MODEL_DEFINITIONS)),
    db: Session = Depends(get_db),
):
    if edit:
        row = db.get(ModelDefinition, edit)
        if row is None or row.project_id != access.project.id:
            raise HTTPException(404, "Not found.")
        return _page(request, db, access, editing=row, values=_form_values(row))
    if override:  # start a project definition from a built-in one
        base = db.get(ModelDefinition, override)
        if base is None or base.project_id not in (None, access.project.id):
            raise HTTPException(404, "Not found.")
        return _page(request, db, access, values=_form_values(base))
    if new:
        return _page(request, db, access, values=_form_values(None))
    return _page(request, db, access, notice=notice)


async def _save(request, db, access, container, definition_id: str | None):
    form = await request.form()
    editing = db.get(ModelDefinition, definition_id) if definition_id else None
    try:
        fields = _parse_form(form)
        if definition_id:
            row = admin.update_definition(
                db, container.models, access.project.id, definition_id, **fields
            )
            action = "model_definition.updated"
        else:
            row = admin.create_definition(db, container.models, access.project.id, **fields)
            action = "model_definition.created"
    except admin.DefinitionNotFound as exc:
        raise HTTPException(404, "Not found.") from exc
    except admin.DefinitionError as exc:
        db.rollback()
        return _page(
            request,
            db,
            access,
            editing=editing,
            values=_form_values(editing, form),
            error=str(exc),
            status=400,
        )
    audit.record(
        db,
        action=action,
        actor_type="user",
        actor_id=access.user.id,
        org_id=access.org.id,
        project_id=access.project.id,
        target=row.id,
        detail={"model_name": row.model_name},
    )
    db.commit()
    return RedirectResponse(
        f"/project/{access.project.id}/settings/models?notice=saved", status_code=303
    )


@router.post("/project/{project_id}/settings/models", response_class=HTMLResponse)
async def create_definition(
    request: Request,
    access: Access = Depends(require(Perm.MODEL_DEFINITIONS)),
    container: Container = Depends(get_container),
    db: Session = Depends(get_db),
):
    return await _save(request, db, access, container, None)


@router.post("/project/{project_id}/settings/models/{definition_id}", response_class=HTMLResponse)
async def update_definition(
    request: Request,
    definition_id: str,
    access: Access = Depends(require(Perm.MODEL_DEFINITIONS)),
    container: Container = Depends(get_container),
    db: Session = Depends(get_db),
):
    return await _save(request, db, access, container, definition_id)


@router.post("/project/{project_id}/settings/models/{definition_id}/delete")
def delete_definition(
    definition_id: str,
    access: Access = Depends(require(Perm.MODEL_DEFINITIONS)),
    container: Container = Depends(get_container),
    db: Session = Depends(get_db),
):
    try:
        row = db.get(ModelDefinition, definition_id)
        name = row.model_name if row else ""
        admin.delete_definition(db, container.models, access.project.id, definition_id)
    except admin.DefinitionNotFound as exc:
        raise HTTPException(404, "Not found.") from exc
    audit.record(
        db,
        action="model_definition.deleted",
        actor_type="user",
        actor_id=access.user.id,
        org_id=access.org.id,
        project_id=access.project.id,
        target=definition_id,
        detail={"model_name": name},
    )
    db.commit()
    return RedirectResponse(
        f"/project/{access.project.id}/settings/models?notice=deleted", status_code=303
    )
