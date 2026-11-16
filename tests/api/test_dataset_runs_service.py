from __future__ import annotations

import pytest

from traceloom.auth.permissions import Role
from traceloom.datasets import runs, service
from traceloom.datasets.service import Actor


@pytest.fixture
def member(world):
    return Actor.for_user(world.users[Role.MEMBER], Role.MEMBER)


@pytest.fixture
def dataset(db, world, member):
    d = service.create_dataset(db, world.project, member, name="d")
    db.commit()
    return d


def test_run_item_records_output_latency_and_cost(db, world, member, dataset):
    view = service.create_item(db, world.project, member, dataset, input="q")
    item = service.get_item(db, world.project, view.id)
    run = runs.create_run(
        db, world.project, member, dataset, name="r", config={"model": "m"}, metadata={"a": 1}
    )
    link = runs.link_run_item(
        db,
        world.project,
        member,
        run,
        item,
        trace_id="t1",
        observation_id="o1",
        output={"text": "a"},
        latency_ms=42.0,
        cost=0.01,
        estimated_cost=0.02,
    )
    db.commit()
    (got,) = runs.run_items(db, world.project, run)
    assert got.id == link.id
    assert (got.trace_id, got.observation_id, got.output) == ("t1", "o1", {"text": "a"})
    assert (got.latency_ms, got.cost, got.estimated_cost, got.status) == (
        42.0,
        0.01,
        0.02,
        "completed",
    )
    assert run.config == {"model": "m"} and run.metadata_ == {"a": 1}
    assert runs.list_runs(db, world.project, dataset) == [run]
    assert runs.get_run(db, world.project, run.id) is run


def test_item_created_after_run_cannot_be_linked(db, world, member, dataset):
    run = runs.create_run(db, world.project, member, dataset, name="r")
    view = service.create_item(db, world.project, member, dataset, input="late")
    item = service.get_item(db, world.project, view.id)
    with pytest.raises(service.DatasetError, match="did not exist"):
        runs.link_run_item(db, world.project, member, run, item)


def test_run_queries_are_project_scoped(db, world, member, dataset, make_org, make_project):
    run = runs.create_run(db, world.project, member, dataset, name="r")
    db.commit()
    other = make_project(make_org())
    with pytest.raises(service.DatasetNotFound):
        runs.get_run(db, other, run.id)
    with pytest.raises(service.DatasetNotFound):
        runs.run_items(db, other, run)
    with pytest.raises(service.DatasetNotFound):
        runs.list_runs(db, other, dataset)


def test_viewer_cannot_create_runs(db, world, dataset):
    viewer = Actor.for_user(world.users[Role.VIEWER], Role.VIEWER)
    with pytest.raises(service.NotAllowed):
        runs.create_run(db, world.project, viewer, dataset, name="r")
