"""Factories for datasets, items and runs, loaded as a pytest plugin from conftest.py."""

from __future__ import annotations

from collections.abc import Callable
from itertools import count

import pytest
from sqlalchemy.orm import Session

from traceloom.db.base import utcnow
from traceloom.db.models import Project
from traceloom.db.models_datasets import (
    Dataset,
    DatasetItem,
    DatasetItemVersion,
    DatasetRun,
    DatasetRunItem,
)

_seq = count(1)


@pytest.fixture
def make_dataset(db: Session) -> Callable[..., Dataset]:
    def make(project: Project, name: str | None = None, **fields) -> Dataset:
        dataset = Dataset(project_id=project.id, name=name or f"dataset-{next(_seq)}", **fields)
        db.add(dataset)
        db.commit()
        return dataset

    return make


@pytest.fixture
def make_dataset_item(db: Session) -> Callable[..., DatasetItem]:
    """An item with its version 1 row."""

    def make(dataset: Dataset, input: object = None, expected_output: object = None, **fields):
        item = DatasetItem(dataset_id=dataset.id, project_id=dataset.project_id, **fields)
        db.add(item)
        db.flush()
        db.add(
            DatasetItemVersion(
                item_id=item.id,
                version=item.current_version,
                project_id=dataset.project_id,
                input=input if input is not None else {"q": f"question {next(_seq)}"},
                expected_output=expected_output,
                metadata_={},
                status=item.status,
            )
        )
        db.commit()
        return item

    return make


@pytest.fixture
def make_dataset_run(db: Session) -> Callable[..., DatasetRun]:
    def make(dataset: Dataset, name: str | None = None, **fields) -> DatasetRun:
        fields.setdefault("dataset_version", utcnow())
        run = DatasetRun(
            dataset_id=dataset.id,
            project_id=dataset.project_id,
            name=name or f"run-{next(_seq)}",
            **fields,
        )
        db.add(run)
        db.commit()
        return run

    return make


@pytest.fixture
def make_dataset_run_item(db: Session) -> Callable[..., DatasetRunItem]:
    def make(run: DatasetRun, item: DatasetItem, **fields) -> DatasetRunItem:
        fields.setdefault("item_version", item.current_version)
        run_item = DatasetRunItem(
            run_id=run.id, project_id=run.project_id, item_id=item.id, **fields
        )
        db.add(run_item)
        db.commit()
        return run_item

    return make
