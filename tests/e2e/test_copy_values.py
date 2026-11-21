"""Copying: `data-copy` is always the literal value (a user id of `#1` copies "#1", never the
element with that id), `data-copy-from` copies an element's text, and a copy chosen from a "..."
menu shows its "Copied" feedback on the menu's button, since the menu itself closes."""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e

# Record what the page copies, through the async clipboard API or the execCommand fallback.
RECORD = """
window.__copied = [];
Object.defineProperty(navigator, 'clipboard', {configurable: true, value: {
  writeText: function (t) { window.__copied.push(t); return Promise.resolve(); }
}});
document.execCommand = function () {
  var el = document.activeElement; window.__copied.push(el && el.value); return true;
};
"""


def open_trace(live, session, events):
    live.ingest(events(1, user="#1"))
    page = session.page
    page.add_init_script(RECORD)
    page.goto(f"{live.url}/project/{live.account.project_id}/traces/trace-001")
    expect(page.locator("[data-menu-toggle=trace-menu]")).to_be_visible()
    return page


def test_a_user_id_starting_with_hash_copies_literally(live, session, events):
    page = open_trace(live, session, events)
    toggle = page.locator("[data-menu-toggle=trace-menu]")
    toggle.click()
    page.get_by_role("menuitem", name="Copy user id").click()
    page.wait_for_function("window.__copied.length === 1")
    assert page.evaluate("window.__copied") == ["#1"]
    assert [p for p in session.problems if p.startswith(("dialog", "pageerror"))] == []


def test_copy_from_copies_the_elements_text(live, session, events):
    page = open_trace(live, session, events)
    page.get_by_role("button", name="Copy trace id").first.click()
    page.wait_for_function("window.__copied.length === 1")
    assert page.evaluate("window.__copied") == ["trace-001"]


def test_menu_copy_feedback_shows_on_the_menu_button(live, session, events):
    page = open_trace(live, session, events)
    toggle = page.locator("[data-menu-toggle=trace-menu]")
    label = toggle.get_attribute("aria-label")
    toggle.click()
    page.get_by_role("menuitem", name="Copy trace id").click()
    expect(page.locator("#trace-menu")).to_be_hidden()
    expect(toggle).to_have_attribute("data-copied", "true")
    expect(toggle).to_have_attribute("aria-label", "Copied")
    assert toggle.evaluate("el => getComputedStyle(el, '::after').content") == '"Copied"'
    expect(toggle).not_to_have_attribute("data-copied", "true", timeout=4000)
    expect(toggle).to_have_attribute("aria-label", label)
