"""UI polish pass, dashboard (AC-7, AC-8): equal stat cards, an equal-height first chart row,
a top-users table whose rank sits on the panel edge, and breakdown bars on one x."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tests.e2e.live_server import browser_page, live_server, make_world, sign_in  # noqa: F401
from tests.factories import DEFAULT_PASSWORD
from traceloom.ports import ObservationRow, TraceRow

pytestmark = pytest.mark.e2e

SEED = [
    ("alice", "model-a", 0.50, "DEFAULT"),
    ("bob", "model-b", 0.20, "ERROR"),
    ("alice", "model-a", 0.30, "DEFAULT"),
    ("carol", "model-c", 0.05, "WARNING"),
]

BOXES_JS = """
(sel) => [...document.querySelectorAll(sel)].map((e) => {
  const r = e.getBoundingClientRect();
  return { x: r.x, y: r.y, w: r.width, h: r.height, r: r.right, b: r.bottom };
})
"""


def seed(container, project_id):
    now = datetime.now(UTC)
    for i, (user, model, cost, level) in enumerate(SEED):
        start = now - timedelta(hours=i + 1)
        trace = TraceRow(id=f"t{i}", timestamp=start, name="chat", user_id=user, event_ts=start)
        obs = ObservationRow(
            id=f"t{i}-o", trace_id=f"t{i}", start_time=start, end_time=start + timedelta(seconds=2),
            type="generation", model=model, usage_details={"total": 1000 * (i + 1)},
            cost_details={"total": cost}, level=level, event_ts=start,
        )  # fmt: skip
        container.trace_store.upsert(project_id, [trace], [obs], [])
        container.trace_store.refresh_trace_aggregates(project_id, [f"t{i}"])


def open_dashboard(live_server, page, scheme):  # noqa: F811
    world = make_world(live_server)
    seed(live_server.container, world["project_id"])
    sign_in(page, live_server, world["viewer"], DEFAULT_PASSWORD)
    page.set_viewport_size({"width": 1440, "height": 900})
    page.emulate_media(color_scheme=scheme)
    page.goto(f"{live_server.url}/project/{world['project_id']}/dashboard")
    page.locator(".dash-stats .stat").first.wait_for()
    page.locator(".chart-box canvas").first.wait_for()
    page.evaluate("document.fonts.ready")
    return page


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_stat_cards_and_chart_row(live_server, browser_page, scheme):  # noqa: F811
    page = open_dashboard(live_server, browser_page, scheme)
    cards = page.evaluate(BOXES_JS, ".dash-stats .stat")
    assert len(cards) == 4
    assert all(abs(c["h"] - 112) <= 1 for c in cards), cards
    assert page.locator(".dash-stats .stat .chipi svg").count() == 4
    chip = page.evaluate(BOXES_JS, ".dash-stats .stat .chipi")
    for card, c in zip(cards, chip, strict=True):  # chip sits top-right inside its card
        assert c["r"] <= card["r"] and c["y"] - card["y"] <= 24 and card["r"] - c["r"] <= 24
    value = page.locator(".dash-stats .stat b").first
    assert "Inter" in value.evaluate("e => getComputedStyle(e).fontFamily")
    assert value.evaluate("e => getComputedStyle(e).fontVariantNumeric") == "tabular-nums"
    assert value.evaluate("e => getComputedStyle(e).fontSize") == "28px"

    over, levels = page.evaluate(
        BOXES_JS, ".dash-grid > .panel[aria-label='Traces over time'], "
        ".dash-grid > .panel[aria-label='Error rate by level']"
    )  # fmt: skip
    assert abs(over["h"] - levels["h"]) <= 1, (over, levels)
    assert abs(over["y"] - levels["y"]) <= 1
    assert abs(levels["x"] - over["r"] - 24) <= 1  # 24px gap between the 8 and the 4
    foot = page.evaluate(BOXES_JS, ".lvl-foot")[0]
    assert levels["b"] - foot["b"] <= 20  # the total footnote is pinned to the panel bottom
    assert [p for p in page.problems if p.startswith(("pageerror", "dialog"))] == []
    assert page.problems == []


def test_top_users_rank_alignment(live_server, browser_page):  # noqa: F811
    page = open_dashboard(live_server, browser_page, "light")
    panel = page.evaluate(BOXES_JS, ".dash-users")[0]
    rank = page.evaluate(BOXES_JS, ".dash-users tbody tr:first-child td.rank")[0]
    assert rank["x"] - panel["x"] <= 20, (panel, rank)  # within 20px of the panel's left edge
    head = page.locator(".dash-users th", has_text="Cost")
    assert head.evaluate("e => getComputedStyle(e).textAlign") == "right"
    cost = page.locator(".dash-users tbody td.num").first
    assert cost.evaluate("e => getComputedStyle(e).textAlign") == "right"
    assert cost.evaluate("e => getComputedStyle(e).fontVariantNumeric") == "tabular-nums"
    right = page.evaluate(BOXES_JS, ".dash-users th:last-child, .dash-users td:last-child")
    assert len({round(b["r"]) for b in right}) == 1  # header and cells share a right edge
    rows = page.evaluate(BOXES_JS, ".dash-users tbody tr")
    assert all(abs(r["h"] - 44) <= 1 for r in rows), rows

    bars = page.evaluate(BOXES_JS, "#bd-cost .hbar .trk")
    assert len(bars) >= 2 and len({round(b["x"]) for b in bars}) == 1  # one start x per row
    names = page.evaluate(BOXES_JS, "#bd-cost .hbar > :first-child")
    assert all(abs(n["w"] - 200) <= 1 for n in names)
    assert page.problems == []
