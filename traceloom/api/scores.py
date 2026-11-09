"""Public scores API (FR-S1..S3), under HTTP Basic project keys like ingestion.

`POST /api/public/scores` takes one score or an array of up to 100 and validates each against
its config; a batch with any invalid score writes nothing and answers 400 with every problem.
`GET` filters and pages, `DELETE` removes a score of the caller's own project, and
`GET /api/public/score-configs` lists the project's configs. Routes are plain `def`, so the
store work runs in the threadpool. Errors use the Phase 1 envelope.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import RootModel
from sqlalchemy.orm import Session

from traceloom.api.ingestion import json_body
from traceloom.auth.api_keys import ResolvedKey
from traceloom.auth.deps import enforce_ingest_rate_limit, get_container, project_key
from traceloom.container import Container
from traceloom.db.models import ScoreConfig
from traceloom.db.session import get_db
from traceloom.ingest.schemas import ScoreBody
from traceloom.ports import InvalidCursor, ScoreQuery, ScoreRow
from traceloom.scores import configs as score_configs
from traceloom.scores import service
from traceloom.scores.configs import config_cache
from traceloom.scores.service import ScoreError, ScoreNotFound

router = APIRouter(prefix="/api/public", tags=["scores"])

MAX_BATCH = 100
MAX_BODY_BYTES = 1_000_000
# Set by the server: a client cannot claim a queue, a judge run or the public-feedback channel.
SERVER_FIELDS = ("queue_id", "execution_id", "channel")


class ScorePayload(RootModel[ScoreBody | list[ScoreBody]]):
    pass


def _write_key(request: Request, key: ResolvedKey = Depends(project_key)) -> ResolvedKey:
    enforce_ingest_rate_limit(request, key, scope="scores")
    return key


def score_json(row: ScoreRow) -> dict[str, Any]:
    if row.data_type == "CATEGORICAL":
        value: Any = row.string_value
    elif row.data_type == "BOOLEAN":
        value = None if row.value is None else bool(row.value)
    else:
        value = row.value
    return {
        "id": row.id,
        "traceId": row.trace_id,
        "observationId": row.observation_id,
        "sessionId": row.session_id,
        "datasetRunId": row.dataset_run_id,
        "name": row.name,
        "value": value,
        "dataType": row.data_type,
        "comment": row.comment,
        "source": row.source,
        "configId": row.config_id,
        "authorId": row.author_id,
        "queueId": row.queue_id,
        "executionId": row.execution_id,
        "channel": row.channel,
        "environment": row.environment,
        "timestamp": row.timestamp.isoformat(),
    }


def config_json(config: ScoreConfig) -> dict[str, Any]:
    return {
        "id": config.id,
        "name": config.name,
        "dataType": config.data_type,
        "minValue": config.min_value,
        "maxValue": config.max_value,
        "categories": config.categories,
        "description": config.description,
        "isArchived": config.archived_at is not None,
        "publicFeedback": config.public_feedback,
        "createdAt": config.created_at.isoformat(),
        "updatedAt": config.updated_at.isoformat(),
    }


def _fields(body: ScoreBody) -> dict[str, Any]:
    return {
        "trace_id": body.trace_id,
        "observation_id": body.observation_id,
        "session_id": body.session_id,
        "dataset_run_id": body.dataset_run_id,
        "comment": body.comment,
        "environment": body.environment or "default",
    }


@router.post("/scores", status_code=201)
def create_scores(
    request: Request,
    payload: ScorePayload = Depends(json_body(ScorePayload, MAX_BODY_BYTES)),
    key: ResolvedKey = Depends(_write_key),
    container: Container = Depends(get_container),
):
    bodies = payload.root if isinstance(payload.root, list) else [payload.root]
    if not bodies or len(bodies) > MAX_BATCH:
        raise HTTPException(400, f"Send between 1 and {MAX_BATCH} scores.")
    store, cache = container.trace_store, config_cache(container)
    problems: list[dict[str, Any]] = []
    for index, body in enumerate(bodies):
        claimed = [f for f in SERVER_FIELDS if getattr(body, f)]
        if claimed:
            problems.append({"index": index, "message": f"{claimed[0]} cannot be set here."})
            continue
        if body.config_id:
            problem = cache.check(key.project_id, body.config_id, body.value, body.data_type)
            if problem:
                problems.append({"index": index, "message": problem})
    if problems:
        message = problems[0]["message"] if len(bodies) == 1 else f"{len(problems)} invalid scores."
        return _errors(message, problems)
    created = []
    try:
        for body in bodies:
            created.append(
                service.create_score(
                    store,
                    key.project_id,
                    name=body.name,
                    value=body.value,
                    source="API",
                    configs=cache,
                    config_id=body.config_id,
                    data_type=body.data_type,
                    score_id=body.id,
                    **_fields(body),
                )
            )
    except ScoreError as exc:
        raise HTTPException(400, str(exc)) from exc
    ids = [{"id": row.id} for row in created]
    return ids[0] if isinstance(payload.root, ScoreBody) else {"data": ids}


def _errors(message: str, problems: list[dict[str, Any]]):
    from fastapi.responses import JSONResponse

    from traceloom.api.errors import error_body

    body = error_body(400, message)
    body["error"]["details"] = problems
    return JSONResponse(body, status_code=400)


@router.get("/scores")
def list_scores(
    request: Request,
    trace_id: str | None = Query(None, alias="traceId"),
    observation_id: str | None = Query(None, alias="observationId"),
    session_id: str | None = Query(None, alias="sessionId"),
    name: str | None = None,
    source: str | None = None,
    config_id: str | None = Query(None, alias="configId"),
    dataset_run_id: str | None = Query(None, alias="datasetRunId"),
    from_time: datetime | None = Query(None, alias="from"),
    to_time: datetime | None = Query(None, alias="to"),
    cursor: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    key: ResolvedKey = Depends(project_key),
    container: Container = Depends(get_container),
):
    if source is not None and source not in service.SOURCES:
        raise HTTPException(400, f"source must be one of {', '.join(service.SOURCES)}.")
    query = ScoreQuery(
        trace_id=trace_id,
        observation_id=observation_id,
        session_id=session_id,
        name=name,
        source=source,
        config_id=config_id,
        dataset_run_id=dataset_run_id,
        from_time=from_time,
        to_time=to_time,
        cursor=cursor,
        limit=limit,
    )
    try:
        page = container.trace_store.list_scores(key.project_id, query)
    except InvalidCursor as exc:
        raise HTTPException(400, "Invalid cursor.") from exc
    return {
        "data": [score_json(row) for row in page.items],
        "meta": {"limit": limit, "nextCursor": page.next_cursor},
    }


@router.delete("/scores/{score_id}", status_code=204)
def delete_score(
    score_id: str,
    key: ResolvedKey = Depends(_write_key),
    container: Container = Depends(get_container),
):
    try:
        service.delete_score(container.trace_store, key.project_id, score_id)
    except ScoreNotFound:
        raise HTTPException(404, "Score not found.") from None
    return Response(status_code=204)


@router.get("/score-configs")
def list_score_configs(
    include_archived: bool = Query(False, alias="includeArchived"),
    key: ResolvedKey = Depends(project_key),
    db: Session = Depends(get_db),
):
    rows = score_configs.list_configs(db, key.project_id, include_archived)
    return {"data": [config_json(c) for c in rows], "meta": {"total": len(rows)}}
