"""Public datasets API (FR-D6), under HTTP Basic project keys (pk + sk).

Datasets are addressed by name, items by id. Items are read as of the latest version and
archived ones are left out unless `includeArchived=true`. A run name is unique per dataset (409).
A run item links a dataset item to a trace and takes output, latency and cost from that trace.
Routes are plain `def`, so the store work runs in the threadpool. Errors use the Phase 1 envelope.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel
from sqlalchemy.orm import Session

from traceloom.auth.api_keys import ResolvedKey
from traceloom.auth.deps import get_container, project_key
from traceloom.container import Container
from traceloom.datasets import runs, service
from traceloom.datasets.service import Actor, ItemView
from traceloom.db.models import Project
from traceloom.db.models_datasets import Dataset, DatasetRun, DatasetRunItem
from traceloom.db.session import get_db

router = APIRouter(prefix="/api/public", tags=["datasets"])


class Body(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")


class DatasetBody(Body):
    name: str
    description: str = ""
    metadata: dict[str, Any] | None = None


class ItemBody(Body):
    input: Any = None
    expected_output: Any = None
    metadata: Any = None
    source_trace_id: str | None = None
    source_observation_id: str | None = None


class RunBody(Body):
    name: str
    description: str = ""
    metadata: dict[str, Any] | None = None


class RunItemBody(Body):
    dataset_item_id: str
    trace_id: str
    run_id: str | None = None
    dataset_name: str | None = None
    run_name: str | None = None
    observation_id: str | None = None
    status: str = "completed"
    error: str | None = None


class RunStatusBody(Body):
    status: str


class Ctx:
    """The caller's project and actor, resolved once per request (pk + sk only)."""

    def __init__(self, key: ResolvedKey = Depends(project_key), db: Session = Depends(get_db)):
        project = db.get(Project, key.project_id)
        if project is None:
            raise HTTPException(401, "Invalid API key.")
        self.db, self.project = db, project
        self.actor = Actor.for_key(key.key_id)


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except service.DatasetConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except service.DatasetNotFound as exc:
        raise HTTPException(404, "Not found.") from exc
    except service.NotAllowed as exc:
        raise HTTPException(403, str(exc)) from exc
    except service.DatasetError as exc:
        raise HTTPException(400, str(exc)) from exc


def _dataset(ctx: Ctx, name: str) -> Dataset:
    return _call(service.get_dataset_by_name, ctx.db, ctx.project, name)


def dataset_json(dataset: Dataset) -> dict[str, Any]:
    return {
        "id": dataset.id,
        "name": dataset.name,
        "description": dataset.description,
        "metadata": dataset.metadata_,
        "createdAt": dataset.created_at.isoformat(),
        "updatedAt": dataset.updated_at.isoformat(),
    }


def item_json(view: ItemView, dataset_name: str | None = None) -> dict[str, Any]:
    return {
        "id": view.id,
        "datasetId": view.dataset_id,
        "datasetName": dataset_name,
        "version": view.version,
        "status": view.status,
        "input": view.input,
        "expectedOutput": view.expected_output,
        "metadata": view.metadata,
        "sourceTraceId": view.source_trace_id,
        "sourceObservationId": view.source_observation_id,
        "createdAt": view.item_created_at.isoformat(),
        "updatedAt": view.created_at.isoformat(),
    }


def run_json(run: DatasetRun, dataset_name: str) -> dict[str, Any]:
    return {
        "id": run.id,
        "datasetId": run.dataset_id,
        "datasetName": dataset_name,
        "name": run.name,
        "description": run.description,
        "metadata": run.metadata_,
        "datasetVersion": run.dataset_version.isoformat(),
        "status": run.status,
        "createdAt": run.created_at.isoformat(),
        "finishedAt": run.finished_at.isoformat() if run.finished_at else None,
    }


def run_item_json(link: DatasetRunItem) -> dict[str, Any]:
    return {
        "id": link.id,
        "runId": link.run_id,
        "datasetItemId": link.item_id,
        "itemVersion": link.item_version,
        "traceId": link.trace_id,
        "observationId": link.observation_id,
        "status": link.status,
        "output": link.output,
        "latencyMs": link.latency_ms,
        "cost": link.cost,
        "error": link.error,
        "createdAt": link.created_at.isoformat(),
    }


# --- datasets -------------------------------------------------------------------------------


@router.post("/datasets", status_code=201)
def create_dataset(body: DatasetBody, ctx: Ctx = Depends()):
    dataset = _call(
        service.create_dataset,
        ctx.db,
        ctx.project,
        ctx.actor,
        name=body.name,
        description=body.description,
        metadata=body.metadata,
    )
    return dataset_json(dataset)


@router.get("/datasets")
def list_datasets(ctx: Ctx = Depends()):
    rows = service.list_datasets(ctx.db, ctx.project)
    data = [
        {**dataset_json(r.dataset), "itemCount": r.item_count, "runCount": r.run_count}
        for r in rows
    ]
    return {"data": data, "meta": {"total": len(data)}}


@router.get("/datasets/{name}")
def get_dataset(name: str, ctx: Ctx = Depends()):
    dataset = _dataset(ctx, name)
    count = service.count_items(ctx.db, ctx.project, dataset)
    return {**dataset_json(dataset), "itemCount": count}


# --- items ----------------------------------------------------------------------------------


@router.post("/datasets/{name}/items", status_code=201)
def create_item(name: str, body: ItemBody, ctx: Ctx = Depends()):
    dataset = _dataset(ctx, name)
    view = _call(
        service.create_item,
        ctx.db,
        ctx.project,
        ctx.actor,
        dataset,
        input=body.input,
        expected_output=body.expected_output,
        metadata=body.metadata,
        source_trace_id=body.source_trace_id,
        source_observation_id=body.source_observation_id,
    )
    return item_json(view, dataset.name)


@router.get("/datasets/{name}/items")
def list_items(
    name: str,
    include_archived: bool = Query(False, alias="includeArchived"),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=500),
    ctx: Ctx = Depends(),
):
    dataset = _dataset(ctx, name)
    views = service.items_as_of(
        ctx.db,
        ctx.project,
        dataset,
        include_archived=include_archived,
        limit=limit,
        offset=(page - 1) * limit,
    )
    return {
        "data": [item_json(v, dataset.name) for v in views],
        "meta": {"page": page, "limit": limit},
    }


@router.post("/dataset-items/{item_id}/archive")
def archive_item(item_id: str, ctx: Ctx = Depends()):
    item = _call(service.get_item, ctx.db, ctx.project, item_id)
    view = _call(service.archive_item, ctx.db, ctx.project, ctx.actor, item)
    return item_json(view)


# --- runs -----------------------------------------------------------------------------------


@router.post("/datasets/{name}/runs", status_code=201)
def create_run(name: str, body: RunBody, ctx: Ctx = Depends()):
    dataset = _dataset(ctx, name)
    run = _call(
        runs.create_run,
        ctx.db,
        ctx.project,
        ctx.actor,
        dataset,
        name=body.name,
        description=body.description,
        metadata=body.metadata,
    )
    return run_json(run, dataset.name)


@router.get("/datasets/{name}/runs")
def list_runs(name: str, ctx: Ctx = Depends()):
    dataset = _dataset(ctx, name)
    data = [run_json(r, dataset.name) for r in runs.list_runs(ctx.db, ctx.project, dataset)]
    return {"data": data, "meta": {"total": len(data)}}


@router.get("/datasets/{name}/runs/{run_name}")
def get_run(name: str, run_name: str, ctx: Ctx = Depends()):
    dataset = _dataset(ctx, name)
    run = runs.find_run(ctx.db, ctx.project, dataset, run_name)
    if run is None:
        raise HTTPException(404, "Not found.")
    links = runs.run_items(ctx.db, ctx.project, run)
    return {**run_json(run, dataset.name), "runItems": [run_item_json(i) for i in links]}


@router.patch("/datasets/{name}/runs/{run_name}")
def set_run_status(name: str, run_name: str, body: RunStatusBody, ctx: Ctx = Depends()):
    dataset = _dataset(ctx, name)
    run = runs.find_run(ctx.db, ctx.project, dataset, run_name)
    if run is None:
        raise HTTPException(404, "Not found.")
    run = _call(runs.set_status, ctx.db, ctx.project, ctx.actor, run, body.status)
    return run_json(run, dataset.name)


# --- run items ------------------------------------------------------------------------------


@router.post("/dataset-run-items", status_code=201)
def create_run_item(
    body: RunItemBody,
    ctx: Ctx = Depends(),
    container: Container = Depends(get_container),
):
    if body.run_id:
        run = _call(runs.get_run, ctx.db, ctx.project, body.run_id)
    elif body.dataset_name and body.run_name:
        dataset = _dataset(ctx, body.dataset_name)
        run = runs.find_run(ctx.db, ctx.project, dataset, body.run_name)
        if run is None:
            raise HTTPException(404, "Not found.")
    else:
        raise HTTPException(400, "Send runId, or datasetName and runName.")
    item = _call(service.get_item, ctx.db, ctx.project, body.dataset_item_id)
    detail = container.trace_store.get_trace(ctx.project.id, body.trace_id)
    if detail is None:
        raise HTTPException(404, "Trace not found.")
    trace = detail.trace
    link = _call(
        runs.link_run_item,
        ctx.db,
        ctx.project,
        ctx.actor,
        run,
        item,
        trace_id=trace.id,
        observation_id=body.observation_id,
        status=body.status,
        error=body.error,
        output=trace.output,
        latency_ms=trace.latency_ms,
        cost=trace.total_cost,
    )
    return run_item_json(link)
