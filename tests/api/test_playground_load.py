"""The Load prompt dialog fragment: chat prompts of this project only, Member+."""

from __future__ import annotations

import pytest

from traceloom.auth.permissions import Role
from traceloom.prompts import service
from traceloom.prompts.service import Actor


def url(project):
    return f"/project/{project.id}/playground/load-dialog"


@pytest.fixture
def seeded(db, world, make_project):
    actor = Actor.for_user(world.users[Role.MEMBER], Role.MEMBER)
    chat = [{"role": "user", "content": "hi"}]
    for text in ("one", "two"):
        service.create_version(
            db,
            world.project,
            actor,
            name="support/chat",
            type="chat",
            content=chat,
            commit_message=text,
        )
    service.create_version(
        db, world.project, actor, name="notes", type="text", content="plain", commit_message="t"
    )
    other = make_project(world.org)
    service.create_version(
        db, other, actor, name="elsewhere", type="chat", content=chat, commit_message="x"
    )
    db.commit()


def test_lists_this_projects_chat_prompts_with_their_latest_version(client, login, world, seeded):
    member = login(client, world.users[Role.MEMBER])
    html = member.get(url(world.project), headers={"Accept": "text/html"}).text
    assert 'id="load-prompt"' in html and "data-load-form" in html
    assert 'value="support/chat" data-latest="2"' in html and "support/chat (latest v2)" in html
    assert "notes" not in html and "elsewhere" not in html
    assert f'data-playground-url="/project/{world.project.id}/playground"' in html


def test_empty_project_says_so(client, login, world):
    member = login(client, world.users[Role.MEMBER])
    html = member.get(url(world.project), headers={"Accept": "text/html"}).text
    assert (
        "no chat prompts yet" in html and "data-load-form" in html and ">Load</button>" not in html
    )


def test_viewers_cannot_load(client, login, world):
    viewer = login(client, world.users[Role.VIEWER])
    assert viewer.get(url(world.project)).status_code == 403
