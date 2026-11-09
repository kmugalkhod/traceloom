from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from traceloom.annotation import claim, queues
from traceloom.annotation.queues import ItemRef, QueueError, QueueNotFound
from traceloom.auth.permissions import Role
from traceloom.db.base import utcnow
from traceloom.db.models import AnnotationItem, AuditLog
from traceloom.ports import TraceQuery, TraceRow
from traceloom.prompts.service import Actor, NotAllowed

T0 = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)


@pytest.fixture
def member(world):
    return Actor.for_user(world.users[Role.MEMBER], Role.MEMBER)


@pytest.fixture
def admin(world):
    return Actor.for_user(world.users[Role.ADMIN], Role.ADMIN)


@pytest.fixture
def viewer(world):
    return Actor.for_user(world.users[Role.VIEWER], Role.VIEWER)


@pytest.fixture
def config(world, make_score_config):
    return make_score_config(world.project, "helpfulness")


@pytest.fixture
def queue(db, world, member, config):
    q = queues.create_queue(db, world.project, member, "Refund review", "desc", [config.id])
    db.commit()
    return q


def put_traces(container, project_id, names):
    rows = [TraceRow(f"t{i}", T0 + timedelta(seconds=i), name=n) for i, n in enumerate(names)]
    container.trace_store.upsert(project_id, rows, [], [])
    return rows


def actions(db):
    db.expire_all()
    return [
        a.action for a in db.scalars(select(AuditLog).order_by(AuditLog.created_at, AuditLog.id))
    ]


# --- create, edit, delete -------------------------------------------------------------------


def test_create_validates_name_and_configs(
    db, world, member, config, make_score_config, make_org, make_project
):
    with pytest.raises(QueueError, match="name"):
        queues.create_queue(db, world.project, member, " ", "", [config.id])
    with pytest.raises(QueueError, match="at least one"):
        queues.create_queue(db, world.project, member, "q", "", [])
    foreign = make_score_config(make_project(make_org()), "theirs")
    with pytest.raises(QueueError, match="from this project"):
        queues.create_queue(db, world.project, member, "q", "", [foreign.id])
    from traceloom.db.base import utcnow as now

    archived = make_score_config(world.project, "old", archived_at=now())
    with pytest.raises(QueueError, match="archived"):
        queues.create_queue(db, world.project, member, "q", "", [archived.id])
    q = queues.create_queue(db, world.project, member, "  ok  ", "d", [config.id, config.id])
    assert (q.name, q.config_ids, q.created_by) == ("ok", [config.id], member.id)
    assert actions(db) == ["annotation_queue.create"]


def test_edit_keeps_an_archived_config_already_on_the_queue(
    db, world, member, queue, config, make_score_config
):
    other = make_score_config(world.project, "tone")
    config.archived_at = utcnow()
    db.commit()
    updated = queues.update_queue(
        db, world.project, member, queue.id, "Renamed", "", [config.id, other.id]
    )
    assert updated.name == "Renamed" and updated.config_ids == [config.id, other.id]
    assert [c.name for c in queues.queue_configs(db, world.project, updated)] == [
        "helpfulness",
        "tone",
    ]


def test_permissions(db, world, queue, viewer, member, admin, config):
    with pytest.raises(NotAllowed):
        queues.create_queue(db, world.project, viewer, "q", "", [config.id])
    with pytest.raises(NotAllowed):
        queues.update_queue(db, world.project, viewer, queue.id, "x", "", [config.id])
    with pytest.raises(NotAllowed):
        queues.add_items(
            db, world.project, viewer, queue.id, [ItemRef("trace", "t")], world_settings(db)
        )
    with pytest.raises(NotAllowed):
        queues.delete_queue(db, world.project, member, queue.id)
    assert queues.get_queue(db, world.project, viewer, queue.id).id == queue.id  # viewers read
    keyless = Actor.for_key("k1")
    with pytest.raises(NotAllowed):
        queues.list_queues(db, world.project, keyless)


def world_settings(db):
    from traceloom.config import Settings

    return Settings(TRACELOOM_ENCRYPTION_KEY="dHJhY2Vsb29tLXRlc3Qta2V5LW5vdC1mb3ItcHJvZCE=")


def test_queries_are_project_scoped(
    db, world, member, queue, make_org, make_project, make_score_config
):
    other = make_project(make_org())
    foreign = Actor.for_user(world.users[Role.MEMBER], Role.MEMBER)
    with pytest.raises(QueueNotFound):
        queues.get_queue(db, other, foreign, queue.id)
    with pytest.raises(QueueNotFound):
        queues.add_items(db, other, foreign, queue.id, [ItemRef("trace", "t")], world_settings(db))
    assert queues.list_queues(db, other, foreign) == []
    item = queues.add_items(
        db, world.project, member, queue.id, [ItemRef("trace", "t1")], world_settings(db)
    )
    assert item.added == 1
    row = db.scalar(select(AnnotationItem))
    with pytest.raises(QueueNotFound):
        queues.get_item(db, other, foreign, queue.id, row.id)


def test_admin_deletes_a_queue_and_its_items(db, world, member, admin, queue):
    queues.add_items(
        db, world.project, member, queue.id, [ItemRef("trace", "t1")], world_settings(db)
    )
    db.commit()
    queues.delete_queue(db, world.project, admin, queue.id)
    db.commit()
    assert db.query(AnnotationItem).count() == 0
    assert actions(db)[-1] == "annotation_queue.delete"
    with pytest.raises(QueueNotFound):
        queues.get_queue(db, world.project, admin, queue.id)


# --- items -----------------------------------------------------------------------------------


def test_add_items_dedupes_and_reports_counts(db, world, member, queue):
    settings = world_settings(db)
    refs = [ItemRef("trace", "t1"), ItemRef("trace", "t2"), ItemRef("trace", "t1")]
    first = queues.add_items(db, world.project, member, queue.id, refs, settings)
    assert (first.added, first.duplicates) == (2, 1)
    again = queues.add_items(
        db,
        world.project,
        member,
        queue.id,
        [ItemRef("trace", "t2"), ItemRef("trace", "t3")],
        settings,
    )
    assert (again.added, again.duplicates) == (1, 1)
    # the same id as another object type is a different item
    mixed = queues.add_items(
        db, world.project, member, queue.id,
        [ItemRef("observation", "t1", "tr9"), ItemRef("session", "s1")], settings,
    )  # fmt: skip
    assert mixed.added == 2
    items, total = queues.list_items(db, world.project, member, queue.id)
    assert total == 5
    obs = next(i for i in items if i.object_type == "observation")
    assert obs.trace_id == "tr9"
    with pytest.raises(QueueError):
        queues.add_items(db, world.project, member, queue.id, [ItemRef("dataset", "x")], settings)


def test_add_from_filter_reports_added_and_duplicates(container, db, world, member, queue):
    put_traces(container, world.project.id, ["chat"] * 4 + ["other"] * 3)
    query = TraceQuery(name="chat")
    first = queues.add_from_filter(
        db, world.project, member, queue.id, container.trace_store, query, container.settings
    )
    assert (first.added, first.duplicates, first.capped) == (4, 0, False)
    again = queues.add_from_filter(
        db, world.project, member, queue.id, container.trace_store, query, container.settings
    )
    assert (again.added, again.duplicates) == (0, 4)
    wider = queues.add_from_filter(
        db, world.project, member, queue.id, container.trace_store, TraceQuery(), container.settings
    )
    assert (wider.added, wider.duplicates) == (3, 4)


def test_add_from_filter_is_capped_by_queue_bulk_max(
    container, db, world, member, queue, monkeypatch
):
    put_traces(container, world.project.id, ["chat"] * 6)
    monkeypatch.setattr(container.settings, "queue_bulk_max", 4)
    result = queues.add_from_filter(
        db, world.project, member, queue.id, container.trace_store, TraceQuery(), container.settings
    )
    assert (result.added, result.capped) == (4, True)
    assert db.query(AnnotationItem).count() == 4


def test_list_items_filters_by_status_and_reviewer(db, world, member, admin, queue):
    settings = world_settings(db)
    queues.add_items(
        db, world.project, member, queue.id, [ItemRef("trace", f"t{i}") for i in range(4)], settings
    )
    db.commit()
    first = claim.claim_next(db, world.project, member, queue.id, settings)
    claim.skip(db, world.project, member, queue.id, first.id)
    second = claim.claim_next(db, world.project, admin, queue.id, settings)
    db.commit()
    assert [
        i.id for i in queues.list_items(db, world.project, member, queue.id, status="skipped")[0]
    ] == [first.id]
    assert [
        i.id for i in queues.list_items(db, world.project, member, queue.id, reviewer=admin.id)[0]
    ] == [second.id]
    page, total = queues.list_items(db, world.project, member, queue.id, limit=2, offset=1)
    assert (len(page), total) == (2, 4)
    with pytest.raises(QueueError):
        queues.list_items(db, world.project, member, queue.id, status="bogus")


def test_stats_counts_per_reviewer_completed_and_median_time(
    container, db, world, member, admin, queue, config
):
    settings = container.settings
    queues.add_items(
        db, world.project, member, queue.id, [ItemRef("trace", f"t{i}") for i in range(6)], settings
    )
    db.commit()
    for actor, durations in ((member, [10_000, 30_000, 20_000]), (admin, [5_000])):
        for ms in durations:
            item = claim.claim_next(db, world.project, actor, queue.id, settings)
            claim.complete(
                db,
                container.trace_store,
                world.project,
                actor,
                queue.id,
                item.id,
                {config.id: 0.5},
                duration_ms=ms,
            )
            db.commit()
    skipped = claim.claim_next(db, world.project, member, queue.id, settings)
    claim.skip(db, world.project, member, queue.id, skipped.id)
    db.commit()
    stats = queues.stats(db, world.project, member, queue.id)
    assert (stats.total, stats.completed, stats.skipped, stats.pending) == (6, 4, 1, 1)
    assert stats.percent == 67 and not stats.complete
    by_id = {r.user_id: r for r in stats.reviewers}
    assert by_id[member.id].completed == 3 and by_id[member.id].median_ms == 20_000
    assert by_id[admin.id].completed == 1 and by_id[admin.id].median_ms == 5_000
    assert stats.reviewers[0].user_id == member.id and by_id[member.id].last_activity is not None
    listing = queues.list_queues(db, world.project, member)
    assert [(q.id, s.completed) for q, s in listing] == [(queue.id, 4)]


def test_queue_is_complete_when_nothing_is_left(container, db, world, member, queue, config):
    queues.add_items(
        db, world.project, member, queue.id, [ItemRef("trace", "t1")], container.settings
    )
    item = claim.claim_next(db, world.project, member, queue.id, container.settings)
    claim.complete(
        db, container.trace_store, world.project, member, queue.id, item.id, {config.id: 1}
    )
    db.commit()
    stats = queues.stats(db, world.project, member, queue.id)
    assert stats.complete and stats.percent == 100
