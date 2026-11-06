"""Playground API: streamed runs, save-as-version and open-from-trace pre-fill.

`POST /run` is a plain `def` route returning a `StreamingResponse` over a sync generator, so the
provider SDK's blocking iterator runs in the thread pool and never on the event loop. Provider
output travels as JSON inside SSE events; the page renders it with `textContent`. The route
decrypts the connection while it still holds its DB session; the stream itself never touches it.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from traceloom.api.errors import error_body
from traceloom.auth.deps import Access, get_container, require
from traceloom.auth.permissions import Perm
from traceloom.container import Container
from traceloom.db.models import Prompt
from traceloom.db.session import get_db
from traceloom.llm import connections, prefill
from traceloom.llm.connections import ConnectionInvalid, ConnectionNotFound, ConnectionPolicy
from traceloom.llm.models import models_for
from traceloom.llm.run import Event, Run, RunSpec
from traceloom.prompts import service as prompts
from traceloom.prompts.service import Actor, NotAllowed, PromptConflict, PromptError
from traceloom.webhooks import deliver

router = APIRouter(tags=["playground"])

MAX_MESSAGES = 200
MAX_CONTENT = 200_000


class Message(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str = Field("", max_length=MAX_CONTENT)
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None


class RunBody(BaseModel):
    connection_id: str
    model: str = Field(min_length=1, max_length=200)
    messages: list[Message] = Field(max_length=MAX_MESSAGES)
    variables: dict[str, str] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)
    tools: list[dict[str, Any]] = Field(default_factory=list, max_length=64)
    response_schema: dict[str, Any] | None = None
    trace: bool = False
    prompt_name: str | None = Field(None, max_length=200)
    prompt_version: int | None = None


class SaveBody(BaseModel):
    name: str
    messages: list[Message] = Field(max_length=MAX_MESSAGES)
    commit_message: str
    mode: Literal["auto", "new", "version"] = (
        "auto"  # new: the name must be free; version: it must exist
    )
    model: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    tools: list[dict[str, Any]] = Field(default_factory=list, max_length=64)
    labels: list[str] = Field(default_factory=list)


def sse(event: Event) -> bytes:
    return f"event: {event.name}\ndata: {json.dumps(event.data, default=str)}\n\n".encode()


def _policy(container: Container) -> ConnectionPolicy:
    return ConnectionPolicy.from_settings(container.settings)


def _actor(access: Access) -> Actor:
    return Actor.for_user(access.user, access.role)


def _limit(container: Container, access: Access) -> JSONResponse | None:
    """None when allowed, else the 429. It is always the JSON envelope: this endpoint is called by
    the page's fetch, whatever `Accept` it sent."""
    result = container.limiter.hit(
        f"playground:{access.user.id}:{access.project.id}", container.settings.playground_rate
    )
    if not result.allowed:
        message = (
            f"Playground limit reached ({container.settings.playground_rate}). "
            "Wait a moment and run again."
        )
        return JSONResponse(
            error_body(429, message),
            status_code=429,
            headers={"Retry-After": str(max(1, round(result.retry_after)))},
        )
    return None


@router.post("/project/{project_id}/playground/run")
def run(
    request: Request,
    body: RunBody,
    access: Access = Depends(require(Perm.PLAYGROUND_RUN)),
    container: Container = Depends(get_container),
    db: Session = Depends(get_db),
) -> Response:
    if (limited := _limit(container, access)) is not None:
        return limited
    policy = _policy(container)
    try:
        provider = connections.open_provider(
            db, access.project, _actor(access), policy, body.connection_id
        )
        row = connections.get(db, access.project, body.connection_id)
        cfg = connections.provider_config(policy, row)
    except ConnectionNotFound:
        raise HTTPException(404, "Connection not found.") from None
    except ConnectionInvalid as exc:
        raise HTTPException(400, str(exc)) from None
    spec = RunSpec(
        messages=[m.model_dump(exclude_none=True) for m in body.messages],
        model=body.model,
        params=body.params,
        tools=body.tools,
        response_schema=body.response_schema,
        variables=body.variables,
        trace=body.trace,
        prompt_name=body.prompt_name,
        prompt_version=body.prompt_version,
        secrets=[cfg.api_key, *cfg.secrets.values(), *(cfg.config.get("headers") or {}).values()],
    )
    active = Run(container, access.project.id, provider, spec)

    async def stream() -> AsyncIterator[bytes]:
        # Each blocking pull from the provider SDK runs in the thread pool; between pulls the
        # loop checks for a disconnect, because under newer servers a closed socket only shows
        # as a failed write (and Starlette then skips background tasks). `finally` always stops
        # the run, which closes the provider stream.
        events = active.events()
        try:
            while True:
                event = await run_in_threadpool(next, events, None)
                if event is None or await request.is_disconnected():
                    break
                yield sse(event)
        finally:
            await run_in_threadpool(active.cancel)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        background=BackgroundTask(active.cancel),
    )


@router.post("/project/{project_id}/playground/save")
def save(
    body: SaveBody,
    access: Access = Depends(require(Perm.PLAYGROUND_RUN)),
    container: Container = Depends(get_container),
    db: Session = Depends(get_db),
) -> JSONResponse:
    config: dict[str, Any] = {
        k: v for k, v in (("model", body.model), ("params", body.params)) if v
    }
    if body.tools:
        config["tools"] = body.tools
    exists = (
        db.scalar(
            select(Prompt.id).where(
                Prompt.project_id == access.project.id, Prompt.name == body.name
            )
        )
        is not None
    )
    if body.mode == "new" and exists:
        raise HTTPException(
            400, f"A prompt named {body.name} already exists. Save it as a new version instead."
        )
    if body.mode == "version" and not exists:
        raise HTTPException(400, f"There is no prompt named {body.name} to add a version to.")
    try:
        row = prompts.create_version(
            db,
            access.project,
            _actor(access),
            name=body.name,
            type="chat",
            content=[{"role": m.role, "content": m.content} for m in body.messages],
            commit_message=body.commit_message,
            config=config,
            labels=body.labels,
        )
        db.commit()
    except NotAllowed as exc:
        db.rollback()
        raise HTTPException(403, str(exc)) from None
    except PromptError as exc:
        db.rollback()
        raise HTTPException(409 if isinstance(exc, PromptConflict) else 400, str(exc)) from None
    deliver.kick(container)  # webhooks for the new version go out now
    url = f"/project/{access.project.id}/prompts/{quote(body.name, safe='/')}?version={row.version}"
    return JSONResponse({"name": body.name, "version": row.version, "url": url}, status_code=201)


@router.get("/project/{project_id}/playground/from-observation/{obs_id}")
def from_observation(
    obs_id: str,
    trace_id: str = Query(min_length=1),
    access: Access = Depends(require(Perm.PLAYGROUND_RUN)),
    container: Container = Depends(get_container),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Pre-fill data for a traced generation: its exact messages, model, parameters and tools,
    and the project's connection for that provider when there is one."""
    detail = container.trace_store.get_trace(access.project.id, trace_id)
    obs = next((o for o in detail.observations if o.id == obs_id), None) if detail else None
    if obs is None:
        raise HTTPException(404, "Not found.")
    fill = prefill.from_observation(obs)
    usable = connections.list_usable(db, access.project, _actor(access), _policy(container))
    match = next((u for u in usable if u.provider == fill["provider"]), None)
    fill["connection_id"] = match.id if match else None
    fill["models"] = models_for(fill["provider"]) if fill["provider"] else []
    fill["connections"] = [
        {"id": u.id, "name": u.name, "provider": u.provider, "models": u.models} for u in usable
    ]
    return fill


__all__ = ["router"]
