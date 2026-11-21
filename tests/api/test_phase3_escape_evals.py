"""User-controlled text on the execution log renders as text (AC-13): judge reasoning, errors, rule
and score names. Provider secrets never reach the page."""

from __future__ import annotations

import pytest

from tests.api.eval_helpers import SECRET, capture_eval_jobs, good, setup
from traceloom.auth.permissions import Role
from traceloom.evals import runner
from traceloom.llm.providers import ErrorDelta

HTML = {"Accept": "text/html"}
XSS = "<script>alert(1)</script>"
ESCAPED = "&lt;script&gt;alert(1)&lt;/script&gt;"


@pytest.fixture
def env(client, container, db, world, make_key, make_eval_template, make_eval_rule,
        make_eval_execution, fake_provider, monkeypatch, login):  # fmt: skip
    made = setup(client, container, db, world, make_key, make_eval_template, make_eval_rule,
                 make_eval_execution, rule={"name": f"Rule {XSS}"})  # fmt: skip
    capture_eval_jobs(container, monkeypatch)
    made.fake = fake_provider
    login(client, world.users[Role.VIEWER])
    return made


def open_details(client, world, execution):
    url = f"/project/{world.project.id}/evals/log?open={execution.id}"
    return client.get(url, headers=HTML).text


def test_judge_reasoning_renders_escaped(env, client, world, db, container):
    env.fake.script(*good(f'{{"score": 0.5, "reasoning": "quote {XSS} {SECRET}"}}'))
    runner.handle(container, {"execution_id": env.execution.id})
    container.queue.drain()
    db.expire_all()
    html = open_details(client, world, env.execution)
    assert "quote " + ESCAPED in html
    assert XSS not in html


def test_errors_and_names_render_escaped_and_secrets_are_scrubbed(
    env, client, world, db, container
):
    for _ in range(3):
        env.fake.script(ErrorDelta(f"{XSS} bad key {SECRET} Bearer abcdefghijklmnop"))
    for _ in range(3):
        runner.handle(container, {"execution_id": env.execution.id})
    db.expire_all()
    for query in (f"?open={env.execution.id}", ""):
        html = client.get(f"/project/{world.project.id}/evals/log{query}", headers=HTML).text
        assert XSS not in html
        assert ESCAPED in html
        assert SECRET not in html and "abcdefghijklmnop" not in html
    assert env.execution.error and SECRET not in env.execution.error


def test_the_stored_error_never_holds_the_connection_secret(env, db, container):
    env.fake.script(RuntimeError(f"connect failed for key {SECRET}"))
    runner.handle(container, {"execution_id": env.execution.id})
    db.expire_all()
    assert SECRET not in (env.execution.error or "")
