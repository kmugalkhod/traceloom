"""Trace detail: metrics share a baseline; a comment draft survives node selection."""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e


def test_header_metrics_share_one_top_offset(live, session, events):
    page, a = session.page, live.account
    live.ingest(events(1))
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{live.url}/project/{a.project_id}/traces/trace-001")
    tops = page.locator(".th-head .mline dd").evaluate_all(
        "els => els.map(e => Math.round(e.getBoundingClientRect().top))"
    )
    assert len(tops) >= 4 and max(tops) - min(tops) <= 2, tops


def test_comment_draft_survives_selecting_another_node(live, session, events):
    page, a = session.page, live.account
    live.ingest(events(1))
    page.goto(f"{live.url}/project/{a.project_id}/traces/trace-001")
    assert page.locator("#io-pane #trace-discussion").count() == 0
    page.get_by_role("tab", name="Comments").click()
    page.fill("#comment-body", "draft text")
    page.locator("[role=treeitem] a[data-obs-link]").nth(1).click()
    expect(page.locator("#io-pane h2")).not_to_have_text("support-agent")
    page.get_by_role("tab", name="Comments").click()
    expect(page.locator("#comment-body")).to_have_value("draft text")
