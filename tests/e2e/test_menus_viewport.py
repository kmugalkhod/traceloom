"""Popup "..." menus stay inside the viewport and stay clickable, on phones and desktops, even
when the table around them has a single row and clips with overflow:hidden."""

from __future__ import annotations

import pytest
from playwright.sync_api import expect
from sqlalchemy import select

from traceloom.auth.permissions import Role
from traceloom.db.models import Project, User, Webhook
from traceloom.llm import connections
from traceloom.llm.connections import ConnectionPolicy
from traceloom.prompts import service
from traceloom.prompts.service import Actor
from traceloom.webhooks import events

pytestmark = pytest.mark.e2e

SIZES = [(390, 800), (1280, 900)]


def seed(live) -> None:
    with live.container.session_factory() as db:
        owner = db.scalar(select(User).where(User.email == live.account.email))
        project = db.get(Project, live.account.project_id)
        who = Actor.for_user(owner, Role.OWNER)
        connections.create(
            db,
            project,
            who,
            ConnectionPolicy.from_settings(live.container.settings),
            provider="anthropic",
            name="Anthropic · production key",
            secrets={"api_key": "sk-ant-1234"},
        )
        db.add(
            Webhook(
                project_id=project.id,
                url="https://93.184.216.34/hook",
                events=[events.LABEL_MOVED],
                enabled=True,
                secret_ciphertext="x",
            )
        )
        service.create_version(
            db, project, who, name="p", type="text", content="x", commit_message="m"
        )
        db.commit()


def open_menu(page, toggle_selector: str):
    page.locator(toggle_selector).scroll_into_view_if_needed()  # as a person would, before clicking
    page.wait_for_timeout(100)
    page.click(toggle_selector)
    menu = page.locator("[data-menu]:not([hidden])")
    expect(menu).to_have_count(1)
    expect(menu).to_be_visible()
    return menu


def assert_inside_viewport(page, menu) -> None:
    box = menu.bounding_box()
    size = page.viewport_size
    assert box["x"] >= 0 and box["y"] >= 0, box
    assert box["x"] + box["width"] <= size["width"] + 0.5, (box, size)
    assert box["y"] + box["height"] <= size["height"] + 0.5, (box, size)


@pytest.mark.parametrize(("width", "height"), SIZES)
@pytest.mark.parametrize(
    ("path", "toggle", "item", "dialog"),
    [
        ("settings/connections", "[data-menu-toggle^=cn-menu]", "Edit", "dialog[open]"),
        ("settings/webhooks", "[data-menu-toggle^=wh-menu]", "Edit", "dialog[open]"),
    ],
)
def test_row_menus_are_inside_the_viewport_and_clickable(
    live, session, width, height, path, toggle, item, dialog
):
    seed(live)
    page = session.page
    page.set_viewport_size({"width": width, "height": height})
    page.goto(f"{live.url}/project/{live.account.project_id}/{path}")
    menu = open_menu(page, toggle)
    assert_inside_viewport(page, menu)
    # The popup is on top of the page (not covered or clipped): clicking its item reaches it.
    menu.get_by_role("menuitem", name=item).click()
    expect(page.locator(dialog)).to_have_count(1)
    assert [p for p in session.problems if p.startswith(("dialog", "pageerror"))] == []


@pytest.mark.parametrize(("width", "height"), SIZES)
def test_prompt_detail_menu_is_inside_the_viewport(live, session, width, height):
    seed(live)
    page = session.page
    page.set_viewport_size({"width": width, "height": height})
    page.goto(f"{live.url}/project/{live.account.project_id}/prompts/p")
    menu = open_menu(page, "[data-menu-toggle=prompt-menu]")
    assert_inside_viewport(page, menu)
    expect(menu.get_by_role("menuitem", name="Copy SDK snippet")).to_be_visible()


def test_menu_flips_above_near_the_bottom_and_closes_on_escape(live, session):
    seed(live)
    page = session.page
    page.set_viewport_size({"width": 390, "height": 420})  # little room below the button
    page.goto(f"{live.url}/project/{live.account.project_id}/settings/connections")
    menu = open_menu(page, "[data-menu-toggle^=cn-menu]")
    assert_inside_viewport(page, menu)
    menu_id = menu.get_attribute("id")
    page.keyboard.press("Escape")
    expect(page.locator("[data-menu]:not([hidden])")).to_have_count(0)
    closed = page.locator(f"#{menu_id}")
    assert closed.evaluate("el => el.style.left === '' && !el.classList.contains('is-placed')")
