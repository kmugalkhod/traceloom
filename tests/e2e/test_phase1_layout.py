"""v3.1 layout pass on the Phase 1 pages: the dashboard's tabbed Breakdown panel and the "..."
menus that hold each view's rare actions. Keyboard first, then the same at phone width."""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e

TABS = ["Cost", "Tokens", "Latency by model", "Latency by trace"]
PANELS = ["#bd-cost", "#bd-tokens", "#bd-latm", "#bd-latn"]


def assert_selected(page, index: int) -> None:
    tabs = page.get_by_role("tablist", name="Breakdown").get_by_role("tab")
    for i in range(len(TABS)):
        on = i == index
        expect(tabs.nth(i)).to_have_attribute("aria-selected", "true" if on else "false")
        expect(tabs.nth(i)).to_have_attribute("tabindex", "0" if on else "-1")  # roving
        expect(page.locator(PANELS[i])).to_be_visible() if on else expect(
            page.locator(PANELS[i])
        ).to_be_hidden()
    expect(tabs.nth(index)).to_be_focused()


def no_page_scroll_x(page) -> bool:
    return page.evaluate(
        "document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1"
    )


@pytest.mark.parametrize("width", [1280, 390])
def test_breakdown_tabs_switch_with_the_keyboard(live, session, events, width):
    live.ingest(events(1) + events(2, name="search-ranker"))
    page = session.page
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(f"{live.url}/project/{live.account.project_id}/dashboard")
    tablist = page.get_by_role("tablist", name="Breakdown")
    expect(tablist.get_by_role("tab")).to_have_text(TABS)
    # Stat row, traces over time and error rate stay visible next to the one Breakdown panel.
    for name in ("Traces over time", "Error rate by level", "Breakdown", "Top users by cost"):
        expect(page.get_by_role("region", name=name, exact=True)).to_be_visible()
    expect(page.locator("#bd-cost")).to_contain_text("claude-sonnet-4-5")

    tablist.get_by_role("tab", name="Cost").focus()
    page.keyboard.press("ArrowRight")
    assert_selected(page, 1)
    expect(page.locator("#bd-tokens")).to_contain_text("2,040")  # 2 traces x 1,020 tokens
    page.keyboard.press("End")
    assert_selected(page, 3)
    expect(page.locator("#bd-latn")).to_contain_text("search-ranker")
    page.keyboard.press("ArrowRight")  # wraps around
    assert_selected(page, 0)
    page.keyboard.press("ArrowLeft")
    assert_selected(page, 3)
    page.keyboard.press("Home")
    assert_selected(page, 0)
    page.keyboard.press("Tab")  # out of the tablist, into the shown panel
    expect(page.locator("#bd-cost")).to_be_focused()
    page.keyboard.press("Shift+Tab")
    expect(tablist.get_by_role("tab", name="Cost")).to_be_focused()

    tablist.get_by_role("tab", name="Latency by model").click()  # pointer and touch
    expect(page.locator("#bd-latm")).to_be_visible()
    expect(page.locator("#bd-latm")).to_contain_text("claude-sonnet-4-5")
    assert no_page_scroll_x(page)
    assert session.problems == []


def open_by_keyboard(page, toggle: str):
    page.focus(toggle)
    page.keyboard.press("Enter")
    menu = page.locator("[data-menu]:not([hidden])")
    expect(menu).to_have_count(1)
    expect(menu).to_be_visible()
    return menu


def inside_viewport(page, menu) -> None:
    box, size = menu.bounding_box(), page.viewport_size
    assert box["x"] >= 0 and box["y"] >= 0, box
    assert box["x"] + box["width"] <= size["width"] + 0.5, (box, size)
    assert box["y"] + box["height"] <= size["height"] + 0.5, (box, size)


@pytest.mark.parametrize("width", [1280, 390])
def test_traces_list_keeps_one_primary_and_the_rest_in_its_menu(live, session, events, width):
    live.ingest(events(1))
    page = session.page
    page.set_viewport_size({"width": width, "height": 800})
    page.goto(f"{live.url}/project/{live.account.project_id}/traces?q=question")
    header = page.locator(".page-h")
    expect(header.locator(".btn-primary")).to_have_count(1)
    expect(header.locator(".btn-primary")).to_have_text("Save view")
    expect(header.get_by_role("link", name="Delete matching traces…")).to_be_hidden()

    toggle = "[data-menu-toggle=list-menu]"
    expect(
        page.get_by_role("button", name="More actions: saved views, columns, delete matching")
    ).to_have_count(1)
    menu = open_by_keyboard(page, toggle)
    inside_viewport(page, menu)
    expect(menu.get_by_role("group", name="Saved views")).to_be_visible()
    expect(menu.get_by_role("group", name="Columns").locator("input[type=checkbox]")).to_have_count(
        page.locator("[name=col]").count()
    )
    # Arrow keys walk the items, including the column checkboxes inside their labels.
    first = menu.locator(".item").first
    expect(first).to_be_focused()
    page.keyboard.press("ArrowDown")
    expect(menu.locator(".item").nth(1).locator("input")).to_be_focused()
    page.keyboard.press("ArrowUp")
    page.keyboard.press("ArrowUp")  # wraps to the last item: delete matching, in red
    danger = menu.get_by_role("menuitem", name="Delete matching traces…")
    expect(danger).to_be_focused()
    expect(danger).to_have_class("item danger")
    assert "q=question" in danger.get_attribute("href")  # carries the current filters
    page.keyboard.press("Escape")
    expect(menu).to_be_hidden()
    expect(page.locator(toggle)).to_be_focused()

    page.click(toggle)  # pointer or touch: the action opens its confirm page
    with page.expect_navigation():
        page.get_by_role("menuitem", name="Delete matching traces…").click()
    assert "/traces-delete-matching?" in page.url and "q=question" in page.url
    assert no_page_scroll_x(page)
    assert session.problems == []


@pytest.mark.parametrize("width", [1280, 390])
def test_trace_detail_share_copy_and_delete_live_in_the_menu(live, session, events, width):
    live.ingest(events(1))
    page = session.page
    page.set_viewport_size({"width": width, "height": 800})
    page.goto(f"{live.url}/project/{live.account.project_id}/traces/trace-001")
    toggle = "#trace-actions [data-menu-toggle=trace-menu]"
    expect(page.locator(toggle)).to_be_visible()  # the lazy actions fragment arrived
    head = page.locator(".th-title")
    expect(head.get_by_role("button", name="Bookmark", exact=True)).to_be_visible()
    expect(head.locator(".btn-danger:visible")).to_have_count(0)  # red only in the dialog
    expect(head.get_by_role("switch", name="Public share")).to_be_hidden()

    menu = open_by_keyboard(page, toggle)
    inside_viewport(page, menu)
    for name in ("Copy trace id", "Copy user id", "Delete trace…"):
        expect(menu.get_by_role("menuitem", name=name)).to_be_visible()
    menu.get_by_role("switch", name="Public share").click()
    expect(page.locator("#share-url")).to_contain_text(
        f"/share/{live.account.project_id}/trace-001"
    )
    page.click(toggle)
    expect(page.get_by_role("menuitem", name="Copy share link")).to_be_visible()
    page.get_by_role("menuitem", name="Copy trace id").click()
    expect(page.locator("[data-menu]:not([hidden])")).to_have_count(0)  # choosing closes it

    page.click(toggle)
    page.get_by_role("menuitem", name="Delete trace…").click()
    dialog = page.locator("dialog#delete-trace")
    expect(dialog).to_be_visible()
    expect(dialog.get_by_role("button", name="Delete trace")).to_be_visible()
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    expect(page.locator(toggle)).to_be_focused()  # focus came back to "..."
    assert no_page_scroll_x(page)
    assert session.problems == []


def test_settings_key_revoke_is_a_menu_item_that_opens_the_confirm(live, session):
    page = session.page
    page.goto(f"{live.url}/project/{live.account.project_id}/settings/keys")
    row = page.locator("tr", has_text=live.account.public_key)
    expect(row.locator(".btn", has_text="Revoke")).to_have_count(0)
    row.get_by_role("button", name="More actions for e2e").click()
    page.get_by_role("menuitem", name="Revoke…").click()
    dialog = page.locator("dialog#revoke-key")
    expect(dialog).to_be_visible()
    expect(dialog.locator("[data-slot=name]")).to_have_text("e2e")
    assert session.problems == []
