"""AC-4: user content is literal text in every IO mode, in the list and in the detail views."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest

from traceloom.auth.permissions import Role
from traceloom.ports import ObservationRow, TraceRow

H = {"Accept": "text/html"}
SCRIPT = "<script>alert(1)</script>"
IMG = "<img src=x onerror=alert(1)>"
T0 = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)


@pytest.fixture
def evil(container, world, login, client):
    ts = T0
    trace = TraceRow(
        id="x1",
        timestamp=ts,
        event_ts=ts,
        name=SCRIPT,
        user_id=SCRIPT,
        tags=[SCRIPT],
        input=SCRIPT,
        output=f"{SCRIPT}\n\n{IMG}\n\n[click](javascript:alert(1)) <b>bold</b>",
        metadata={"k": SCRIPT},
        release=SCRIPT,
    )
    obs = [
        ObservationRow(
            id="o1",
            trace_id="x1",
            name=SCRIPT,
            type="generation",
            start_time=ts,
            end_time=ts + timedelta(seconds=1),
            event_ts=ts,
            model=SCRIPT,
            status_message=SCRIPT,
            level="ERROR",
            input=[{"role": "user", "content": SCRIPT}, {"role": SCRIPT, "content": IMG}],
            output={"text": SCRIPT},
            metadata={"note": SCRIPT, SCRIPT: "key"},
            model_parameters={SCRIPT: SCRIPT},
            usage_details={SCRIPT: 1},
        ),
    ]
    container.trace_store.upsert(world.project.id, [trace], obs, [])
    container.trace_store.refresh_trace_aggregates(world.project.id, ["x1"])
    login(client, world.user(Role.VIEWER))
    return world.project.id


def literal_only(html: str) -> None:
    assert SCRIPT not in html
    assert IMG not in html
    assert not re.search(r"<script(?![^>]*\bsrc=)", html, flags=re.I), "an inline script element"
    assert "onerror=" not in re.sub(r"&lt;.*?&gt;", "", html)  # only inside escaped text
    assert 'href="javascript:' not in html.lower()


@pytest.mark.parametrize("mode", ["formatted", "json", "markdown"])
@pytest.mark.parametrize("selection", ["", "o1"])
def test_script_escaped_in_every_io_mode_on_the_page(client, evil, world, mode, selection):
    query = "&".join(p for p in (f"mode={mode}", f"obs={selection}" if selection else "") if p)
    html = client.get(f"/project/{evil}/traces/x1?{query}", headers=H).text
    literal_only(html)
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html  # shown as text


@pytest.mark.parametrize("mode", ["formatted", "json", "markdown"])
@pytest.mark.parametrize("selection", ["", "o1"])
def test_script_escaped_in_every_io_mode_in_the_partial(client, evil, mode, selection):
    query = "&".join(p for p in (f"mode={mode}", f"obs={selection}" if selection else "") if p)
    html = client.get(f"/project/{evil}/traces/x1/io?{query}", headers=H).text
    literal_only(html)
    assert "&lt;script&gt;" in html


def test_script_escaped_in_the_list(client, evil):
    for query in ("", "q=alert", "sort=cost.asc", "tab=errors"):
        html = client.get(
            f"/project/{evil}/traces?{query}&range=30d".replace("?&", "?"), headers=H
        ).text
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html or "No traces match" in html
        literal_only(html)


def test_markdown_never_renders_raw_html_or_javascript_links(client, evil):
    html = client.get(f"/project/{evil}/traces/x1?mode=markdown", headers=H).text
    pane = html.split('id="io-pane"')[1]
    assert "<b>bold</b>" not in pane and "<img" not in pane.replace('<img class="', "")
    assert "javascript:" not in re.sub(r"&lt;.*?&gt;", "", pane).lower().replace(
        "[click](javascript:alert(1))", ""
    )


def test_script_in_status_message_model_and_metadata_keys_is_escaped(client, evil):
    html = client.get(f"/project/{evil}/traces/x1/io?obs=o1", headers=H).text
    assert (
        html.count("&lt;script&gt;alert(1)&lt;/script&gt;") >= 4
    )  # status, model, params, usage, metadata
    literal_only(html)
