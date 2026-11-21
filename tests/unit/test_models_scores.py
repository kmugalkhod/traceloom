import pytest
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import IntegrityError

from traceloom.db.models import AnnotationItem, Score, ScoreConfig
from traceloom.db.session import make_engine, upgrade_to_head


def test_factories_produce_valid_rows(db, world, make_score_config, make_queue, make_queue_item):
    config = make_score_config(world.project, "helpfulness")
    queue = make_queue(world.project, configs=[config])
    item = make_queue_item(queue)
    assert (config.data_type, config.min_value, config.max_value) == ("NUMERIC", 0.0, 1.0)
    assert config.archived_at is None and config.public_feedback is False
    assert queue.config_ids == [config.id]
    assert (item.status, item.object_type, item.queue_id) == ("pending", "trace", queue.id)
    assert item.project_id == world.project.id


def test_annotation_item_is_unique_per_queue_and_object(db, world, make_queue, make_queue_item):
    queue, other = make_queue(world.project), make_queue(world.project)
    make_queue_item(queue, "t1")
    make_queue_item(queue, "t1", object_type="observation")  # another object type is fine
    make_queue_item(other, "t1")  # another queue is fine
    with pytest.raises(IntegrityError):
        make_queue_item(queue, "t1")
    db.rollback()
    assert db.query(AnnotationItem).filter_by(queue_id=queue.id).count() == 2


def test_config_name_is_unique_among_live_rows_only(
    db, world, make_org, make_project, make_score_config
):
    from traceloom.db.base import utcnow

    first = make_score_config(world.project, "quality")
    with pytest.raises(IntegrityError):
        make_score_config(world.project, "quality")
    db.rollback()

    first.archived_at = utcnow()
    db.commit()
    again = make_score_config(world.project, "quality")  # the name is free once archived
    make_score_config(world.project, "quality", archived_at=utcnow())  # several archived are fine
    assert again.id != first.id

    other_project = make_project(make_org())
    make_score_config(other_project, "quality")  # uniqueness is per project
    assert db.query(ScoreConfig).filter_by(name="quality").count() == 4


def test_deleting_a_queue_removes_its_items(db, world, make_queue, make_queue_item):
    queue = make_queue(world.project)
    make_queue_item(queue)
    db.delete(queue)
    db.commit()
    assert db.query(AnnotationItem).count() == 0


def test_migration_adds_score_columns_and_index(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'm.db'}")
    upgrade_to_head(engine)
    insp = sa_inspect(engine)
    assert {"queue_id", "execution_id", "channel"} <= {
        c["name"] for c in insp.get_columns("scores")
    }
    indexes = {i["name"]: i["column_names"] for i in insp.get_indexes("scores")}
    assert indexes["ix_scores_project_name_timestamp"] == ["project_id", "name", "timestamp"]
    assert Score.__table__.c.channel.nullable
    engine.dispose()
