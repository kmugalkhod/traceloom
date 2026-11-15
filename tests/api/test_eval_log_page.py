from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tests.api.eval_helpers import capture_eval_jobs, good, setup
from traceloom.auth.permissions import Role
from traceloom.db.models_eval import EvalExecution
from traceloom.evals import runner
from traceloom.llm.providers import ErrorDelta

HTML = {"Accept": "text/html"}
PILLS = {"succeeded": "ok", "failed": "err", "capped": "capd", "skipped": "skip", "running": "run",
         "pending": "pend"}  # fmt: skip
STATUSES = ("succeeded", "failed", "capped", "skipped", "running", "pending")


def runner_labels():
    return {s: s.capitalize() for s in STATUSES}


def log_url(world, query=""):
    return f"/project/{world.project.id}/evals/log{query}"


@pytest.fixture
def env(client, container, db, world, make_key, make_eval_template, make_eval_rule,
        make_eval_execution, fake_provider, monkeypatch):  # fmt: skip
    made = setup(client, container, db, world, make_key, make_eval_template, make_eval_rule,
                 make_eval_execution)  # fmt: skip
    made.make = make_eval_execution
    made.sent = capture_eval_jobs(container, monkeypatch)
    made.fake = fake_provider
    return made


def page(client, login, world, role=Role.MEMBER, query=""):
    login(client, world.users[role])
    response = client.get(log_url(world, query), headers=HTML)
    assert response.status_code == 200
    return response.text


def seed_all_statuses(env):
    rows = {}
    for status in STATUSES[:-1]:  # the setup's own execution is the pending one
        rows[status] = env.make(env.rule, f"obj-{status}", status=status, attempts=1,
                                error="boom" if status == "failed" else None)  # fmt: skip
    return rows


def test_the_log_shows_every_status_with_counts(env, client, login, world, db):
    seed_all_statuses(env)
    html = page(client, login, world)
    for status, label in runner_labels().items():
        assert f'<span class="pill {PILLS[status]}">' in html
        assert f'{label} <span class="c">1</span>' in html
    assert 'aria-label="Rule"' in html and 'aria-label="Range"' in html
    assert "Retry" in html  # the failed row


def test_filters_by_status_rule_and_time(env, client, login, world, db, make_eval_rule):
    rows = seed_all_statuses(env)
    other = make_eval_rule(world.project, env.template, score_name="tone", name="Tone rule")
    elsewhere = env.make(other, "obj-other", status="succeeded")
    old = env.make(env.rule, "obj-old", status="failed")
    old.created_at = datetime.now(UTC) - timedelta(days=3)
    db.commit()

    html = page(client, login, world, query="?status=failed")
    assert rows["failed"].object_id in html and rows["succeeded"].object_id not in html
    assert old.object_id not in html  # outside the default 24 h

    assert old.object_id in page(client, login, world, query="?status=failed&range=7d")
    html = page(client, login, world, query=f"?rule={other.id}")
    assert elsewhere.object_id in html and rows["failed"].object_id not in html
    assert "Tone rule" in html


def test_an_unknown_filter_value_falls_back_to_all(env, client, login, world):
    seed_all_statuses(env)
    html = page(client, login, world, query="?status=bogus&rule=nope&range=forever")
    assert "obj-failed" in html and "obj-succeeded" in html


def test_empty_state(client, login, world):
    html = page(client, login, world)
    assert "No executions match" in html


def test_retry_requeues_a_failed_execution(env, client, login, world, db):
    failed = seed_all_statuses(env)["failed"]
    login(client, world.users[Role.MEMBER])
    response = client.post(log_url(world, f"/{failed.id}/retry"), follow_redirects=False)
    assert response.status_code == 303 and "notice=retried" in response.headers["location"]
    db.expire_all()
    assert (failed.status, failed.attempts, failed.error) == ("pending", 0, None)
    assert env.sent[-1][0] == {"type": "eval_job", "execution_id": failed.id}
    assert (
        "queued for another attempt" in client.get(response.headers["location"], headers=HTML).text
    )


def test_retry_only_applies_to_failed_or_capped(env, client, login, world):
    succeeded = seed_all_statuses(env)["succeeded"]
    login(client, world.users[Role.ADMIN])
    assert client.post(log_url(world, f"/{succeeded.id}/retry")).status_code == 409
    assert client.post(log_url(world, "/missing/retry")).status_code == 409


def test_a_viewer_reads_the_log_but_cannot_retry(env, client, login, world, db):
    failed = seed_all_statuses(env)["failed"]
    html = page(client, login, world, Role.VIEWER)
    assert failed.object_id in html and "/retry" not in html
    assert client.post(log_url(world, f"/{failed.id}/retry")).status_code == 403
    db.expire_all()
    assert failed.status == "failed" and env.sent == []


def test_retry_cannot_reach_another_projects_execution(
    env, client, login, world, make_org, make_project, make_eval_rule, make_eval_execution
):
    other_project = make_project(make_org())
    other_rule = make_eval_rule(other_project, template=env.template)
    foreign = make_eval_execution(other_rule, "x1", status="failed")
    login(client, world.users[Role.ADMIN])
    assert client.post(log_url(world, f"/{foreign.id}/retry")).status_code == 409
    assert foreign.id not in client.get(log_url(world, f"?open={foreign.id}"), headers=HTML).text


def test_a_judged_trace_shows_its_score_cost_and_judge_trace(
    env, client, login, world, db, container
):
    env.fake.script(*good())
    runner.handle(container, {"execution_id": env.execution.id})
    container.queue.drain()
    db.expire_all()
    ex = env.execution
    assert ex.status == "succeeded"
    html = page(client, login, world, query=f"?open={ex.id}")
    assert "relevance:" in html and "<b>0.8</b>" in html
    assert "grounded in the answer" in html
    assert ex.judge_trace_id[:12] in html
    assert f"${ex.cost:.4f}" in html


def test_failed_details_show_the_scrubbed_error_and_the_retry_button(
    env, client, login, world, db, container
):
    for _ in range(3):
        env.fake.script(ErrorDelta("401 key sk-live-abcdef123456 rejected"))
    for _ in range(3):
        runner.handle(container, {"execution_id": env.execution.id})
    db.expire_all()
    html = page(client, login, world, query=f"?open={env.execution.id}")
    assert "Failed after 3 attempts" in html and "rejected" in html
    assert "sk-live-abcdef123456" not in html
    assert f"/evals/log/{env.execution.id}/retry" in html


def test_the_log_is_listed_as_a_tab(env, client, login, world):
    assert ">Log</a>" in page(client, login, world)
    assert isinstance(EvalExecution.id, object)
