"""Shared setup for the judge runner tests: a project with a trace, a fake LLM connection, a
template, a rule and one pending execution."""

from __future__ import annotations

from types import SimpleNamespace

from tests.api.test_ingestion import auth, event, post
from traceloom.db.models import LLMConnection
from traceloom.llm.connections import ConnectionPolicy, Secrets
from traceloom.llm.providers import DoneDelta, TextDelta, UsageDelta

SECRET = "sk-judge-secret-123456"
REPLY = '{"score": 0.8, "reasoning": "grounded in the answer"}'


def good(reply: str = REPLY, input_tokens: int = 100, output_tokens: int = 50):
    return [TextDelta(reply), UsageDelta(input_tokens, output_tokens), DoneDelta("stop")]


def make_connection(db, world, container) -> LLMConnection:
    policy = ConnectionPolicy.from_settings(container.settings)
    row = LLMConnection(
        project_id=world.project.id,
        provider="fake",
        name="judge",
        secret_ciphertext=policy.box.encrypt(Secrets(api_key=SECRET).dump()),
    )
    db.add(row)
    db.commit()
    return row


def ingest_trace(client, container, world, make_key, trace_id="t1", name="chat", **body):
    _, public, secret = make_key(world.project)
    body = {"id": trace_id, "name": name, "input": "What is 2+2?", "output": "4", **body}
    assert post(client, auth(public, secret), [event(f"e-{trace_id}", "trace-create", body)])
    container.queue.drain()


def setup(
    client, container, db, world, make_key, make_eval_template, make_eval_rule,
    make_eval_execution, *, template=None, rule=None, trace_id="t1", **trace_body,
):  # fmt: skip
    """Returns a namespace with `.execution`, `.rule`, `.template` and `.connection`."""
    connection = make_connection(db, world, container)
    ingest_trace(client, container, world, make_key, trace_id, **trace_body)
    template = make_eval_template(
        world.project,
        connection_id=connection.id,
        model="gpt-4o",
        params={"max_tokens": 100},
        **(template or {}),
    )
    rule = make_eval_rule(
        world.project, template, filter={"name": "chat"}, score_name="relevance",
        **(rule or {}),
    )  # fmt: skip
    execution = make_eval_execution(rule, trace_id, status="pending")
    return SimpleNamespace(execution=execution, rule=rule, template=template, connection=connection)


def capture_eval_jobs(container, monkeypatch) -> list[tuple[dict, float]]:
    """Intercept eval_job enqueues (retries); everything else still reaches the real queue."""
    sent: list[tuple[dict, float]] = []
    real = container.queue.enqueue

    def enqueue(message, delay_seconds=0):
        if message.get("type") == "eval_job":
            sent.append((dict(message), delay_seconds))
        else:
            real(message, delay_seconds)

    monkeypatch.setattr(container.queue, "enqueue", enqueue)
    return sent
