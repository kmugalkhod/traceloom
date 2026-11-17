"""T-18 QA journey: dataset list, detail, item drawer, history, archive, delete, roles."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import expect

from tests.e2e.live_server import browser_page, live_server, make_world, sign_in  # noqa: F401
from tests.factories import DEFAULT_PASSWORD
from traceloom.auth.permissions import Role
from traceloom.datasets import service
from traceloom.datasets.service import Actor
from traceloom.db.models import Project, User

pytestmark = pytest.mark.e2e

XSS = "<script>alert(1)</script>"


def _csrf_post(page, url: str, body: str = "") -> int:
    return page.evaluate(
        """async ([url, body]) => {
            const token = document.cookie.match(/(?:^|; )tl_csrf=([^;]*)/)[1];
            const r = await fetch(url, {method: 'POST', redirect: 'manual', headers: {
              'X-CSRF-Token': token, 'Content-Type': 'application/x-www-form-urlencoded'}, body});
            return r.status;
        }""",
        [url, body],
    )


def _only_real_problems(page):
    return [p for p in page.problems if p.startswith(("pageerror", "dialog"))]


def test_member_dataset_item_versions_history_archive_xss(live_server, browser_page):  # noqa: F811
    browser_page.set_viewport_size({"width": 1280, "height": 900})
    world = make_world(live_server)
    page = browser_page
    base = f"{live_server.url}/project/{world['project_id']}/datasets"
    sign_in(page, live_server, world["member"], DEFAULT_PASSWORD)

    # create a dataset
    page.goto(base)
    expect(page.locator("body")).to_contain_text("No datasets yet")
    page.click("button:has-text('New dataset') >> nth=0")
    page.fill("#dn", "refund-cases")
    page.fill("#dd", "QA dataset")
    page.click("button:has-text('Create dataset')")
    expect(page.locator("h1")).to_have_text("refund-cases")
    expect(page.locator("body")).to_contain_text("No items yet")
    ds_url = page.url.split("?")[0]

    # add an item whose input is a script tag (JSON string)
    page.click("button:has-text('Add item') >> nth=0")
    page.fill("#ni-input", '"' + XSS + '"')
    page.fill("#ni-expected_output", '{"answer": "ok"}')
    page.click("#new-item button[type=submit]")
    expect(page.locator("table.tbl")).to_contain_text("<script>alert(1)</script>")
    assert page.locator("table script, #item-drawer script").count() == 0

    # edit twice
    page.click("a:has-text('Edit') >> nth=0")
    drawer = page.locator("#item-drawer")
    expect(drawer).to_be_visible()
    expect(drawer).to_contain_text("Save as v2")
    expect(drawer.locator("#it-input")).to_have_value('"' + XSS + '"')
    drawer.locator("#it-input").fill('{"q": "second"}')
    drawer.locator("button:has-text('Save as v2')").click()
    expect(page.locator("body")).to_contain_text("Item saved as a new version")
    expect(page.locator("#item-drawer")).to_contain_text("Save as v3")
    page.locator("#it-input").fill('{"q": "third"}')
    page.locator("#item-drawer button:has-text('Save as v3')").click()
    drawer = page.locator("#item-drawer")
    expect(drawer).to_contain_text("Save as v4")
    versions = drawer.locator(".ver .v")
    expect(versions).to_have_count(3)
    expect(versions.first).to_contain_text("Current")
    page.screenshot(path=".sdlc/evidence/t18-drawer.png", full_page=True)

    # layout: footer pinned to the drawer bottom, versions visible, no stray header bars
    box = lambda sel: page.locator(sel).first.bounding_box()  # noqa: E731
    dlg, foot, ver, h2 = (
        box("#item-drawer .dialog"),
        box("#item-drawer footer"),
        box("#item-drawer .ver .v"),
        box("#item-drawer h2"),
    )
    assert abs((foot["y"] + foot["height"]) - (dlg["y"] + dlg["height"])) <= 1
    assert ver["y"] + ver["height"] <= foot["y"]
    assert all(b["y"] > h2["y"] for b in [box("#item-drawer .jed-bar")])
    assert foot["height"] < 90

    # invalid JSON is rejected with an inline error and creates no version
    page.locator("#it-input").fill("{bad")
    page.locator("#item-drawer button:has-text('Save as v4')").click()
    expect(page.locator("body")).to_contain_text("Fix the JSON before saving")
    expect(page.locator("#item-drawer")).to_contain_text("Save as v4")

    # history shows create + 2 edits, XSS rendered as literal text
    page.goto(ds_url + "?tab=history")
    rows = page.locator("table.tbl tbody tr")
    expect(rows).to_have_count(3)
    expect(page.locator("body")).to_contain_text("v3")
    assert page.locator("table script").count() == 0
    page.screenshot(path=".sdlc/evidence/t18-history.png", full_page=True)

    # archive (auto-accept the confirm so only unexpected alerts are flagged)
    page.goto(ds_url)
    page.click("a:has-text('Edit') >> nth=0")
    page.evaluate("window.confirm = () => true")
    page.click("#item-drawer button:has-text('Archive item')")
    expect(page.locator("body")).to_contain_text("No items yet")
    page.goto(ds_url + "?status=archived")
    expect(page.locator("table.tbl")).to_contain_text("v4")  # archiving records a version
    page.goto(ds_url + "?tab=history")
    expect(page.locator("table.tbl tbody tr")).to_have_count(4)
    expect(page.locator("body")).to_contain_text("Archived")
    assert _only_real_problems(page) == []

    # Member cannot delete (403) — UI hides the action and the direct POST is refused
    page.goto(ds_url)
    assert page.locator("text=Delete dataset").count() == 0
    assert _csrf_post(page, ds_url + "/delete", "confirm_name=refund-cases") == 403


def test_viewer_is_read_only(live_server, browser_page):  # noqa: F811
    world = make_world(live_server)
    with live_server.container.session_factory() as db:
        project = db.get(Project, world["project_id"])
        user = db.query(User).filter_by(email=world["member"]).one()
        actor = Actor.for_user(user, Role.MEMBER)
        ds = service.create_dataset(db, project, actor, name="ro")
        item = service.create_item(db, project, actor, ds, input={"q": XSS}, expected_output=None)
        db.commit()
        ds_id, item_id = ds.id, item.id
    page = browser_page
    base = f"{live_server.url}/project/{world['project_id']}/datasets"
    sign_in(page, live_server, world["viewer"], DEFAULT_PASSWORD)

    page.goto(base)
    assert page.locator("button:has-text('New dataset')").count() == 0
    page.goto(f"{base}/{ds_id}")
    expect(page.locator(".rolebar")).to_contain_text("Read-only")
    assert page.locator("button:has-text('Add item')").count() == 0
    expect(page.locator("a:has-text('View')")).to_have_count(1)
    page.goto(f"{base}/{ds_id}?item={item_id}")
    drawer = page.locator("#item-drawer")
    expect(drawer).to_be_visible()
    assert drawer.locator("textarea:not([readonly])").count() == 0
    assert drawer.locator("button:has-text('Save'), button:has-text('Archive')").count() == 0
    expect(drawer.locator("#it-input")).to_have_value(re.compile("<script>"))
    assert page.locator("text=Delete dataset").count() == 0

    # direct writes are refused
    assert _csrf_post(page, base, "name=x") == 403
    assert _csrf_post(page, f"{base}/{ds_id}/items", 'input="1"') == 403
    assert _csrf_post(page, f"{base}/{ds_id}/items/{item_id}", 'input="1"') == 403
    assert _csrf_post(page, f"{base}/{ds_id}/items/{item_id}/archive") == 403
    assert _csrf_post(page, f"{base}/{ds_id}/delete", "confirm_name=ro") == 403
    assert _only_real_problems(page) == []


def test_admin_can_delete_dataset(live_server, browser_page):  # noqa: F811
    world = make_world(live_server)
    with live_server.container.session_factory() as db:
        project = db.get(Project, world["project_id"])
        user = db.query(User).filter_by(email=world["admin"]).one()
        ds = service.create_dataset(db, project, Actor.for_user(user, Role.ADMIN), name="gone")
        db.commit()
        ds_id = ds.id
    page = browser_page
    base = f"{live_server.url}/project/{world['project_id']}/datasets"
    sign_in(page, live_server, world["admin"], DEFAULT_PASSWORD)
    page.goto(f"{base}/{ds_id}")
    page.click("[data-menu-toggle=ds-more]")
    page.click("button:has-text('Delete dataset…')")
    page.fill("#delete-dataset-name", "wrong")
    page.click("#delete-dataset button[type=submit]")
    expect(page.locator("body")).to_contain_text("Type the dataset name exactly")
    page.goto(f"{base}/{ds_id}")
    page.click("[data-menu-toggle=ds-more]")
    page.click("button:has-text('Delete dataset…')")
    page.fill("#delete-dataset-name", "gone")
    page.click("#delete-dataset button[type=submit]")
    expect(page.locator("body")).to_contain_text("No datasets yet")
    assert _only_real_problems(page) == []


def test_detail_counts_render_numbers(live_server, browser_page):  # noqa: F811
    """`counts.items` in Jinja is dict.items, so the item count must not render as a method repr."""
    world = make_world(live_server)
    with live_server.container.session_factory() as db:
        project = db.get(Project, world["project_id"])
        user = db.query(User).filter_by(email=world["member"]).one()
        actor = Actor.for_user(user, Role.MEMBER)
        ds = service.create_dataset(db, project, actor, name="counts")
        service.create_item(db, project, actor, ds, input={"q": 1}, expected_output=None)
        db.commit()
        ds_id = ds.id
    page = browser_page
    sign_in(page, live_server, world["member"], DEFAULT_PASSWORD)
    page.goto(f"{live_server.url}/project/{world['project_id']}/datasets/{ds_id}")
    expect(page.locator("body")).not_to_contain_text("built-in method")
    expect(page.locator(".page-h .sub")).to_contain_text("1 active item ·")


def test_responsive_no_horizontal_scroll_and_screenshots(live_server, browser_page):  # noqa: F811
    world = make_world(live_server)
    page = browser_page
    base = f"{live_server.url}/project/{world['project_id']}/datasets"
    sign_in(page, live_server, world["member"], DEFAULT_PASSWORD)
    page.goto(base)
    page.click("button:has-text('New dataset') >> nth=0")
    page.fill("#dn", "responsive-set")
    page.click("button:has-text('Create dataset')")
    expect(page.locator("h1")).to_have_text("responsive-set")
    ds_url = page.url.split("?")[0]
    page.click("button:has-text('Add item') >> nth=0")
    page.fill("#ni-input", '{"q": "hello"}')
    page.click("#new-item button[type=submit]")
    for width in (1280, 390):
        page.set_viewport_size({"width": width, "height": 900})
        for url in (base, ds_url, ds_url + "?tab=history"):
            page.goto(url)
            overflow = page.evaluate(
                "document.documentElement.scrollWidth - document.documentElement.clientWidth"
            )
            assert overflow <= 0, f"horizontal scroll {overflow}px at {width} on {url}"
        page.goto(ds_url)
        page.screenshot(path=f".sdlc/evidence/t18-detail-{width}.png", full_page=True)
        page.goto(base)
        page.screenshot(path=f".sdlc/evidence/t18-list-{width}.png", full_page=True)
