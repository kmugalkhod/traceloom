"""Public share, delete (one and by filter), comment cleanup and the audit log."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from tests.helpers_web import T0, form_token, post_form, score, seed_trace
from traceloom.auth.permissions import Role
from traceloom.db.models import AuditLog, Comment
from traceloom.ports import ObservationRow, TraceQuery, TraceRow
from traceloom.services import comments as comment_service

HX = {"HX-Request": "true"}
XSS = "<script>alert(1)</script>"


def pid(world):
    return world.project.id


def base(world, tid="t1"):
    return f"/project/{pid(world)}/traces/{tid}"


def as_role(client, login, world, role):
    return login(client, world.user(role), csrf=False)


def toggle_share(client, world, enabled):
    html = client.get(base(world) + "/actions").text
    return post_form(client, html, base(world) + "/share", {"enabled": enabled}, headers=HX)


def anonymous(app):
    return TestClient(app)


@pytest.fixture
def trace(container, world):
    seed_trace(container, pid(world), "t1", name="support-agent", tags=["x"])
    return "t1"


# --- share ----------------------------------------------------------------------------------


def test_share_toggle(client, app, login, world, trace, db):
    as_role(client, login, world, Role.ADMIN)
    url = f"/share/{pid(world)}/t1"
    assert anonymous(app).get(url).status_code == 404  # off by default
    r = toggle_share(client, world, "1")
    assert r.status_code == 200 and url in r.text  # the link is shown
    page = anonymous(app).get(url)
    assert page.status_code == 200 and "support-agent" in page.text
    assert "noindex" in page.headers["x-robots-tag"] and page.headers["cache-control"] == "no-store"
    r = toggle_share(client, world, "0")
    assert url not in r.text
    assert anonymous(app).get(url).status_code == 404
    actions = list(db.scalars(select(AuditLog.action).where(AuditLog.project_id == pid(world))))
    assert actions == ["trace.share_enabled", "trace.share_disabled"]


def test_only_admins_toggle_sharing(client, login, world, trace, container):
    for role in (Role.VIEWER, Role.MEMBER):
        as_role(client, login, world, role)
        client.get(base(world) + "/actions")
        token = client.cookies["tl_csrf"]
        r = client.post(
            base(world) + "/share", data={"csrf_token": token, "enabled": "1"}, headers=HX
        )
        assert r.status_code == 403
        assert "Public share" not in client.get(base(world) + "/actions").text
    assert container.trace_store.get_trace(pid(world), "t1").trace.public is False


def test_share_page_has_no_chrome_or_private_data(client, app, login, world, trace, db):
    as_role(client, login, world, Role.ADMIN)
    seed_trace(
        client.app.state.container, pid(world), "t1", user_id="secret-user", session_id="sess-9"
    )
    html = client.get(base(world) + "/discussion").text
    post_form(client, html, base(world) + "/comments", {"body": "private note"}, headers=HX)
    toggle_share(client, world, "1")
    page = anonymous(app).get(f"/share/{pid(world)}/t1").text
    assert 'aria-label="Main"' not in page and "Settings" not in page
    assert "private note" not in page and "secret-user" not in page and "sess-9" not in page
    assert "Read-only" in page


def test_share_page_escapes_script_input_in_every_place(client, app, login, world, container):
    ts = T0
    trace = TraceRow(
        id="x1", timestamp=ts, name=XSS, input=[{"role": XSS, "content": XSS}], output={"k": XSS},
        metadata={XSS: XSS}, tags=[XSS], release=XSS, public=True, event_ts=ts,
    )  # fmt: skip
    obs = ObservationRow(
        id="x1-o", trace_id="x1", start_time=trace.timestamp, name=XSS, input=XSS, output=XSS,
        metadata={"a": XSS}, status_message=XSS, level="ERROR", model=XSS, event_ts=trace.timestamp,
    )  # fmt: skip
    container.trace_store.upsert(pid(world), [trace], [obs], [])
    r = anonymous(app).get(f"/share/{pid(world)}/x1")
    assert r.status_code == 200
    assert "<script>alert" not in r.text and "<script>" not in r.text.replace(
        '<script src="/static/app.js">', ""
    )
    assert r.text.count("&lt;script&gt;alert(1)&lt;/script&gt;") >= 8


def test_share_page_404_for_other_project_unknown_trace_and_private(
    client, app, login, world, trace, make_project, container
):
    as_role(client, login, world, Role.ADMIN)
    toggle_share(client, world, "1")
    other = make_project(world.org, "Other")
    seed_trace(container, other.id, "t2")  # exists but is not shared
    anon = anonymous(app)
    assert anon.get(f"/share/{other.id}/t1").status_code == 404  # right id, wrong project
    assert anon.get(f"/share/{other.id}/t2").status_code == 404  # not shared
    assert anon.get(f"/share/{pid(world)}/ghost").status_code == 404
    assert anon.get("/share/nope/t1").status_code == 404


# --- delete one -------------------------------------------------------------------------------


def counts(db, container, world, tid="t1"):
    detail = container.trace_store.get_trace(pid(world), tid)
    comments = db.scalar(
        select(func.count())
        .select_from(Comment)
        .where(Comment.trace_id == tid, Comment.project_id == pid(world))
    )
    return (None if detail is None else (len(detail.observations), len(detail.scores))), comments


def test_delete_one_removes_everything_and_is_audited(
    client, login, world, container, db, make_project
):
    seed_trace(container, pid(world), "t1", scores=[score("t1", "q", 1.0)])
    other = make_project(world.org, "Other")
    seed_trace(container, other.id, "t1")  # same id in another project must survive
    as_role(client, login, world, Role.ADMIN)
    html = client.get(base(world) + "/discussion").text
    post_form(client, html, base(world) + "/comments", {"body": "gone soon"}, headers=HX)
    assert counts(db, container, world) == ((2, 1), 1)
    actions = client.get(base(world) + "/actions").text
    assert "Deletes <span" in actions and "2 observations, 1 score and 1 comment" in actions
    r = post_form(client, actions, f"/project/{pid(world)}/traces-delete/t1", {})
    assert r.status_code == 303 and r.headers["location"].startswith(
        f"/project/{pid(world)}/traces"
    )
    db.expire_all()
    assert counts(db, container, world) == (None, 0)
    assert container.trace_store.get_trace(other.id, "t1") is not None
    row = db.scalar(select(AuditLog).where(AuditLog.action == "trace.deleted"))
    assert row.actor_id == world.user(Role.ADMIN).id and row.project_id == pid(world)
    assert row.target == "t1" and row.detail["count"] == 1 and row.detail["trace_ids"] == ["t1"]


@pytest.mark.parametrize("role", [Role.VIEWER, Role.MEMBER])
def test_only_admins_delete(client, login, world, trace, container, role):
    as_role(client, login, world, role)
    html = client.get(base(world) + "/actions").text
    assert "Delete" not in html and "delete-trace" not in html
    token = client.cookies["tl_csrf"]
    path = f"/project/{pid(world)}/traces-delete/t1"
    assert client.post(path, data={"csrf_token": token}).status_code == 403
    assert client.get(f"/project/{pid(world)}/traces-delete-matching").status_code == 403
    r = client.post(
        f"/project/{pid(world)}/traces-delete-matching",
        data={"csrf_token": token, "filter": "", "confirm": "1"},
    )
    assert r.status_code == 403
    assert container.trace_store.get_trace(pid(world), "t1") is not None


def test_delete_unknown_trace_is_404(client, login, world, trace):
    as_role(client, login, world, Role.ADMIN)
    html = client.get(base(world) + "/actions").text
    token = form_token(html, f"/project/{pid(world)}/traces-delete/t1")
    r = client.post(f"/project/{pid(world)}/traces-delete/ghost", data={"csrf_token": token})
    assert r.status_code == 404


# --- delete by filter -----------------------------------------------------------------------------


@pytest.fixture
def many(container, world, make_project):
    for i in range(5):
        seed_trace(container, pid(world), f"keep{i}", name="keep", environment="prod")
    for i in range(3):
        seed_trace(container, pid(world), f"drop{i}", name="drop", environment="prod")
    other = make_project(world.org, "Other")
    for i in range(2):
        seed_trace(container, other.id, f"drop{i}", name="drop", environment="prod")
    return other


def dialog(client, world, query):
    return client.get(f"/project/{pid(world)}/traces-delete-matching?{query}")


def test_dialog_shows_the_match_count_and_filter(client, login, world, many):
    as_role(client, login, world, Role.ADMIN)
    r = dialog(client, world, "name=drop&env=prod")
    assert r.status_code == 200
    assert "Delete 3 traces?" in r.text and "Type <b>3</b> to confirm" in r.text
    assert "name: drop" in r.text and "env: prod" in r.text and "support" not in r.text
    assert "Only traces in" in r.text
    assert "Delete 8 traces?" in dialog(client, world, "env=prod").text
    assert "Delete 0 traces?" in dialog(client, world, "name=nothing").text


def test_delete_by_filter_audited(client, login, world, many, container, db):
    as_role(client, login, world, Role.ADMIN)
    page = dialog(client, world, "name=drop").text
    action = f"/project/{pid(world)}/traces-delete-matching"
    # wrong number: refused, nothing deleted
    r = post_form(client, page, action, {"filter": "name=drop", "confirm": "99"})
    assert r.status_code == 400 and "Type 3 to confirm" in r.text
    assert len(container.trace_store.matching_trace_ids(pid(world), TraceQuery())) == 8
    r = post_form(client, page, action, {"filter": "name=drop", "confirm": "3"})
    assert r.status_code == 303 and "count=3" in r.headers["location"]
    remaining = set(container.trace_store.matching_trace_ids(pid(world), TraceQuery()))
    assert remaining == {f"keep{i}" for i in range(5)}  # exactly the matches are gone
    assert (
        len(container.trace_store.matching_trace_ids(many.id, TraceQuery())) == 2
    )  # other project
    row = db.scalar(select(AuditLog).where(AuditLog.action == "traces.deleted_by_filter"))
    admin = world.user(Role.ADMIN)
    assert row.actor_id == admin.id and row.project_id == pid(world) and row.created_at
    assert row.detail["count"] == 3 and sorted(row.detail["trace_ids"]) == [
        "drop0",
        "drop1",
        "drop2",
    ]
    assert row.detail["filter"] == {"name": "drop"} and row.detail["query_string"] == "name=drop"


def test_count_is_rechecked_at_delete_time(client, login, world, many, container):
    as_role(client, login, world, Role.ADMIN)
    page = dialog(client, world, "name=drop").text
    seed_trace(container, pid(world), "drop-late", name="drop", environment="prod")  # a 4th appears
    r = post_form(
        client,
        page,
        f"/project/{pid(world)}/traces-delete-matching",
        {"filter": "name=drop", "confirm": "3"},
    )
    assert r.status_code == 400 and "Type 4 to confirm" in r.text
    assert container.trace_store.get_trace(pid(world), "drop0") is not None


def test_delete_by_filter_with_no_matches_does_nothing(client, login, world, many, db):
    as_role(client, login, world, Role.ADMIN)
    page = dialog(client, world, "name=nothing").text
    r = post_form(
        client,
        page,
        f"/project/{pid(world)}/traces-delete-matching",
        {"filter": "name=nothing", "confirm": "0"},
    )
    assert r.status_code == 400 and "No traces match" in r.text
    assert (
        db.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action == "traces.deleted_by_filter")
        )
        == 0
    )


def test_comments_are_removed_even_when_the_store_leaves_them(
    client, login, world, many, container, db, monkeypatch
):
    """ClickHouse deletes cannot reach the meta DB, so the service layer removes comments."""
    admin = world.user(Role.ADMIN)
    for tid in ("drop0", "drop1", "keep0"):
        comment_service.add_comment(
            db, project=world.project, trace_id=tid, author=admin, body="note"
        )
    db.commit()
    monkeypatch.setattr(
        container.trace_store, "delete_traces", lambda project_id, target: len(target)
    )
    as_role(client, login, world, Role.ADMIN)
    page = dialog(client, world, "name=drop").text
    post_form(
        client,
        page,
        f"/project/{pid(world)}/traces-delete-matching",
        {"filter": "name=drop", "confirm": "3"},
    )
    db.expire_all()
    left = set(db.scalars(select(Comment.trace_id).where(Comment.project_id == pid(world))))
    assert left == {"keep0"}


def test_dialog_values_are_escaped(client, login, world, many):
    as_role(client, login, world, Role.ADMIN)
    r = dialog(client, world, "q=%3Cscript%3Ealert(1)%3C/script%3E")
    assert "<script>alert" not in r.text and "&lt;script&gt;alert(1)&lt;/script&gt;" in r.text
    assert 'name="filter" value="q=%3Cscript%3E' in r.text  # stays percent-encoded


# --- audit log tab -------------------------------------------------------------------------------


def test_audit_log_tab(client, login, world, many, container):
    as_role(client, login, world, Role.ADMIN)
    page = dialog(client, world, "name=drop").text
    post_form(
        client,
        page,
        f"/project/{pid(world)}/traces-delete-matching",
        {"filter": "name=drop", "confirm": "3"},
    )
    r = client.get(f"/project/{pid(world)}/settings/audit")
    assert r.status_code == 200
    assert "traces.deleted_by_filter" in r.text and world.user(Role.ADMIN).name in r.text
    assert "3 traces" in r.text and "drop0" in r.text and ">Audit log</a>" in r.text
    assert 'name": "drop"' in r.text or "&#34;name&#34;: &#34;drop&#34;" in r.text


@pytest.mark.parametrize("role", [Role.VIEWER, Role.MEMBER])
def test_audit_log_is_admin_only(client, login, world, role):
    as_role(client, login, world, role)
    assert client.get(f"/project/{pid(world)}/settings/audit").status_code == 403
    assert "Audit log" not in client.get(f"/project/{pid(world)}/settings").text


def test_audit_log_is_project_scoped(client, login, world, make_project, db):
    from traceloom.services import audit

    other = make_project(world.org, "Other")
    audit.record(db, action="trace.deleted", actor_type="user", actor_id=None, org_id=world.org.id,
                 project_id=other.id, target="other-secret")  # fmt: skip
    db.commit()
    as_role(client, login, world, Role.ADMIN)
    assert "other-secret" not in client.get(f"/project/{pid(world)}/settings/audit").text
