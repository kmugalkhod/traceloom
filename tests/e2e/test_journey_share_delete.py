"""Admin journey: share a trace and view it logged out, then delete by filter."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tests.e2e.live_server import browser_page, live_server, make_world, sign_in  # noqa: F401
from tests.factories import DEFAULT_PASSWORD
from tests.helpers_web import seed_trace
from traceloom.ports import TraceQuery

pytestmark = pytest.mark.e2e


def test_share_then_unshare(live_server, browser_page):  # noqa: F811
    world = make_world(live_server)
    seed_trace(
        live_server.container, world["project_id"], "t1", name="<script>window.pwned=1</script>"
    )
    base = f"{live_server.url}/project/{world['project_id']}/traces/t1"
    page = browser_page
    sign_in(page, live_server, world["admin"], DEFAULT_PASSWORD)

    page.goto(base + "/actions")
    with page.expect_navigation():
        page.get_by_role("switch", name="Public share").click()
    page.goto(base + "/actions")
    link = page.locator("#share-url").inner_text()
    assert link.endswith(f"/share/{world['project_id']}/t1")

    # a private window: no cookies, no session
    private = page.context.browser.new_context()
    shared = private.new_page()
    shared.on("dialog", lambda d: (_ for _ in ()).throw(AssertionError(d.message)))
    response = shared.goto(f"{live_server.url}/share/{world['project_id']}/t1")
    assert response.status == 200
    assert "Read-only" in shared.content()
    assert shared.locator("h1").inner_text() == "<script>window.pwned=1</script>"
    assert shared.evaluate("window.pwned") is None

    page.goto(base + "/actions")
    with page.expect_navigation():
        page.get_by_role("switch", name="Public share").click()
    assert shared.goto(f"{live_server.url}/share/{world['project_id']}/t1").status == 404
    private.close()


def test_delete_matching_in_the_browser(live_server, browser_page):  # noqa: F811
    world = make_world(live_server)
    now = datetime.now(UTC)  # inside the list's default 24 hour range
    for i in range(4):
        seed_trace(
            live_server.container, world["project_id"], f"keep{i}", name="keep", timestamp=now
        )
    for i in range(3):
        seed_trace(
            live_server.container, world["project_id"], f"drop{i}", name="drop", timestamp=now
        )
    page = browser_page
    sign_in(page, live_server, world["admin"], DEFAULT_PASSWORD)

    page.goto(f"{live_server.url}/project/{world['project_id']}/traces-delete-matching?name=drop")
    assert "Delete 3 traces?" in page.content()
    page.fill("#confirm-count", "2")
    with page.expect_navigation():
        page.get_by_role("button", name="Delete 3 traces").click()
    assert "Type 3 to confirm" in page.content()
    page.fill("#confirm-count", "3")
    with page.expect_navigation():
        page.get_by_role("button", name="Delete 3 traces").click()

    left = live_server.container.trace_store.matching_trace_ids(world["project_id"], TraceQuery())
    assert sorted(left) == [f"keep{i}" for i in range(4)]
    page.goto(f"{live_server.url}/project/{world['project_id']}/settings/audit")
    assert "traces.deleted_by_filter" in page.content() and "Admin Person" in page.content()
