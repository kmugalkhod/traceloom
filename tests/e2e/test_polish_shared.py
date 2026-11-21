"""UI polish pass, shared chrome (AC-1, AC-10, AC-11): the context bar's height and controls, and
text contrast on the polished pages in both themes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tests.e2e.live_server import browser_page, live_server, make_world, sign_in  # noqa: F401
from tests.e2e.test_journey_dashboard import CONTRAST_JS
from tests.factories import DEFAULT_PASSWORD
from tests.helpers_web import seed_trace

pytestmark = pytest.mark.e2e

BOXES = """
(sel) => [...document.querySelectorAll(sel)].filter((e) => e.getClientRects().length).map((e) => {
  const r = e.getBoundingClientRect();
  return { y: r.y, h: r.height, w: r.width };
})
"""


def open_traces(live_server, page, size, scheme="light", path="/traces"):  # noqa: F811
    world = make_world(live_server)
    now = datetime.now(UTC)
    seed_trace(
        live_server.container, world["project_id"], "t1", timestamp=now - timedelta(minutes=3),
        user_id="usr_a", tags=["alpha"], cost=0.01, tokens=500, duration_ms=900,
    )  # fmt: skip
    sign_in(page, live_server, world["viewer"], DEFAULT_PASSWORD)
    page.set_viewport_size({"width": size[0], "height": size[1]})
    page.emulate_media(color_scheme=scheme)
    page.goto(f"{live_server.url}/project/{world['project_id']}{path}")
    page.evaluate("document.fonts.ready")
    return world


def test_context_bar_is_56px_with_36px_controls(live_server, browser_page):  # noqa: F811
    page = browser_page
    open_traces(live_server, page, (1440, 900))
    (bar,) = page.evaluate(BOXES, ".ctxbar")
    assert abs(bar["h"] - 56) <= 1
    controls = page.evaluate(
        BOXES, ".ctxbar .select, .ctxbar .seg, .ctxbar .btn, .ctxbar .btn-icon, .ctxbar .picker"
    )
    assert controls and all(abs(c["h"] - 36) <= 1 for c in controls), controls
    assert max(c["y"] for c in controls) - min(c["y"] for c in controls) <= 1


def test_mobile_context_row_shares_one_top_and_page_fits(live_server, browser_page):  # noqa: F811
    page = browser_page
    open_traces(live_server, page, (390, 844))
    row = page.evaluate(BOXES, ".ctx-m .select, .ctx-m .seg")
    assert len(row) == 2 and abs(row[0]["y"] - row[1]["y"]) <= 1, row
    assert page.evaluate(
        "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
    )


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("path", ["/traces", "/traces/t1"])
def test_text_contrast_on_traces_pages(live_server, browser_page, scheme, path):  # noqa: F811
    page = browser_page
    open_traces(live_server, page, (1440, 900), scheme, path)
    page.locator("#trace-rows tr, .split .node").first.wait_for()
    selectors = {
        "/traces": [".stat .sub", ".stat b", ".tbl th", "#trace-rows td.num", ".cell2 span"],
        "/traces/t1": [".mline dd", ".th-head h1", ".tabs .tab", ".tree-pane .node .dur"],
    }[path]
    for selector, ratio in page.evaluate(CONTRAST_JS, selectors):
        assert ratio is None or ratio >= 4.5, (scheme, path, selector, ratio)
    assert [p for p in page.problems if "403" not in p and "404" not in p] == []


def test_dark_mode_swaps_the_surface_tokens(live_server, browser_page):  # noqa: F811
    page = browser_page
    open_traces(live_server, page, (1440, 900), "dark")
    dark = page.evaluate("getComputedStyle(document.body).backgroundColor")
    page.emulate_media(color_scheme="light")
    light = page.evaluate("getComputedStyle(document.body).backgroundColor")
    assert dark != light
