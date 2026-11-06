from __future__ import annotations

import re

import pytest
from sqlalchemy import select

from traceloom.auth.permissions import Role
from traceloom.db.models import AuditLog, Prompt, PromptLabel
from traceloom.prompts import service
from traceloom.prompts.service import Actor

HTML = {"Accept": "text/html"}
XSS = "<script>alert(1)</script>"


def base(world):
    return f"/project/{world.project.id}/prompts"


def token(client, url):
    page = client.get(url, headers=HTML)
    assert page.status_code == 200, page.text[:300]
    return re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)


def post_new(client, world, **fields):
    data = {"mode": "prompt", "type": "text", "commit_message": "init", **fields}
    data["csrf_token"] = token(client, f"{base(world)}/new")
    return client.post(f"{base(world)}/new", data=data, headers=HTML, follow_redirects=False)


def actor(world, role=Role.MEMBER):
    return Actor.for_user(world.users[role], role)


def save(db, world, name, content="Hello", role=Role.MEMBER, **kw):
    kw.setdefault("type", "text")
    service.create_version(
        db, world.project, actor(world, role), name=name, content=content, commit_message="m", **kw
    )
    db.commit()


@pytest.fixture
def member(client, login, world):
    return login(client, world.users[Role.MEMBER])


# --- list ----------------------------------------------------------------------------------


def test_empty_list_shows_the_sdk_snippet(member, world):
    page = member.get(base(world), headers=HTML)
    assert page.status_code == 200
    assert "No prompts yet" in page.text and "traceloom_sdk.get_prompt" in page.text
    assert "New prompt" in page.text


def test_viewer_sees_the_list_without_a_create_button(client, login, world, db):
    save(db, world, "support/a")
    login(client, world.users[Role.VIEWER])
    page = client.get(base(world), headers=HTML)
    assert page.status_code == 200 and "support" in page.text
    assert "New prompt" not in page.text and f"{base(world)}/new" not in page.text


def test_viewer_cannot_open_or_post_the_editor(client, login, world):
    login(client, world.users[Role.VIEWER])
    assert client.get(f"{base(world)}/new", headers=HTML).status_code == 403
    form = {"name": "x", "type": "text", "text": "y", "commit_message": "m"}
    assert client.post(f"{base(world)}/new", data=form, headers=HTML).status_code == 403


def test_list_rows_folders_labels_and_latest(member, world, db):
    save(db, world, "support/greeting", labels=["staging"], tags=["cs"])
    save(db, world, "support/greeting", content="Hello again")
    save(db, world, "sales-pitch", type="chat", content=[{"role": "user", "content": "x"}])
    page = member.get(base(world), headers=HTML).text
    assert "sales-pitch" in page and "greeting" in page
    assert "v2" in page and "staging" in page and "No production label" in page
    assert "Chat" in page and "Text" in page
    assert "support" in page  # folder tree


def test_list_folder_search_tag_label_filters(member, world, db):
    save(db, world, "support/greeting", content="Welcome aboard", tags=["cs"], labels=["staging"])
    save(db, world, "support/refund", content="Refund policy text", tags=["legal"])
    save(db, world, "sales-pitch", content="Buy now")
    url = base(world)
    assert "refund" in member.get(f"{url}?q=REFUND", headers=HTML).text
    only = member.get(f"{url}?q=refund", headers=HTML).text
    assert "Welcome" not in only and "greeting" not in only
    assert "Refund policy text" not in only  # the name explains this match: no snippet
    content = member.get(f"{url}?q=policy", headers=HTML).text
    assert "Refund policy text" in content and "greeting" not in content  # content match
    assert "sales-pitch" not in member.get(f"{url}?folder=support", headers=HTML).text
    tagged = member.get(f"{url}?tag=legal", headers=HTML).text
    assert "refund" in tagged and "greeting" not in tagged
    labelled = member.get(f"{url}?label=staging", headers=HTML).text
    assert "greeting" in labelled and "refund" not in labelled
    none = member.get(f"{url}?q=zzzz", headers=HTML).text
    assert "No prompts match" in none and "Clear" in none


def test_list_sort_by_name(member, world, db):
    save(db, world, "b-prompt")
    save(db, world, "a-prompt")
    page = member.get(f"{base(world)}?sort=name", headers=HTML).text
    assert page.index("a-prompt") < page.index("b-prompt")


def test_list_is_project_scoped(member, world, db, make_project):
    other = make_project(world.org)
    service.create_version(
        db, other, actor(world), name="secret-one", type="text", content="x", commit_message="m"
    )
    db.commit()
    assert "secret-one" not in member.get(base(world), headers=HTML).text


def test_list_escapes_user_content(member, world, db):
    save(db, world, "x/n", content=f"find {XSS} me", tags=[XSS])
    page = member.get(f"{base(world)}?q=find", headers=HTML).text
    assert XSS not in page and "&lt;script&gt;" in page
    assert XSS not in member.get(f"{base(world)}?q={XSS}", headers=HTML).text


# --- editor --------------------------------------------------------------------------------


def test_editor_page_renders_for_a_member(member, world):
    page = member.get(f"{base(world)}/new", headers=HTML).text
    assert "Commit message" in page and "data-prompt-form" in page
    assert "/static/prompts.js" in page and "<script>" not in page
    assert 'name="csrf_token" value="' in page
    assert 'name="label" value="production"' in page and "disabled" in page  # locked for Members


def test_member_creates_a_text_prompt(member, world, db):
    r = post_new(
        member,
        world,
        name="folder/greeting",
        text="Hello {{name}}",
        tags="cs, hello",
        cfg_model="m1",
        cfg_temperature="0.2",
        cfg_max_tokens="50",
        config='{"stop": ["x"]}',
        label="staging",
    )
    assert r.status_code == 303
    assert r.headers["location"].startswith(f"{base(world)}/folder/greeting")
    view = service.get(db, world.project, actor(world), "folder/greeting", label="staging")
    assert view.prompt == "Hello {{name}}" and view.tags == ["cs", "hello"]
    assert view.commit_message == "init"
    assert view.config == {"stop": ["x"], "model": "m1", "temperature": 0.2, "max_tokens": 50}
    assert db.scalar(select(AuditLog).where(AuditLog.action == "prompt.created")) is not None


def test_member_creates_a_chat_prompt(member, world, db):
    r = post_new(
        member,
        world,
        name="chatty",
        type="chat",
        role=["system", "user", "placeholder"],
        msg=["Be {{tone}}", "{{q}}", "history"],
    )
    assert r.status_code == 303
    view = service.get(db, world.project, actor(world), "chatty")
    assert view.type == "chat"
    assert [(m["role"], m["content"]) for m in view.prompt] == [
        ("system", "Be {{tone}}"),
        ("user", "{{q}}"),
        ("placeholder", "history"),
    ]


def test_validation_errors_are_inline_and_keep_the_input(member, world, db):
    r = post_new(member, world, name="bad name", text="keep me", commit_message=" ")
    assert r.status_code == 400
    assert "Prompt names use letters" in r.text
    assert "keep me" in r.text
    r2 = post_new(member, world, name="ok", text="x", commit_message="")
    assert r2.status_code == 400 and "A commit message is required" in r2.text
    r3 = post_new(member, world, name="ok", text="x", config="{nope")
    assert r3.status_code == 400 and "valid JSON" in r3.text
    r4 = post_new(member, world, name="ok", text="x", config="[1]")
    assert r4.status_code == 400 and "JSON object" in r4.text
    r5 = post_new(member, world, name="ok", text="x", cfg_temperature="hot")
    assert r5.status_code == 400
    assert db.scalar(select(Prompt)) is None


def test_member_cannot_set_production_while_saving(member, world, db):
    r = post_new(member, world, name="p", text="x", label="production")
    assert r.status_code == 403
    assert db.scalar(select(Prompt)) is None


def test_admin_can_set_production_on_save(client, login, world, db):
    login(client, world.users[Role.ADMIN])
    page = client.get(f"{base(world)}/new", headers=HTML).text
    assert 'value="production"' in page and 'value="production" disabled' not in page
    r = post_new(client, world, name="p", text="x", label="production")
    assert r.status_code == 303
    assert db.scalar(select(PromptLabel.label)) == "production"


def test_new_version_editor_prefills_from_the_latest(member, world, db):
    save(db, world, "support/hi", content="v1 text {{a}}", tags=["t"])
    save(db, world, "support/hi", content="v2 text {{b}}", config={"model": "m", "x": 1})
    page = member.get(f"{base(world)}/new?from=support/hi", headers=HTML).text
    assert "New version of support/hi" in page and "v2 text" in page and "v1 text" not in page
    assert 'name="cfg_model" value="m"' in page and "Starts from v2" in page
    older = member.get(f"{base(world)}/new?from=support/hi&version=1", headers=HTML).text
    assert "v1 text" in older
    assert member.get(f"{base(world)}/new?from=ghost", headers=HTML).status_code == 404


def test_saving_a_new_version(member, world, db):
    save(db, world, "support/hi", content="one")
    r = post_new(
        member, world, mode="version", name="support/hi", text="two", commit_message="tweak"
    )
    assert r.status_code == 303 and r.headers["location"].endswith("?version=2")
    assert service.get(db, world.project, actor(world), "support/hi", version=2).prompt == "two"


def test_script_in_content_renders_as_text_in_the_editor(member, world, db):
    save(db, world, "x", content=f"{XSS} and {{{{v}}}}", tags=["ok"])
    page = member.get(f"{base(world)}/new?from=x", headers=HTML).text
    assert XSS not in page and "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    bad = post_new(member, world, name="y z", text=XSS)
    assert XSS not in bad.text and "&lt;script&gt;" in bad.text


def test_forms_without_a_valid_token_are_rejected(client, login, world):
    login(client, world.users[Role.MEMBER], csrf=False)
    r = client.post(f"{base(world)}/new", data={"name": "x"}, headers=HTML)
    assert r.status_code == 403
