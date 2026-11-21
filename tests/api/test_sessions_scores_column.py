"""Scores column and column picker on the sessions list (T-10)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tests.helpers_web import post_form, seed_trace
from traceloom.auth.permissions import Role
from traceloom.ports import ScoreRow

NOW = datetime.now(UTC)


@pytest.fixture
def scored_session(container, world):
    seed_trace(
        container, world.project.id, "t1", session_id="sess-9", timestamp=NOW,
    )  # fmt: skip
    container.trace_store.add_score(
        world.project.id,
        ScoreRow(id="s2", session_id="sess-9", name="<b>quality</b>", value=0.5, timestamp=NOW),
    )


def page(client, login, world, role=Role.VIEWER):
    login(client, world.user(role), csrf=False)
    return client.get(f"/project/{world.project.id}/sessions")


def test_sessions_list_has_scores_column_and_picker(client, login, world, scored_session):
    html = page(client, login, world).text
    assert '<th class="c">Scores</th>' in html
    assert 'name="scope" value="sessions"' in html and 'name="col" value="scores"' in html
    assert "&lt;b&gt;quality&lt;/b&gt;" in html and "<b>quality</b>" not in html


def test_sessions_columns_preference_hides_columns(client, login, world, scored_session):
    login(client, world.user(Role.MEMBER))
    base = f"/project/{world.project.id}"
    html = client.get(f"{base}/sessions").text
    r = post_form(
        client, html, f"{base}/prefs/columns",
        {"scope": "sessions", "col": ["traces", "cost"], "next": f"{base}/sessions"},
    )  # fmt: skip
    assert r.status_code == 303 and r.headers["location"] == f"{base}/sessions"
    html = client.get(f"{base}/sessions").text
    assert '<th class="c">Scores</th>' not in html and ">Users</th>" not in html
    assert ">Cost</a></th>" in html
