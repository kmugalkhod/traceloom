"""QA adversarial checks for annotation queues (T-11, T-12)."""

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
from traceloom.ports import TraceRow
from traceloom.prompts.service import Actor

HTML = {"Accept": "text/html"}
T0 = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)


@pytest.fixture
def r1(world):
    return Actor.for_user(world.users[Role.MEMBER], Role.MEMBER)


@pytest.fixture
def config(world, make_score_config):
    return make_score_config(world.project, "helpfulness")


@pytest.fixture
def queue(db, world, r1, config):
    q = queues.create_queue(db, world.project, r1, "Review", "", [config.id])
    db.commit()
    return q


def add(db, world, actor, queue, container, ids):
    queues.add_items(
        db, world.project, actor, queue.id, [ItemRef("trace", i) for i in ids], container.settings
    )
    db.commit()


def test_double_submit_of_complete_writes_scores_once(container, db, world, queue, r1, config):
    add(db, world, r1, queue, container, ["t0"])
    item = claim.claim_next(db, world.project, r1, queue.id, container.settings)
    db.commit()
    outcomes = []
    barrier = threading.Barrier(2)

    def go():
        barrier.wait()
        with container.session_factory() as session:
            try:
                claim.complete(
                    session,
                    container.trace_store,
                    world.project,
                    r1,
                    queue.id,
                    item.id,
                    {config.id: 0.5},
                )
                session.commit()
                outcomes.append("ok")
            except ClaimError:
                outcomes.append("refused")

    threads = [threading.Thread(target=go) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert "ok" in outcomes
    with container.session_factory() as session:
        n = len(list(session.scalars(select(Score).where(Score.project_id == world.project.id))))
    assert n == 1


def test_item_of_another_queue_is_not_reachable_through_this_one(
    container, db, world, queue, r1, config
):
    other = queues.create_queue(db, world.project, r1, "Other", "", [config.id])
    add(db, world, r1, other, container, ["x1"])
    item = db.scalar(select(AnnotationItem).where(AnnotationItem.queue_id == other.id))
    for call in (
        lambda: claim.claim_item(db, world.project, r1, queue.id, item.id, container.settings),
        lambda: claim.skip(db, world.project, r1, queue.id, item.id),
        lambda: claim.release(db, world.project, r1, queue.id, item.id),
    ):
        with pytest.raises(QueueNotFound):
            call()


def test_cap_applies_after_dedupe_and_counts_repeats(container, db, world, queue, r1, monkeypatch):
    monkeypatch.setattr(container.settings, "queue_bulk_max", 3)
    result = queues.add_items(
        db,
        world.project,
        r1,
        queue.id,
        [ItemRef("trace", i) for i in ["a", "a", "b", "c", "d", "e"]],
        container.settings,
    )
    assert (result.added, result.capped) == (3, True)
    assert result.duplicates == 1


def test_negative_and_huge_durations_are_clamped(container, db, world, queue, r1, config):
    add(db, world, r1, queue, container, ["t0", "t1"])
    for duration in (-5, 10**15):
        item = claim.claim_next(db, world.project, r1, queue.id, container.settings)
        db.commit()
        claim.complete(
            db,
            container.trace_store,
            world.project,
            r1,
            queue.id,
            item.id,
            {config.id: 0.5},
            duration_ms=duration,
        )
        db.commit()
    db.expire_all()
    durations = [i.duration_ms for i in db.scalars(select(AnnotationItem))]
    assert all(0 <= d <= 24 * 3600 * 1000 for d in durations)


def test_skipped_status_pill_is_not_hidden_by_the_skip_link_class(
    client, login, world, db, container, queue, r1
):
    """The items table shows 'Skipped' as a pill. The class `skip` is also the off-screen
    'Skip to content' link in app.css, so a pill using it renders invisible."""
    container.trace_store.upsert(world.project.id, [TraceRow("t0", T0, name="chat")], [], [])
    add(db, world, r1, queue, container, ["t0"])
    item = claim.claim_next(db, world.project, r1, queue.id, container.settings)
    claim.skip(db, world.project, r1, queue.id, item.id)
    db.commit()
    login(client, world.users[Role.MEMBER])
    html = client.get(f"/project/{world.project.id}/queues/{queue.id}", headers=HTML).text
    assert "Skipped</span>" in html
    assert 'class="pill skip"' not in html
    _ = timedelta
