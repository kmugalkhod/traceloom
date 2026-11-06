"""Scores, bookmark and tags for Members; read-only for Viewers."""

from __future__ import annotations

import pytest

from tests.helpers_web import post_form, seed_trace
from traceloom.auth.permissions import Role

HX = {"HX-Request": "true"}


@pytest.fixture
def trace(container, world):
    seed_trace(container, world.project.id, "t1", tags=["alpha"])
    return "t1"


def base(world, tid="t1"):
    return f"/project/{world.project.id}/traces/{tid}"


def page(client, world, part):
    r = client.get(f"{base(world)}/{part}")
    assert r.status_code == 200, r.text
    return r.text


def login_as(client, login, world, role):
    return login(client, world.user(role), csrf=False)


def stored(container, world):
    return container.trace_store.get_trace(world.project.id, "t1")


def test_member_adds_numeric_boolean_and_categorical_scores(client, login, world, trace, container):
    login_as(client, login, world, Role.MEMBER)
    for name, kind, value in (
        ("quality", "NUMERIC", "0.8"),
        ("resolved", "BOOLEAN", "false"),
        ("tone", "CATEGORICAL", "polite"),
    ):
        html = page(client, world, "discussion")
        r = post_form(
            client, html, base(world) + "/scores",
            {"name": name, "data_type": kind, "value": value, "comment": "why"}, headers=HX,
        )  # fmt: skip
        assert r.status_code == 200, r.text
    scores = {s.name: s for s in stored(container, world).scores}
    me = world.user(Role.MEMBER)
    assert scores["quality"].value == 0.8 and scores["quality"].data_type == "NUMERIC"
    assert scores["resolved"].value == 0.0 and scores["resolved"].data_type == "BOOLEAN"
    assert scores["tone"].string_value == "polite" and scores["tone"].data_type == "CATEGORICAL"
    for s in scores.values():
        assert s.source == "ANNOTATION" and s.author_id == me.id and s.comment == "why"
    html = page(client, world, "discussion")
    assert "<b>quality</b><span>0.8</span>" in html
    assert "<b>resolved</b><span>false</span>" in html
    assert "<b>tone</b><span>polite</span>" in html
    assert "Annotation" in html and me.name in html


@pytest.mark.parametrize(
    ("kind", "value", "message"),
    [
        ("NUMERIC", "lots", "Enter a number."),
        ("NUMERIC", "nan", "finite"),
        ("BOOLEAN", "maybe", "true or false"),
        ("CATEGORICAL", "  ", "Enter a value."),
        ("WEIRD", "x", "Choose a score type."),
    ],
)
def test_invalid_score_shows_an_inline_error_and_saves_nothing(
    client, login, world, trace, container, kind, value, message
):
    login_as(client, login, world, Role.MEMBER)
    html = page(client, world, "discussion")
    r = post_form(
        client, html, base(world) + "/scores", {"name": "q", "data_type": kind, "value": value},
        headers=HX,
    )  # fmt: skip
    assert r.status_code == 400 and message in r.text and 'role="alert"' in r.text
    assert stored(container, world).scores == []


def test_score_needs_a_name(client, login, world, trace):
    login_as(client, login, world, Role.MEMBER)
    html = page(client, world, "discussion")
    r = post_form(
        client, html, base(world) + "/scores", {"name": " ", "data_type": "NUMERIC", "value": "1"},
        headers=HX,
    )  # fmt: skip
    assert r.status_code == 400 and "Give the score a name" in r.text


def test_bookmark_toggles(client, login, world, trace, container):
    login_as(client, login, world, Role.MEMBER)
    html = page(client, world, "actions")
    assert 'aria-pressed="false"' in html
    r = post_form(client, html, base(world) + "/bookmark", {"value": "1"}, headers=HX)
    assert r.status_code == 200 and "Bookmarked" in r.text and r.headers["HX-Refresh"] == "true"
    assert stored(container, world).trace.bookmarked is True
    r = post_form(client, r.text, base(world) + "/bookmark", {"value": "0"}, headers=HX)
    assert stored(container, world).trace.bookmarked is False


def test_tags_add_remove_dedupe_and_validate(client, login, world, trace, container):
    login_as(client, login, world, Role.MEMBER)
    html = page(client, world, "tags")
    r = post_form(client, html, base(world) + "/tags", {"tag": "  refund   flow "}, headers=HX)
    assert (
        r.status_code == 200 and r.headers["HX-Refresh"] == "true"
    )  # the header's tag chips change
    post_form(client, r.text, base(world) + "/tags", {"tag": "alpha"}, headers=HX)  # duplicate
    assert stored(container, world).trace.tags == ["alpha", "refund flow"]
    r = post_form(client, page(client, world, "tags"), base(world) + "/tags/remove",
                  {"tag": "alpha"}, headers=HX)  # fmt: skip
    assert stored(container, world).trace.tags == ["refund flow"]
    r = post_form(client, r.text, base(world) + "/tags", {"tag": "  "}, headers=HX)
    assert r.status_code == 400 and "Enter a tag" in r.text


def test_tag_with_markup_renders_as_text(client, login, world, trace):
    login_as(client, login, world, Role.MEMBER)
    html = page(client, world, "tags")
    r = post_form(client, html, base(world) + "/tags", {"tag": "<script>alert(1)</script>"},
                  headers=HX)  # fmt: skip
    assert "<script>alert" not in r.text and "&lt;script&gt;alert(1)&lt;/script&gt;" in r.text


def test_member_sees_controls(client, login, world, trace):
    login_as(client, login, world, Role.MEMBER)
    actions, discussion = page(client, world, "actions"), page(client, world, "discussion")
    tags = page(client, world, "tags")
    assert "Bookmark" in actions and 'name="tag"' in tags and "Remove tag alpha" in tags
    assert 'name="body"' in discussion and 'name="data_type"' in discussion
    assert "Public share" not in actions and "data-dialog-open" not in actions  # Admin+ only


def test_no_edit_controls_for_viewer(client, login, world, trace):
    login_as(client, login, world, Role.VIEWER)
    actions, discussion = page(client, world, "actions"), page(client, world, "discussion")
    tags = page(client, world, "tags")
    for html in (actions, discussion, tags):
        assert (
            "<form" not in html
            and "<textarea" not in html
            and "<button" not in html.replace("data-copy", "").replace("btn btn-icon", "")
        )  # nothing interactive at all
    assert "alpha" not in tags  # the detail page itself shows the tags; no editor for a Viewer
    assert "Remove tag" not in actions and "Add tag" not in actions


@pytest.mark.parametrize(
    ("path", "data"),
    [
        ("/comments", {"body": "x"}),
        ("/scores", {"name": "q", "data_type": "NUMERIC", "value": "1"}),
        ("/bookmark", {"value": "1"}),
        ("/tags", {"tag": "x"}),
        ("/tags/remove", {"tag": "alpha"}),
    ],
)
def test_viewer_gets_403_on_every_write(client, login, world, trace, container, path, data):
    # the token comes from a page the Viewer can load, so only the permission can refuse this
    login_as(client, login, world, Role.VIEWER)
    token = client.cookies["tl_csrf"] if "tl_csrf" in client.cookies else None
    if token is None:
        client.get(base(world) + "/actions")
        token = client.cookies["tl_csrf"]
    r = client.post(base(world) + path, data={"csrf_token": token, **data}, headers=HX)
    assert r.status_code == 403
    after = stored(container, world)
    assert after.trace.tags == ["alpha"] and not after.trace.bookmarked and after.scores == []


def test_writes_need_a_login_and_a_trace(client, login, world, trace):
    client.get("/api/public/health")
    token = client.cookies["tl_csrf"]
    r = client.post(
        base(world) + "/bookmark",
        data={"csrf_token": token, "value": "1"},
        follow_redirects=False,
    )
    assert r.status_code in (303, 401)  # browsers are sent to the login page
    if r.status_code == 303:
        assert "/login" in r.headers["location"]
    login_as(client, login, world, Role.MEMBER)
    html = page(client, world, "actions")
    r = client.post(base(world, "ghost") + "/bookmark", data={"csrf_token": "x"})
    assert r.status_code == 403  # csrf first; a bad token never reaches the handler
    from tests.helpers_web import form_token

    token = form_token(html, base(world) + "/bookmark")
    r = client.post(base(world, "ghost") + "/bookmark", data={"csrf_token": token, "value": "1"})
    assert r.status_code == 404


def test_other_projects_traces_are_not_reachable(
    client, login, world, trace, make_org, make_project, make_user, make_membership
):
    outsider_org = make_org("Elsewhere")
    foreign = make_project(outsider_org, "Foreign")
    login_as(client, login, world, Role.OWNER)
    assert client.get(f"/project/{foreign.id}/traces/t1/actions").status_code == 404
    # and a trace id from another project does not resolve inside this one
    assert client.get(base(world, "other-project-trace") + "/actions").status_code == 404


def test_score_chips_macro_for_the_list(client):
    from tests.helpers_web import score
    from traceloom.web.templating import templates

    template = templates.env.from_string(
        '{% import "traces/_scores.html" as s with context %}{{ s.chips(scores) }}'
    )
    out = template.render(
        scores=[score("t", "quality", 0.8), score("t", "ok", 1.0, data_type="BOOLEAN"),
                score("t", "tone", None, data_type="CATEGORICAL", string_value="<b>x</b>")],
        csrf_token="",
    )  # fmt: skip
    assert "<b>quality</b><span>0.8</span>" in out and "<b>ok</b><span>true</span>" in out
    assert "&lt;b&gt;x&lt;/b&gt;" in out


def test_detail_page_slots_load_the_fragments_lazily(world, trace):
    """The partials detail.html includes are small lazy-load slots (its context: trace, project)."""
    from traceloom.web.templating import templates

    ctx = {"project": world.project, "trace": type("T", (), {"id": "t1"})(), "csrf_token": ""}
    for name, div, part in (
        ("traces/_actions.html", "trace-actions", "actions"),
        ("traces/_tags.html", "trace-tags-controls", "tags"),
        ("traces/_comments.html", "trace-discussion", "discussion"),
    ):
        html = templates.env.get_template(name).render(**ctx)
        assert (
            f'id="{div}"' in html
            and f'hx-get="/project/{world.project.id}/traces/t1/{part}"' in html
        )
        assert 'hx-trigger="load"' in html
    # _scores.html only exports the chips macro, so including it in the page renders nothing
    assert templates.env.get_template("traces/_scores.html").render(**ctx).strip() == ""
