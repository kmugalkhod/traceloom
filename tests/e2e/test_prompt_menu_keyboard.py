"""The "..." menu on prompt detail works from the keyboard: Enter opens it and focuses the first
item, arrows move, Escape closes it and returns focus to the button. At 390px the version rail
stacks instead of scrolling sideways."""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

from tests.e2e.test_journey_prompt_label_switch import seed_support

pytestmark = pytest.mark.e2e


def test_menu_is_keyboard_accessible(live, session):
    seed_support(live)
    page = session.page
    page.goto(f"{live.url}/project/{live.account.project_id}/prompts/support")
    toggle = page.locator("[data-menu-toggle=prompt-menu]")
    toggle.focus()
    page.keyboard.press("Enter")
    menu = page.locator("#prompt-menu")
    expect(menu).to_be_visible()
    expect(toggle).to_have_attribute("aria-expanded", "true")
    first = page.evaluate("document.activeElement.textContent.trim()")
    assert first  # focus moved into the menu
    page.keyboard.press("ArrowDown")
    second = page.evaluate("document.activeElement.textContent.trim()")
    assert second != first
    page.keyboard.press("ArrowUp")
    assert page.evaluate("document.activeElement.textContent.trim()") == first
    page.keyboard.press("Escape")
    expect(menu).to_be_hidden()
    expect(toggle).to_be_focused()
    assert [p for p in session.problems if p.startswith(("dialog", "pageerror"))] == []


def test_version_rail_stacks_on_a_phone(live, session):
    seed_support(live)
    page = session.page
    page.set_viewport_size({"width": 390, "height": 800})
    page.goto(f"{live.url}/project/{live.account.project_id}/prompts/support")
    rail = page.locator("nav.rail")
    assert rail.evaluate("el => getComputedStyle(el).flexDirection") == "column"
    assert rail.evaluate("el => el.scrollWidth <= el.clientWidth + 1")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
