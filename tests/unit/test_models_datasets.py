import pytest
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import IntegrityError

from traceloom.db.models_datasets import Dataset, DatasetItem, DatasetItemVersion, DatasetRunItem
from traceloom.db.session import make_engine, upgrade_to_head


def test_factories_produce_valid_rows(
    db, world, make_dataset, make_dataset_item, make_dataset_run, make_dataset_run_item
):
    dataset = make_dataset(world.project, "qa")
    item = make_dataset_item(dataset, {"q": "hi"}, {"a": "hello"})
    run = make_dataset_run(dataset, "baseline")
    run_item = make_dataset_run_item(run, item, output={"text": "hello"}, latency_ms=12.5)
    version = db.get(DatasetItemVersion, (item.id, 1))
    assert (item.current_version, item.status, item.project_id) == (1, "active", world.project.id)
    assert (version.input, version.expected_output) == ({"q": "hi"}, {"a": "hello"})
    assert (run.status, run.dataset_version is not None) == ("running", True)
    assert (run_item.item_version, run_item.status, run_item.output) == (
        1,
        "pending",
        {"text": "hello"},
    )


def test_dataset_name_is_unique_per_project(db, world, make_org, make_project, make_dataset):
    make_dataset(world.project, "qa")
    with pytest.raises(IntegrityError):
        make_dataset(world.project, "qa")
    db.rollback()
    make_dataset(make_project(make_org()), "qa")  # another project may reuse the name
    assert db.query(Dataset).filter_by(name="qa").count() == 2


def test_run_name_is_unique_per_dataset(db, world, make_dataset, make_dataset_run):
    first, second = make_dataset(world.project), make_dataset(world.project)
    make_dataset_run(first, "v1")
    make_dataset_run(second, "v1")
    with pytest.raises(IntegrityError):
        make_dataset_run(first, "v1")
    db.rollback()


def test_item_version_key_is_unique(db, world, make_dataset, make_dataset_item):
    item = make_dataset_item(make_dataset(world.project))
    db.add(DatasetItemVersion(item_id=item.id, version=1, project_id=world.project.id))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_deleting_a_dataset_cascades(
    db, world, make_dataset, make_dataset_item, make_dataset_run, make_dataset_run_item
):
    dataset = make_dataset(world.project)
    item = make_dataset_item(dataset)
    make_dataset_run_item(make_dataset_run(dataset), item)
    db.delete(dataset)
    db.commit()
    assert [db.query(m).count() for m in (DatasetItem, DatasetItemVersion, DatasetRunItem)] == [
        0,
        0,
        0,
    ]


def test_migration_creates_dataset_tables(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'm.db'}")
    upgrade_to_head(engine)
    insp = sa_inspect(engine)
    assert {
        "datasets",
        "dataset_items",
        "dataset_item_versions",
        "dataset_runs",
        "dataset_run_items",
    } <= set(insp.get_table_names())
    assert {"output", "latency_ms", "cost", "estimated_cost"} <= {
        c["name"] for c in insp.get_columns("dataset_run_items")
    }
    engine.dispose()
