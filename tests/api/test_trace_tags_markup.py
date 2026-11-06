"""Trace detail tags (FR-TD2): one set of chips, inline edit for Members, none for Viewers."""

from __future__ import annotations

import re

import pytest

from tests.helpers_web import seed_trace
from traceloom.auth.permissions import Role


@pytest.fixture
def trace(container, world):
    seed_trace(container, world.project.id, "t1", tags=["alpha", "beta"])
    return "t1"


def detail(client, login, world, role):
    login(client, world.user(role), csrf=False)
    return client.get(f"/project/{world.project.id}/traces/t1").text


def tags_row(html):
    return html[html.index('id="trace-tags"') : html.index('id="trace-tags"') + 3000]


def test_no_separate_edit_tags_row_and_each_tag_once(client, login, world, trace):
    for role in (Role.MEMBER, Role.VIEWER):
        html = detail(client, login, world, role)
        assert "Edit tags:" not in html
        row = tags_row(html)
        for tag in ("alpha", "beta"):
            assert len(re.findall(rf'<span class="chip">\s*{tag}\b', row)) == 1


def test_member_sees_remove_and_inline_add(client, login, world, trace):
    row = tags_row(detail(client, login, world, Role.MEMBER))
    assert 'aria-label="Remove tag alpha"' in row and 'aria-label="Remove tag beta"' in row
    assert 'name="tag"' in row and "Add tag" in row
    assert f"/project/{world.project.id}/traces/t1/tags" in row  # the existing endpoints


def test_viewer_sees_neither_remove_nor_add(client, login, world, trace):
    row = tags_row(detail(client, login, world, Role.VIEWER))
    assert "Remove tag" not in row and "Add tag" not in row and "<form" not in row
