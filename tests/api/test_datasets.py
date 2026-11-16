from __future__ import annotations

import pytest
from sqlalchemy import select

from tests.helpers_web import seed_trace
from traceloom.auth.permissions import Role
from traceloom.datasets import runs, service
from traceloom.datasets.service import Actor, DatasetConflict, DatasetError, NotAllowed
from traceloom.db.models import AuditLog


def actor(world, role: Role = Role.MEMBER) -> Actor:
    return Actor.for_user(world.users[role], role)


@pytest.fixture
def dataset(db, world):
    d = service.create_dataset(db, world.project, actor(world), name="qa")
    db.commit()
    return d


def new_item(db, world, dataset, **kw):
    kw.setdefault("input", {"q": "hi"})
    view = service.create_item(db, world.project, actor(world), dataset, **kw)
    db.commit()
    return service.get_item(db, world.project, view.id)


def test_dataset_name_unique_and_validated(db, world, dataset):
    with pytest.raises(DatasetConflict):
        service.create_dataset(db, world.project, actor(world), name=" qa ")
    with pytest.raises(DatasetError):
        service.create_dataset(db, world.project, actor(world), name="  ")


def test_item_versions_and_run_reads_old_version(db, world, dataset):
    item = new_item(db, world, dataset, input={"q": "one"}, expected_output="a")
    run = runs.create_run(db, world.project, actor(world), dataset, name="before")
    db.commit()

    view = service.update_item(db, world.project, actor(world), item, input={"q": "two"})
    db.commit()
    assert (view.version, view.input, view.expected_output) == (2, {"q": "two"}, "a")

    # The run created earlier still reads version 1; the latest read shows version 2.
    (old,) = runs.run_item_source(db, world.project, run)
    assert (old.version, old.input) == (1, {"q": "one"})
    (now,) = service.items_as_of(db, world.project, dataset)
    assert (now.version, now.input) == (2, {"q": "two"})
    assert service.dataset_version(db, dataset) > run.dataset_version

    link = runs.link_run_item(db, world.project, actor(world), run, item, trace_id="t1")
    assert link.item_version == 1
    later = runs.create_run(db, world.project, actor(world), dataset, name="after")
    assert runs.link_run_item(db, world.project, actor(world), later, item).item_version == 2
    history = service.item_history(db, world.project, item)
    assert [v.version for v in history] == [2, 1]


def test_unchanged_edit_writes_no_version(db, world, dataset):
    item = new_item(db, world, dataset)
    view = service.update_item(db, world.project, actor(world), item, input={"q": "hi"})
    assert view.version == 1


def test_archive_writes_version_and_hides_item(db, world, dataset):
    item = new_item(db, world, dataset)
    run = runs.create_run(db, world.project, actor(world), dataset, name="r")
    service.archive_item(db, world.project, actor(world), item)
    db.commit()
    assert item.status == "archived" and item.current_version == 2
    assert service.items_as_of(db, world.project, dataset) == []
    assert len(service.items_as_of(db, world.project, dataset, include_archived=True)) == 1
    assert len(runs.run_item_source(db, world.project, run)) == 1  # active when the run began
    with pytest.raises(DatasetError):
        service.update_item(db, world.project, actor(world), item, input="x")
    audit = db.scalars(select(AuditLog).where(AuditLog.action == "dataset.item.archive")).all()
    assert len(audit) == 1 and audit[0].target == item.id


def test_run_name_is_unique_per_dataset(db, world, dataset):
    other = service.create_dataset(db, world.project, actor(world), name="other")
    runs.create_run(db, world.project, actor(world), dataset, name="r1")
    runs.create_run(db, world.project, actor(world), other, name="r1")
    with pytest.raises(DatasetConflict):
        runs.create_run(db, world.project, actor(world), dataset, name="r1")


def test_run_status(db, world, dataset):
    run = runs.create_run(db, world.project, actor(world), dataset, name="r")
    assert (run.status, run.finished_at) == ("running", None)
    runs.set_status(db, world.project, actor(world), run, "completed")
    assert run.status == "completed" and run.finished_at is not None
    with pytest.raises(DatasetError):
        runs.set_status(db, world.project, actor(world), run, "bogus")


def test_permissions(db, world, dataset):
    viewer, member, admin = (actor(world, r) for r in (Role.VIEWER, Role.MEMBER, Role.ADMIN))
    with pytest.raises(NotAllowed):
        service.create_dataset(db, world.project, viewer, name="x")
    with pytest.raises(NotAllowed):
        service.create_item(db, world.project, viewer, dataset, input="x")
    with pytest.raises(NotAllowed):
        service.delete_dataset(db, world.project, member, dataset)
    service.delete_dataset(db, world.project, admin, dataset)
    db.commit()
    assert service.list_datasets(db, world.project) == []
    assert db.scalars(select(AuditLog).where(AuditLog.action == "dataset.delete")).one()


def test_queries_are_project_scoped(db, world, dataset, make_org, make_project):
    item = new_item(db, world, dataset)
    other = make_project(make_org())
    with pytest.raises(service.DatasetNotFound):
        service.get_dataset(db, other, dataset.id)
    with pytest.raises(service.DatasetNotFound):
        service.get_dataset_by_name(db, other, "qa")
    with pytest.raises(service.DatasetNotFound):
        service.get_item(db, other, item.id)
    assert service.list_datasets(db, other) == []
    with pytest.raises(service.DatasetNotFound):
        service.items_as_of(db, other, dataset)
    with pytest.raises(service.DatasetNotFound):
        runs.create_run(db, other, actor(world), dataset, name="r")
    with pytest.raises(service.DatasetNotFound):
        service.update_item(db, other, actor(world), item, input="x")
    assert service.dataset_history(db, world.project, dataset)


def test_list_datasets_summary(db, world, dataset):
    new_item(db, world, dataset)
    runs.create_run(db, world.project, actor(world), dataset, name="r")
    (summary,) = service.list_datasets(db, world.project)
    assert (summary.item_count, summary.run_count, summary.last_run_status) == (1, 1, "running")


def test_add_from_observation_prefill(db, world, dataset, container):
    seed_trace(
        container,
        world.project.id,
        "t1",
        input={"q": "trace in"},
        output="trace out",
        metadata={"k": "v"},
    )
    store = container.trace_store
    obs_id = "t1-o1"
    detail = store.get_trace(world.project.id, "t1")
    obs = next(o for o in detail.observations if o.id == obs_id)

    fill = service.prefill_from_trace(store, world.project, "t1", obs_id)
    assert (fill.input, fill.expected_output) == (obs.input, obs.output)
    view = service.add_from_trace(
        db,
        store,
        world.project,
        actor(world),
        dataset,
        trace_id="t1",
        observation_id=obs_id,
        input={"q": "obs in"},
        expected_output="edited",
    )
    assert (view.source_trace_id, view.source_observation_id) == ("t1", obs_id)
    assert (view.input, view.expected_output) == ({"q": "obs in"}, "edited")
    whole = service.add_from_trace(db, store, world.project, actor(world), dataset, trace_id="t1")
    assert (whole.input, whole.expected_output) == ({"q": "trace in"}, "trace out")
    assert whole.metadata == {"k": "v"} and whole.source_observation_id is None
    with pytest.raises(service.DatasetNotFound):
        service.prefill_from_trace(store, world.project, "missing")
