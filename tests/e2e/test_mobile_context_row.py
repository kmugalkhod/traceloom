"""At 390px the environment and date range live in the mobile context row on every page that
has them, in light and dark, and the row fits the screen."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tests.e2e.live_server import browser_page, live_server, make_world, sign_in  # noqa: F401
from tests.factories import DEFAULT_PASSWORD
from tests.helpers_web import seed_trace

pytestmark = pytest.mark.e2e


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_mobile_context_row_on_every_list_page(live_server, browser_page, scheme):  # noqa: F811
    world = make_world(live_server)
    now = datetime.now(UTC)
    for env in ("prod", "dev"):
        seed_trace(live_server.container, world["project_id"], f"t-{env}", environment=env,
                   session_id=f"s-{env}", user_id=f"u-{env}", timestamp=now)  # fmt: skip
    page = browser_page
    sign_in(page, live_server, world["viewer"], DEFAULT_PASSWORD)
    page.set_viewport_size({"width": 390, "height": 800})
    page.emulate_media(color_scheme=scheme)
    base = f"{live_server.url}/project/{world['project_id']}"
    for path in ("traces", "sessions", "users", "dashboard"):
        page.goto(f"{base}/{path}")
        row = page.locator(".ctx-m")
        row.wait_for(state="visible")
        assert row.locator("select[name=env]").is_visible(), path
        links = row.locator(".seg a")
        assert links.count() == 4 and links.first.is_visible(), path
        box = row.bounding_box()
        assert box["x"] >= 0 and box["x"] + box["width"] <= 391, (path, box)
        assert page.evaluate(
            "document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1"
        ), path
        # the controls work: choose 7d, then an environment
        with page.expect_navigation():
            row.locator(".seg a", has_text="7d").click()
        assert "range=7d" in page.url, path
        with page.expect_navigation():
            page.locator(".ctx-m select[name=env]").select_option("prod")
        assert "range=7d" in page.url  # the switcher returns to the same page and range
        page.goto(f"{base}/{path}?range=7d")
        assert page.locator(".ctx-m select[name=env]").input_value() == "prod", path
        # reset for the next page
        with page.expect_navigation():
            page.locator(".ctx-m select[name=env]").select_option("all")
    assert [p for p in page.problems if p.startswith(("pageerror", "dialog"))] == []


def test_mobile_status_tabs_and_env_switcher_fit_390px(live_server, browser_page):  # noqa: F811
    world = make_world(live_server)
    seed_trace(live_server.container, world["project_id"], "t-1", environment="prod",
               timestamp=datetime.now(UTC))  # fmt: skip
    page = browser_page
    sign_in(page, live_server, world["viewer"], DEFAULT_PASSWORD)
    page.set_viewport_size({"width": 390, "height": 800})
    page.goto(f"{live_server.url}/project/{world['project_id']}/traces")
    tabs = page.locator(".tbar .seg[role=tablist]")
    tabs.wait_for(state="visible")
    # Every quick-filter tab, Bookmarked included, is fully inside the tray: nothing clipped.
    tray = tabs.bounding_box()
    for i in range(tabs.locator("a[role=tab]").count()):
        box = tabs.locator("a[role=tab]").nth(i).bounding_box()
        assert tray["x"] <= box["x"] and box["x"] + box["width"] <= tray["x"] + tray["width"] + 0.5
    assert tabs.evaluate("e => e.scrollWidth <= e.clientWidth")
    assert tabs.locator("a[role=tab]", has_text="Bookmarked").is_visible()
    # The environment switcher is drawn like the other controls, not as a bare native select.
    select = page.locator(".ctx-m select[name=env]")
    assert select.evaluate("e => getComputedStyle(e).appearance") == "none"
    assert page.locator(".ctx-m .select-wrap .ico").is_visible()
