"""Comments: Markdown sanitising, @mentions that send mail, delete rights."""

from __future__ import annotations

import pytest

from tests.helpers_web import form_token, post_form, seed_trace
from traceloom.auth.permissions import Role

HX = {"HX-Request": "true"}


@pytest.fixture
def trace(container, world):
    seed_trace(container, world.project.id, "t1")
    return "t1"


def base(world):
    return f"/project/{world.project.id}/traces/t1"


def as_user(client, login, world, role):
    return login(client, world.user(role), csrf=False)


def discussion(client, world):
    r = client.get(base(world) + "/discussion")
    assert r.status_code == 200
    return r.text


def comment(client, world, body, **extra):
    html = discussion(client, world)
    return post_form(client, html, base(world) + "/comments", {"body": body, **extra}, headers=HX)


def test_member_adds_a_comment_and_it_renders(client, login, world, trace):
    as_user(client, login, world, Role.MEMBER)
    r = comment(client, world, "Looks **wrong** to me")
    assert r.status_code == 200
    assert "<strong>wrong</strong>" in r.text
    assert "No comments yet" not in r.text


def test_plain_form_post_redirects_to_the_detail_page(client, login, world, trace):
    as_user(client, login, world, Role.MEMBER)
    r = comment(client, world, "hello", **{})
    html = discussion(client, world)
    r = client.post(
        base(world) + "/comments",
        data={"csrf_token": form_token(html, base(world) + "/comments"), "body": "no htmx"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == base(world)


def test_comment_markdown_is_sanitised(client, login, world, trace):
    as_user(client, login, world, Role.MEMBER)
    body = (
        "<script>alert(1)</script> <img src=x onerror=alert(2)> "
        "[bad](javascript:alert(3)) ![pic](http://evil.test/p.png) <b onclick=1>x</b>"
    )
    r = comment(client, world, body)
    html = r.text
    assert "<script>alert" not in html and "<img src=x" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert 'href="javascript' not in html and "onerror=alert(2)>" not in html.replace("&lt;", "")
    assert "<img" not in html.split('class="md"', 1)[1]


def test_empty_and_overlong_comments_are_rejected(client, login, world, trace):
    as_user(client, login, world, Role.MEMBER)
    r = comment(client, world, "   ")
    assert r.status_code == 400 and "Write a comment first" in r.text
    r = comment(client, world, "x" * 5001)
    assert r.status_code == 400 and "limited to 5,000" in r.text
    assert "No comments yet" in discussion(client, world)


def test_mention_sends_mail(client, login, world, trace, container, make_user, make_membership):
    author = world.user(Role.MEMBER)
    other = make_user(email="dana@example.com", name="Dana Rivera")
    make_membership(other, world.org, Role.VIEWER)
    outsider = make_user(email="outsider@example.com", name="Olly Outsider")
    as_user(client, login, world, Role.MEMBER)
    container.mailer.outbox.clear()
    r = comment(client, world, "cc @Dana Rivera and @outsider@example.com and @Olly Outsider")
    assert r.status_code == 200
    sent = list(container.mailer.outbox)
    assert [m.to for m in sent] == ["dana@example.com"]  # members only, once
    assert author.name in sent[0].subject or author.email in sent[0].subject
    assert f"/project/{world.project.id}/traces/t1" in sent[0].body
    assert 'class="mention"' in r.text and outsider.email not in "".join(m.to for m in sent)


def test_mention_by_email_and_no_self_mail(client, login, world, trace, container):
    me = world.user(Role.MEMBER)
    as_user(client, login, world, Role.MEMBER)
    container.mailer.outbox.clear()
    comment(client, world, f"note to self @{me.email}")
    assert list(container.mailer.outbox) == []
    admin = world.user(Role.ADMIN)
    comment(client, world, f"ping @{admin.email}")
    assert [m.to for m in container.mailer.outbox] == [admin.email]


def _post_comment_as(client, login, world, role, body):
    as_user(client, login, world, role)
    comment(client, world, body)
    client.cookies.clear()


def _comment_id(html):
    import re

    return re.search(r"/comments/([0-9a-f]+)/delete", html).group(1)


def test_author_can_delete_own_comment(client, login, world, trace):
    as_user(client, login, world, Role.MEMBER)
    comment(client, world, "mine")
    html = discussion(client, world)
    action = f"{base(world)}/comments/{_comment_id(html)}/delete"
    r = post_form(client, html, action, {}, headers=HX)
    assert r.status_code == 200 and "No comments yet" in r.text


def test_other_member_cannot_delete_but_admin_can(
    client, login, world, trace, make_user, make_membership
):
    second = make_user(email="second@example.com", name="Second Member")
    make_membership(second, world.org, Role.MEMBER)
    _post_comment_as(client, login, world, Role.MEMBER, "by first")
    login(client, second, csrf=False)
    html = discussion(client, world)
    assert "/delete" not in html  # no delete control for someone else's comment
    cid = _comment_id(_admin_html(client, login, world))
    # direct POST by the other member is refused
    login(client, second, csrf=False)
    html = discussion(client, world)
    token = form_token(html, base(world) + "/comments")
    r = client.post(f"{base(world)}/comments/{cid}/delete", data={"csrf_token": token}, headers=HX)
    assert r.status_code == 403
    # an admin can
    login(client, world.user(Role.ADMIN), csrf=False)
    html = discussion(client, world)
    r = post_form(client, html, f"{base(world)}/comments/{cid}/delete", {}, headers=HX)
    assert r.status_code == 200 and "by first" not in r.text


def _admin_html(client, login, world):
    login(client, world.user(Role.ADMIN), csrf=False)
    return discussion(client, world)


def test_comment_on_an_observation_and_unknown_observation(client, login, world, trace):
    as_user(client, login, world, Role.MEMBER)
    r = comment(client, world, "on step", observation_id="t1-o1")
    assert r.status_code == 200 and "step1" in r.text
    r = comment(client, world, "on nothing", observation_id="ghost")
    assert r.status_code == 404


def test_comments_are_project_scoped(client, login, world, trace, make_project, container):
    other = make_project(world.org, "Other")
    seed_trace(container, other.id, "t1")
    as_user(client, login, world, Role.MEMBER)
    comment(client, world, "only here")
    r = client.get(f"/project/{other.id}/traces/t1/discussion")
    assert r.status_code == 200 and "only here" not in r.text
