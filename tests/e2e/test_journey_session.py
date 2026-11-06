"""Session journey: list, replay in time order with totals, the users pages."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tests.e2e.live_server import browser_page, live_server, make_world, sign_in  # noqa: F401
from tests.factories import DEFAULT_PASSWORD
from tests.helpers_web import seed_trace

pytestmark = pytest.mark.e2e


def test_session_replay_and_users(live_server, browser_page):  # noqa: F811
    world = make_world(live_server)
    now = datetime.now(UTC)
    for minute, cost in [(3, 0.03), (0, 0.01), (4, 0.05), (1, 0.02), (2, 0.04)]:
        seed_trace(
            live_server.container, world["project_id"], f"c{minute}", session_id="sess-1",
            user_id="alice", cost=cost, tokens=100, input=f"question {minute}",
            output="<script>window.pwned=1</script>" if minute == 2 else f"answer {minute}",
            timestamp=now - timedelta(minutes=30 - minute * 5),
        )  # fmt: skip
    base = f"{live_server.url}/project/{world['project_id']}"
    page = browser_page
    sign_in(page, live_server, world["viewer"], DEFAULT_PASSWORD)

    page.goto(base + "/sessions")
    row = page.locator("tr[data-row]").first
    assert "sess-1" in row.inner_text() and "$0.1500" in row.inner_text()
    with page.expect_navigation():
        page.get_by_role("link", name="sess-1").click()

    text = page.locator("main").inner_text()
    order = [text.index(f"question {i}") for i in range(5)]
    assert order == sorted(order)  # time order although inserted out of order
    assert "$0.1500" in text and "<script>window.pwned=1</script>" in text  # literal text
    assert page.evaluate("window.pwned") is None
    assert [p for p in page.problems if p.startswith(("pageerror", "dialog"))] == []

    page.goto(base + "/users")
    with page.expect_navigation():
        page.get_by_role("link", name="alice").click()
    link = page.get_by_role("link", name="View traces")
    assert link.get_attribute("href") == f"/project/{world['project_id']}/traces?user_id=alice"
    assert "$0.1500" in page.locator("main").inner_text()
