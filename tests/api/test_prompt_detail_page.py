from __future__ import annotations

import re

import pytest
from sqlalchemy import select

from traceloom.auth.permissions import Role
from traceloom.db.models import AuditLog, Prompt, PromptLabel, PromptVersion
from traceloom.prompts import service
from traceloom.prompts.service import Actor

HTML = {"Accept": "text/html"}
XSS = "<script>alert(1)</script>"


def url(world, name="support", suffix=""):
    return f"/project/{world.project.id}/prompts/{name}{suffix}"


def actor(world, role=Role.MEMBER):
    return Actor.for_user(world.users[role], role)


def save(db, world, name="support", content="Hello {{name}}", role=Role.ADMIN, **kw):
    kw.setdefault("type", "text")
    kw.setdefault("commit_message", "m")
    service.create_version(db, world.project, actor(world, role), name=name, content=content, **kw)
    db.commit()


def csrf(client, page_url):
    page = client.get(page_url, headers=HTML)
    return re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)


def set_label(client, world, label, version, name="support", **extra):
    token = csrf(client, url(world, name))
    data = {"label": label, "version": str(version), "csrf_token": token, **extra}
    return client.post(url(world, name, "/labels"), data=data, headers=HTML, follow_redirects=False)


@pytest.fixture
def three(db, world):
    save(db, world, content="one {{a}}", commit_message="first", config={"model": "m1"})
    save(
        db,
        world,
        content="two {{a}}\nsecond line",
        commit_message="second",
        config={"model": "m2", "temperature": 0.2},
    )
    save(db, world, content="three {{a}}", commit_message="third")


@pytest.fixture
def admin(client, login, world):
    return login(client, world.users[Role.ADMIN])


@pytest.fixture
def member(client, login, world):
    return login(client, world.users[Role.MEMBER])


# --- versions tab -------------------------------------------------------------------------


def test_detail_lists_versions_and_shows_the_selected_one(admin, world, three):
    page = admin.get(url(world), headers=HTML).text
    assert "support" in page and "3 versions" in page
    for text in ("first", "second", "third"):
        assert text in page
    assert "Version 3" in page and "three" in page  # latest selected by default
    assert (
        'class="rv" aria-current="true"' in page.replace("  ", " ") or 'aria-current="true"' in page
    )
    v1 = admin.get(url(world, suffix="?version=1"), headers=HTML).text
    assert "Version 1" in v1 and "m1" in v1  # the config line


def test_variables_are_highlighted(admin, world, three):
    assert '<span class="var">{{a}}</span>' in admin.get(url(world), headers=HTML).text


def test_labels_and_latest_pills(admin, world, three):
    set_label(admin, world, "production", 2)
    set_label(admin, world, "staging", 3)
    page = admin.get(url(world), headers=HTML).text
    assert 'class="lab prod"' in page and 'class="lab latest"' in page and ">staging<" in page


def test_unknown_prompt_and_version_are_404(admin, world, three, make_project):
    assert admin.get(url(world, "ghost"), headers=HTML).status_code == 404
    assert admin.get(url(world, suffix="?version=9"), headers=HTML).status_code == 404


def test_other_projects_prompt_is_404(client, login, world, db, make_project, make_membership):
    other = make_project(world.org)
    service.create_version(
        db,
        other,
        actor(world, Role.ADMIN),
        name="theirs",
        type="text",
        content="x",
        commit_message="m",
    )
    db.commit()
    login(client, world.users[Role.ADMIN])
    assert client.get(url(world, "theirs"), headers=HTML).status_code == 404


def test_names_with_slashes(admin, world, db):
    save(db, world, name="support/tone/formal", content="Be formal")
    page = admin.get(url(world, "support/tone/formal"), headers=HTML)
    assert page.status_code == 200 and "support/tone/formal" in page.text


def test_script_in_content_and_commit_message_is_text(admin, world, db):
    save(db, world, name="x", content=f"{XSS} {{{{v}}}}", commit_message=XSS, tags=[XSS])
    for suffix in ("", "?tab=compare", "?tab=preview&v_v=" + XSS):
        page = admin.get(url(world, "x", suffix), headers=HTML).text
        assert XSS not in page, suffix
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in admin.get(url(world, "x"), headers=HTML).text


# --- permissions and label moves ------------------------------------------------------------


def test_viewer_sees_no_actions(client, login, world, three):
    login(client, world.users[Role.VIEWER])
    page = client.get(url(world), headers=HTML).text
    assert "Set production" not in page and "New version" not in page
    assert "Delete prompt" not in page and "/labels" not in page
    assert "Copy SDK snippet" in page


def test_member_production_is_disabled_with_a_reason_and_the_server_refuses(
    member, world, three, db
):
    page = member.get(url(world), headers=HTML).text
    assert 'aria-disabled="true"' in page and "Only Admins can move production" in page
    assert f'action="{url(world, suffix="/labels")}"' in page  # staging control exists
    r = set_label(member, world, "production", 2)
    assert r.status_code == 403
    assert db.scalar(select(PromptLabel)) is None
    assert db.scalars(select(AuditLog).where(AuditLog.action == "prompt.label_moved")).all() == []


def test_member_moves_staging_and_it_is_audited(member, world, three, db):
    r = set_label(member, world, "staging", 2)
    assert r.status_code == 303 and "moved=staging" in r.headers["location"]
    assert db.scalar(select(PromptLabel.version).where(PromptLabel.label == "staging")) == 2
    (row,) = db.scalars(select(AuditLog).where(AuditLog.action == "prompt.label_moved")).all()
    assert row.detail["label"] == "staging" and row.actor_id == world.users[Role.MEMBER].id


def test_admin_moves_production_in_one_click_and_history_shows_it(admin, world, three, db):
    page = admin.get(url(world, suffix="?version=2"), headers=HTML).text
    assert (
        "Set production" in page and "aria-disabled" not in page.split("Set production")[0][-200:]
    )
    r = set_label(admin, world, "production", 2)
    assert r.status_code == 303
    after = admin.get(r.headers["location"], headers=HTML).text
    assert "In production" in after
    assert "Label history" in after and "production" in after and "now points at v2" in after
    (audit,) = db.scalars(select(AuditLog).where(AuditLog.action == "prompt.label_moved")).all()
    assert audit.detail == {
        "prompt": "support",
        "label": "production",
        "from_version": None,
        "to_version": 2,
    }


def test_toast_undo_restores_the_previous_version(admin, world, three, db):
    set_label(admin, world, "production", 1)
    r = set_label(admin, world, "production", 3)
    page = admin.get(r.headers["location"], headers=HTML).text
    assert "Undo" in page and 'name="version" value="1"' in page
    undo = set_label(admin, world, "production", 1)
    assert undo.status_code == 303
    assert db.scalar(select(PromptLabel.version).where(PromptLabel.label == "production")) == 1


def test_first_move_undo_removes_the_label(admin, world, three, db):
    r = set_label(admin, world, "staging", 2)
    page = admin.get(r.headers["location"], headers=HTML).text
    assert 'name="op" value="remove"' in page
    out = set_label(admin, world, "staging", 2, op="remove")
    assert out.status_code == 303
    assert db.scalar(select(PromptLabel)) is None


def test_invalid_label_comes_back_as_a_banner(admin, world, three):
    r = set_label(admin, world, "Not Valid!", 1)
    assert r.status_code == 303 and "err=" in r.headers["location"]
    page = admin.get(r.headers["location"], headers=HTML).text
    assert "Labels use lowercase" in page


def test_viewer_cannot_post_label_moves(client, login, world, three):
    login(client, world.users[Role.VIEWER])
    token = csrf(client, url(world))
    r = client.post(
        url(world, suffix="/labels"),
        data={"label": "staging", "version": "1", "csrf_token": token},
        headers=HTML,
    )
    assert r.status_code == 403


def test_delete_is_admin_only(client, login, world, three, db):
    login(client, world.users[Role.MEMBER])
    token = csrf(client, url(world))
    r = client.post(url(world, suffix="/delete"), data={"csrf_token": token}, headers=HTML)
    assert r.status_code == 403 and db.scalar(select(Prompt)) is not None
    login(client, world.users[Role.ADMIN])
    token = csrf(client, url(world))
    r = client.post(
        url(world, suffix="/delete"),
        data={"csrf_token": token},
        headers=HTML,
        follow_redirects=False,
    )
    assert r.status_code == 303 and db.scalar(select(Prompt)) is None


# --- compare ----------------------------------------------------------------------------------


def test_any_two_versions_diff(admin, world, three):
    page = admin.get(url(world, suffix="?tab=compare&from=1&to=3"), headers=HTML).text
    assert 'class="dl del"' in page and 'class="dl add"' in page
    assert "one {{a}}" in page and "three {{a}}" in page
    assert "1 config key" in page or "config key" in page
    assert "model" in page and "m1" in page


def test_compare_defaults_to_latest_against_previous(admin, world, three):
    page = admin.get(url(world, suffix="?tab=compare"), headers=HTML).text
    assert "two {{a}}" in page and "three {{a}}" in page


def test_chat_compare_goes_message_by_message(admin, world, db):
    one = [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "{{q}}"}]
    two = [
        {"role": "system", "content": "Be brief."},
        {"role": "user", "content": f"Q: {{{{q}}}} {XSS}"},
    ]
    save(db, world, name="chatty", type="chat", content=one)
    save(db, world, name="chatty", type="chat", content=two)
    page = admin.get(url(world, "chatty", "?tab=compare&from=1&to=2"), headers=HTML).text
    assert "Message 1 · System" in page and "Message 2 · User" in page
    assert "unchanged" in page and "changed" in page
    assert XSS not in page and "&lt;script&gt;" in page


def test_tabs_swap_as_fragments_for_htmx(admin, world, three):
    full = admin.get(url(world, suffix="?tab=compare"), headers=HTML).text
    part = admin.get(url(world, suffix="?tab=compare"), headers={**HTML, "HX-Request": "true"}).text
    assert "<html" in full and "<html" not in part and "<header" not in part
    assert "Config" in part and "cmp-from" in part


# --- preview ----------------------------------------------------------------------------------


def test_preview_shows_included_prompts_and_fills_variables(admin, world, db):
    save(db, world, name="shared/tone", content="Write plainly.", labels=["production"])
    save(
        db,
        world,
        name="main",
        content="Hi {{name}}.\n@@@prompt:shared/tone|label=production@@@\nBye {{name}}",
    )
    page = admin.get(url(world, "main", "?tab=preview&v_name=Ann"), headers=HTML).text
    assert "Hi Ann." in page and "Bye Ann" in page
    assert "Included from shared/tone" in page and "Write plainly." in page
    assert "All variables filled" in page and "shared/tone" in page and "v1" in page


def test_preview_reports_empty_variables(admin, world, three):
    page = admin.get(url(world, suffix="?tab=preview"), headers=HTML).text
    assert "1 left empty" in page and '<span class="var">{{a}}</span>' in page


def test_preview_error_for_a_loop_in_old_data(admin, world, db):
    save(db, world, name="a", content="A", labels=["x"])
    save(db, world, name="b", content="B @@@prompt:a|label=x@@@", labels=["x"])
    prompt = db.scalar(select(Prompt).where(Prompt.name == "a"))
    db.add(
        PromptVersion(
            project_id=world.project.id,
            prompt_id=prompt.id,
            version=2,
            content="A @@@prompt:b|label=x@@@",
            commit_message="legacy",
        )
    )
    db.get(PromptLabel, (prompt.id, "x")).version = 2
    db.commit()
    page = admin.get(url(world, "a", "?tab=preview&version=2"), headers=HTML)
    assert page.status_code == 200 and "Circular prompt reference" in page.text


def test_preview_chat_placeholder(admin, world, db):
    save(
        db,
        world,
        name="c",
        type="chat",
        content=[
            {"role": "system", "content": "S {{x}}"},
            {"role": "placeholder", "content": "history"},
        ],
    )
    page = admin.get(url(world, "c", "?tab=preview&v_x=1"), headers=HTML).text
    assert "S 1" in page and "history" in page


# --- metrics container, snippet and menu -----------------------------------------------------


def test_metrics_tab_loads_from_the_metrics_partial_url(admin, world, three):
    page = admin.get(url(world, suffix="?tab=metrics&version=2"), headers=HTML).text
    assert (
        f'hx-get="{url(world, suffix="/metrics")}?version=2"' in page
        or f"{url(world, suffix='/metrics')}?version=2" in page
    )
    assert 'hx-trigger="load"' in page


def test_open_in_playground_and_sdk_snippet(admin, client, world, three):
    page = admin.get(url(world), headers=HTML).text
    assert f"/project/{world.project.id}/playground?prompt=support" in page
    assert "get_prompt(&#34;support&#34;" in page or "get_prompt(" in page
    assert "Copy SDK snippet" in page


def test_new_version_link_starts_from_the_selected_version(admin, world, three):
    page = admin.get(url(world, suffix="?version=2"), headers=HTML).text
    assert f"/project/{world.project.id}/prompts/new?from=support&amp;version=2" in page
