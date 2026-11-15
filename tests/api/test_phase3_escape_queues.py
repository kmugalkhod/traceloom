from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from traceloom.annotation import queues
from traceloom.annotation.queues import ItemRef
from traceloom.auth.permissions import Role
from traceloom.db.models import AnnotationItem
from traceloom.ports import ObservationRow, TraceRow
from traceloom.prompts.service import Actor

HTML = {"Accept": "text/html"}
XSS = "<script>alert(1)</script>"
ESCAPED = "&lt;script&gt;alert(1)&lt;/script&gt;"
T0 = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)


def url(world, suffix=""):
    return f"/project/{world.project.id}/queues{suffix}"


@pytest.fixture
def setup(db, world, container, make_score_config):
    config = make_score_config(world.project, XSS)
    member = Actor.for_user(world.users[Role.MEMBER], Role.MEMBER)
    queue = queues.create_queue(db, world.project, member, XSS, XSS, [config.id])
    db.commit()  # the trace store shares the database file
    trace = TraceRow("t1", T0, name=XSS, input=XSS, output={"answer": XSS}, metadata={"k": XSS})
    obs = ObservationRow("o1", "t1", T0, type="span", name=XSS, input=XSS, output=XSS)
    container.trace_store.upsert(world.project.id, [trace], [obs], [])
    queues.add_items(
        db, world.project, member, queue.id, [ItemRef("trace", "t1")], container.settings
    )
    db.commit()
    return queue, config


def test_no_page_renders_user_content_as_markup(client, login, world, db, setup):
    queue, config = setup
    login(client, world.users[Role.MEMBER])
    pages = [url(world), url(world, f"/{queue.id}")]
    for page in pages:
        text = client.get(page, headers=HTML).text
        assert XSS not in text and ESCAPED in text
    start = client.get(url(world, f"/{queue.id}/review"), headers=HTML, follow_redirects=False)
    item = start.headers["location"].split("item=")[1]
    review = client.get(url(world, f"/{queue.id}/review?item={item}"), headers=HTML).text
    assert XSS not in review and ESCAPED in review


def test_a_comment_with_a_script_tag_is_stored_and_shown_as_text(client, login, world, db, setup):
    queue, config = setup
    login(client, world.users[Role.MEMBER])
    start = client.get(url(world, f"/{queue.id}/review"), headers=HTML, follow_redirects=False)
    item = start.headers["location"].split("item=")[1]
    done = client.post(
        url(world, f"/{queue.id}/review/{item}/complete"),
        data={f"score_{config.id}": "0.5", "comment": XSS},
        headers=HTML,
        follow_redirects=False,
    )
    assert done.status_code == 303
    login(client, world.users[Role.VIEWER])
    page = client.get(url(world, f"/{queue.id}/review?item={item}"), headers=HTML).text
    assert XSS not in page and ESCAPED in page
    row = db.scalar(select(AnnotationItem))
    assert row.status == "completed"


def test_a_rejected_form_echoes_the_comment_escaped(client, login, world, db, setup):
    queue, config = setup
    login(client, world.users[Role.MEMBER])
    start = client.get(url(world, f"/{queue.id}/review"), headers=HTML, follow_redirects=False)
    item = start.headers["location"].split("item=")[1]
    bad = client.post(
        url(world, f"/{queue.id}/review/{item}/complete"),
        data={f"score_{config.id}": f"{XSS}", "comment": XSS},
        headers=HTML,
    )
    assert bad.status_code == 400 and XSS not in bad.text and ESCAPED in bad.text


def test_notice_and_error_query_parameters_are_escaped(client, login, world, db, setup):
    queue, _ = setup
    login(client, world.users[Role.MEMBER])
    page = client.get(url(world, f"/{queue.id}"), params={"error": XSS}, headers=HTML).text
    assert XSS not in page and ESCAPED in page
    assert (
        XSS
        not in client.get(
            url(world, f"/{queue.id}/review"),
            params={"notice": XSS},
            headers=HTML,
            follow_redirects=True,
        ).text
    )
