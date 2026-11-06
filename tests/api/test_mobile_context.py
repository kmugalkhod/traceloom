"""The mobile context row (environment + date range) comes from the shared shell on every page
that filters by range, not from each page."""

from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest

from tests.helpers_web import seed_trace
from traceloom.auth.permissions import Role

PAGES = ["traces", "sessions", "users", "dashboard"]


@pytest.fixture
def data(container, world):
    now = datetime.now(UTC)
    for env in ("prod", "dev"):
        seed_trace(container, world.project.id, f"t-{env}", environment=env, session_id=f"s-{env}",
                   user_id=f"u-{env}", timestamp=now)  # fmt: skip


def row(html: str) -> str:
    match = re.search(r'<div class="ctx-m row-wrap">(.*?)\n</div>', html, re.S)
    assert match, "no mobile context row"
    return match.group(1)


@pytest.mark.parametrize("page", PAGES)
def test_each_page_renders_the_mobile_env_and_range_controls(client, login, world, data, page):
    login(client, world.user(Role.VIEWER), csrf=False)
    html = client.get(f"/project/{world.project.id}/{page}?env=prod").text
    mobile = row(html)
    assert 'aria-label="Environment"' in mobile and 'name="env"' in mobile
    assert '<option value="prod" selected>' in mobile and '<option value="dev">' in mobile
    assert f'action="/project/{world.project.id}/prefs/env"' in mobile
    for key in ("1h", "24h", "7d", "30d"):
        assert re.search(
            rf'<a href="[^"]*range={key}[^"]*" aria-pressed="(true|false)">{key}</a>', mobile
        ), key
    assert mobile.count('aria-pressed="true"') == 1  # the current range is marked
    assert html.count('class="ctx-m') == 1  # one row, from the shell


@pytest.mark.parametrize("page", ["sessions", "users", "dashboard"])
def test_range_links_keep_the_other_filters(client, login, world, data, page):
    login(client, world.user(Role.VIEWER), csrf=False)
    mobile = row(client.get(f"/project/{world.project.id}/{page}?env=prod&range=30d").text)
    assert "env=prod&amp;range=7d" in mobile and 'aria-pressed="true">30d' in mobile


def test_pages_without_a_range_have_no_mobile_row(client, login, world, data):
    login(client, world.user(Role.VIEWER), csrf=False)
    for path in ("/traces/t-prod", "/sessions/s-prod", "/users/u-prod", "/settings"):
        assert 'class="ctx-m' not in client.get(f"/project/{world.project.id}{path}").text, path
