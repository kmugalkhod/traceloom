"""Project model-definition CRUD for the Models settings page (Admin)."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from sqlalchemy import event, select
from sqlalchemy.orm import Session

from traceloom.cost.registry import ModelRegistry
from traceloom.db.models import ModelDefinition


def _invalidate_after_commit(db: Session, registry: ModelRegistry, project_id: str) -> None:
    """Drop the registry cache only once the change is visible to other sessions."""
    event.listen(db, "after_commit", lambda _: registry.invalidate(project_id), once=True)


class DefinitionError(ValueError):
    pass


class DefinitionNotFound(LookupError):
    pass


def _prices(value: Any, label: str) -> dict[str, float]:
    if not isinstance(value, dict) or not value:
        raise DefinitionError(f"{label} must be a non-empty map of usage key to USD per token.")
    out = {}
    for key, price in value.items():
        if isinstance(price, bool) or not isinstance(price, int | float) or price < 0:
            raise DefinitionError(f"{label}.{key} must be a non-negative number.")
        out[str(key)] = float(price)
    return out


def _tiers(value: Any) -> list[dict[str, Any]]:
    tiers = []
    for tier in value or []:
        threshold = tier.get("threshold") if isinstance(tier, dict) else None
        if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold <= 0:
            raise DefinitionError("tier threshold must be a positive integer.")
        tiers.append({"threshold": threshold, "prices": _prices(tier.get("prices"), "tier prices")})
    return sorted(tiers, key=lambda t: t["threshold"])


def _validated(fields: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if "model_name" in fields:
        name = (fields["model_name"] or "").strip()
        if not name:
            raise DefinitionError("model_name is required.")
        out["model_name"] = name
    if "match_pattern" in fields:
        pattern = fields["match_pattern"] or ""
        try:
            re.compile(pattern)
        except re.error as exc:
            raise DefinitionError(f"match_pattern is not a valid regex: {exc}") from exc
        if not pattern:
            raise DefinitionError("match_pattern is required.")
        out["match_pattern"] = pattern
    if "prices" in fields:
        out["prices"] = _prices(fields["prices"], "prices")
    if "tiers" in fields:
        out["tiers"] = _tiers(fields["tiers"])
    if "tokenizer" in fields:
        out["tokenizer"] = fields["tokenizer"] or None
    if "valid_from" in fields:
        valid_from = fields["valid_from"]
        if valid_from is not None and (
            not isinstance(valid_from, datetime) or valid_from.tzinfo is None
        ):
            raise DefinitionError("valid_from must be a timezone-aware datetime.")
        out["valid_from"] = valid_from
    return out


def list_definitions(db: Session, project_id: str) -> list[ModelDefinition]:
    """The project's own definitions first, then the global ones (read-only)."""
    rows = db.scalars(
        select(ModelDefinition).where(
            (ModelDefinition.project_id == project_id) | ModelDefinition.project_id.is_(None)
        )
    ).all()
    return sorted(rows, key=lambda r: (r.project_id is None, r.model_name, r.valid_from or 0))


def _own(db: Session, project_id: str, definition_id: str) -> ModelDefinition:
    row = db.get(ModelDefinition, definition_id)
    if row is None or row.project_id != project_id:  # globals and other projects: not editable
        raise DefinitionNotFound(definition_id)
    return row


def create_definition(
    db: Session, registry: ModelRegistry, project_id: str, **fields: Any
) -> ModelDefinition:
    for required in ("model_name", "match_pattern", "prices"):
        fields.setdefault(required, None)
    row = ModelDefinition(project_id=project_id, source="user", **_validated(fields))
    db.add(row)
    db.flush()
    _invalidate_after_commit(db, registry, project_id)
    return row


def update_definition(
    db: Session, registry: ModelRegistry, project_id: str, definition_id: str, **fields: Any
) -> ModelDefinition:
    row = _own(db, project_id, definition_id)
    for key, value in _validated(fields).items():
        setattr(row, key, value)
    db.flush()
    _invalidate_after_commit(db, registry, project_id)
    return row


def delete_definition(
    db: Session, registry: ModelRegistry, project_id: str, definition_id: str
) -> None:
    db.delete(_own(db, project_id, definition_id))
    db.flush()
    _invalidate_after_commit(db, registry, project_id)
