"""Datasets (FR-D1..D4): datasets, items with immutable versions, archive and add-from-trace.

Every function takes the project and the acting `Actor`, scopes each query to that project and
re-checks the permission. Writes add audit rows to the caller's transaction (the caller commits).

Versioning: every create, edit and archive of an item writes a `dataset_item_versions` row and
moves `dataset_items.current_version`. The dataset's version is the timestamp of its latest
change, and `items_as_of` reads each item as it was at such a timestamp.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from traceloom.auth.permissions import Perm, Role, can
from traceloom.db.base import utcnow
from traceloom.db.models import Project, User
from traceloom.db.models_datasets import (
    Dataset,
    DatasetItem,
    DatasetItemVersion,
    DatasetRun,
)
from traceloom.ports import TraceStore
from traceloom.services import audit

MAX_NAME = 200
MAX_JSON_CHARS = 1_000_000
_UNSET: Any = object()


class DatasetError(ValueError):
    """The request is invalid (the message is safe to show)."""


class DatasetConflict(DatasetError):
    """The name is already taken (HTTP 409)."""


class DatasetNotFound(LookupError):
    pass


class NotAllowed(PermissionError):
    pass


@dataclass(frozen=True)
class Actor:
    """Who is acting: a signed-in user (with their org role) or a project API key."""

    type: str  # user | api_key
    id: str | None
    role: Role | None = None
    name: str = ""

    @classmethod
    def for_user(cls, user: User, role: Role | None) -> Actor:
        return cls("user", user.id, role, user.email)

    @classmethod
    def for_key(cls, key_id: str) -> Actor:
        return cls("api_key", key_id, None, f"api_key:{key_id}")

    def can(self, perm: Perm) -> bool:
        if self.type == "api_key":
            return perm in (Perm.EVAL_READ, Perm.DATASETS_WRITE)
        return can(self.role, perm)

    def require(self, perm: Perm, message: str) -> None:
        if not self.can(perm):
            raise NotAllowed(message)


@dataclass(frozen=True)
class ItemView:
    """One item as of one version."""

    id: str
    dataset_id: str
    version: int
    status: str
    input: Any
    expected_output: Any
    metadata: Any
    source_trace_id: str | None
    source_observation_id: str | None
    actor: str | None
    created_at: datetime  # when this version was written
    item_created_at: datetime


@dataclass(frozen=True)
class DatasetSummary:
    dataset: Dataset
    item_count: int
    run_count: int
    last_run_status: str | None
    last_run_at: datetime | None


def validate_name(name: str | None) -> str:
    name = (name or "").strip()
    if not name:
        raise DatasetError("A name is required.")
    if len(name) > MAX_NAME:
        raise DatasetError(f"A name can be at most {MAX_NAME} characters.")
    return name


def _json_size_ok(value: Any, label: str) -> None:
    try:
        size = len(json.dumps(value, default=str))
    except (TypeError, ValueError) as exc:
        raise DatasetError(f"{label} is not valid JSON.") from exc
    if size > MAX_JSON_CHARS:
        raise DatasetError(f"{label} is too large (limit {MAX_JSON_CHARS:,} characters).")


def _dict(value: Any, label: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise DatasetError(f"{label} must be a JSON object.")
    _json_size_ok(value, label)
    return value


# --- datasets -------------------------------------------------------------------------------


def create_dataset(
    db: Session,
    project: Project,
    actor: Actor,
    *,
    name: str,
    description: str = "",
    metadata: dict[str, Any] | None = None,
) -> Dataset:
    actor.require(Perm.DATASETS_WRITE, "You need the Member role to create datasets.")
    name = validate_name(name)
    if find_dataset(db, project, name):
        raise DatasetConflict(f"A dataset named {name!r} already exists.")
    dataset = Dataset(
        project_id=project.id,
        name=name,
        description=(description or "").strip(),
        metadata_=_dict(metadata, "Metadata"),
        created_by=actor.id,
    )
    db.add(dataset)
    try:
        db.flush()
    except IntegrityError as exc:  # a concurrent create won
        db.rollback()
        raise DatasetConflict(f"A dataset named {name!r} already exists.") from exc
    return dataset


def find_dataset(db: Session, project: Project, name: str) -> Dataset | None:
    return db.scalar(select(Dataset).where(Dataset.project_id == project.id, Dataset.name == name))


def get_dataset(db: Session, project: Project, dataset_id: str) -> Dataset:
    dataset = db.scalar(
        select(Dataset).where(Dataset.project_id == project.id, Dataset.id == dataset_id)
    )
    if dataset is None:
        raise DatasetNotFound(dataset_id)
    return dataset


def get_dataset_by_name(db: Session, project: Project, name: str) -> Dataset:
    dataset = find_dataset(db, project, name)
    if dataset is None:
        raise DatasetNotFound(name)
    return dataset


def list_datasets(db: Session, project: Project) -> list[DatasetSummary]:
    datasets = db.scalars(
        select(Dataset).where(Dataset.project_id == project.id).order_by(Dataset.name)
    ).all()
    items = dict(
        db.execute(
            select(DatasetItem.dataset_id, func.count())
            .where(DatasetItem.project_id == project.id, DatasetItem.status == "active")
            .group_by(DatasetItem.dataset_id)
        ).all()
    )
    runs: dict[str, list[DatasetRun]] = {}
    for run in db.scalars(
        select(DatasetRun)
        .where(DatasetRun.project_id == project.id)
        .order_by(DatasetRun.created_at.desc())
    ):
        runs.setdefault(run.dataset_id, []).append(run)
    out = []
    for dataset in datasets:
        mine = runs.get(dataset.id, [])
        out.append(
            DatasetSummary(
                dataset,
                items.get(dataset.id, 0),
                len(mine),
                mine[0].status if mine else None,
                mine[0].created_at if mine else None,
            )
        )
    return out


def delete_dataset(db: Session, project: Project, actor: Actor, dataset: Dataset) -> None:
    actor.require(Perm.DATASETS_DELETE, "You need the Admin role to delete datasets.")
    _own(project, dataset)
    audit.record(
        db,
        action="dataset.delete",
        actor_type=actor.type,
        actor_id=actor.id,
        org_id=project.org_id,
        project_id=project.id,
        target=dataset.id,
        detail={"name": dataset.name},
    )
    db.delete(dataset)
    db.flush()


def dataset_version(db: Session, dataset: Dataset) -> datetime:
    """The timestamp of the dataset's latest change (its creation when it has no items)."""
    latest = db.scalar(
        select(func.max(DatasetItemVersion.created_at))
        .join(DatasetItem, DatasetItem.id == DatasetItemVersion.item_id)
        .where(DatasetItem.dataset_id == dataset.id, DatasetItem.project_id == dataset.project_id)
    )
    return max(latest, dataset.created_at) if latest else dataset.created_at


def _own(project: Project, dataset: Dataset) -> None:
    if dataset.project_id != project.id:
        raise DatasetNotFound(dataset.id)


# --- items ----------------------------------------------------------------------------------


def _view(item: DatasetItem, version: DatasetItemVersion) -> ItemView:
    return ItemView(
        id=item.id,
        dataset_id=item.dataset_id,
        version=version.version,
        status=version.status,
        input=version.input,
        expected_output=version.expected_output,
        metadata=version.metadata_,
        source_trace_id=item.source_trace_id,
        source_observation_id=item.source_observation_id,
        actor=version.actor,
        created_at=version.created_at,
        item_created_at=item.created_at,
    )


def _write_version(
    db: Session, item: DatasetItem, actor: Actor, *, input, expected_output, metadata, status: str
) -> DatasetItemVersion:
    now = utcnow()
    version = DatasetItemVersion(
        item_id=item.id,
        version=item.current_version,
        project_id=item.project_id,
        input=input,
        expected_output=expected_output,
        metadata_=metadata,
        status=status,
        actor=actor.id,
        created_at=now,
    )
    item.status = status
    item.updated_at = now
    db.add(version)
    db.flush()
    return version


def create_item(
    db: Session,
    project: Project,
    actor: Actor,
    dataset: Dataset,
    *,
    input: Any,
    expected_output: Any = None,
    metadata: Any = None,
    source_trace_id: str | None = None,
    source_observation_id: str | None = None,
) -> ItemView:
    actor.require(Perm.DATASETS_WRITE, "You need the Member role to edit datasets.")
    _own(project, dataset)
    if input is None:
        raise DatasetError("An item needs an input.")
    for label, value in (("Input", input), ("Expected output", expected_output)):
        _json_size_ok(value, label)
    item = DatasetItem(
        dataset_id=dataset.id,
        project_id=project.id,
        current_version=1,
        source_trace_id=source_trace_id,
        source_observation_id=source_observation_id,
    )
    db.add(item)
    db.flush()
    version = _write_version(
        db,
        item,
        actor,
        input=input,
        expected_output=expected_output,
        metadata=metadata if metadata is not None else {},
        status="active",
    )
    dataset.updated_at = version.created_at
    return _view(item, version)


def get_item(db: Session, project: Project, item_id: str) -> DatasetItem:
    item = db.scalar(
        select(DatasetItem).where(DatasetItem.project_id == project.id, DatasetItem.id == item_id)
    )
    if item is None:
        raise DatasetNotFound(item_id)
    return item


def current_item(db: Session, project: Project, item: DatasetItem) -> ItemView:
    return item_at_version(db, project, item, item.current_version)


def item_at_version(db: Session, project: Project, item: DatasetItem, version: int) -> ItemView:
    row = db.get(DatasetItemVersion, (item.id, version))
    if row is None or item.project_id != project.id:
        raise DatasetNotFound(f"{item.id}@{version}")
    return _view(item, row)


def update_item(
    db: Session,
    project: Project,
    actor: Actor,
    item: DatasetItem,
    *,
    input: Any = _UNSET,
    expected_output: Any = _UNSET,
    metadata: Any = _UNSET,
) -> ItemView:
    """Write the next version, keeping any field that was not passed."""
    actor.require(Perm.DATASETS_WRITE, "You need the Member role to edit datasets.")
    if item.project_id != project.id:
        raise DatasetNotFound(item.id)
    if item.status == "archived":
        raise DatasetError("An archived item cannot be edited.")
    current = db.get(DatasetItemVersion, (item.id, item.current_version))
    new_input = current.input if input is _UNSET else input
    new_expected = current.expected_output if expected_output is _UNSET else expected_output
    new_metadata = current.metadata_ if metadata is _UNSET else metadata
    if new_input is None:
        raise DatasetError("An item needs an input.")
    for label, value in (("Input", new_input), ("Expected output", new_expected)):
        _json_size_ok(value, label)
    if (new_input, new_expected, new_metadata) == (
        current.input,
        current.expected_output,
        current.metadata_,
    ):
        return _view(item, current)  # nothing changed, no new version
    item.current_version += 1
    version = _write_version(
        db,
        item,
        actor,
        input=new_input,
        expected_output=new_expected,
        metadata=new_metadata,
        status="active",
    )
    _touch_dataset(db, item, version.created_at)
    return _view(item, version)


def archive_item(db: Session, project: Project, actor: Actor, item: DatasetItem) -> ItemView:
    actor.require(Perm.DATASETS_WRITE, "You need the Member role to edit datasets.")
    if item.project_id != project.id:
        raise DatasetNotFound(item.id)
    current = db.get(DatasetItemVersion, (item.id, item.current_version))
    if item.status == "archived":
        return _view(item, current)
    item.current_version += 1
    version = _write_version(
        db,
        item,
        actor,
        input=current.input,
        expected_output=current.expected_output,
        metadata=current.metadata_,
        status="archived",
    )
    _touch_dataset(db, item, version.created_at)
    audit.record(
        db,
        action="dataset.item.archive",
        actor_type=actor.type,
        actor_id=actor.id,
        org_id=project.org_id,
        project_id=project.id,
        target=item.id,
        detail={"dataset_id": item.dataset_id, "version": version.version},
    )
    return _view(item, version)


def _touch_dataset(db: Session, item: DatasetItem, when: datetime) -> None:
    dataset = db.get(Dataset, item.dataset_id)
    if dataset is not None:
        dataset.updated_at = when


def item_history(db: Session, project: Project, item: DatasetItem) -> list[ItemView]:
    """Every version of the item, newest first."""
    if item.project_id != project.id:
        raise DatasetNotFound(item.id)
    rows = db.scalars(
        select(DatasetItemVersion)
        .where(DatasetItemVersion.item_id == item.id, DatasetItemVersion.project_id == project.id)
        .order_by(DatasetItemVersion.version.desc())
    ).all()
    return [_view(item, row) for row in rows]


def dataset_history(
    db: Session, project: Project, dataset: Dataset, *, limit: int = 200
) -> list[ItemView]:
    """The dataset's change log: item versions across the dataset, newest first."""
    _own(project, dataset)
    rows = db.execute(
        select(DatasetItem, DatasetItemVersion)
        .join(DatasetItemVersion, DatasetItemVersion.item_id == DatasetItem.id)
        .where(DatasetItem.dataset_id == dataset.id, DatasetItem.project_id == project.id)
        .order_by(DatasetItemVersion.created_at.desc(), DatasetItemVersion.version.desc())
        .limit(limit)
    ).all()
    return [_view(item, version) for item, version in rows]


def items_as_of(
    db: Session,
    project: Project,
    dataset: Dataset,
    as_of: datetime | None = None,
    *,
    include_archived: bool = False,
    limit: int | None = None,
    offset: int = 0,
) -> list[ItemView]:
    """The items as they were at `as_of` (the latest state when None), oldest item first.

    An item written after `as_of` is absent; one archived by then is left out unless
    `include_archived`."""
    _own(project, dataset)
    newest = select(
        DatasetItemVersion.item_id.label("item_id"),
        func.max(DatasetItemVersion.version).label("version"),
    ).where(DatasetItemVersion.project_id == project.id)
    if as_of is not None:
        newest = newest.where(DatasetItemVersion.created_at <= as_of)
    newest = newest.group_by(DatasetItemVersion.item_id).subquery()
    stmt = (
        select(DatasetItem, DatasetItemVersion)
        .join(newest, newest.c.item_id == DatasetItem.id)
        .join(
            DatasetItemVersion,
            (DatasetItemVersion.item_id == newest.c.item_id)
            & (DatasetItemVersion.version == newest.c.version),
        )
        .where(DatasetItem.dataset_id == dataset.id, DatasetItem.project_id == project.id)
        .order_by(DatasetItem.created_at, DatasetItem.id)
        .offset(offset)
    )
    if not include_archived:
        stmt = stmt.where(DatasetItemVersion.status == "active")
    if limit is not None:
        stmt = stmt.limit(limit)
    return [_view(item, version) for item, version in db.execute(stmt).all()]


def count_items(db: Session, project: Project, dataset: Dataset) -> int:
    return (
        db.scalar(
            select(func.count())
            .select_from(DatasetItem)
            .where(
                DatasetItem.dataset_id == dataset.id,
                DatasetItem.project_id == project.id,
                DatasetItem.status == "active",
            )
        )
        or 0
    )


# --- add from trace -------------------------------------------------------------------------


@dataclass(frozen=True)
class Prefill:
    input: Any
    expected_output: Any
    metadata: dict[str, Any]
    source_trace_id: str
    source_observation_id: str | None


def prefill_from_trace(
    store: TraceStore, project: Project, trace_id: str, observation_id: str | None = None
) -> Prefill:
    """The values the "Add to dataset" dialog starts with: the object's input as `input`, its
    output as `expected_output` and its metadata."""
    detail = store.get_trace(project.id, trace_id)
    if detail is None:
        raise DatasetNotFound(trace_id)
    source: Any = detail.trace
    if observation_id:
        source = next((o for o in detail.observations if o.id == observation_id), None)
        if source is None:
            raise DatasetNotFound(observation_id)
    return Prefill(
        input=source.input,
        expected_output=source.output,
        metadata=dict(source.metadata or {}),
        source_trace_id=trace_id,
        source_observation_id=observation_id,
    )


def add_from_trace(
    db: Session,
    store: TraceStore,
    project: Project,
    actor: Actor,
    dataset: Dataset,
    *,
    trace_id: str,
    observation_id: str | None = None,
    input: Any = _UNSET,
    expected_output: Any = _UNSET,
    metadata: Any = _UNSET,
) -> ItemView:
    """Save a trace or observation as an item with a source link; any field passed overrides
    the prefilled value (the dialog's edits)."""
    actor.require(Perm.DATASETS_WRITE, "You need the Member role to edit datasets.")
    fill = prefill_from_trace(store, project, trace_id, observation_id)
    return create_item(
        db,
        project,
        actor,
        dataset,
        input=fill.input if input is _UNSET else input,
        expected_output=fill.expected_output if expected_output is _UNSET else expected_output,
        metadata=fill.metadata if metadata is _UNSET else metadata,
        source_trace_id=fill.source_trace_id,
        source_observation_id=fill.source_observation_id,
    )
