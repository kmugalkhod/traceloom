"""Public prompt API (FR-P9), under HTTP Basic project keys like ingestion.

Routes are plain `def`: the database work runs in the threadpool, not on the event loop. Names
containing `/` may be URL-encoded (`support%2Ftone`). Errors use the Phase 1 envelope. Write bodies
are read with a size limit before they are parsed (413 beyond `MAX_BODY_BYTES`); a write that
lost a race to a concurrent one is a 409. Writes commit before returning and then hand any
webhook deliveries they created to the queue, rather than waiting for the next sweep.
"""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.orm import Session

from traceloom.api.ingestion import read_body
from traceloom.auth.api_keys import ResolvedKey
from traceloom.auth.deps import get_container, project_key
from traceloom.container import Container
from traceloom.db.models import Project
from traceloom.db.session import get_db
from traceloom.ingest.accept import BatchTooLarge
from traceloom.prompts import service
from traceloom.prompts.service import (
    Actor,
    NotAllowed,
    PromptConflict,
    PromptError,
    PromptNotFound,
    PromptView,
)
from traceloom.webhooks import deliver

router = APIRouter(prefix="/api/public/v2", tags=["prompts"])

# A version holds at most service.MAX_CONTENT_CHARS characters; leave room for JSON escaping
# (up to 6 bytes for one character) plus config, tags and labels.
MAX_BODY_BYTES = 2_000_000


class CreatePrompt(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    name: str
    type: str | None = None
    prompt: Any
    config: dict[str, Any] | None = None
    labels: list[str] = Field(default_factory=list)
    tags: list[str] | None = None
    commit_message: str = Field("", alias="commitMessage")


class SetLabels(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    new_labels: list[str] = Field(alias="newLabels")


def _json_body[M: BaseModel](model: type[M]) -> Callable[[Request], Awaitable[M]]:
    """A dependency that reads at most MAX_BODY_BYTES and parses them as `model`. FastAPI would
    otherwise buffer and parse a body of any size before the route runs."""

    async def parse(request: Request) -> M:
        try:
            raw = await read_body(request, MAX_BODY_BYTES)
        except BatchTooLarge as exc:
            raise HTTPException(413, str(exc)) from None
        try:
            return model.model_validate_json(raw)
        except ValidationError as exc:
            raise RequestValidationError(exc.errors(include_url=False)) from None

    return parse


def _context(key: ResolvedKey, db: Session) -> tuple[Project, Actor]:
    project = db.get(Project, key.project_id)  # the key's own project, never one from the URL
    if project is None:
        raise HTTPException(401, "Authentication required.")
    return project, Actor.for_key(key.key_id, may_move_protected=key.may_move_protected_labels)


def _fail(exc: Exception) -> HTTPException:
    if isinstance(exc, NotAllowed):
        return HTTPException(403, str(exc))
    if isinstance(exc, PromptNotFound):
        return HTTPException(404, str(exc))
    if isinstance(exc, PromptConflict):
        return HTTPException(409, str(exc))
    return HTTPException(400, str(exc))


def _body(view: PromptView) -> dict[str, Any]:
    return {
        "name": view.name,
        "type": view.type,
        "version": view.version,
        "prompt": view.prompt,
        "config": view.config,
        "labels": view.labels,
        "tags": view.tags,
        "commitMessage": view.commit_message,
        "dependencies": view.dependencies,
        "createdAt": view.created_at.isoformat(),
        "latestVersion": view.latest_version,
    }


@router.get("/prompts")
def list_prompts(
    name: str = "",
    label: str | None = None,
    tag: str | None = None,
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    key: ResolvedKey = Depends(project_key),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    project, actor = _context(key, db)
    try:
        result = service.list_prompts(
            db, project, actor, name=name, tag=tag, label=label, page=page, limit=limit
        )
    except (PromptError, NotAllowed) as exc:
        raise _fail(exc) from exc
    chunk, total = result.items, result.total
    return {
        "data": [
            {
                "name": m.name,
                "type": m.type,
                "tags": m.tags,
                "labels": sorted(m.labels),
                "versions": list(range(1, m.latest_version + 1)),
                "latestVersion": m.latest_version,
                "lastUpdatedAt": m.updated_at.isoformat(),
            }
            for m in chunk
        ],
        "meta": {
            "page": page,
            "limit": limit,
            "totalItems": total,
            "totalPages": math.ceil(total / limit),
        },
    }


@router.get("/prompts/{name:path}")
def get_prompt(
    name: str,
    label: str | None = None,
    version: int | None = Query(None, ge=1),
    resolve: bool = True,
    key: ResolvedKey = Depends(project_key),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    project, actor = _context(key, db)
    try:
        view = service.get(db, project, actor, name, label=label, version=version, resolve=resolve)
    except (PromptError, PromptNotFound, NotAllowed) as exc:
        raise _fail(exc) from exc
    return _body(view)


@router.post("/prompts", status_code=201)
def create_prompt(
    key: ResolvedKey = Depends(project_key),  # before the body: no key, no reading it
    body: CreatePrompt = Depends(_json_body(CreatePrompt)),
    container: Container = Depends(get_container),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    project, actor = _context(key, db)
    try:
        row = service.create_version(
            db,
            project,
            actor,
            name=body.name,
            type=body.type,
            content=body.prompt,
            config=body.config,
            commit_message=body.commit_message,
            tags=body.tags,
            labels=body.labels,
        )
        view = service.get(db, project, actor, body.name, version=row.version, resolve=False)
    except (PromptError, PromptNotFound, NotAllowed) as exc:
        raise _fail(exc) from exc  # the session rolls back, so nothing is half-saved
    db.commit()
    deliver.kick(container)  # webhooks for the new version (and labels) go out now
    return _body(view)


@router.patch("/prompts/{name:path}/versions/{version}")
def set_labels(
    name: str,
    version: int,
    key: ResolvedKey = Depends(project_key),  # before the body: no key, no reading it
    body: SetLabels = Depends(_json_body(SetLabels)),
    container: Container = Depends(get_container),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    project, actor = _context(key, db)
    try:
        for label in dict.fromkeys(body.new_labels):
            service.move_label(db, project, actor, name, label, version)
        view = service.get(db, project, actor, name, version=version, resolve=False)
    except (PromptError, PromptNotFound, NotAllowed) as exc:
        raise _fail(exc) from exc
    db.commit()
    deliver.kick(container)
    return _body(view)
