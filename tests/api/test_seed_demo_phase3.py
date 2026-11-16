from __future__ import annotations

from sqlalchemy import func, select

from traceloom.db.models import AnnotationQueue, ScoreConfig, User
from traceloom.db.models_datasets import Dataset, DatasetItem, DatasetRun, DatasetRunItem
from traceloom.services.demo import (
    DEMO_CONFIG,
    DEMO_DATASET,
    DEMO_QUEUE,
    seed_demo,
)

HTML = {"Accept": "text/html"}


def count(db, model) -> int:
    return db.scalar(select(func.count()).select_from(model))


def test_seed_demo_twice_creates_the_evaluation_data_once(settings, container, client, login):
    first = seed_demo(settings, container.session_factory)
    second = seed_demo(settings, container.session_factory)
    assert first.project_id == second.project_id
    with container.session_factory() as db:
        assert count(db, ScoreConfig) == 1
        assert count(db, AnnotationQueue) == 1
        assert count(db, Dataset) == 1
        assert count(db, DatasetItem) == 20
        assert count(db, DatasetRun) == 2
        assert count(db, DatasetRunItem) == 40
        assert {r.status for r in db.scalars(select(DatasetRun))} == {"completed"}
        user = db.scalar(select(User))
        dataset = db.scalar(select(Dataset))
    login(client, user)
    base = f"/project/{first.project_id}"
    assert DEMO_CONFIG in client.get(f"{base}/settings/scores", headers=HTML).text
    assert DEMO_QUEUE in client.get(f"{base}/queues", headers=HTML).text
    assert DEMO_DATASET in client.get(f"{base}/datasets", headers=HTML).text
    assert client.get(f"{base}/datasets/{dataset.id}", headers=HTML).status_code == 200
