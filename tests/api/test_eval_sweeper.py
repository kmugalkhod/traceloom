from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from traceloom.db.models_eval import EvalSpendDaily
from traceloom.evals import rules, spend
from traceloom.queue_registry import EVAL_JOB


@pytest.fixture
def jobs(container):
    seen: list[dict] = []
    container.queue.register(EVAL_JOB, lambda message: seen.append(dict(message)))
    return seen


def age(db, execution, **fields):
    for key, value in fields.items():
        setattr(execution, key, value)
    db.commit()


def test_the_sweeper_re_enqueues_due_pending_jobs(
    container, db, world, jobs, make_eval_rule, make_eval_execution
):
    rule = make_eval_rule(world.project)
    now = datetime.now(UTC)
    due = make_eval_execution(rule, "due", run_after=now - timedelta(minutes=5))
    make_eval_execution(rule, "fresh", run_after=now - timedelta(seconds=2))  # message en route
    make_eval_execution(rule, "future", run_after=now + timedelta(hours=1))
    make_eval_execution(rule, "done", run_after=now - timedelta(hours=1), status="succeeded")
    make_eval_execution(rule, "capped", run_after=now - timedelta(hours=1), status="capped")

    assert container.sweeper.run_once() >= 1
    container.queue.drain()
    assert jobs == [{"type": EVAL_JOB, "execution_id": due.id}]


def test_stuck_running_jobs_are_reset_released_and_re_enqueued(
    container, db, world, jobs, make_eval_rule, make_eval_execution, settings
):
    rule = make_eval_rule(world.project)
    now = datetime.now(UTC)
    stuck = make_eval_execution(
        rule,
        "stuck",
        status="running",
        started_at=now - timedelta(seconds=settings.eval_stuck_seconds + 60),
        estimated_cost=0.4,
    )
    busy = make_eval_execution(rule, "busy", status="running", started_at=now, estimated_cost=0.2)
    spend.reserve(container.session_factory, world.project.id, spend.Estimate(0.6, True), cap=5)

    messages = rules.due_messages(container)
    assert messages == [{"type": EVAL_JOB, "execution_id": stuck.id}]
    db.expire_all()
    assert (stuck.status, stuck.started_at, stuck.estimated_cost) == ("pending", None, 0.0)
    assert (busy.status, busy.estimated_cost) == ("running", 0.2)  # a live job is left alone
    ledger = db.get(EvalSpendDaily, (world.project.id, spend.utc_day()))
    assert ledger.reserved_usd == pytest.approx(0.2)  # the dead job's hold was freed

    assert rules.due_messages(container) == []  # the reset job is not due again yet


def test_the_sweep_batch_limits_one_pass(
    container, db, world, make_eval_rule, make_eval_execution, monkeypatch
):
    rule = make_eval_rule(world.project)
    old = datetime.now(UTC) - timedelta(hours=1)
    for i in range(5):
        make_eval_execution(rule, f"o{i}", run_after=old + timedelta(seconds=i))
    monkeypatch.setattr(container.settings, "eval_sweep_batch", 3)
    assert len(rules.due_messages(container)) == 3


def test_a_lost_delayed_message_is_recovered_after_a_restart(
    container, db, world, jobs, make_eval_rule, make_eval_execution
):
    """The queue is only a wake-up: dropping it (a crash) loses nothing the rows still hold."""
    rule = make_eval_rule(world.project)
    execution = make_eval_execution(rule, "t1")
    container.queue.enqueue({"type": EVAL_JOB, "execution_id": execution.id}, 3600)
    assert container.queue.discard() >= 1
    age(db, execution, run_after=datetime.now(UTC) - timedelta(minutes=2))
    container.sweeper.run_once()
    container.queue.drain()
    assert [m["execution_id"] for m in jobs] == [execution.id]


def test_the_sweeper_is_wired_at_startup_and_builtins_are_seeded(client, container, db):
    from traceloom.db.models_eval import EvalTemplate

    assert container.sweeper._due_jobs is not None
    db.expire_all()
    assert db.query(EvalTemplate).filter(EvalTemplate.project_id.is_(None)).count() == 6


def test_job_sources_that_return_nothing_are_harmless(container):
    from traceloom.ingest.sweeper import Sweeper

    sweeper = Sweeper(container.blob_store, container.queue, 1, 1)
    assert sweeper.run_once() == 0
    sweeper.set_due_jobs(lambda: [{"type": EVAL_JOB, "execution_id": "x"}])
    assert sweeper.run_once() == 1
