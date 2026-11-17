"""Run pages: the New experiment dialog, the run detail page and its polled progress (T-29)."""

from __future__ import annotations

import re

import pytest
from sqlalchemy import select

from tests.api.eval_helpers import good, make_connection
from traceloom.auth.permissions import Role
from traceloom.datasets import runs
from traceloom.datasets.service import Actor
from traceloom.db.models_datasets import DatasetRunItem
from traceloom.prompts import service as prompts
from traceloom.queue_registry import EXPERIMENT_ITEM

HTML = {"Accept": "text/html"}


@pytest.fixture
def env(client, container, db, world, fake_provider, make_dataset, make_dataset_item):
    connection = make_connection(db, world, container)
    dataset = make_dataset(world.project)
    for n in range(3):
        make_dataset_item(dataset, {"q": f"question {n}"}, {"a": f"answer {n}"})
    actor = prompts.Actor.for_user(world.users[Role.MEMBER], Role.MEMBER)
    for text in ("Answer briefly: {{q}}", "Answer in detail: {{q}}"):
        prompts.create_version(
            db, world.project, actor, name="qa", content=text, commit_message="v", type="text"
        )
    db.commit()
    real = fake_provider.stream

    def stream(messages, model, params=None, tools=None, response_schema=None):
        if response_schema is not None and not fake_provider.scripts:
            fake_provider.scripts.append(good('{"score": 0.9, "reasoning": "close"}'))
        return real(messages, model, params, tools, response_schema)

    fake_provider.stream = stream
    return connection, dataset


def base(world, dataset):
    return f"/project/{world.project.id}/datasets/{dataset.id}"


def post_form(client, world, dataset, **data):
    page = client.get(base(world, dataset), headers=HTML)
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    data["csrf_token"] = token.group(1) if token else "x"
    return client.post(
        f"{base(world, dataset)}/experiments", data=data, headers=HTML, follow_redirects=False
    )


def form(connection, **over):
    data = {
        "name": "prompt v1",
        "prompt": "qa",
        "ref": "v:1",
        "connection_id": connection.id,
        "model": "gpt-4o",
        "map_q": "input.q",
    }
    data.update(over)
    return data


def test_member_starts_an_experiment_and_sees_progress(
    client, login, world, env, container, monkeypatch, db
):
    connection, dataset = env
    login(client, world.users[Role.MEMBER], csrf=False)
    page = client.get(base(world, dataset), headers=HTML)
    assert "New experiment" in page.text and 'id="new-experiment"' in page.text
    real = container.queue.enqueue
    monkeypatch.setattr(container.queue, "enqueue", lambda *a, **k: None)  # nothing runs yet
    resp = post_form(client, world, dataset, **form(connection))
    assert resp.status_code == 303
    run_url = resp.headers["location"]
    live = client.get(run_url, headers=HTML)
    assert live.status_code == 200 and "hx-trigger" in live.text and "Pending" in live.text
    monkeypatch.setattr(container.queue, "enqueue", real)
    for row in db.scalars(select(DatasetRunItem)):
        real({"type": EXPERIMENT_ITEM, "run_item_id": row.id})
    container.queue.drain()
    done = client.get(run_url + "/progress", headers=HTML)
    assert "hx-trigger" not in done.text and "Run completed" in done.text
    assert done.text.count("Succeeded") >= 3
    assert re.search(r"/project/[^/]+/traces/\w+", done.text)  # items link to their traces


def test_viewer_has_no_start_control_and_gets_403(client, login, world, env):
    connection, dataset = env
    login(client, world.users[Role.VIEWER], csrf=False)
    page = client.get(base(world, dataset), headers=HTML)
    assert page.status_code == 200 and "New experiment" not in page.text
    assert post_form(client, world, dataset, **form(connection)).status_code == 403


def test_duplicate_name_reopens_the_dialog(client, login, world, env):
    connection, dataset = env
    login(client, world.users[Role.MEMBER], csrf=False)
    assert post_form(client, world, dataset, **form(connection)).status_code == 303
    again = post_form(client, world, dataset, **form(connection))
    assert again.status_code == 400 and "already exists" in again.text
    assert "data-autoopen" in again.text


def test_run_page_is_project_scoped(client, login, world, env, db):
    _, dataset = env
    login(client, world.users[Role.MEMBER], csrf=False)
    assert client.get(base(world, dataset) + "/runs/nope", headers=HTML).status_code == 404
    actor = Actor.for_user(world.users[Role.MEMBER], Role.MEMBER)
    run = runs.create_run(db, world.project, actor, dataset, name="sdk")
    db.commit()
    assert client.get(f"{base(world, dataset)}/runs/{run.id}", headers=HTML).status_code == 200


def test_polling_continues_until_evals_land_and_header_follows(client, login, world, env, db):
    from traceloom.db.models_datasets import DatasetRun
    from traceloom.db.models_eval import EvalExecution, EvalTemplateVersion

    connection, dataset = env
    login(client, world.users[Role.MEMBER], csrf=False)
    run_url = post_form(client, world, dataset, **form(connection)).headers["location"]
    run = db.scalars(select(DatasetRun)).first()
    item = db.scalars(select(DatasetRunItem)).first()
    db.add(
        EvalExecution(
            project_id=world.project.id,
            run_item_id=item.id,
            template_version_id=db.scalars(select(EvalTemplateVersion.id)).first(),
            object_type="trace",
            object_id="t",
            status="pending",
        )
    )
    run.status = "completed"
    db.commit()
    pending = client.get(run_url + "/progress", headers=HTML)
    assert "hx-trigger" in pending.text  # evals still outstanding: keep polling
    assert 'id="run-status"' in pending.text and "Completed" in pending.text
    db.query(EvalExecution).update({"status": "succeeded"})
    db.commit()
    final = client.get(run_url + "/progress", headers=HTML)
    assert "hx-trigger" not in final.text and "Updating every" not in final.text
