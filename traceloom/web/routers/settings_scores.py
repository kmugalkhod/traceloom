"""Settings > Score configs: what a score may be (type, range, categories). Admin edits."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from traceloom.auth.deps import Access, form_data, get_container, require
from traceloom.auth.permissions import Perm, can
from traceloom.container import Container
from traceloom.db.models import ScoreConfig
from traceloom.db.session import get_db
from traceloom.scores import configs as score_configs
from traceloom.scores.configs import ConfigError, ConfigNotFound, config_cache
from traceloom.services import audit
from traceloom.web.context import page_context, settings_tabs
from traceloom.web.templating import render

router = APIRouter(tags=["score-configs"])

TYPE_LABELS = {"NUMERIC": "Numeric", "CATEGORICAL": "Categorical", "BOOLEAN": "Boolean"}
NOTES = {
    "created": "Score config created.",
    "saved": "Score config saved.",
    "archived": "Score config archived. Its scores stay on their traces.",
    "unarchived": "Score config restored.",
}
MIN_CATEGORY_ROWS = 3


@dataclass
class FormState:
    """A dialog to reopen with the user's values and a message."""

    scope: str  # "new" or a config id
    message: str
    values: dict[str, Any] = field(default_factory=dict)


def _number(text: str, label: str) -> float | None:
    text = (text or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        raise ConfigError(f"{label} must be a number.") from None


def _form_values(form: FormData) -> dict[str, Any]:
    """What the dialog shows again after an error."""
    labels = [str(v) for v in form.getlist("cat_label")]
    numbers = [str(v) for v in form.getlist("cat_value")]
    return {
        "name": str(form.get("name", "")),
        "data_type": str(form.get("data_type", "NUMERIC")),
        "min_value": str(form.get("min_value", "")),
        "max_value": str(form.get("max_value", "")),
        "description": str(form.get("description", "")),
        "public_feedback": form.get("public_feedback") is not None,
        "categories": list(zip(labels, numbers, strict=False)),
    }


def _fields(values: dict[str, Any]) -> dict[str, Any]:
    """Config fields from the dialog's text. Raises ConfigError for a number that is not one."""
    categories = []
    for index, (label, number) in enumerate(values["categories"], start=1):
        label, number = label.strip(), number.strip()
        if not label and not number:
            continue  # an unused row
        value = _number(number, f"Category {index} value")
        if not label or value is None:
            raise ConfigError(f"Category {index} needs a label and a number.")
        categories.append({"label": label, "value": value})
    return {
        "name": values["name"],
        "data_type": values["data_type"],
        "min_value": _number(values["min_value"], "The minimum"),
        "max_value": _number(values["max_value"], "The maximum"),
        "categories": categories,
        "description": values["description"],
        "public_feedback": values["public_feedback"],
    }


def _describe(config: ScoreConfig) -> dict[str, Any]:
    """Display and form values for one config."""
    if config.data_type == "NUMERIC":
        lo, hi = config.min_value, config.max_value
        if lo is not None and hi is not None:
            shape = f"{lo:g} to {hi:g}"
        elif lo is not None:
            shape = f"{lo:g} or more"
        elif hi is not None:
            shape = f"{hi:g} or less"
        else:
            shape = "Any number"
    elif config.data_type == "BOOLEAN":
        shape = "true, false"
    else:
        shape = ""
    return {
        "config": config,
        "type_label": TYPE_LABELS[config.data_type],
        "shape": shape,
        "chips": [f"{c['label']} = {c['value']:g}" for c in config.categories or []],
        "values": {
            "name": config.name,
            "data_type": config.data_type,
            "min_value": "" if config.min_value is None else f"{config.min_value:g}",
            "max_value": "" if config.max_value is None else f"{config.max_value:g}",
            "description": config.description,
            "public_feedback": config.public_feedback,
            "categories": [(c["label"], f"{c['value']:g}") for c in config.categories or []],
        },
    }


def _page(
    request: Request,
    db: Session,
    access: Access,
    container: Container,
    *,
    show: str = "active",
    notice: str = "",
    error: FormState | None = None,
    status: int = 200,
):
    project_id = access.project.id
    show = show if show in ("active", "archived") else "active"
    everything = score_configs.list_configs(db, project_id, include_archived=True)
    counts = container.trace_store.score_counts(project_id, [c.id for c in everything])
    rows = [
        {**_describe(c), "scores": counts.get(c.id, 0)}
        for c in everything
        if (c.archived_at is None) == (show == "active")
    ]
    context = page_context(
        db,
        access,
        settings_tabs=settings_tabs(project_id, "scores", access.role),
        rows=rows,
        show=show,
        active_count=sum(1 for c in everything if c.archived_at is None),
        archived_count=sum(1 for c in everything if c.archived_at is not None),
        can_edit=can(access.role, Perm.SCORE_CONFIGS),
        notice=NOTES.get(notice, ""),
        error=error,
        base=f"/project/{project_id}/settings/scores",
        type_labels=TYPE_LABELS,
        blank=_describe(ScoreConfig(name="", data_type="NUMERIC", description=""))["values"],
        min_category_rows=MIN_CATEGORY_ROWS,
    )
    return render(request, "settings/scores.html", context, status=status)


@router.get("/project/{project_id}/settings/scores", response_class=HTMLResponse)
def scores_page(
    request: Request,
    show: str = "active",
    notice: str = "",
    access: Access = Depends(require(Perm.EVAL_READ)),
    container: Container = Depends(get_container),
    db: Session = Depends(get_db),
):
    return _page(request, db, access, container, show=show, notice=notice)


def _audit(db: Session, access: Access, action: str, config: ScoreConfig, **detail: Any) -> None:
    audit.record(
        db,
        action=action,
        actor_type="user",
        actor_id=access.user.id,
        org_id=access.org.id,
        project_id=access.project.id,
        target=config.id,
        detail={"name": config.name, **detail},
    )
    db.commit()


@router.post("/project/{project_id}/settings/scores", response_class=HTMLResponse)
def create_config(
    request: Request,
    access: Access = Depends(require(Perm.SCORE_CONFIGS)),
    container: Container = Depends(get_container),
    form: FormData = Depends(form_data),
    db: Session = Depends(get_db),
):
    values = _form_values(form)
    try:
        config = score_configs.create_config(
            db,
            access.project.id,
            created_by=access.user.id,
            cache=config_cache(container),
            **_fields(values),
        )
    except ConfigError as exc:
        db.rollback()
        return _page(
            request, db, access, container, error=FormState("new", str(exc), values), status=400
        )
    _audit(db, access, "score_config.create", config, data_type=config.data_type)
    return RedirectResponse(f"/project/{access.project.id}/settings/scores?notice=created", 303)


@router.post("/project/{project_id}/settings/scores/{config_id}", response_class=HTMLResponse)
def update_config(
    request: Request,
    config_id: str,
    access: Access = Depends(require(Perm.SCORE_CONFIGS)),
    container: Container = Depends(get_container),
    form: FormData = Depends(form_data),
    db: Session = Depends(get_db),
):
    values = _form_values(form)
    store = container.trace_store
    try:
        changes = _fields(values)
        config = score_configs.update_config(
            db,
            access.project.id,
            config_id,
            changes,
            in_use=store.config_has_scores,
            cache=config_cache(container),
        )
    except ConfigNotFound as exc:
        raise HTTPException(404, "Not found.") from exc
    except ConfigError as exc:
        db.rollback()
        return _page(
            request, db, access, container, error=FormState(config_id, str(exc), values), status=400
        )
    _audit(db, access, "score_config.update", config)
    return RedirectResponse(f"/project/{access.project.id}/settings/scores?notice=saved", 303)


def _archive(
    request: Request,
    config_id: str,
    archived: bool,
    access: Access,
    container: Container,
    db: Session,
):
    try:
        config = score_configs.set_archived(
            db, access.project.id, config_id, archived, cache=config_cache(container)
        )
    except ConfigNotFound as exc:
        raise HTTPException(404, "Not found.") from exc
    except ConfigError as exc:
        db.rollback()
        # Unarchiving fails when another live config took the name: say so on the page.
        return _page(
            request,
            db,
            access,
            container,
            show="archived",
            error=FormState("page", str(exc)),
            status=409,
        )
    action = "score_config.archive" if archived else "score_config.unarchive"
    _audit(db, access, action, config)
    note = "archived" if archived else "unarchived"
    target = "" if archived else "?show=archived"
    sep = "&" if target else "?"
    return RedirectResponse(
        f"/project/{access.project.id}/settings/scores{target}{sep}notice={note}", 303
    )


@router.post("/project/{project_id}/settings/scores/{config_id}/archive")
def archive_config(
    request: Request,
    config_id: str,
    access: Access = Depends(require(Perm.SCORE_CONFIGS)),
    container: Container = Depends(get_container),
    db: Session = Depends(get_db),
):
    return _archive(request, config_id, True, access, container, db)


@router.post("/project/{project_id}/settings/scores/{config_id}/unarchive")
def unarchive_config(
    request: Request,
    config_id: str,
    access: Access = Depends(require(Perm.SCORE_CONFIGS)),
    container: Container = Depends(get_container),
    db: Session = Depends(get_db),
):
    return _archive(request, config_id, False, access, container, db)
