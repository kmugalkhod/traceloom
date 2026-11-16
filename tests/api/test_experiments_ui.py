"""UI experiments: `start_experiment`, the `experiment_item` handler and sweeper recovery (AC-6)."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update

from tests.api.eval_helpers import good, make_connection
from traceloom.auth.permissions import Role
from traceloom.datasets import experiment, runs
from traceloom.datasets.service import Actor
from traceloom.db.base import utcnow
from traceloom.db.models_datasets import DatasetRunItem
from traceloom.db.models_eval import EvalExecution
from traceloom.evals import spend
from traceloom.llm.providers import ErrorDelta
from traceloom.prompts import service as prompts


@pytest.fixture
def env(client, container, db, world, fake_provider, make_dataset, make_dataset_item,
        make_eval_template):  # fmt: skip
    project = world.project
    actor = Actor.for_user(world.users[Role.MEMBER], Role.MEMBER)
    connection = make_connection(db, world, container)
    dataset = make_dataset(project)
    for n in range(20):
        make_dataset_item(dataset, {"q": f"question {n}"}, {"a": f"answer {n}"})
    p_actor = prompts.Actor.for_user(world.users[Role.MEMBER], Role.MEMBER)
    for text in ("Answer briefly: {{q}}", "Answer in detail: {{q}}"):
        prompts.create_version(
            db, project, p_actor, name="qa", content=text, commit_message="v", type="text"
        )
    db.commit()
    template = make_eval_template(
        project,
        prompt="Expected {{expected_output}} got {{output}}",
        connection_id=connection.id,
        model="gpt-4o",
        params={"max_tokens": 100},
    )
    evaluator = {
        "template_version_id": template.latest.id,
        "score_name": "correctness",
        "mapping": {"q": {"source": "input"}},
    }
    real_stream = fake_provider.stream

    def stream(messages, model, params=None, tools=None, response_schema=None):
        if response_schema is not None and not fake_provider.scripts:  # a judge call
            fake_provider.scripts.append(good('{"score": 0.9, "reasoning": "close"}'))
        return real_stream(messages, model, params, tools, response_schema)

    fake_provider.stream = stream
    return SimpleNamespace(
        project=project, actor=actor, connection=connection, dataset=dataset,
        evaluator=evaluator, fake=fake_provider, db=db, container=container,
    )  # fmt: skip


def start(env, name, version, **kw):
    kw.setdefault("evaluators", [env.evaluator])
    return experiment.start_experiment(
        env.db, env.container, env.project, env.actor, env.dataset, name=name,
        prompt_name="qa", prompt_version=version, connection_id=env.connection.id,
        model="gpt-4o", params={"max_tokens": 100}, mapping={"q": {"source": "input"}}, **kw,
    )  # fmt: skip


def run_items(env, run):
    env.db.expire_all()
    return runs.run_items(env.db, env.project, run)


def test_two_prompt_versions_on_20_items(env, settings):
    r1 = start(env, "v1", 1)
    env.container.queue.drain()
    r2 = start(env, "v2", 2)
    env.container.queue.drain()
    store = env.container.trace_store
    for run, version in ((r1, 1), (r2, 2)):
        env.db.refresh(run)
        assert run.status == "completed" and run.dataset_version is not None
        assert run.config["prompt_version"] == version
        items = run_items(env, run)
        assert len(items) == 20 and {i.status for i in items} == {"completed"}
        assert len({i.item_id for i in items}) == 20
        for ri in items:
            assert ri.output and ri.latency_ms is not None
            detail = store.get_trace(env.project.id, ri.trace_id)
            assert detail.trace.environment == settings.experiment_environment
            [score] = detail.scores
            assert (score.name, score.source) == ("correctness", "EVAL")
            assert score.dataset_run_id == run.id
    executions = env.db.scalars(select(EvalExecution)).all()
    assert len(executions) == 40
    judged = [c for c in env.fake.calls if "Expected" in str(c["messages"])]
    assert len(judged) == 40 and all("answer" in str(c["messages"][-1]) for c in judged)
    assert len([c for c in env.fake.calls if "in detail" in str(c["messages"])]) == 20


def test_a_capped_item_is_marked_and_not_evaluated(env):
    spend.set_cap(env.db, env.project.id, 0.0)
    env.db.commit()
    run = start(env, "capped", 1)
    env.container.queue.drain()
    items = run_items(env, run)
    assert {i.status for i in items} == {"capped"} and all("cap" in i.error for i in items)
    assert env.fake.calls == [] and env.db.scalars(select(EvalExecution)).all() == []
    env.db.refresh(run)
    assert run.status == "completed"


def test_a_provider_error_fails_only_that_item(env):
    env.fake.script(ErrorDelta("401 bad key"))
    run = start(env, "err", 1, evaluators=[])
    env.container.queue.drain()
    statuses = sorted(i.status for i in run_items(env, run))
    assert statuses.count("failed") == 1 and statuses.count("completed") == 19


def test_restart_mid_run_is_recovered_by_the_sweeper(env, monkeypatch):
    real = env.container.queue.enqueue
    monkeypatch.setattr(env.container.queue, "enqueue", lambda *a, **k: None)  # the process died
    run = start(env, "restart", 1, evaluators=[])
    monkeypatch.setattr(env.container.queue, "enqueue", real)
    items = run_items(env, run)
    env.db.execute(update(DatasetRunItem).values(run_after=utcnow() - timedelta(minutes=10)))
    # one item was claimed by a worker that died mid-call
    env.db.execute(
        update(DatasetRunItem).where(DatasetRunItem.id == items[0].id).values(status="running")
    )
    env.db.commit()
    messages = experiment.due_messages(env.container)
    assert {m["run_item_id"] for m in messages} == {i.id for i in items}
    env.container.queue.drain()
    for message in messages:
        env.container.queue.enqueue(message)
    env.container.queue.drain()
    env.db.refresh(run)
    assert {i.status for i in run_items(env, run)} == {"completed"}
    assert run.status == "completed"
    assert len(env.fake.calls) == 20  # each item called exactly once
    assert experiment.due_messages(env.container) == []
