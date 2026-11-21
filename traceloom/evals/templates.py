"""Judge templates (FR-J1, J2): immutable versions, read-only built-ins that can be cloned.

Every function takes the project id and only sees that project's templates plus the built-ins
(`project_id IS NULL`). Writes flush but leave the commit to the caller. Permission checks
(Perm.EVAL_TEMPLATES_WRITE) belong to the caller, as for the score configs.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from traceloom.db.models_eval import EvalTemplate, EvalTemplateVersion
from traceloom.prompts.variables import find_variables

STARTER_PATH = Path(__file__).resolve().parent / "starter.yaml"
SCORE_TYPES = ("numeric", "boolean", "categorical")
NAME_MAX = 200
PROMPT_MAX = 100_000
_VERSION_RETRIES = 5


class TemplateError(ValueError):
    """The template cannot be saved; the message is safe to show."""


class TemplateNotFound(LookupError):
    pass


class BuiltinReadOnly(PermissionError):
    """Built-in templates cannot be edited; clone one instead."""


@dataclass(frozen=True)
class TemplateView:
    template: EvalTemplate
    latest: EvalTemplateVersion

    @property
    def builtin(self) -> bool:
        return self.template.project_id is None

    @property
    def variables(self) -> list[str]:
        return find_variables(self.latest.prompt)


# --- Validation --------------------------------------------------------------------------------


def validate_output_schema(schema: Any) -> dict[str, Any]:
    """The score's shape: numeric (min < max), boolean, or categorical (distinct labels)."""
    if not isinstance(schema, Mapping) or schema.get("type") not in SCORE_TYPES:
        raise TemplateError(f"output schema type must be one of {', '.join(SCORE_TYPES)}")
    kind = schema["type"]
    if kind == "numeric":
        lo, hi = schema.get("min"), schema.get("max")
        numbers = all(isinstance(x, int | float) and not isinstance(x, bool) for x in (lo, hi))
        if not numbers or lo >= hi:
            raise TemplateError("a numeric output schema needs min and max, with min below max")
        return {"type": kind, "min": lo, "max": hi}
    if kind == "categorical":
        labels = schema.get("categories")
        if (
            not isinstance(labels, list)
            or len(labels) < 2
            or not all(isinstance(c, str) and c.strip() for c in labels)
            or len(set(labels)) != len(labels)
        ):
            raise TemplateError("a categorical output schema needs at least two distinct labels")
        return {"type": kind, "categories": list(labels)}
    return {"type": kind}


def _clean(
    name: str | None, prompt: str, output_schema: Any, params: Mapping[str, Any] | None
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    if not isinstance(prompt, str) or not prompt.strip():
        raise TemplateError("the prompt is required")
    if len(prompt) > PROMPT_MAX:
        raise TemplateError(f"the prompt is longer than {PROMPT_MAX} characters")
    if name is not None and not (name.strip() and len(name) <= NAME_MAX):
        raise TemplateError(f"the name is required and at most {NAME_MAX} characters")
    return prompt, validate_output_schema(output_schema), dict(params or {})


# --- Reads -------------------------------------------------------------------------------------


def _visible(project_id: str):
    return or_(EvalTemplate.project_id == project_id, EvalTemplate.project_id.is_(None))


def get_template(db: Session, project_id: str, template_id: str) -> EvalTemplate:
    template = db.scalar(
        select(EvalTemplate).where(EvalTemplate.id == template_id, _visible(project_id))
    )
    if template is None:
        raise TemplateNotFound(template_id)
    return template


def versions(db: Session, template_id: str) -> list[EvalTemplateVersion]:
    """Newest first."""
    return list(
        db.scalars(
            select(EvalTemplateVersion)
            .where(EvalTemplateVersion.template_id == template_id)
            .order_by(EvalTemplateVersion.version.desc())
        )
    )


def get_version(db: Session, template_id: str, version: int | None = None) -> EvalTemplateVersion:
    """One version, or the latest when `version` is None (a rule that follows the latest)."""
    query = select(EvalTemplateVersion).where(EvalTemplateVersion.template_id == template_id)
    if version is not None:
        query = query.where(EvalTemplateVersion.version == version)
    found = db.scalar(query.order_by(EvalTemplateVersion.version.desc()).limit(1))
    if found is None:
        raise TemplateNotFound(f"{template_id} v{version}" if version else template_id)
    return found


def list_templates(db: Session, project_id: str) -> list[TemplateView]:
    """Built-ins first, then the project's templates by name, each with its latest version."""
    out = []
    for template in db.scalars(select(EvalTemplate).where(_visible(project_id))):
        out.append(TemplateView(template, get_version(db, template.id)))
    return sorted(out, key=lambda v: (not v.builtin, v.template.name))


# --- Writes ------------------------------------------------------------------------------------


def _add_version(
    db: Session,
    template: EvalTemplate,
    *,
    prompt: str,
    output_schema: dict[str, Any],
    params: dict[str, Any],
    connection_id: str | None,
    model: str | None,
    created_by: str | None,
) -> EvalTemplateVersion:
    """Append the next version. A concurrent save may take the number first; retry on it."""
    for _ in range(_VERSION_RETRIES):
        latest = db.scalar(
            select(EvalTemplateVersion.version)
            .where(EvalTemplateVersion.template_id == template.id)
            .order_by(EvalTemplateVersion.version.desc())
            .limit(1)
        )
        row = EvalTemplateVersion(
            template_id=template.id,
            version=(latest or 0) + 1,
            prompt=prompt,
            output_schema=output_schema,
            params=params,
            connection_id=connection_id,
            model=model,
            created_by=created_by,
        )
        try:
            with db.begin_nested():
                db.add(row)
        except IntegrityError:
            continue
        return row
    raise TemplateError("the template was saved concurrently; try again")


def create_template(
    db: Session,
    project_id: str,
    name: str,
    prompt: str,
    output_schema: Mapping[str, Any],
    *,
    params: Mapping[str, Any] | None = None,
    connection_id: str | None = None,
    model: str | None = None,
    created_by: str | None = None,
) -> TemplateView:
    prompt, schema, params_ = _clean(name, prompt, output_schema, params)
    template = EvalTemplate(project_id=project_id, name=name.strip())
    try:
        with db.begin_nested():
            db.add(template)
    except IntegrityError:
        raise TemplateError(f"a template named {name.strip()!r} already exists") from None
    version = _add_version(
        db,
        template,
        prompt=prompt,
        output_schema=schema,
        params=params_,
        connection_id=connection_id,
        model=model,
        created_by=created_by,
    )
    return TemplateView(template, version)


def save_version(
    db: Session,
    project_id: str,
    template_id: str,
    prompt: str,
    output_schema: Mapping[str, Any],
    *,
    params: Mapping[str, Any] | None = None,
    connection_id: str | None = None,
    model: str | None = None,
    created_by: str | None = None,
) -> EvalTemplateVersion:
    """Every save is a new immutable version; existing versions are never touched."""
    template = get_template(db, project_id, template_id)
    if template.project_id is None:
        raise BuiltinReadOnly("built-in templates cannot be edited; clone it first")
    prompt, schema, params_ = _clean(None, prompt, output_schema, params)
    return _add_version(
        db,
        template,
        prompt=prompt,
        output_schema=schema,
        params=params_,
        connection_id=connection_id,
        model=model,
        created_by=created_by,
    )


def clone(
    db: Session,
    project_id: str,
    template_id: str,
    name: str | None = None,
    *,
    created_by: str | None = None,
) -> TemplateView:
    """Copy the latest version of any visible template (built-ins included) into the project."""
    source = get_template(db, project_id, template_id)
    latest = get_version(db, source.id)
    return create_template(
        db,
        project_id,
        name or f"{source.name} (copy)",
        latest.prompt,
        latest.output_schema,
        params=latest.params,
        connection_id=latest.connection_id,
        model=latest.model,
        created_by=created_by,
    )


# --- Built-ins ---------------------------------------------------------------------------------


def load_starter(path: Path = STARTER_PATH) -> list[dict[str, Any]]:
    entries = yaml.safe_load(path.read_text())["templates"]
    for entry in entries:  # fail startup on a broken file, not when a user picks the template
        _clean(entry["name"], entry["prompt"], entry["output_schema"], entry.get("params"))
    return entries


def seed_builtins(db: Session, path: Path = STARTER_PATH) -> int:
    """Create the starter templates, and add a version when the shipped wording changed.
    Safe to run at every startup. Returns how many templates were created or revised."""
    changed = 0
    for entry in load_starter(path):
        prompt = entry["prompt"].strip()
        schema, params = validate_output_schema(entry["output_schema"]), entry.get("params") or {}
        template = db.scalar(
            select(EvalTemplate).where(
                EvalTemplate.project_id.is_(None), EvalTemplate.name == entry["name"]
            )
        )
        if template is None:
            template = EvalTemplate(project_id=None, name=entry["name"])
            db.add(template)
            db.flush()
        else:
            current = get_version(db, template.id)
            if (current.prompt, current.output_schema, current.params) == (prompt, schema, params):
                continue
        _add_version(
            db,
            template,
            prompt=prompt,
            output_schema=schema,
            params=params,
            connection_id=None,
            model=None,
            created_by=None,
        )
        changed += 1
    return changed
