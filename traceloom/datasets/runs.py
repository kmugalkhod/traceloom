"""Dataset runs (FR-D5): create a run at the dataset's current version, link run items, status.

A run records `dataset_version`, so `run_items_source` keeps reading each item as it was when the
run started even after later edits.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from traceloom.auth.permissions import Perm
from traceloom.datasets import service
from traceloom.datasets.service import (
    Actor,
    DatasetConflict,
    DatasetError,
    DatasetNotFound,
    ItemView,
)
from traceloom.db.base import utcnow
from traceloom.db.models import Project
from traceloom.db.models_datasets import (
    Dataset,
    DatasetItem,
    DatasetItemVersion,
    DatasetRun,
    DatasetRunItem,
)

STATUSES = ("running", "completed", "failed")


def create_run(
    db: Session,
    project: Project,
    actor: Actor,
    dataset: Dataset,
    *,
    name: str,
    description: str = "",
    metadata: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
) -> DatasetRun:
    actor.require(Perm.DATASETS_WRITE, "You need the Member role to create runs.")
    service._own(project, dataset)
    name = service.validate_name(name)
    if find_run(db, project, dataset, name):
        raise DatasetConflict(f"A run named {name!r} already exists in this dataset.")
    run = DatasetRun(
        dataset_id=dataset.id,
        project_id=project.id,
        name=name,
        description=(description or "").strip(),
        metadata_=service._dict(metadata, "Metadata"),
        dataset_version=service.dataset_version(db, dataset),
        config=service._dict(config, "Config"),
        status="running",
        created_by=actor.id,
    )
    db.add(run)
    try:
        db.flush()
    except IntegrityError as exc:  # a concurrent create won
        db.rollback()
        raise DatasetConflict(f"A run named {name!r} already exists in this dataset.") from exc
    return run


def find_run(db: Session, project: Project, dataset: Dataset, name: str) -> DatasetRun | None:
    return db.scalar(
        select(DatasetRun).where(
            DatasetRun.project_id == project.id,
            DatasetRun.dataset_id == dataset.id,
            DatasetRun.name == name,
        )
    )


def get_run(db: Session, project: Project, run_id: str) -> DatasetRun:
    run = db.scalar(
        select(DatasetRun).where(DatasetRun.project_id == project.id, DatasetRun.id == run_id)
    )
    if run is None:
        raise DatasetNotFound(run_id)
    return run


def list_runs(db: Session, project: Project, dataset: Dataset) -> list[DatasetRun]:
    service._own(project, dataset)
    return list(
        db.scalars(
            select(DatasetRun)
            .where(DatasetRun.project_id == project.id, DatasetRun.dataset_id == dataset.id)
            .order_by(DatasetRun.created_at.desc(), DatasetRun.id)
        )
    )


def run_item_source(db: Session, project: Project, run: DatasetRun) -> list[ItemView]:
    """The items a run works on: the dataset as of the run's recorded version."""
    dataset = service.get_dataset(db, project, run.dataset_id)
    return service.items_as_of(db, project, dataset, run.dataset_version)


def link_run_item(
    db: Session,
    project: Project,
    actor: Actor,
    run: DatasetRun,
    item: DatasetItem,
    *,
    trace_id: str | None = None,
    observation_id: str | None = None,
    status: str = "completed",
    output: Any = None,
    latency_ms: float | None = None,
    cost: float | None = None,
    estimated_cost: float | None = None,
    error: str | None = None,
) -> DatasetRunItem:
    """Link an item (as of the run's version) to the trace that processed it."""
    actor.require(Perm.DATASETS_WRITE, "You need the Member role to edit runs.")
    if run.project_id != project.id or item.project_id != project.id:
        raise DatasetNotFound(run.id)
    if item.dataset_id != run.dataset_id:
        raise DatasetError("The item belongs to a different dataset than the run.")
    as_of = _version_at(db, item, run.dataset_version)
    if as_of is None:
        raise DatasetError("The item did not exist when the run started.")
    link = DatasetRunItem(
        run_id=run.id,
        project_id=project.id,
        item_id=item.id,
        item_version=as_of,
        trace_id=trace_id,
        observation_id=observation_id,
        status=status,
        output=output,
        latency_ms=latency_ms,
        cost=cost,
        estimated_cost=estimated_cost,
        error=error,
    )
    db.add(link)
    db.flush()
    return link


def _version_at(db: Session, item: DatasetItem, when: datetime) -> int | None:
    return db.scalar(
        select(func.max(DatasetItemVersion.version)).where(
            DatasetItemVersion.item_id == item.id, DatasetItemVersion.created_at <= when
        )
    )


def run_items(db: Session, project: Project, run: DatasetRun) -> list[DatasetRunItem]:
    if run.project_id != project.id:
        raise DatasetNotFound(run.id)
    return list(
        db.scalars(
            select(DatasetRunItem)
            .where(DatasetRunItem.project_id == project.id, DatasetRunItem.run_id == run.id)
            .order_by(DatasetRunItem.created_at, DatasetRunItem.id)
        )
    )


def set_status(
    db: Session, project: Project, actor: Actor, run: DatasetRun, status: str
) -> DatasetRun:
    actor.require(Perm.DATASETS_WRITE, "You need the Member role to edit runs.")
    if run.project_id != project.id:
        raise DatasetNotFound(run.id)
    if status not in STATUSES:
        raise DatasetError(f"status must be one of {', '.join(STATUSES)}.")
    run.status = status
    run.finished_at = None if status == "running" else utcnow()
    db.flush()
    return run
