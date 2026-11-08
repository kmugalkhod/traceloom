"""Factories for score configs and annotation queues, loaded as a pytest plugin from conftest.py."""

from __future__ import annotations

from collections.abc import Callable
from itertools import count

import pytest
from sqlalchemy.orm import Session

from traceloom.db.models import AnnotationItem, AnnotationQueue, Project, ScoreConfig

_seq = count(1)


@pytest.fixture
def make_score_config(db: Session) -> Callable[..., ScoreConfig]:
    def make(project: Project, name: str | None = None, **fields) -> ScoreConfig:
        fields.setdefault("data_type", "NUMERIC")
        if fields["data_type"] == "NUMERIC":
            fields.setdefault("min_value", 0.0)
            fields.setdefault("max_value", 1.0)
        config = ScoreConfig(project_id=project.id, name=name or f"config-{next(_seq)}", **fields)
        db.add(config)
        db.commit()
        return config

    return make


@pytest.fixture
def make_queue(db: Session) -> Callable[..., AnnotationQueue]:
    def make(
        project: Project, name: str | None = None, configs: list[ScoreConfig] | None = None
    ) -> AnnotationQueue:
        queue = AnnotationQueue(
            project_id=project.id,
            name=name or f"queue-{next(_seq)}",
            config_ids=[c.id for c in configs or []],
        )
        db.add(queue)
        db.commit()
        return queue

    return make


@pytest.fixture
def make_queue_item(db: Session) -> Callable[..., AnnotationItem]:
    def make(queue: AnnotationQueue, object_id: str | None = None, **fields) -> AnnotationItem:
        fields.setdefault("object_type", "trace")
        object_id = object_id or f"trace-{next(_seq)}"
        fields.setdefault("trace_id", object_id)
        item = AnnotationItem(
            queue_id=queue.id, project_id=queue.project_id, object_id=object_id, **fields
        )
        db.add(item)
        db.commit()
        return item

    return make
