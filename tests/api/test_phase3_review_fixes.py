"""Regression tests for the Phase 3 code-review findings (judge rules, ledger, sweeper,
experiments, score ingest and the feedback endpoint)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from tests.api.eval_helpers import good, setup
from tests.api.test_experiments_ui import env as experiment_env  # noqa: F401 - fixture
from tests.api.test_experiments_ui import run_items, start
from tests.api.test_ingestion import auth, event, post, stored
from traceloom.datasets import experiment
from traceloom.db.models import ScoreConfig
from traceloom.db.models_eval import EvalExecution, EvalSpendDaily
from traceloom.evals import rules, runner, spend
from traceloom.evals.backfill import backfill, rule_query
from traceloom.evals.rules import RuleSnapshot
from traceloom.queue_registry import EVAL_JOB
from traceloom.scores import configs as cfg


@pytest.fixture
def headers(world, make_key):
    _, public, secret = make_key(world.project)
    return auth(public, secret)


@pytest.fixture
def jobs(container):
    seen: list[dict] = []
    container.queue.register(EVAL_JOB, lambda message: seen.append(dict(message)))
    return seen


def ingest(client, container, headers, events):
    assert post(client, headers, events).status_code == 207
    container.queue.drain()


def executions(db):
    db.expire_all()
    return db.scalars(select(EvalExecution)).all()


def ledger(db, project_id):
    db.expire_all()
    return db.get(EvalSpendDaily, (project_id, spend.utc_day()))


# --- 1. observation rules answer trace-level filters from the parent trace ---------------------


def _two_traces(client, container, headers):
    ingest(
        client,
        container,
        headers,
        [
            event(
                "t1",
                "trace-create",
                {"id": "t1", "userId": "u1", "tags": ["vip"], "sessionId": "s1", "release": "r1"},
            ),  # fmt: skip
            event("g1", "generation-create", {"id": "g1", "traceId": "t1", "name": "answer"}),
            event("t2", "trace-create", {"id": "t2", "userId": "u2", "tags": ["x"]}),
            event("g2", "generation-create", {"id": "g2", "traceId": "t2", "name": "answer"}),
        ],
    )


def test_observation_rules_match_trace_level_filters_at_ingest(
    client, container, db, world, headers, jobs, make_eval_rule
):
    make_eval_rule(
        world.project,
        target="observation",
        target_name="answer",
        filter={"user_id": "u1", "tags": ["vip"], "session_id": "s1", "release": "r1"},
        delay_seconds=0,
    )
    _two_traces(client, container, headers)
    assert [(e.object_type, e.object_id) for e in executions(db)] == [("observation", "g1")]


def test_observation_rules_match_trace_level_filters_on_backfill(
    client, container, db, world, headers, jobs, make_eval_rule
):
    _two_traces(client, container, headers)
    rule = make_eval_rule(
        world.project, target="observation", filter={"user_id": "u1"}, delay_seconds=0
    )
    assert backfill(container, world.project.id, rule.id).created == 1
    assert [e.object_id for e in executions(db)] == ["g1"]


def test_the_runner_rechecks_trace_level_filters_of_an_observation_on_its_parent(
    client, container, db, world, make_key, make_eval_template, make_eval_rule,
    make_eval_execution, fake_provider,
):  # fmt: skip
    env = setup(
        client, container, db, world, make_key, make_eval_template, make_eval_rule,
        make_eval_execution, userId="u1",
    )  # fmt: skip
    _, public, secret = make_key(world.project)
    generation = {"id": "g1", "traceId": "t1", "name": "a", "input": "q", "output": "4"}
    ingest(client, container, auth(public, secret),
           [event("g1", "generation-create", generation)])  # fmt: skip
    env.rule.target, env.rule.filter = "observation", {"user_id": "u1"}
    env.execution.object_type, env.execution.object_id = "observation", "g1"
    db.commit()
    fake_provider.script(*good())
    runner.handle(container, {"execution_id": env.execution.id})
    container.queue.drain()
    db.expire_all()
    assert (env.execution.status, env.execution.error) == ("succeeded", None)


# --- 6. backfill coerces has_error like the runner ---------------------------------------------


@pytest.mark.parametrize(("raw", "want"), [("false", False), ("0", False), ("true", True),
                                            (True, True), (None, None)])  # fmt: skip
def test_backfill_query_coerces_has_error(raw, want):
    snapshot = RuleSnapshot("r", "p", "trace", None, None, {"has_error": raw}, 100.0, 0, "v")
    assert rule_query(snapshot, None, None, None).has_error is want


# --- 3 and 5. the ledger: unknown cost, and a job the sweeper reset ----------------------------


@pytest.fixture
def judge(client, container, db, world, make_key, make_eval_template, make_eval_rule,
          make_eval_execution, fake_provider):  # fmt: skip
    return setup(
        client, container, db, world, make_key, make_eval_template, make_eval_rule,
        make_eval_execution,
    )  # fmt: skip


def run_judge(container, db, judge):
    runner.handle(container, {"execution_id": judge.execution.id})
    container.queue.drain()
    db.expire_all()
    return judge.execution


def test_an_unknown_judge_cost_is_charged_at_the_reservation(
    container, db, world, judge, monkeypatch
):
    reserved: list[float] = []
    real_reserve = spend.reserve

    def reserve(*args, **kwargs):
        held = real_reserve(*args, **kwargs)
        reserved.append(held.amount)
        return held

    monkeypatch.setattr(spend, "reserve", reserve)
    monkeypatch.setattr(runner, "_call", lambda *a, **k: (good()[0].text, None, None))
    assert run_judge(container, db, judge).status == "succeeded"
    [amount] = reserved
    assert amount > 0
    row = ledger(db, world.project.id)
    assert row.spent_usd == pytest.approx(amount) and row.reserved_usd == pytest.approx(0.0)


def test_a_slow_job_reset_by_the_sweeper_does_not_corrupt_the_ledger(
    container, db, world, judge, fake_provider
):
    pid = world.project.id
    spend.reserve(container.session_factory, pid, spend.Estimate(0.2, True), cap=5)  # another job
    real_stream = fake_provider.stream

    def slow_stream(*args, **kwargs):
        # While the call is in flight the sweeper decides the job is stuck and resets it.
        with container.session_factory.begin() as s:
            s.execute(
                update(EvalExecution)
                .where(EvalExecution.id == judge.execution.id)
                .values(started_at=datetime.now(UTC) - timedelta(hours=1))
            )
        rules.due_messages(container)
        return real_stream(*args, **kwargs)

    fake_provider.stream = slow_stream
    fake_provider.script(*good())
    ex = run_judge(container, db, judge)
    row = ledger(db, pid)
    assert row.reserved_usd == pytest.approx(0.2)  # only the other job's hold is left
    assert row.spent_usd == pytest.approx(ex.cost or 0.0)


def test_the_stuck_threshold_outlasts_the_provider_timeout(
    container, db, world, make_eval_rule, make_eval_execution, monkeypatch
):
    monkeypatch.setattr(container.settings, "provider_timeout_seconds", 900)
    monkeypatch.setattr(container.settings, "eval_stuck_seconds", 600)
    rule = make_eval_rule(world.project)
    live = make_eval_execution(
        rule, "slow", status="running", attempts=1,
        started_at=datetime.now(UTC) - timedelta(seconds=700),
    )  # fmt: skip
    assert rules.due_messages(container) == []
    db.expire_all()
    assert live.status == "running"


# --- 2. an experiment item is never completed without its evaluator executions ----------------


class _ProcessDied(BaseException):
    """The worker process is killed: nothing after this point runs."""


def test_a_crash_creating_evaluators_never_leaves_a_completed_item_without_them(
    experiment_env,  # noqa: F811
    monkeypatch,
):
    env = experiment_env

    class Boom:
        def __call__(self, *args, **kwargs):
            raise _ProcessDied

        def __getattr__(self, name):
            raise _ProcessDied

    real = env.container.queue.enqueue
    monkeypatch.setattr(env.container.queue, "enqueue", lambda *a, **k: None)
    run = start(env, "crash", 1)
    monkeypatch.setattr(env.container.queue, "enqueue", real)
    monkeypatch.setattr(experiment, "EvalExecution", Boom())
    item = run_items(env, run)[0]
    with pytest.raises(_ProcessDied):
        experiment.handle(env.container, {"run_item_id": item.id})
    monkeypatch.undo()
    env.db.expire_all()
    have = {e.run_item_id for e in env.db.scalars(select(EvalExecution))}
    item = next(i for i in run_items(env, run) if i.id == item.id)
    # Either both happened or neither: a still-running item is reset by the sweeper and re-run.
    assert item.status != "completed" or item.id in have, item.status


# --- 7. a just-created score config is found at once --------------------------------------------


def test_a_missing_config_is_not_negative_cached(db, container, world, make_score_config):
    cache = cfg.ConfigCache(container.session_factory, ttl_seconds=60)
    pid = world.project.id
    assert cache.get(pid, "cfg-new") is None
    config = make_score_config(world.project, "fresh")
    db.commit()
    assert cache.get(pid, config.id) is not None
    created = ScoreConfig(id="cfg-new", project_id=pid, name="late", data_type="NUMERIC")
    db.add(created)
    db.commit()
    assert cache.get(pid, "cfg-new") is not None


# --- 8. clients cannot set server-owned score fields --------------------------------------------


@pytest.mark.parametrize(
    "field", [{"executionId": "x1"}, {"queueId": "q1"}, {"channel": "public_feedback"}]
)
def test_public_ingest_rejects_server_owned_score_fields(client, container, world, headers, field):
    body = {"id": "forged", "traceId": "t1", "name": "q", "value": 1, **field}
    response = post(client, headers, [event("e1", "score-create", body)])
    assert response.status_code == 207
    [error] = response.json()["errors"]
    assert error["id"] == "e1" and error["status"] == 400 and "cannot be set" in error["message"]
    container.queue.drain()
    assert stored(container, world.project.id, score=["forged"])["score"] == {}


def test_public_ingest_keeps_dataset_run_id(client, container, world, headers):
    body = {"id": "ok", "traceId": "t1", "name": "q", "value": 1, "datasetRunId": "r1"}
    response = post(client, headers, [event("e1", "score-create", body)])
    assert response.json()["errors"] == []
    container.queue.drain()
    row = stored(container, world.project.id, score=["ok"])["score"]["ok"]
    assert (row.dataset_run_id, row.source, row.execution_id) == ("r1", "API", None)


# --- 9. the feedback endpoint checks the visitor's IP before the shared key budget -------------


def test_a_visitor_over_its_ip_limit_does_not_spend_the_key_budget(
    client, container, db, world, make_key, monkeypatch
):
    from traceloom.scores import feedback

    key, public, _ = make_key(world.project)
    cfg.create_config(db, world.project.id, name="thumbs", data_type="BOOLEAN",
                      public_feedback=True)  # fmt: skip
    feedback.set_feedback_origins(db, world.project.id, ["https://app.example.com"])
    db.commit()
    monkeypatch.setattr(container.settings, "rate_feedback", "2/minute")
    for _ in range(2):  # this visitor already used its allowance
        container.limiter.hit("feedback:ip:testclient", "2/minute")
    r = client.post(
        "/api/public/feedback",
        json={"traceId": "t1", "name": "thumbs", "value": True},
        headers={"X-Traceloom-Public-Key": public, "Origin": "https://app.example.com"},
    )
    assert r.status_code == 429
    assert container.limiter.hit(f"feedback:key:{key.id}", "2/minute").remaining == 1


# --- rule score conditions (filter["scores"], `name:op:value`) ----------------------------------


def _score(client, container, headers, trace_id, name, value):
    body = {"id": f"s-{trace_id}-{name}", "traceId": trace_id, "name": name, "value": value}
    ingest(client, container, headers, [event(f"e-s-{trace_id}", "score-create", body)])


def test_the_runner_skips_a_trace_whose_scores_fail_the_rule_conditions(
    client, container, db, world, make_key, make_eval_template, make_eval_rule,
    make_eval_execution, fake_provider,
):  # fmt: skip
    env = setup(
        client, container, db, world, make_key, make_eval_template, make_eval_rule,
        make_eval_execution,
    )  # fmt: skip
    env.rule.filter = {"name": "chat", "scores": ["quality:<:0.5"]}
    db.commit()
    _, public, secret = make_key(world.project)
    _score(client, container, auth(public, secret), "t1", "quality", 0.9)
    runner.handle(container, {"execution_id": env.execution.id})
    container.queue.drain()
    db.expire_all()
    assert env.execution.status == "skipped" and fake_provider.calls == []


def test_the_runner_runs_a_trace_whose_scores_pass_the_rule_conditions(
    client, container, db, world, make_key, make_eval_template, make_eval_rule,
    make_eval_execution, fake_provider,
):  # fmt: skip
    env = setup(
        client, container, db, world, make_key, make_eval_template, make_eval_rule,
        make_eval_execution,
    )  # fmt: skip
    env.rule.filter = {"name": "chat", "scores": ["quality:<:0.5", "toxicity:missing"]}
    db.commit()
    _, public, secret = make_key(world.project)
    _score(client, container, auth(public, secret), "t1", "quality", 0.2)
    fake_provider.script(*good())
    runner.handle(container, {"execution_id": env.execution.id})
    container.queue.drain()
    db.expire_all()
    assert (env.execution.status, env.execution.error) == ("succeeded", None)


def test_backfill_applies_the_rule_score_conditions(
    client, container, db, world, headers, jobs, make_eval_rule
):
    ingest(client, container, headers, [
        event(f"e{i}", "trace-create", {"id": f"t{i}", "name": "chat"}) for i in range(3)
    ])  # fmt: skip
    _score(client, container, headers, "t0", "quality", 0.2)
    _score(client, container, headers, "t1", "quality", 0.9)
    rule = make_eval_rule(world.project, filter={"scores": ["quality:<:0.5"]})
    assert backfill(container, world.project.id, rule.id).created == 1
    assert [e.object_id for e in executions(db)] == ["t0"]


def test_matches_applies_score_conditions_to_the_scores_it_is_given():
    from traceloom.ports import ScoreRow, TraceRow

    snapshot = RuleSnapshot(
        "r", "p", "trace", None, None, {"scores": ["quality:>=:0.5", "label:in:a,b"]}, 100.0, 0,
        "v",
    )  # fmt: skip
    trace = TraceRow(id="t1", timestamp=datetime.now(UTC))

    def score(name, value=None, text=None):
        return ScoreRow(id=name, name=name, trace_id="t1", value=value,
                        string_value=text, timestamp=datetime.now(UTC))  # fmt: skip

    assert rules.matches(snapshot, trace, scores=[score("quality", 0.7), score("label", text="b")])
    assert not rules.matches(snapshot, trace, scores=[score("quality", 0.2),
                                                      score("label", text="b")])  # fmt: skip
    assert not rules.matches(snapshot, trace, scores=[score("quality", 0.7)])
    bad = RuleSnapshot("r", "p", "trace", None, None, {"scores": ["nonsense"]}, 100.0, 0, "v")
    assert not rules.matches(bad, trace, scores=[])  # an unreadable condition matches nothing


def test_at_ingest_score_conditions_wait_for_the_job_re_check(
    client, container, db, world, headers, jobs, make_eval_rule
):
    # Scores usually arrive after their trace, so ingest-time matching defers them to the job.
    make_eval_rule(world.project, filter={"scores": ["quality:<:0.5"]}, delay_seconds=0)
    ingest(client, container, headers, [event("e0", "trace-create", {"id": "t0", "name": "c"})])
    assert [e.object_id for e in executions(db)] == ["t0"]
