from __future__ import annotations

import pytest

from tests.api.eval_helpers import capture_eval_jobs, good, setup
from traceloom.db.models_eval import EvalSpendDaily
from traceloom.evals import runner, spend


@pytest.fixture
def env(client, container, db, world, make_key, make_eval_template, make_eval_rule,
        make_eval_execution, fake_provider, monkeypatch):  # fmt: skip
    made = setup(client, container, db, world, make_key, make_eval_template, make_eval_rule,
                 make_eval_execution)  # fmt: skip
    made.second = make_eval_execution(made.rule, "t2", trace_id="t2")
    made.sent = capture_eval_jobs(container, monkeypatch)
    return made


def run(container, db, execution):
    runner.handle(container, {"execution_id": execution.id})
    container.queue.drain()
    db.expire_all()
    return execution


def test_the_second_call_over_the_cap_is_capped_and_not_retried(
    env, container, db, world, fake_provider, client, make_key
):
    from tests.api.eval_helpers import ingest_trace

    ingest_trace(client, container, world, make_key, "t2")
    with container.session_factory.begin() as s:
        spend.set_cap(s, world.project.id, 0.01)
    fake_provider.script(*good(input_tokens=100, output_tokens=900))  # ~$0.0093 spent
    first = run(container, db, env.execution)
    assert first.status == "succeeded"
    second = run(container, db, env.second)
    assert second.status == "capped"
    assert second.error == "daily cost cap of $0.01 reached"
    assert len(fake_provider.calls) == 1 and env.sent == []  # no model call, no retry
    assert second.attempts == 1 and db.get(EvalSpendDaily, (world.project.id, spend.utc_day()))


def test_a_cap_of_zero_pauses_judges(env, container, db, world, fake_provider):
    with container.session_factory.begin() as s:
        spend.set_cap(s, world.project.id, 0)
    ex = run(container, db, env.execution)
    assert ex.status == "capped" and "cap of $0" in ex.error and fake_provider.calls == []


def test_a_failed_call_releases_its_reservation(env, container, db, world, fake_provider):
    from traceloom.llm.providers import ErrorDelta

    fake_provider.script(ErrorDelta("down"))
    ex = run(container, db, env.execution)
    assert ex.status == "pending"
    row = db.get(EvalSpendDaily, (world.project.id, spend.utc_day()))
    assert row is None or abs(row.reserved_usd) < 1e-9
