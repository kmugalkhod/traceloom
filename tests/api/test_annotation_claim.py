from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from traceloom.annotation import claim, queues
from traceloom.annotation.claim import ClaimError
from traceloom.annotation.queues import ItemRef, QueueNotFound
from traceloom.auth.permissions import Role
from traceloom.db.models import AnnotationItem, Score
from traceloom.prompts.service import Actor, NotAllowed

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)


def actor_for(world, role):
    return Actor.for_user(world.users[role], role)


@pytest.fixture
def r1(world):
    return actor_for(world, Role.MEMBER)


@pytest.fixture
def r2(world):
    return actor_for(world, Role.ADMIN)


@pytest.fixture
def config(world, make_score_config):
    return make_score_config(world.project, "helpfulness")


@pytest.fixture
def queue(db, world, r1, config):
    q = queues.create_queue(db, world.project, r1, "Review", "", [config.id])
    db.commit()
    return q


def add(db, world, actor, queue, n, container):
    queues.add_items(
        db,
        world.project,
        actor,
        queue.id,
        [ItemRef("trace", f"t{i}") for i in range(n)],
        container.settings,
    )
    db.commit()


def scores(container, world, trace_id):
    with container.session_factory() as session:
        return list(
            session.scalars(
                select(Score).where(
                    Score.project_id == world.project.id, Score.trace_id == trace_id
                )
            )
        )


def test_twenty_concurrent_claims_one_wins(container, db, world, queue, r1):
    add(db, world, r1, queue, 1, container)
    results = [None] * 20
    barrier = threading.Barrier(20)

    def go(i):
        actor = Actor("user", f"reviewer-{i}", Role.MEMBER, f"R{i}")
        barrier.wait()
        with container.session_factory() as session:
            try:
                item = claim.claim_next(session, world.project, actor, queue.id, container.settings)
                session.commit()
                results[i] = item.id if item else None
            except Exception as exc:  # collected for the assertion
                results[i] = exc

    threads = [threading.Thread(target=go, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not [r for r in results if isinstance(r, Exception)], results
    winners = [r for r in results if r]
    assert len(winners) == 1
    db.expire_all()
    item = db.scalar(select(AnnotationItem))
    assert item.locked_by.startswith("reviewer-") and item.locked_until is not None


def test_claims_hand_out_distinct_items_under_concurrency(container, db, world, queue, r1):
    add(db, world, r1, queue, 10, container)
    got: list = []
    barrier = threading.Barrier(10)

    def go(i):
        actor = Actor("user", f"rv-{i}", Role.MEMBER, f"R{i}")
        barrier.wait()
        with container.session_factory() as session:
            item = claim.claim_next(session, world.project, actor, queue.id, container.settings)
            session.commit()
            got.append(item.id)

    threads = [threading.Thread(target=go, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(got)) == 10


def test_a_lock_blocks_the_other_reviewer_until_it_expires(container, db, world, queue, r1, r2):
    add(db, world, r1, queue, 1, container)
    settings = container.settings
    mine = claim.claim_next(db, world.project, r1, queue.id, settings, now=NOW)
    db.commit()
    assert mine.locked_by == r1.id
    assert mine.locked_until == NOW + timedelta(minutes=settings.annotation_lock_minutes)
    assert (
        claim.claim_next(db, world.project, r2, queue.id, settings, now=NOW + timedelta(minutes=5))
        is None
    )
    opened = claim.claim_item(
        db, world.project, r2, queue.id, mine.id, settings, now=NOW + timedelta(minutes=5)
    )
    assert (opened.state, opened.read_only) == ("locked", True)
    assert (
        opened.locked_by_name == world.users[Role.MEMBER].name
        or world.users[Role.MEMBER].email in opened.locked_by_name
        or opened.locked_by_name
    )
    later = NOW + timedelta(minutes=settings.annotation_lock_minutes + 1)
    taken = claim.claim_next(db, world.project, r2, queue.id, settings, now=later)
    db.commit()
    assert taken.id == mine.id and taken.locked_by == r2.id
    assert (
        claim.renew(db, world.project, r1, queue.id, mine.id, settings, now=later) is False
    )  # r1 lost it


def test_the_holder_gets_the_same_item_back_and_can_renew(container, db, world, queue, r1):
    add(db, world, r1, queue, 3, container)
    settings = container.settings
    first = claim.claim_next(db, world.project, r1, queue.id, settings, now=NOW)
    again = claim.claim_next(
        db, world.project, r1, queue.id, settings, now=NOW + timedelta(minutes=1)
    )
    assert again.id == first.id
    assert claim.renew(
        db, world.project, r1, queue.id, first.id, settings, now=NOW + timedelta(minutes=9)
    )
    db.commit()
    db.expire_all()
    assert db.get(AnnotationItem, first.id).locked_until == NOW + timedelta(
        minutes=9 + settings.annotation_lock_minutes
    )
    assert claim.release(db, world.project, r1, queue.id, first.id)
    db.commit()
    assert db.get(AnnotationItem, first.id, populate_existing=True).locked_by is None


def test_claim_next_takes_the_oldest_pending_item(container, db, world, queue, r1, r2):
    add(db, world, r1, queue, 3, container)
    items, _ = queues.list_items(db, world.project, r1, queue.id)
    a = claim.claim_next(db, world.project, r1, queue.id, container.settings)
    b = claim.claim_next(db, world.project, r2, queue.id, container.settings)
    db.commit()
    assert (a.id, b.id) == (items[0].id, items[1].id)


def test_complete_writes_one_annotation_score_per_config(
    container, db, world, queue, r1, make_score_config
):
    tone = make_score_config(world.project, "ok", data_type="BOOLEAN")
    cats = make_score_config(
        world.project,
        "tone",
        data_type="CATEGORICAL",
        categories=[{"label": "warm", "value": 1}, {"label": "cold", "value": 0}],
    )
    queues.update_queue(
        db, world.project, r1, queue.id, "Review", "", [*queue.config_ids, tone.id, cats.id]
    )
    add(db, world, r1, queue, 1, container)
    item = claim.claim_next(db, world.project, r1, queue.id, container.settings)
    values = {queue.config_ids[0]: 0.8, tone.id: True, cats.id: "warm"}
    ids = claim.complete(
        db,
        container.trace_store,
        world.project,
        r1,
        queue.id,
        item.id,
        values,
        comment="  nice  ",
        duration_ms=1234,
    )
    db.commit()
    assert len(ids) == 3
    got = {s.name: s for s in scores(container, world, "t0")}
    assert set(got) == {"helpfulness", "ok", "tone"}
    for s in got.values():
        assert (s.source, s.queue_id, s.author_id, s.comment) == (
            "ANNOTATION",
            queue.id,
            r1.id,
            "nice",
        )
    assert (
        got["helpfulness"].value == 0.8
        and got["ok"].value == 1.0
        and got["tone"].string_value == "warm"
    )
    db.expire_all()
    done = db.get(AnnotationItem, item.id)
    assert (done.status, done.completed_by, done.duration_ms, done.locked_by) == (
        "completed",
        r1.id,
        1234,
        None,
    )
    assert done.completed_at is not None


def test_complete_validates_before_writing_anything(container, db, world, queue, r1):
    add(db, world, r1, queue, 1, container)
    item = claim.claim_next(db, world.project, r1, queue.id, container.settings)
    cid = queue.config_ids[0]
    for values, message in (
        ({}, "helpfulness"),
        ({cid: 1.5}, "above the maximum"),
        ({cid: "x"}, "number"),
    ):
        with pytest.raises(ClaimError, match=message):
            claim.complete(db, container.trace_store, world.project, r1, queue.id, item.id, values)
    assert scores(container, world, "t0") == []
    db.expire_all()
    assert db.get(AnnotationItem, item.id).status == "pending"


def test_complete_needs_the_lock_and_is_not_repeatable(container, db, world, queue, r1, r2):
    add(db, world, r1, queue, 1, container)
    item = claim.claim_next(db, world.project, r1, queue.id, container.settings)
    db.commit()
    values = {queue.config_ids[0]: 0.5}
    with pytest.raises(ClaimError, match="locked by someone else"):
        claim.complete(db, container.trace_store, world.project, r2, queue.id, item.id, values)
    assert scores(container, world, "t0") == []
    claim.complete(db, container.trace_store, world.project, r1, queue.id, item.id, values)
    db.commit()
    with pytest.raises(ClaimError):
        claim.complete(db, container.trace_store, world.project, r1, queue.id, item.id, values)
    assert len(scores(container, world, "t0")) == 1


def test_complete_after_the_lock_expired_works_unless_someone_else_took_it(
    container, db, world, queue, r1, r2
):
    add(db, world, r1, queue, 1, container)
    settings = container.settings
    long_ago = datetime.now(UTC) - timedelta(minutes=settings.annotation_lock_minutes + 5)
    item = claim.claim_next(db, world.project, r1, queue.id, settings, now=long_ago)
    db.commit()
    claim.complete(
        db, container.trace_store, world.project, r1, queue.id, item.id, {queue.config_ids[0]: 1}
    )
    db.commit()
    assert db.get(AnnotationItem, item.id, populate_existing=True).status == "completed"


def test_skip_and_reopen(container, db, world, queue, r1, r2):
    add(db, world, r1, queue, 1, container)
    item = claim.claim_next(db, world.project, r1, queue.id, container.settings)
    with pytest.raises(ClaimError):
        claim.skip(db, world.project, r2, queue.id, item.id)  # r1 holds it
    claim.skip(db, world.project, r1, queue.id, item.id)
    db.commit()
    skipped = db.get(AnnotationItem, item.id, populate_existing=True)
    assert (skipped.status, skipped.locked_by, skipped.completed_by) == ("skipped", None, None)
    assert claim.claim_next(db, world.project, r1, queue.id, container.settings) is None
    assert (
        claim.claim_item(db, world.project, r1, queue.id, item.id, container.settings).state
        == "closed"
    )
    claim.reopen(db, world.project, r2, queue.id, item.id)
    db.commit()
    again = claim.claim_next(db, world.project, r2, queue.id, container.settings)
    assert again.id == item.id
    with pytest.raises(ClaimError, match="skipped"):
        claim.reopen(db, world.project, r2, queue.id, item.id)


def test_session_and_observation_items_score_the_right_object(container, db, world, queue, r1):
    queues.add_items(
        db, world.project, r1, queue.id,
        [ItemRef("session", "s1"), ItemRef("observation", "o1", "t9")], container.settings,
    )  # fmt: skip
    db.commit()
    cid = queue.config_ids[0]
    for _ in range(2):
        item = claim.claim_next(db, world.project, r1, queue.id, container.settings)
        claim.complete(db, container.trace_store, world.project, r1, queue.id, item.id, {cid: 1})
        db.commit()
    with container.session_factory() as session:
        rows = session.execute(
            select(Score.session_id, Score.trace_id, Score.observation_id).where(
                Score.project_id == world.project.id
            )
        ).all()
    assert sorted(map(tuple, rows), key=str) == sorted(
        [(None, "t9", "o1"), ("s1", None, None)], key=str
    )


def test_permissions_and_project_scope(container, db, world, queue, r1, make_org, make_project):
    add(db, world, r1, queue, 1, container)
    viewer = actor_for(world, Role.VIEWER)
    with pytest.raises(NotAllowed):
        claim.claim_next(db, world.project, viewer, queue.id, container.settings)
    item = db.scalar(select(AnnotationItem))
    opened = claim.claim_item(db, world.project, viewer, queue.id, item.id, container.settings)
    assert opened.read_only and opened.item.locked_by is None  # a viewer reads without locking
    other = make_project(make_org())
    with pytest.raises(QueueNotFound):
        claim.claim_next(db, other, r1, queue.id, container.settings)
    with pytest.raises(QueueNotFound):
        claim.claim_item(db, other, r1, queue.id, item.id, container.settings)
    with pytest.raises(NotAllowed):
        claim.complete(db, container.trace_store, world.project, viewer, queue.id, item.id, {})


class _FailingStore:
    """Delegates to the real store but fails the `fail_on`-th add_score call."""

    def __init__(self, store, fail_on):
        self._store, self._fail_on, self.calls = store, fail_on, 0

    def add_score(self, project_id, score):
        self.calls += 1
        if self.calls == self._fail_on:
            raise RuntimeError("store is down")
        return self._store.add_score(project_id, score)

    def __getattr__(self, name):
        return getattr(self._store, name)


def _two_config_item(container, db, world, queue, r1, make_score_config):
    ok = make_score_config(world.project, "ok", data_type="BOOLEAN")
    queues.update_queue(db, world.project, r1, queue.id, "Review", "", [*queue.config_ids, ok.id])
    add(db, world, r1, queue, 1, container)
    item = claim.claim_next(db, world.project, r1, queue.id, container.settings)
    db.commit()
    return item, {queue.config_ids[0]: 0.8, ok.id: True}


def test_complete_writes_nothing_when_a_score_write_fails(
    container, db, world, queue, r1, make_score_config
):
    item, values = _two_config_item(container, db, world, queue, r1, make_score_config)
    store = _FailingStore(container.trace_store, fail_on=2)
    with pytest.raises(RuntimeError):
        claim.complete(db, store, world.project, r1, queue.id, item.id, values)
    db.rollback()
    assert scores(container, world, "t0") == []
    db.expire_all()
    assert db.get(AnnotationItem, item.id).status == "pending"


def test_complete_removes_its_scores_when_closing_the_item_fails(
    container, db, world, queue, r1, make_score_config, monkeypatch
):
    item, values = _two_config_item(container, db, world, queue, r1, make_score_config)

    def lost(*args, **kwargs):
        raise ClaimError("This item is locked by someone else or is already done.")

    monkeypatch.setattr(claim, "_close", lost)
    with pytest.raises(ClaimError):
        claim.complete(db, container.trace_store, world.project, r1, queue.id, item.id, values)
    db.rollback()
    assert scores(container, world, "t0") == []
    db.expire_all()
    assert db.get(AnnotationItem, item.id).status == "pending"


def test_complete_commits_the_item_status_with_the_scores(
    container, db, world, queue, r1, make_score_config
):
    item, values = _two_config_item(container, db, world, queue, r1, make_score_config)
    claim.complete(db, container.trace_store, world.project, r1, queue.id, item.id, values)
    db.rollback()  # nothing is left for the caller to commit
    assert len(scores(container, world, "t0")) == 2
    db.expire_all()
    assert db.get(AnnotationItem, item.id).status == "completed"
