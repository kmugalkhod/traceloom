"""Phase 3 tenant isolation: a second project's users and keys can neither read nor change the
first project's score configs, queues, datasets, rules or executions."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from tests.api.test_ingestion import auth
from traceloom.auth.permissions import Role
from traceloom.db.models import AnnotationQueue, ScoreConfig
from traceloom.db.models_datasets import Dataset
from traceloom.db.models_eval import EvalExecution, EvalRule
from traceloom.ports import ScoreQuery, ScoreRow

HTML = {"Accept": "text/html"}
SECRET_NAME = "tenant-a-private-name"


@pytest.fixture
def a(world, db, container, make_score_config, make_queue, make_dataset, make_eval_rule,
      make_eval_execution):  # fmt: skip
    """Project A (the `world` project) with one of everything."""
    project = world.project
    config = make_score_config(project, SECRET_NAME)
    queue = make_queue(project, SECRET_NAME, [config])
    dataset = make_dataset(project, SECRET_NAME)
    rule = make_eval_rule(project, score_name=SECRET_NAME)
    execution = make_eval_execution(rule, "trace-a", status="failed")
    container.trace_store.add_score(
        project.id,
        ScoreRow(
            id="score-a",
            trace_id="trace-a",
            name=SECRET_NAME,
            value=1.0,
            timestamp=datetime(2026, 11, 9, tzinfo=UTC),
        ),
    )
    return SimpleNamespace(
        project=project, config=config, queue=queue, dataset=dataset, rule=rule,
        execution=execution,
    )  # fmt: skip


@pytest.fixture
def b(make_user, make_org, make_project, make_membership, make_key):
    """Project B in another org: an Admin and an Owner of it, and a key."""
    org = make_org()
    project = make_project(org)
    users = {}
    for role in (Role.ADMIN, Role.OWNER):
        users[role] = make_user()
        make_membership(users[role], org, role)
    _, public, secret = make_key(project)
    return SimpleNamespace(project=project, users=users, public=public, secret=secret)


def a_urls(a):
    base = f"/project/{a.project.id}"
    return [
        f"{base}/settings/scores",
        f"{base}/settings/evaluation",
        f"{base}/queues",
        f"{base}/queues/{a.queue.id}",
        f"{base}/queues/{a.queue.id}/review",
        f"{base}/datasets",
        f"{base}/datasets/{a.dataset.id}",
        f"{base}/evals",
        f"{base}/evals/rules",
        f"{base}/evals/rules/{a.rule.id}",
        f"{base}/evals/log",
        f"{base}/evals/log?open={a.execution.id}",
        f"{base}/scores/analytics",
    ]


def a_writes(a):
    base = f"/project/{a.project.id}"
    return [
        f"{base}/settings/scores/{a.config.id}/archive",
        f"{base}/settings/evaluation/cap",
        f"{base}/queues/{a.queue.id}/delete",
        f"{base}/datasets/{a.dataset.id}/delete",
        f"{base}/evals/rules/{a.rule.id}/delete",
        f"{base}/evals/rules/{a.rule.id}/toggle",
        f"{base}/evals/log/{a.execution.id}/retry",
    ]


def test_another_projects_owner_cannot_read_a_pages(client, login, a, b):
    login(client, b.users[Role.OWNER])
    for url in a_urls(a):
        r = client.get(url, headers=HTML, follow_redirects=False)
        assert r.status_code in (403, 404), f"GET {url} -> {r.status_code}"
        assert SECRET_NAME not in r.text


def test_another_projects_admin_cannot_write_to_a(client, login, db, a, b):
    login(client, b.users[Role.ADMIN])
    for url in a_writes(a):
        r = client.post(url, json={}, headers=HTML, follow_redirects=False)
        assert r.status_code in (403, 404), f"POST {url} -> {r.status_code}"
    db.expire_all()
    assert db.get(ScoreConfig, a.config.id).archived_at is None
    assert db.get(AnnotationQueue, a.queue.id) is not None
    assert db.get(Dataset, a.dataset.id) is not None
    assert db.get(EvalRule, a.rule.id) is not None
    assert db.get(EvalRule, a.rule.id).enabled == a.rule.enabled
    assert db.get(EvalExecution, a.execution.id).status == "failed"


def test_bs_urls_do_not_reach_as_objects(client, login, db, a, b):
    """An Admin of B calling B's own routes with A's ids finds nothing and changes nothing."""
    login(client, b.users[Role.ADMIN])
    base = f"/project/{b.project.id}"
    cross = [
        f"{base}/queues/{a.queue.id}/delete",
        f"{base}/datasets/{a.dataset.id}/delete",
        f"{base}/evals/rules/{a.rule.id}/delete",
        f"{base}/evals/rules/{a.rule.id}/toggle",
        f"{base}/evals/log/{a.execution.id}/retry",
        f"{base}/settings/scores/{a.config.id}/archive",
    ]
    for url in cross:
        r = client.post(url, json={}, headers=HTML, follow_redirects=False)
        assert r.status_code in (403, 404, 409), f"POST {url} -> {r.status_code}"
    for url in (
        f"{base}/queues/{a.queue.id}",
        f"{base}/datasets/{a.dataset.id}",
        f"{base}/evals/rules/{a.rule.id}",
    ):
        r = client.get(url, headers=HTML, follow_redirects=False)
        assert r.status_code in (403, 404), f"GET {url} -> {r.status_code}"
        assert SECRET_NAME not in r.text
    # The log opens an execution by query string: B's own log shows nothing of A's.
    r = client.get(f"{base}/evals/log?open={a.execution.id}", headers=HTML)
    assert SECRET_NAME not in r.text and "trace-a" not in r.text
    db.expire_all()
    assert db.get(AnnotationQueue, a.queue.id) is not None
    assert db.get(Dataset, a.dataset.id) is not None
    assert db.get(EvalRule, a.rule.id) is not None
    assert db.get(ScoreConfig, a.config.id).archived_at is None
    assert db.get(EvalExecution, a.execution.id).status == "failed"
    for page in (
        f"{base}/queues",
        f"{base}/datasets",
        f"{base}/evals/rules",
        f"{base}/settings/scores",
    ):
        assert SECRET_NAME not in client.get(page, headers=HTML).text


def test_bs_key_cannot_read_or_delete_as_scores(client, a, b, container):
    headers = auth(b.public, b.secret)
    listed = client.get("/api/public/scores", headers=headers)
    assert listed.status_code == 200
    assert SECRET_NAME not in listed.text
    configs = client.get("/api/public/score-configs", headers=headers)
    assert configs.status_code == 200
    assert SECRET_NAME not in configs.text
    assert client.delete("/api/public/scores/score-a", headers=headers).status_code == 404
    page = container.trace_store.list_scores(a.project.id, ScoreQuery())
    assert [s.id for s in page.items] == ["score-a"]


def test_a_score_for_an_as_trace_through_bs_key_lands_in_b_only(client, a, b, container):
    r = client.post(
        "/api/public/scores",
        json={"traceId": "trace-a", "name": "x", "value": 1},
        headers=auth(b.public, b.secret),
    )
    assert r.status_code in (201, 400, 404)
    mine = container.trace_store.list_scores(a.project.id, ScoreQuery())
    assert [s.id for s in mine.items] == ["score-a"]


def test_the_urls_exist_for_as_own_admin(client, login, world, a):
    """Guards the sweep: the pages answered 403/404 above are real ones for the owning project."""
    login(client, world.users[Role.ADMIN])
    for url in a_urls(a):
        r = client.get(url, headers=HTML, follow_redirects=False)
        assert r.status_code in (200, 303), f"GET {url} -> {r.status_code}"
