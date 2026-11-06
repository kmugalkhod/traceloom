"""Save as prompt: the dialog fragment and the new-prompt / new-version modes (FR-L6)."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from traceloom.auth.permissions import Role
from traceloom.db.models import Prompt, PromptVersion
from traceloom.prompts import service
from traceloom.prompts.service import Actor

MSGS = [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "{{q}}"}]


@pytest.fixture
def member_client(client, login, world):
    member = login(client, world.users[Role.MEMBER])
    member.headers["Accept"] = "application/json"
    return member


@pytest.fixture
def existing(db, world):
    actor = Actor.for_user(world.users[Role.MEMBER], Role.MEMBER)
    service.create_version(
        db, world.project, actor, name="support/chat", type="chat",
        content=[{"role": "user", "content": "hi"}], commit_message="first",
    )  # fmt: skip
    service.create_version(
        db, world.project, actor, name="notes", type="text", content="plain", commit_message="t"
    )
    db.commit()


def save(client, world, **body):
    payload = {"messages": MSGS, "model": "gpt-4o", "params": {"temperature": 0.1}, **body}
    return client.post(f"/project/{world.project.id}/playground/save", json=payload)


def test_dialog_lists_chat_prompts_only(client, login, world, existing):
    member = login(client, world.users[Role.MEMBER])
    r = member.get(
        f"/project/{world.project.id}/playground/save-dialog", headers={"Accept": "text/html"}
    )
    assert r.status_code == 200 and 'id="save-prompt"' in r.text and "data-save-form" in r.text
    assert "support/chat (latest v1)" in r.text
    assert "notes" not in r.text  # a text prompt cannot take chat messages
    assert f'data-save-url="/project/{world.project.id}/playground/save"' in r.text


def test_dialog_needs_member(client, login, world):
    viewer = login(client, world.users[Role.VIEWER])
    r = viewer.get(f"/project/{world.project.id}/playground/save-dialog")
    assert r.status_code == 403


def test_saving_as_a_new_prompt_creates_v1(member_client, world, db):
    r = save(member_client, world, name="pg/new", commit_message="from playground", mode="new")
    assert r.status_code == 201
    assert r.json()["version"] == 1
    assert r.json()["url"] == f"/project/{world.project.id}/prompts/pg/new?version=1"
    version = db.scalars(select(PromptVersion)).one()
    assert version.commit_message == "from playground" and version.content == MSGS
    assert version.config["model"] == "gpt-4o"


def test_new_mode_refuses_a_taken_name(member_client, world, existing):
    r = save(member_client, world, name="support/chat", commit_message="x", mode="new")
    assert r.status_code == 400 and "already exists" in r.json()["error"]["message"]


def test_version_mode_adds_n_plus_one_with_the_commit_message(member_client, world, db, existing):
    r = save(
        member_client, world, name="support/chat", commit_message="tighten tone", mode="version"
    )
    assert r.status_code == 201 and r.json()["version"] == 2
    prompt = db.scalars(select(Prompt).where(Prompt.name == "support/chat")).one()
    rows = db.scalars(
        select(PromptVersion)
        .where(PromptVersion.prompt_id == prompt.id)
        .order_by(PromptVersion.version)
    ).all()
    assert [(v.version, v.commit_message) for v in rows] == [(1, "first"), (2, "tighten tone")]


def test_version_mode_needs_an_existing_prompt(member_client, world):
    r = save(member_client, world, name="missing", commit_message="x", mode="version")
    assert r.status_code == 400 and "no prompt named" in r.json()["error"]["message"]
