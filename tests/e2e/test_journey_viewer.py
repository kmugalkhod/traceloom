"""Viewer journey: a Viewer reads a trace's annotations but has no way to change anything."""

from __future__ import annotations

import pytest

from tests.e2e.live_server import browser_page, live_server, make_world, sign_in  # noqa: F401
from tests.factories import DEFAULT_PASSWORD
from tests.helpers_web import seed_trace
from traceloom.ports import ScoreRow

pytestmark = pytest.mark.e2e


def test_viewer_reads_but_cannot_annotate(live_server, browser_page):  # noqa: F811
    world = make_world(live_server)
    seed_trace(
        live_server.container, world["project_id"], "t1", tags=["alpha"],
        scores=[ScoreRow(id="s1", name="quality", timestamp=__import__("datetime").datetime.now(
            __import__("datetime").timezone.utc), trace_id="t1", value=0.8, source="ANNOTATION")],
    )  # fmt: skip
    base = f"{live_server.url}/project/{world['project_id']}/traces/t1"
    page = browser_page

    sign_in(page, live_server, world["viewer"], DEFAULT_PASSWORD)
    page.goto(base + "/actions")
    assert "alpha" in page.content()
    assert page.locator("form, textarea, input, button[type=submit]").count() == 0
    page.goto(base + "/discussion")
    assert "quality" in page.content() and "0.8" in page.content()
    assert page.locator("form, textarea, input").count() == 0

    # A direct write from the Viewer's own browser session is refused.
    status = page.evaluate(
        """async (url) => {
            const token = document.cookie.match(/(?:^|; )tl_csrf=([^;]*)/)[1];
            const r = await fetch(url, {method: 'POST', headers: {'X-CSRF-Token': token,
              'Content-Type': 'application/x-www-form-urlencoded'}, body: 'value=1'});
            return r.status;
        }""",
        base + "/bookmark",
    )
    assert status == 403
    assert [p for p in page.problems if p.startswith(("pageerror", "dialog"))] == []


def test_member_journey_in_the_browser(live_server, browser_page):  # noqa: F811
    world = make_world(live_server)
    seed_trace(live_server.container, world["project_id"], "t1")
    base = f"{live_server.url}/project/{world['project_id']}/traces/t1"
    page = browser_page
    sign_in(page, live_server, world["member"], DEFAULT_PASSWORD)

    page.goto(base + "/actions")
    page.fill("#new-tag", "refund")
    with page.expect_navigation():
        page.get_by_role(
            "button", name="Add", exact=True
        ).click()  # plain form post: redirects to the detail page
    stored = live_server.container.trace_store.get_trace(world["project_id"], "t1")
    assert stored.trace.tags == ["refund"]

    page.goto(base + "/discussion")
    page.fill("#comment-body", "checked **by hand** <script>window.pwned=1</script>")
    with page.expect_navigation():
        page.get_by_role("button", name="Comment", exact=True).click()
    page.goto(base + "/discussion")
    assert "by hand" in page.content()
    assert page.evaluate("window.pwned") is None
    page.fill("#score-name", "quality")
    page.fill("#score-value", "0.9")
    with page.expect_navigation():
        page.get_by_role("button", name="Add score").click()
    stored = live_server.container.trace_store.get_trace(world["project_id"], "t1")
    assert [s.name for s in stored.scores] == ["quality"]
    assert not any("dialog" in p for p in page.problems)
