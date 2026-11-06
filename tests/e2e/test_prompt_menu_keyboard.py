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


def test_list_search_button_stays_on_the_filter_row(live, session):
    seed_support(live)
    page = session.page
    page.goto(f"{live.url}/project/{live.account.project_id}/prompts")
    search = page.get_by_role("button", name="Search").bounding_box()
    box = page.locator("input[name=q]").bounding_box()
    assert abs(search["y"] - box["y"]) < 12  # same row at 1280


def test_metrics_row_shows_model_latency_tokens_and_cost_on_a_phone(live, session):
    from datetime import UTC, datetime, timedelta

    from traceloom.db.models import Observation, Trace

    seed_support(live)
    with live.container.session_factory() as db:
        start = datetime.now(UTC) - timedelta(minutes=1)
        db.add(Trace(project_id=live.account.project_id, id="trace-mob", timestamp=start))
        db.add(
            Observation(
                project_id=live.account.project_id, id="o-mob", trace_id="trace-mob",
                type="generation", start_time=start, end_time=start + timedelta(seconds=2),
                model="claude-sonnet-5-5", usage_details={"total": 900},
                cost_details={"total": 0.0061}, prompt_name="support", prompt_version=2,
            )
        )  # fmt: skip
        db.commit()
    page = session.page
    page.set_viewport_size({"width": 390, "height": 800})
    page.goto(f"{live.url}/project/{live.account.project_id}/prompts/support?tab=metrics&version=2")
    row = page.locator("#metrics-pane tbody tr").first
    expect(row).to_contain_text("claude-sonnet-5-5")
    expect(row).to_contain_text("2.00 s")
    expect(row).to_contain_text("900 tokens")
    expect(row).to_contain_text("$0.0061")
    cost, link = row.locator("td.num.m-show").bounding_box(), row.locator("a").bounding_box()
    assert abs(cost["y"] - link["y"]) < 14  # the cost sits on the first line of the row
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
