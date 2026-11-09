from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tests.api.test_ingestion import auth, event, post
from traceloom.db.models_eval import EvalExecution
from traceloom.evals import rules
from traceloom.evals.backfill import backfill
from traceloom.ports import TraceRow
from traceloom.queue_registry import EVAL_JOB

T0 = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)


@pytest.fixture
def headers(world, make_key):
    _, public, secret = make_key(world.project)
    return auth(public, secret)


@pytest.fixture
def jobs(container):
    """Collects the eval_job messages the queue delivers (the runner is a later task)."""
    seen: list[dict] = []
    container.queue.register(EVAL_JOB, lambda message: seen.append(dict(message)))
    return seen


def trace_events(names, environment="default", prefix="t"):
    return [
        event(
            f"e-{prefix}{i}",
            "trace-create",
            {"id": f"{prefix}{i}", "name": name, "environment": environment, "input": "q"},
        )
        for i, name in enumerate(names)
    ]


def ingest(client, container, headers, events):
    assert post(client, headers, events).status_code == 207
    container.queue.drain()


def executions(db):
    db.expire_all()
    return db.query(EvalExecution).all()


def test_only_hash_selected_chat_traces_get_an_execution(
    client, container, db, world, headers, jobs, make_eval_rule
):
    rule = make_eval_rule(
        world.project, filter={"name": "chat"}, sampling_pct=40.0, delay_seconds=0
    )
    names = ["chat"] * 20 + ["other"] * 10
    ingest(client, container, headers, trace_events(names))

    expected = {f"t{i}" for i in range(20) if rules.is_sampled(rule.id, f"t{i}", 40.0)}
    assert 0 < len(expected) < 20  # the sample is a strict subset for these ids
    rows = executions(db)
    assert {e.object_id for e in rows} == expected  # `other` traces never match
    assert all(
        (e.status, e.rule_id, e.object_type, e.trace_id)
        == ("pending", rule.id, "trace", e.object_id)
        for e in rows
    )
    assert {m["execution_id"] for m in jobs} == {e.id for e in rows}
    assert all(m["type"] == EVAL_JOB for m in jobs)


def test_sampling_is_deterministic_and_proportional():
    ids = [f"trace-{i}" for i in range(2000)]
    picked = [i for i in ids if rules.is_sampled("rule-a", i, 25)]
    assert picked == [i for i in ids if rules.is_sampled("rule-a", i, 25)]
    assert 400 < len(picked) < 600
    assert all(rules.is_sampled("rule-a", i, 100) for i in ids)
    assert not any(rules.is_sampled("rule-a", i, 0) for i in ids)
    assert picked != [i for i in ids if rules.is_sampled("rule-b", i, 25)]


def test_reingesting_does_not_create_a_second_execution(
    client, container, db, world, headers, jobs, make_eval_rule
):
    make_eval_rule(world.project, delay_seconds=0)
    batch = trace_events(["chat"] * 3)
    ingest(client, container, headers, batch)
    first = {e.id for e in executions(db)}
    ingest(client, container, headers, batch)  # the same events again
    ingest(  # and an update to an existing trace
        client,
        container,
        headers,
        [event("u1", "trace-create", {"id": "t0", "name": "chat", "output": "a"}, at=5)],
    )
    assert {e.id for e in executions(db)} == first and len(first) == 3
    assert len(jobs) == 3  # no extra wake-ups either


def test_judge_and_experiment_environments_never_trigger_rules(
    client, container, db, world, headers, jobs, make_eval_rule, settings
):
    make_eval_rule(world.project, delay_seconds=0)
    ingest(client, container, headers, trace_events(["x"], settings.eval_environment, "j"))
    ingest(client, container, headers, trace_events(["x"], settings.experiment_environment, "x"))
    assert executions(db) == [] and jobs == []
    ingest(client, container, headers, trace_events(["x"], "production", "p"))
    assert [e.object_id for e in executions(db)] == ["p0"]


def test_observation_rules_match_name_and_type(
    client, container, db, world, headers, jobs, make_eval_rule
):
    rule = make_eval_rule(
        world.project,
        target="observation",
        target_name="answer",
        target_type="GENERATION",
        delay_seconds=0,
    )
    ingest(
        client,
        container,
        headers,
        [
            event("a", "generation-create", {"id": "g1", "traceId": "t1", "name": "answer"}),
            event("b", "generation-create", {"id": "g2", "traceId": "t1", "name": "other"}),
            event("c", "span-create", {"id": "s1", "traceId": "t1", "name": "answer"}),
            event("d", "trace-create", {"id": "t1", "name": "answer"}),
        ],
    )
    rows = executions(db)
    assert [(e.object_type, e.object_id, e.trace_id, e.rule_id) for e in rows] == [
        ("observation", "g1", "t1", rule.id)
    ]


def test_filter_on_tags_metadata_and_environment(
    client, container, db, world, headers, jobs, make_eval_rule
):
    make_eval_rule(
        world.project,
        filter={"tags": ["vip", "beta"], "metadata": {"tier": "pro"}, "environment": "production"},
        delay_seconds=0,
    )

    def tr(id_, **body):
        return event(f"e-{id_}", "trace-create", {"id": id_, "name": "n", **body})

    ingest(
        client,
        container,
        headers,
        [
            tr("ok", tags=["vip"], metadata={"tier": "pro"}, environment="production"),
            tr("no-tag", tags=["x"], metadata={"tier": "pro"}, environment="production"),
            tr("no-meta", tags=["vip"], metadata={"tier": "free"}, environment="production"),
            tr("no-env", tags=["beta"], metadata={"tier": "pro"}, environment="staging"),
        ],
    )
    assert [e.object_id for e in executions(db)] == ["ok"]


def test_the_delay_sets_run_after_and_a_delayed_enqueue(
    container, db, world, make_eval_rule, monkeypatch
):
    make_eval_rule(world.project, delay_seconds=3600)
    sent = []
    monkeypatch.setattr(
        container.queue, "enqueue", lambda m, delay_seconds=0: sent.append((m, delay_seconds))
    )
    before = datetime.now(UTC)
    assert (
        rules.on_ingested(container, world.project.id, [TraceRow("t1", T0, name="chat")], []) == 1
    )
    (execution,) = executions(db)
    assert before + timedelta(seconds=3599) <= execution.run_after
    assert execution.run_after <= datetime.now(UTC) + timedelta(seconds=3601)
    assert sent == [({"type": EVAL_JOB, "execution_id": execution.id}, 3600)]


def test_a_disabled_rule_and_other_projects_do_not_trigger(
    client, container, db, world, headers, jobs, make_eval_rule, make_org, make_project
):
    make_eval_rule(world.project, delay_seconds=0, enabled=False)
    make_eval_rule(make_project(make_org()), delay_seconds=0)  # someone else's rule
    ingest(client, container, headers, trace_events(["chat"]))
    assert executions(db) == []


def test_a_rule_error_never_fails_ingestion(
    client, container, db, world, headers, jobs, make_eval_rule, monkeypatch
):
    make_eval_rule(world.project, delay_seconds=0)

    def boom(*args, **kwargs):
        raise RuntimeError("matching exploded")

    monkeypatch.setattr(rules, "load_rules", boom)
    ingest(client, container, headers, trace_events(["chat"]))
    got = container.trace_store.get_existing(world.project.id, {"trace": ["t0"]})
    assert "t0" in got["trace"]  # the trace is stored
    assert not list(container.blob_store.list("pending/"))  # and the batch was marked processed
    assert executions(db) == []


def test_a_failing_enqueue_keeps_the_row_for_the_sweeper(
    client, container, db, world, headers, jobs, make_eval_rule, monkeypatch
):
    make_eval_rule(world.project, delay_seconds=0)
    real = container.queue.enqueue

    def flaky(message, delay_seconds=0):
        if message.get("type") == EVAL_JOB:
            raise ConnectionError("redis down")
        real(message, delay_seconds)

    monkeypatch.setattr(container.queue, "enqueue", flaky)
    ingest(client, container, headers, trace_events(["chat"]))
    assert len(executions(db)) == 1 and jobs == []


def test_rules_are_cached_and_invalidated(client, container, db, world, make_eval_rule):
    assert rules.load_rules(container, world.project.id) == []
    rule = make_eval_rule(world.project)
    assert rules.load_rules(container, world.project.id) == []  # still the cached answer
    rules.invalidate_rules(container, world.project.id)
    assert [r.id for r in rules.load_rules(container, world.project.id)] == [rule.id]


def test_a_rule_pinned_to_a_version_resolves_it(
    container, db, world, make_eval_rule, make_eval_template
):
    from traceloom.evals import templates

    template = make_eval_template(world.project, "t")
    first = template.latest
    templates.save_version(db, world.project.id, template.id, "v2 {{output}}", first.output_schema)
    db.commit()
    pinned = make_eval_rule(world.project, template, template_version=1)
    make_eval_rule(world.project, template)  # follows the latest
    got = {r.id: r.template_version_id for r in rules.load_rules(container, world.project.id)}
    assert got[pinned.id] == first.id
    assert sorted(got.values()) != [first.id, first.id]


# --- backfill ----------------------------------------------------------------------------------


def test_backfill_respects_its_cap(
    client, container, db, world, headers, jobs, make_eval_rule, monkeypatch
):
    ingest(client, container, headers, trace_events(["chat"] * 12))
    rule = make_eval_rule(world.project, filter={"name": "chat"}, delay_seconds=999)
    monkeypatch.setattr(container.settings, "eval_backfill_max", 5)

    result = backfill(container, world.project.id, rule.id)
    assert (result.created, result.capped) == (5, True)
    assert len(executions(db)) == 5
    container.queue.drain()
    assert len(jobs) == 5  # backfill runs without the rule's delay

    again = backfill(container, world.project.id, rule.id)  # continues past the existing five
    assert (again.created, again.capped) == (5, True)
    monkeypatch.setattr(container.settings, "eval_backfill_max", 1000)
    last = backfill(container, world.project.id, rule.id)
    assert (last.created, last.capped) == (2, False)
    assert len(executions(db)) == 12


def test_backfill_honours_filter_sampling_and_environment(
    client, container, db, world, headers, jobs, make_eval_rule, settings
):
    ingest(client, container, headers, trace_events(["chat"] * 10 + ["other"] * 5))
    ingest(client, container, headers, trace_events(["chat"], settings.eval_environment, "j"))
    rule = make_eval_rule(world.project, filter={"name": "chat"}, sampling_pct=50.0)
    result = backfill(container, world.project.id, rule.id)
    expected = {f"t{i}" for i in range(10) if rules.is_sampled(rule.id, f"t{i}", 50.0)}
    assert {e.object_id for e in executions(db)} == expected
    assert result.created == len(expected) and not result.capped


def test_backfill_is_project_scoped(container, world, make_org, make_project, make_eval_rule):
    from traceloom.evals.backfill import RuleNotFound

    other = make_project(make_org())
    rule = make_eval_rule(other)
    with pytest.raises(RuleNotFound):
        backfill(container, world.project.id, rule.id)
