# ruff: noqa: E501
"""QA for T-6 (header, tags, breadcrumb) and T-8 (IO blocks, Scores/Comments tabs), in a real browser."""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

from tests.e2e.live_server import browser_page, live_server, make_world, sign_in  # noqa: F401
from tests.factories import DEFAULT_PASSWORD
from tests.helpers_web import seed_trace

pytestmark = pytest.mark.e2e

BOX = "e => { const r = e.getBoundingClientRect(); return {t: r.top, b: r.bottom, h: r.height, l: r.left, r: r.right}; }"


def _open(live_server, page, role, **seed):  # noqa: F811
    world = make_world(live_server)
    seed_trace(live_server.container, world["project_id"], "t1", user_id="u-1", session_id="s-1",
               cost=0.5, tokens=10, tags=["alpha"], **seed)  # fmt: skip
    base = f"{live_server.url}/project/{world['project_id']}/traces/t1"
    sign_in(page, live_server, world[role], DEFAULT_PASSWORD)
    page.goto(base)
    return world, base


def test_header_breadcrumb_and_tags_member(live_server, browser_page):  # noqa: F811
    page = browser_page
    page.set_viewport_size({"width": 1440, "height": 900})
    _world, _base = _open(live_server, page, "member")
    tops = page.locator(".th-head .mline dd").evaluate_all(
        "els => els.map(e => Math.round(e.getBoundingClientRect().top))"
    )
    assert len(tops) >= 5 and max(tops) - min(tops) <= 2, tops
    assert page.locator(".ctxbar .crumbs").count() == 1
    bar = page.locator(".ctxbar").evaluate(BOX)
    title = page.locator(".th-head h1").evaluate(BOX)
    # The header block starts 24px under the bar; the 24px h1 is centred in a 36px row (about 27.6px).
    row = page.locator(".th-title").evaluate(BOX)
    assert row["t"] - bar["b"] <= 24 + 1 and title["t"] - bar["b"] <= 24 + 8, (bar, row, title)
    assert page.locator(".th-head").evaluate(BOX)["h"] <= 112
    assert "Edit tags:" not in page.content()
    assert page.locator("#trace-tags .chip", has_text="alpha").count() == 1
    # add then remove through the inline controls
    page.fill("#new-tag", "beta")
    page.get_by_role("button", name="Add", exact=True).click()
    page.wait_for_selector("#trace-tags .chip:has-text('beta')")
    assert page.locator("#trace-tags .chip", has_text="beta").count() == 1
    page.get_by_role("button", name="Remove tag beta").click()
    expect(page.locator("#trace-tags .chip", has_text="beta")).to_have_count(0)
    assert page.locator("#trace-tags .chip", has_text="alpha").count() == 1


def test_viewer_sees_chips_only(live_server, browser_page):  # noqa: F811
    page = browser_page
    _open(live_server, page, "viewer")
    assert page.locator("#trace-tags .chip", has_text="alpha").count() == 1
    assert page.locator("#trace-tags button, #trace-tags input, #trace-tags form").count() == 0


def test_tags_through_plain_form_posts(live_server, browser_page):  # noqa: F811
    """Add and remove use the form's own action and fields (what a no-JS browser posts)."""
    page = browser_page
    world, base = _open(live_server, page, "member")
    store = live_server.container.trace_store

    def post(form_sel, tag=None):
        token = page.locator(f"{form_sel} input[name=csrf_token]").first.input_value()
        action = page.locator(form_sel).first.get_attribute("action")
        r = page.request.post(
            live_server.url + action, form={"csrf_token": token, "tag": tag}, max_redirects=0
        )
        assert r.status in (302, 303), r.status

    post("form.tag-add", "nojs")
    assert "nojs" in store.get_trace(world["project_id"], "t1").trace.tags
    page.goto(base)
    post("form[action$='/tags/remove']:has(input[value=nojs])", "nojs")
    tags = store.get_trace(world["project_id"], "t1").trace.tags
    assert "nojs" not in tags and "alpha" in tags


def test_io_blocks_and_panels(live_server, browser_page):  # noqa: F811
    page = browser_page
    page.set_viewport_size({"width": 1440, "height": 900})
    _world, _base = _open(live_server, page, "member")
    page.wait_for_selector("#comments", state="attached")
    io = page.locator(".io").first
    if io.count():
        css = io.locator(".io-b").evaluate(
            "e => { const s = getComputedStyle(e); return {bw: s.borderTopWidth, rad: s.borderTopLeftRadius, pad: s.paddingTop, bg: s.backgroundColor}; }"
        )
        assert css["bw"] == "1px" and css["rad"] == "10px" and css["pad"] == "12px", css
        lab, btn = io.locator(".io-h h3").evaluate(BOX), io.locator(".io-h .copy").evaluate(BOX)
        assert btn["l"] > lab["r"]
    page.get_by_role("tab", name="Scores").click()
    cols = page.locator(".score-row .input, .score-row .select, .score-row .btn").evaluate_all(
        "els => els.map(e => { const r = e.getBoundingClientRect(); return [Math.round(r.top), Math.round(r.height)]; })"
    )
    assert (
        len(cols) == 4
        and len({h for _t, h in cols}) == 1
        and max(t for t, _ in cols) - min(t for t, _ in cols) <= 1
    ), cols
    page.fill("#score-name", "quality")
    page.fill("#score-value", "0.9")
    page.get_by_role("button", name="Add score").click()
    page.wait_for_selector("#scores .score:has-text('quality')")
    page.get_by_role("tab", name="Comments").click()
    payload = "<img src=x onerror=alert(1)><b>bold</b>"
    page.fill("#comment-body", payload)
    page.get_by_role("button", name="Comment", exact=True).click()
    page.wait_for_selector("#comments .comment")
    assert page.locator("#comments img").count() == 0
    assert [p for p in page.problems if p.startswith(("pageerror", "dialog"))] == []
