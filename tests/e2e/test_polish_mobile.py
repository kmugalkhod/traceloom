"""UI polish pass, 390px (AC-10, FR-M2, FR-M3): no sideways page scroll on the main pages and a
single-line context row with the environment select and range tray on one top offset."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tests.e2e.live_server import browser_page, live_server, make_world, sign_in  # noqa: F401
from tests.factories import DEFAULT_PASSWORD
from tests.helpers_web import seed_trace

pytestmark = pytest.mark.e2e

PAGES = {
    "traces": "traces",
    "trace-detail": "traces/t-1",
    "dashboard": "dashboard",
    "prompts": "prompts",
    "playground": "playground",
    "settings": "settings/keys",
}

BOXES_JS = """
(sel) => [...document.querySelectorAll(sel)].map((e) => {
  const r = e.getBoundingClientRect();
  return { x: r.x, y: r.y, w: r.width, h: r.height, r: r.right, b: r.bottom };
})
"""


def open_mobile(live_server, page, role="admin"):  # noqa: F811
    world = make_world(live_server)
    now = datetime.now(UTC)
    for env in ("prod", "dev"):
        seed_trace(live_server.container, world["project_id"], f"t-{env}", environment=env,
                   session_id=f"s-{env}", user_id=f"u-{env}", timestamp=now)  # fmt: skip
    seed_trace(live_server.container, world["project_id"], "t-1", environment="prod",
               timestamp=now, user_id="u-1")  # fmt: skip
    sign_in(page, live_server, world[role], DEFAULT_PASSWORD)
    page.set_viewport_size({"width": 390, "height": 844})
    return f"{live_server.url}/project/{world['project_id']}"


@pytest.mark.parametrize("name", list(PAGES))
def test_no_horizontal_scroll(live_server, browser_page, name):  # noqa: F811
    base = open_mobile(live_server, browser_page)
    page = browser_page
    page.goto(f"{base}/{PAGES[name]}")
    page.wait_for_load_state("networkidle")
    page.evaluate("document.fonts.ready")
    sizes = page.evaluate(
        "[document.documentElement.scrollWidth, document.documentElement.clientWidth]"
    )
    assert sizes[0] <= sizes[1], (name, sizes)
    assert [p for p in page.problems if p.startswith(("pageerror", "dialog"))] == []


@pytest.mark.parametrize("name", ["traces", "dashboard"])
def test_ctx_row_single_line(live_server, browser_page, name):  # noqa: F811
    base = open_mobile(live_server, browser_page, role="viewer")
    page = browser_page
    page.goto(f"{base}/{PAGES[name]}")
    page.locator(".ctx-m").wait_for(state="visible")
    page.evaluate("document.fonts.ready")
    env, tray = page.evaluate(BOXES_JS, ".ctx-m .select-wrap, .ctx-m .seg")
    assert abs(env["y"] - tray["y"]) <= 1, (env, tray)  # one row: a shared top offset
    assert abs(env["h"] - 44) <= 1 and abs(tray["h"] - 44) <= 1
    assert env["r"] <= tray["x"] + 1 and tray["r"] <= 390  # side by side, nothing clipped
    assert [p for p in page.problems if p.startswith(("pageerror", "dialog"))] == []
