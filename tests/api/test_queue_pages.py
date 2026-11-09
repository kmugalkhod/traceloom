from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from traceloom.annotation import claim, queues
from traceloom.annotation.queues import ItemRef
from traceloom.auth.permissions import Role
from traceloom.db.models import AnnotationItem, AnnotationQueue, AuditLog, Score
from traceloom.ports import ObservationRow, TraceRow
from traceloom.prompts.service import Actor

HTML = {"Accept": "text/html"}
T0 = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)


def url(world, suffix=""):
    return f"/project/{world.project.id}/queues{suffix}"


@pytest.fixture
def config(world, make_score_config):
    return make_score_config(world.project, "helpfulness")


@pytest.fixture
def queue(db, world, config):
    member = Actor.for_user(world.users[Role.MEMBER], Role.MEMBER)
    q = queues.create_queue(
        db, world.project, member, "Refund review", "Check answers", [config.id]
    )
    db.commit()
    return q


def put_traces(container, world, n, **fields):
    rows = [
        TraceRow(
            f"t{i}",
            T0 + timedelta(seconds=i),
            name=f"chat-{i}",
            input=f"question {i}",
            output=f"answer {i}",
            **fields,
        )
        for i in range(n)
    ]
    obs = [
        ObservationRow(
            f"o{i}", f"t{i}", T0 + timedelta(seconds=i), type="generation", name="llm.reply"
        )
        for i in range(n)
    ]
    container.trace_store.upsert(world.project.id, rows, obs, [])
    return rows


def fill(db, world, container, queue, n, **fields):
    put_traces(container, world, n, **fields)
    member = Actor.for_user(world.users[Role.MEMBER], Role.MEMBER)
    queues.add_items(
        db,
        world.project,
        member,
        queue.id,
        [ItemRef("trace", f"t{i}") for i in range(n)],
        container.settings,
    )
    db.commit()


def as_(client, login, world, role):
    return login(client, world.users[role])


def item_id_from(response):
    assert response.status_code == 303, response.text[:300]
    return response.headers["location"].split("item=")[1].split("&")[0]


# --- list --------------------------------------------------------------------------------------


def test_member_sees_the_list_with_progress_and_new_queue(
    client, login, world, db, container, queue
):
    fill(db, world, container, queue, 4)
    as_(client, login, world, Role.MEMBER)
    page = client.get(url(world), headers=HTML)
    assert page.status_code == 200
    assert (
        "Refund review" in page.text
        and "New queue" in page.text
        and 'role="progressbar"' in page.text
    )
    assert (
        "0 of 4 reviewed" in page.text
        and "helpfulness" in page.text
        and "No activity yet" in page.text
    )
    assert f"/queues/{queue.id}/review" in page.text and "Delete queue" not in page.text


def test_viewer_reads_but_gets_no_controls(client, login, world, db, container, queue):
    fill(db, world, container, queue, 2)
    as_(client, login, world, Role.VIEWER)
    page = client.get(url(world), headers=HTML)
    assert page.status_code == 200 and "Refund review" in page.text
    assert (
        "New queue" not in page.text
        and "/review" not in page.text
        and "More actions" not in page.text
    )
    detail = client.get(url(world, f"/{queue.id}"), headers=HTML)
    assert (
        detail.status_code == 200
        and "Start reviewing" not in detail.text
        and "Edit queue" not in detail.text
    )
    assert client.get(url(world, f"/{queue.id}/review"), headers=HTML).status_code == 403
    assert client.post(url(world), data={"name": "x"}, headers=HTML).status_code == 403


def test_empty_state_and_create(client, login, world, db, config):
    as_(client, login, world, Role.MEMBER)
    assert "No annotation queues yet" in client.get(url(world), headers=HTML).text
    bad = client.post(url(world), data={"name": "Q"}, headers=HTML)
    assert (
        bad.status_code == 400
        and "Choose at least one score config" in bad.text
        and "data-autoopen" in bad.text
    )
    ok = client.post(
        url(world),
        data={"name": "Tone audit", "description": "d", "config_id": [config.id]},
        headers=HTML,
        follow_redirects=False,
    )
    assert ok.status_code == 303 and "notice=created" in ok.headers["location"]
    row = db.scalar(select(AnnotationQueue))
    assert (row.name, row.config_ids) == ("Tone audit", [config.id])
    assert "Queue created" in client.get(ok.headers["location"], headers=HTML).text
    db.expire_all()
    assert [a.action for a in db.scalars(select(AuditLog))] == ["annotation_queue.create"]


def test_admin_deletes_a_queue_and_a_member_gets_403(client, login, world, db, container, queue):
    qid = queue.id
    fill(db, world, container, queue, 2)
    as_(client, login, world, Role.MEMBER)
    assert client.post(url(world, f"/{qid}/delete"), headers=HTML).status_code == 403
    assert "Delete queue" not in client.get(url(world, f"/{qid}"), headers=HTML).text
    as_(client, login, world, Role.ADMIN)
    assert "Delete queue" in client.get(url(world, f"/{qid}"), headers=HTML).text
    gone = client.post(url(world, f"/{qid}/delete"), headers=HTML, follow_redirects=False)
    assert gone.status_code == 303 and "notice=deleted" in gone.headers["location"]
    db.expire_all()
    assert db.query(AnnotationQueue).count() == 0 and db.query(AnnotationItem).count() == 0
    assert client.get(url(world, f"/{qid}"), headers=HTML).status_code == 404


def test_edit_queue(client, login, world, db, queue, config, make_score_config):
    other = make_score_config(world.project, "tone")
    as_(client, login, world, Role.MEMBER)
    bad = client.post(url(world, f"/{queue.id}/edit"), data={"name": ""}, headers=HTML)
    assert bad.status_code == 400 and "name" in bad.text
    ok = client.post(
        url(world, f"/{queue.id}/edit"),
        data={"name": "Renamed", "config_id": [config.id, other.id]},
        headers=HTML,
        follow_redirects=False,
    )
    assert ok.status_code == 303
    db.expire_all()
    assert db.get(AnnotationQueue, queue.id).config_ids == [config.id, other.id]


def test_queues_are_project_scoped(
    client, login, world, db, make_org, make_project, make_score_config
):
    other = make_project(make_org())
    member = Actor.for_user(world.users[Role.MEMBER], Role.MEMBER)
    foreign = queues.create_queue(
        db, other, member, "theirs", "", [make_score_config(other, "c").id]
    )
    db.commit()
    as_(client, login, world, Role.ADMIN)
    assert "theirs" not in client.get(url(world), headers=HTML).text
    assert client.get(url(world, f"/{foreign.id}"), headers=HTML).status_code == 404
    assert client.post(url(world, f"/{foreign.id}/delete"), headers=HTML).status_code == 404


# --- detail ------------------------------------------------------------------------------------


def test_detail_shows_progress_reviewers_items_and_filters(
    client, login, world, db, container, queue, config
):
    fill(db, world, container, queue, 4)
    member = Actor.for_user(world.users[Role.MEMBER], Role.MEMBER)
    for _ in range(2):
        item = claim.claim_next(db, world.project, member, queue.id, container.settings)
        claim.complete(
            db,
            container.trace_store,
            world.project,
            member,
            queue.id,
            item.id,
            {config.id: 0.5},
            duration_ms=42_000,
        )
        db.commit()
    skipped = claim.claim_next(db, world.project, member, queue.id, container.settings)
    claim.skip(db, world.project, member, queue.id, skipped.id)
    db.commit()
    as_(client, login, world, Role.MEMBER)
    page = client.get(url(world, f"/{queue.id}"), headers=HTML).text
    assert (
        "50%" in page
        and "2 of 4 reviewed" in page
        and "2 completed" in page
        and "1 skipped" in page
        and "1 pending" in page
    )
    assert (
        "42 s" in page
        and world.users[Role.MEMBER].name in page
        and "Reopen" in page
        and "chat-0" in page
    )
    only = client.get(url(world, f"/{queue.id}?status=skipped"), headers=HTML).text
    assert only.count("Reopen") == 1 and "chat-0" not in only
    mine = client.get(
        url(world, f"/{queue.id}?reviewer={world.users[Role.MEMBER].id}"), headers=HTML
    ).text
    assert "chat-0" in mine
    other = client.get(url(world, f"/{queue.id}?reviewer=nobody"), headers=HTML).text
    assert "No items here" in other
    assert "No items here" in client.get(url(world, f"/{queue.id}?status=bogus"), headers=HTML).text


# --- review ------------------------------------------------------------------------------------


def test_member_works_a_queue_to_queue_complete(client, login, world, db, container, queue, config):
    fill(db, world, container, queue, 3)
    as_(client, login, world, Role.MEMBER)
    first = item_id_from(
        client.get(url(world, f"/{queue.id}/review"), headers=HTML, follow_redirects=False)
    )
    page = client.get(url(world, f"/{queue.id}/review?item={first}"), headers=HTML)
    assert page.status_code == 200
    assert "question 0" in page.text and "answer 0" in page.text and "llm.reply" in page.text
    assert (
        "Item 1 of 3" in page.text
        and "Yours until" in page.text
        and f'name="score_{config.id}"' in page.text
    )
    assert "queue_review.js" in page.text and "Complete" in page.text
    # ⌘↵: complete and land on the next item
    done = client.post(
        url(world, f"/{queue.id}/review/{first}/complete"),
        data={f"score_{config.id}": "0.8", "comment": "good", "duration_ms": "1500"},
        headers=HTML,
        follow_redirects=False,
    )
    assert done.status_code == 303 and done.headers["location"].endswith("/review")
    second = item_id_from(
        client.get(done.headers["location"], headers=HTML, follow_redirects=False)
    )
    assert second != first
    # s: skip
    skip = client.post(
        url(world, f"/{queue.id}/review/{second}/skip"), headers=HTML, follow_redirects=False
    )
    assert skip.status_code == 303
    third = item_id_from(client.get(skip.headers["location"], headers=HTML, follow_redirects=False))
    client.post(
        url(world, f"/{queue.id}/review/{third}/complete"),
        data={f"score_{config.id}": "1"},
        headers=HTML,
    )
    # nothing pending: the skipped one is still open work
    left = client.get(url(world, f"/{queue.id}/review"), headers=HTML)
    assert (
        left.status_code == 200
        and "Nothing to review right now" in left.text
        and "skipped" in left.text
    )
    # reopen the skipped item and complete it
    assert (
        client.post(
            url(world, f"/{queue.id}/items/{second}/reopen"), headers=HTML, follow_redirects=False
        ).status_code
        == 303
    )
    again = item_id_from(
        client.get(url(world, f"/{queue.id}/review"), headers=HTML, follow_redirects=False)
    )
    assert again == second
    client.post(
        url(world, f"/{queue.id}/review/{again}/complete"),
        data={f"score_{config.id}": "0.2"},
        headers=HTML,
    )
    final = client.get(url(world, f"/{queue.id}/review"), headers=HTML)
    assert "Queue complete" in final.text and "You: 3 completed" in final.text
    assert "Queue complete" in client.get(url(world, f"/{queue.id}"), headers=HTML).text
    db.expire_all()
    scores = db.scalars(select(Score).where(Score.queue_id == queue.id)).all()
    assert len(scores) == 3 and {s.source for s in scores} == {"ANNOTATION"}
    assert {s.comment for s in scores} == {"good", None}


def test_complete_with_an_invalid_value_keeps_the_item_and_shows_the_error(
    client, login, world, db, container, queue, config
):
    fill(db, world, container, queue, 1)
    as_(client, login, world, Role.MEMBER)
    item = item_id_from(
        client.get(url(world, f"/{queue.id}/review"), headers=HTML, follow_redirects=False)
    )
    bad = client.post(
        url(world, f"/{queue.id}/review/{item}/complete"),
        data={f"score_{config.id}": "1.5", "comment": "keep me"},
        headers=HTML,
    )
    assert (
        bad.status_code == 400
        and "above the maximum" in bad.text
        and "Nothing was saved" in bad.text
    )
    assert 'value="1.5"' in bad.text and "keep me" in bad.text
    missing = client.post(url(world, f"/{queue.id}/review/{item}/complete"), data={}, headers=HTML)
    assert missing.status_code == 400 and "helpfulness" in missing.text
    notnum = client.post(
        url(world, f"/{queue.id}/review/{item}/complete"),
        data={f"score_{config.id}": "abc"},
        headers=HTML,
    )
    assert notnum.status_code == 400 and "must be a number" in notnum.text
    db.expire_all()
    assert db.scalar(select(AnnotationItem)).status == "pending" and db.query(Score).count() == 0


def test_next_saves_when_valid_and_otherwise_moves_on(
    client, login, world, db, container, queue, config
):
    fill(db, world, container, queue, 3)
    as_(client, login, world, Role.MEMBER)
    first = item_id_from(
        client.get(url(world, f"/{queue.id}/review"), headers=HTML, follow_redirects=False)
    )
    moved = client.post(
        url(world, f"/{queue.id}/review/{first}/next"),
        data={},
        headers=HTML,
        follow_redirects=False,
    )  # nothing filled in
    second = item_id_from(moved)
    assert second != first
    saved = client.post(
        url(world, f"/{queue.id}/review/{second}/next"),
        data={f"score_{config.id}": "0.4"},
        headers=HTML,
        follow_redirects=False,
    )
    assert saved.headers["location"].endswith("/review")
    db.expire_all()
    statuses = {i.id: i.status for i in db.scalars(select(AnnotationItem))}
    assert statuses[first] == "pending" and statuses[second] == "completed"
    assert db.get(AnnotationItem, first).locked_by is None  # released for someone else


def test_next_with_no_other_item_stays_on_this_one(client, login, world, db, container, queue):
    fill(db, world, container, queue, 1)
    as_(client, login, world, Role.MEMBER)
    only = item_id_from(
        client.get(url(world, f"/{queue.id}/review"), headers=HTML, follow_redirects=False)
    )
    moved = client.post(
        url(world, f"/{queue.id}/review/{only}/next"), data={}, headers=HTML, follow_redirects=False
    )
    assert f"item={only}" in moved.headers["location"] and "no-other" in moved.headers["location"]
    page = client.get(moved.headers["location"], headers=HTML)
    assert "no other item available" in page.text.lower() and "Yours until" in page.text


def test_a_locked_item_is_read_only_for_the_second_reviewer(
    client, login, world, db, container, queue, config
):
    fill(db, world, container, queue, 1)
    member_client = as_(client, login, world, Role.MEMBER)
    item = item_id_from(
        member_client.get(url(world, f"/{queue.id}/review"), headers=HTML, follow_redirects=False)
    )
    from fastapi.testclient import TestClient

    with TestClient(client.app) as other:
        login(other, world.users[Role.ADMIN])
        page = other.get(url(world, f"/{queue.id}/review?item={item}"), headers=HTML)
        member_name = world.users[Role.MEMBER].name
        assert page.status_code == 200 and f"Locked by {member_name} until" in page.text
        assert (
            "question 0" in page.text
            and "disabled" in page.text
            and "Skip and Complete are not offered" in page.text
        )
        assert "Yours until" not in page.text
        blocked = other.post(
            url(world, f"/{queue.id}/review/{item}/complete"),
            data={f"score_{config.id}": "0.5"},
            headers=HTML,
        )
        assert blocked.status_code == 400 and "locked by someone else" in blocked.text
        assert (
            other.post(
                url(world, f"/{queue.id}/review/{item}/skip"), headers=HTML, follow_redirects=False
            ).status_code
            == 303
        )
        none = other.get(url(world, f"/{queue.id}/review"), headers=HTML)
        assert "Nothing to review right now" in none.text
    db.expire_all()
    row = db.get(AnnotationItem, item)
    assert (
        row.status == "pending"
        and row.locked_by == world.users[Role.MEMBER].id
        and db.query(Score).count() == 0
    )


def test_an_expired_lock_can_be_taken_over(client, login, world, db, container, queue):
    fill(db, world, container, queue, 1)
    member = Actor.for_user(world.users[Role.MEMBER], Role.MEMBER)
    claim.claim_next(
        db,
        world.project,
        member,
        queue.id,
        container.settings,
        now=datetime.now(UTC) - timedelta(hours=1),
    )
    db.commit()
    as_(client, login, world, Role.ADMIN)
    taken = client.get(url(world, f"/{queue.id}/review"), headers=HTML, follow_redirects=False)
    item = item_id_from(taken)
    assert (
        "Yours until"
        in client.get(url(world, f"/{queue.id}/review?item={item}"), headers=HTML).text
    )


def test_renew_extends_the_lock_and_reports_loss(client, login, world, db, container, queue):
    fill(db, world, container, queue, 1)
    as_(client, login, world, Role.MEMBER)
    item = item_id_from(
        client.get(url(world, f"/{queue.id}/review"), headers=HTML, follow_redirects=False)
    )
    db.expire_all()
    before = db.get(AnnotationItem, item).locked_until
    ok = client.post(url(world, f"/{queue.id}/review/{item}/renew"))
    assert ok.status_code == 200 and ok.json()["renewed"] is True and ok.json()["until"]
    db.expire_all()
    assert db.get(AnnotationItem, item).locked_until >= before
    row = db.get(AnnotationItem, item)
    row.locked_by = "someone-else"
    db.commit()
    assert client.post(url(world, f"/{queue.id}/review/{item}/renew")).json() == {
        "renewed": False,
        "until": None,
    }


def test_viewer_opens_an_item_read_only_and_cannot_post(
    client, login, world, db, container, queue, config
):
    fill(db, world, container, queue, 1)
    item = db.scalar(select(AnnotationItem)).id
    as_(client, login, world, Role.VIEWER)
    page = client.get(url(world, f"/{queue.id}/review?item={item}"), headers=HTML)
    assert (
        page.status_code == 200
        and "question 0" in page.text
        and "Skip and Complete are not offered" in page.text
    )
    db.expire_all()
    assert db.get(AnnotationItem, item).locked_by is None  # reading takes no lock
    for suffix in ("complete", "skip", "next", "renew"):
        assert (
            client.post(
                url(world, f"/{queue.id}/review/{item}/{suffix}"),
                data={f"score_{config.id}": "1"},
                headers=HTML,
            ).status_code
            == 403
        )


def test_boolean_and_categorical_inputs_render(
    client, login, world, db, container, make_score_config
):
    yes = make_score_config(world.project, "correct", data_type="BOOLEAN")
    tone = make_score_config(
        world.project,
        "tone",
        data_type="CATEGORICAL",
        categories=[{"label": "warm", "value": 1}, {"label": "cold", "value": 0}],
    )
    member = Actor.for_user(world.users[Role.MEMBER], Role.MEMBER)
    q = queues.create_queue(db, world.project, member, "Mixed", "", [yes.id, tone.id])
    db.commit()
    fill(db, world, container, q, 1)
    as_(client, login, world, Role.MEMBER)
    item = item_id_from(
        client.get(url(world, f"/{q.id}/review"), headers=HTML, follow_redirects=False)
    )
    page = client.get(url(world, f"/{q.id}/review?item={item}"), headers=HTML).text
    assert (
        "data-bool" in page
        and "data-cats" in page
        and ">warm" in page
        and "<kbd>1</kbd>" in page
        and "Press T or F" in page
    )
    done = client.post(
        url(world, f"/{q.id}/review/{item}/complete"),
        data={f"score_{yes.id}": "true", f"score_{tone.id}": "warm"},
        headers=HTML,
        follow_redirects=False,
    )
    assert done.status_code == 303
    db.expire_all()
    got = {s.name: s for s in db.scalars(select(Score).where(Score.queue_id == q.id))}
    assert got["correct"].value == 1.0 and got["tone"].string_value == "warm"


def test_a_missing_object_can_still_be_skipped(client, login, world, db, container, queue):
    member = Actor.for_user(world.users[Role.MEMBER], Role.MEMBER)
    queues.add_items(
        db, world.project, member, queue.id, [ItemRef("trace", "gone")], container.settings
    )
    db.commit()
    as_(client, login, world, Role.MEMBER)
    item = item_id_from(
        client.get(url(world, f"/{queue.id}/review"), headers=HTML, follow_redirects=False)
    )
    page = client.get(url(world, f"/{queue.id}/review?item={item}"), headers=HTML)
    assert "no longer available" in page.text
    assert (
        client.post(
            url(world, f"/{queue.id}/review/{item}/skip"), headers=HTML, follow_redirects=False
        ).status_code
        == 303
    )


def test_session_items_show_their_traces(client, login, world, db, container, queue):
    rows = [
        TraceRow(
            f"s-t{i}", T0, name=f"turn {i}", input=f"in {i}", output=f"out {i}", session_id="sess1"
        )
        for i in range(2)
    ]
    container.trace_store.upsert(world.project.id, rows, [], [])
    member = Actor.for_user(world.users[Role.MEMBER], Role.MEMBER)
    queues.add_items(
        db, world.project, member, queue.id, [ItemRef("session", "sess1")], container.settings
    )
    db.commit()
    as_(client, login, world, Role.MEMBER)
    item = item_id_from(
        client.get(url(world, f"/{queue.id}/review"), headers=HTML, follow_redirects=False)
    )
    page = client.get(url(world, f"/{queue.id}/review?item={item}"), headers=HTML).text
    assert "turn 0" in page and "in 1" in page and "out 1" in page and "Session sess1" in page


def test_other_scores_on_the_object_are_listed(client, login, world, db, container, queue):
    from traceloom.ports import ScoreRow

    fill(db, world, container, queue, 1)
    container.trace_store.add_score(
        world.project.id,
        ScoreRow(
            "sc1", "relevance", T0, trace_id="t0", value=0.9, source="EVAL", execution_id="e1"
        ),
    )
    as_(client, login, world, Role.MEMBER)
    item = item_id_from(
        client.get(url(world, f"/{queue.id}/review"), headers=HTML, follow_redirects=False)
    )
    page = client.get(url(world, f"/{queue.id}/review?item={item}"), headers=HTML).text
    assert (
        "Already scored on this trace" in page
        and "relevance:" in page
        and "0.9" in page
        and "sc eval" in page
    )


def test_qa_queue_pages_use_no_inline_style_or_handlers(client, login, world, db, container, queue):
    # default-src 'self' blocks style="" and on*= attributes: progress bars render empty and the
    # reviewer filter select never submits.
    import re

    fill(db, world, container, queue, 2)
    as_(client, login, world, Role.MEMBER)
    for path in ("", f"/{queue.id}"):
        html = client.get(url(world, path), headers=HTML).text
        assert not re.search(r'<[^>]+\sstyle="width', html)
        assert not re.search(r"\son(change|click|submit)=", html)


def test_progress_bars_and_filters_work_under_the_csp(
    client, login, world, db, container, queue, config
):
    """The app CSP is default-src 'self': no style="" and no on*= attributes. Progress fill comes
    from width classes, and the reviewer select is wired by the static script."""
    import re

    fill(db, world, container, queue, 4)
    member = Actor.for_user(world.users[Role.MEMBER], Role.MEMBER)
    for _ in range(2):
        item = claim.claim_next(db, world.project, member, queue.id, container.settings)
        claim.complete(
            db, container.trace_store, world.project, member, queue.id, item.id, {config.id: 1}
        )
        db.commit()
    skipped = claim.claim_next(db, world.project, member, queue.id, container.settings)
    claim.skip(db, world.project, member, queue.id, skipped.id)
    db.commit()
    as_(client, login, world, Role.MEMBER)
    start = client.get(url(world, f"/{queue.id}/review"), headers=HTML, follow_redirects=False)
    review = client.get(start.headers["location"], headers=HTML).text
    pages = [
        client.get(url(world), headers=HTML).text,
        client.get(url(world, f"/{queue.id}"), headers=HTML).text,
        review,
    ]
    for html in pages:
        assert not re.search(r"<[^>]+\sstyle=", html)
        assert not re.search(r"\son[a-z]+=", html)
    assert '<i class="d w50"></i><i class="s w25"></i>' in pages[1]
    assert "data-autosubmit" in pages[1] and "/static/queue_review.js" in pages[1]
    assert "data-autosubmit" in client.get("/static/queue_review.js").text
    css = client.get("/static/ui/queues.css").text
    assert ".w50{width:50%}" in css and ".w100{width:100%}" in css and ".qd2{width:32px}" in css
