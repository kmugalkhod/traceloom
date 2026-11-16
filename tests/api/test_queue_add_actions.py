from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from traceloom.annotation import queues
from traceloom.auth.permissions import Role
from traceloom.db.base import utcnow
from traceloom.db.models import AnnotationItem
from traceloom.ports import ObservationRow, TraceRow
from traceloom.prompts.service import Actor

HTML = {"Accept": "text/html"}
HX = {"Accept": "text/html", "HX-Request": "true"}
T0 = datetime.now(UTC) - timedelta(hours=2)


def add_url(world):
    return f"/project/{world.project.id}/queues/add"


@pytest.fixture
def config(world, make_score_config):
    return make_score_config(world.project, "helpfulness")


@pytest.fixture
def queue(db, world, config):
    member = Actor.for_user(world.users[Role.MEMBER], Role.MEMBER)
    q = queues.create_queue(db, world.project, member, "Refund review", "", [config.id])
    db.commit()
    return q


@pytest.fixture
def traces(container, world):
    rows = [
        TraceRow(
            f"t{i}",
            T0 + timedelta(seconds=i),
            name=f"chat-{i}",
            session_id="s1",
            input="q",
            output="a",
            environment="production" if i < 3 else "staging",
        )
        for i in range(5)
    ]
    obs = [ObservationRow("o0", "t0", T0, type="generation", name="llm")]
    container.trace_store.upsert(world.project.id, rows, obs, [])


def items(db, queue):
    return db.query(AnnotationItem).filter_by(queue_id=queue.id).all()


def test_single_trace_dialog_and_add_then_duplicate(client, login, world, db, queue, traces):
    login(client, world.users[Role.MEMBER])
    dialog = client.get(add_url(world), params={"trace_id": "t0"}, headers=HX)
    assert dialog.status_code == 200 and "Refund review" in dialog.text
    assert "data-autoopen" in dialog.text
    first = client.post(add_url(world), data={"queue_id": queue.id, "trace_id": "t0"}, headers=HX)
    assert "Added 1 to Refund review." in first.text and len(items(db, queue)) == 1
    again = client.post(add_url(world), data={"queue_id": queue.id, "trace_id": "t0"}, headers=HX)
    assert "All 1 were already in Refund review, so nothing was added." in again.text
    assert len(items(db, queue)) == 1


def test_observation_and_session_items(client, login, world, db, queue, traces):
    login(client, world.users[Role.MEMBER])
    client.post(
        add_url(world),
        data={"queue_id": queue.id, "trace_id": "t0", "observation_id": "o0"},
        headers=HX,
    )
    client.post(add_url(world), data={"queue_id": queue.id, "session_id": "s1"}, headers=HX)
    got = {(i.object_type, i.object_id, i.trace_id) for i in items(db, queue)}
    assert got == {("observation", "o0", "t0"), ("session", "s1", None)}


def test_selection_reports_added_and_skipped(client, login, world, db, queue, traces):
    login(client, world.users[Role.MEMBER])
    client.post(add_url(world), data={"queue_id": queue.id, "trace_id": "t0"}, headers=HX)
    res = client.post(
        add_url(world), data={"queue_id": queue.id, "trace_id": ["t0", "t1", "t2"]}, headers=HX
    )
    assert "Added 2 to Refund review. 1 skipped, already in the queue." in res.text


def test_unknown_trace_is_not_found(client, login, world, queue, traces):
    login(client, world.users[Role.MEMBER])
    res = client.post(add_url(world), data={"queue_id": queue.id, "trace_id": "nope"}, headers=HX)
    assert res.status_code == 404


def test_add_all_matching_snapshots_the_filter_and_skips_repeats(
    client, login, world, db, queue, traces
):
    login(client, world.users[Role.MEMBER])
    flt = "env=production&range=30d"
    dialog = client.get(add_url(world), params={"matching": "1", "filter": flt}, headers=HX)
    assert dialog.status_code == 200 and "3 traces match" in dialog.text
    data = {"queue_id": queue.id, "matching": "1", "filter": flt}
    first = client.post(add_url(world), data=data, headers=HX)
    assert "Added 3 to Refund review." in first.text
    assert {i.object_id for i in items(db, queue)} == {"t0", "t1", "t2"}
    second = client.post(add_url(world), data=data, headers=HX)
    assert "All 3 were already in Refund review" in second.text


def test_matching_is_capped_at_queue_bulk_max(client, login, world, db, queue, traces, container):
    container.settings.queue_bulk_max = 2
    login(client, world.users[Role.MEMBER])
    flt = "range=30d"
    dialog = client.get(add_url(world), params={"matching": "1", "filter": flt}, headers=HX)
    assert "5 traces match; the limit is 2" in dialog.text
    res = client.post(
        add_url(world), data={"queue_id": queue.id, "matching": "1", "filter": flt}, headers=HX
    )
    assert "Added 2 to Refund review" in res.text and "bulk limit" in res.text
    assert len(items(db, queue)) == 2


def test_queue_with_only_archived_configs_is_disabled(
    client, login, world, db, queue, config, traces
):
    config.archived_at = utcnow()
    db.commit()
    login(client, world.users[Role.MEMBER])
    dialog = client.get(add_url(world), params={"trace_id": "t0"}, headers=HX)
    assert "all configs archived" in dialog.text and "disabled" in dialog.text
    res = client.post(add_url(world), data={"queue_id": queue.id, "trace_id": "t0"}, headers=HX)
    assert res.status_code == 400 and "archived" in res.text and not items(db, queue)


def test_viewer_gets_no_action_and_no_endpoint(client, login, world, queue, traces):
    login(client, world.users[Role.VIEWER])
    for path in (
        f"/project/{world.project.id}/traces/t0",
        f"/project/{world.project.id}/traces?range=30d&env=production",
        f"/project/{world.project.id}/sessions",
        f"/project/{world.project.id}/sessions/s1",
    ):
        page = client.get(path, headers=HTML)
        assert page.status_code == 200
        assert "Add to queue" not in page.text and "queues/add" not in page.text, path
    assert client.get(add_url(world), params={"trace_id": "t0"}, headers=HX).status_code == 403
    data = {"queue_id": queue.id, "trace_id": "t0"}
    assert client.post(add_url(world), data=data, headers=HX).status_code == 403


def test_member_sees_the_actions(client, login, world, queue, traces):
    login(client, world.users[Role.MEMBER])
    base = f"/project/{world.project.id}"
    assert "queues/add?trace_id=t0" in client.get(f"{base}/traces/t0", headers=HTML).text
    listing = client.get(f"{base}/traces?range=30d&env=production", headers=HTML).text
    assert "data-bulk-queue" in listing and "matching to queue" in listing
    assert "data-session-box" in client.get(f"{base}/sessions", headers=HTML).text
    assert "queues/add?session_id=s1" in client.get(f"{base}/sessions/s1", headers=HTML).text


def test_plain_page_without_htmx(client, login, world, queue, traces):
    login(client, world.users[Role.MEMBER])
    page = client.get(add_url(world), params={"trace_id": "t0"}, headers=HTML)
    assert page.status_code == 200 and "<form" in page.text
