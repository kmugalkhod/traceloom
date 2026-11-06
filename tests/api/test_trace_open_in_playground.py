"""Trace detail: "Open in playground" (Member+) and the prompt a generation was linked to."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from traceloom.auth.permissions import Role
from traceloom.ports import ObservationRow, TraceRow

H = {"Accept": "text/html"}
T0 = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)


@pytest.fixture
def seeded(container, world):
    trace = TraceRow(id="tr1", timestamp=T0, event_ts=T0, name="chat")
    obs = [
        ObservationRow(id="root", trace_id="tr1", name="chat", type="span", start_time=T0,
                       end_time=T0 + timedelta(seconds=2), event_ts=T0),
        ObservationRow(id="gen1", trace_id="tr1", name="answer", type="generation",
                       parent_observation_id="root", start_time=T0, event_ts=T0,
                       end_time=T0 + timedelta(seconds=1), model="claude-haiku-4-5",
                       prompt_name="support/<b>x", prompt_version=2),
        ObservationRow(id="gen2", trace_id="tr1", name="plain", type="generation",
                       parent_observation_id="root", start_time=T0, event_ts=T0,
                       end_time=T0 + timedelta(seconds=1)),
    ]  # fmt: skip
    container.trace_store.upsert(world.project.id, [trace], obs, [])
    container.trace_store.refresh_trace_aggregates(world.project.id, ["tr1"])


def page(client, world, obs=None, tail=""):
    suffix = f"?obs={obs}" if obs else ""
    return client.get(f"/project/{world.project.id}/traces/tr1{tail}{suffix}", headers=H)


def pg_link(world, obs):
    return f"/project/{world.project.id}/playground?trace=tr1&amp;obs={obs}"


def test_member_sees_open_in_playground_on_generations_only(client, login, world, seeded):
    member = login(client, world.users[Role.MEMBER])
    html = page(member, world, "gen1").text
    assert html.count("data-open-playground") == 3  # row for gen1, row for gen2, pane header
    assert f'href="{pg_link(world, "gen1")}"' in html and "Open in playground" in html
    assert pg_link(world, "root") not in html  # a span has nothing to reopen
    root = page(member, world, "root").text
    assert 'class="btn btn-sm" href' not in root.split('id="io-pane"')[1]  # no header button


def test_io_partial_carries_the_button_too(client, login, world, seeded):
    member = login(client, world.users[Role.MEMBER])
    r = member.get(f"/project/{world.project.id}/traces/tr1/io?obs=gen1", headers=H)
    assert "data-open-playground" in r.text and f'href="{pg_link(world, "gen1")}"' in r.text


def test_viewer_has_no_button(client, login, world, seeded):
    viewer = login(client, world.users[Role.VIEWER])
    html = page(viewer, world, "gen1").text
    assert "data-open-playground" not in html and "Open in playground" not in html
    assert page(viewer, world, "gen1", "/io").status_code in (200, 404)


def test_prompt_link_is_shown_escaped(client, login, world, seeded):
    viewer = login(client, world.users[Role.VIEWER])
    html = page(viewer, world, "gen1").text
    assert "prompt: support/&lt;b&gt;x v2" in html and "<b>x" not in html
    assert f'href="/project/{world.project.id}/prompts/support/%3Cb%3Ex?version=2"' in html
    unlinked = page(viewer, world, "gen2").text
    assert "data-prompt-link" not in unlinked.split('id="io-pane"')[1]
