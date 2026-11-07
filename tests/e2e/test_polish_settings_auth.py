"""UI polish pass, Settings and Auth (FR-SA1, FR-SA2): 40px tabs on the page edge, a 48px
right-aligned row-menu column, and a centred 380px auth card with aligned labels."""

from __future__ import annotations

import pytest

from tests.e2e.live_server import browser_page, live_server, make_world, sign_in  # noqa: F401
from tests.factories import DEFAULT_PASSWORD

pytestmark = pytest.mark.e2e

BOXES_JS = """
(sel) => [...document.querySelectorAll(sel)].map((e) => {
  const r = e.getBoundingClientRect();
  return { x: r.x, y: r.y, w: r.width, h: r.height, r: r.right, b: r.bottom };
})
"""


def no_errors(page):
    assert [p for p in page.problems if p.startswith(("pageerror", "dialog"))] == []
    assert page.problems == []


@pytest.mark.parametrize("tab", ["keys", "members", "webhooks"])
def test_settings_tabs_and_menu_column(live_server, browser_page, tab):  # noqa: F811
    world = make_world(live_server)
    page = browser_page
    sign_in(page, live_server, world["admin"], DEFAULT_PASSWORD)
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{live_server.url}/project/{world['project_id']}/settings/{tab}")
    page.evaluate("document.fonts.ready")
    tabs = page.evaluate(BOXES_JS, ".stabs a[role=tab]")
    title = page.evaluate(BOXES_JS, ".page-h h1")[0]
    assert len(tabs) >= 5 and all(abs(t["h"] - 40) <= 1 for t in tabs), tabs
    assert abs(tabs[0]["x"] - title["x"]) <= 1  # first tab sits on the title's left edge
    assert page.locator(".ctxbar .crumbs").is_visible()  # crumbs live in the context bar
    menus = page.locator("td.menu .menu-wrap")
    if menus.count():
        cells = page.evaluate(BOXES_JS, "td.menu:has(.menu-wrap)")
        assert all(abs(c["w"] - 48) <= 1 for c in cells), cells
        assert (
            page.locator("td.menu").first.evaluate("e => getComputedStyle(e).textAlign") == "right"
        )
        menus.first.get_by_role("button").click()
        assert menus.first.locator("[role=menu]").is_visible()
        page.keyboard.press("Escape")
        assert not menus.first.locator("[role=menu]").is_visible()
    no_errors(page)


def test_viewer_settings_tabs(live_server, browser_page):  # noqa: F811
    world = make_world(live_server)
    page = browser_page
    sign_in(page, live_server, world["viewer"], DEFAULT_PASSWORD)
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{live_server.url}/project/{world['project_id']}/settings")
    tabs = page.evaluate(BOXES_JS, ".stabs a[role=tab]")
    assert tabs and all(abs(t["h"] - 40) <= 1 for t in tabs)
    no_errors(page)


@pytest.mark.parametrize("path", ["login", "signup", "forgot"])
def test_auth_card_desktop(live_server, browser_page, path):  # noqa: F811
    page = browser_page
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{live_server.url}/{path}")
    page.evaluate("document.fonts.ready")
    card = page.evaluate(BOXES_JS, ".auth-card")[0]
    main = page.evaluate(BOXES_JS, ".auth-main")[0]
    assert abs(card["w"] - 380) <= 1, card
    assert abs((card["y"] + card["h"] / 2) - (main["y"] + main["h"] / 2)) <= 2  # vertically centred
    assert abs((card["x"] + card["w"] / 2) - (main["x"] + main["w"] / 2)) <= 2
    inputs = page.evaluate(BOXES_JS, ".auth-card .input")
    assert inputs and all(abs(i["h"] - 40) <= 1 for i in inputs), inputs
    no_errors(page)


def test_login_forgot_link_on_label_baseline(live_server, browser_page):  # noqa: F811
    page = browser_page
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{live_server.url}/login")
    label, link = page.evaluate(BOXES_JS, "label[for=password], .label-row a")
    assert abs(label["y"] - link["y"]) <= 1 and abs(label["b"] - link["b"]) <= 1
    no_errors(page)


def test_auth_mobile_one_logo_no_scroll(live_server, browser_page):  # noqa: F811
    page = browser_page
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{live_server.url}/login")
    page.evaluate("document.fonts.ready")
    brands = page.locator(".brand").evaluate_all("els => els.filter(e => e.offsetParent).length")
    assert brands == 1  # the side panel is hidden, so the logo shows once
    assert page.evaluate(
        "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
    )
    inputs = page.evaluate(BOXES_JS, ".auth-card .input")
    assert all(abs(i["h"] - 44) <= 1 for i in inputs)
    no_errors(page)
