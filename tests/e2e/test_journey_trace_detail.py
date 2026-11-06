"""Journey 1 (second half): open a trace, switch views, jump to the error, flip the IO modes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e


def deep_trace(events) -> list[dict]:
    """The standard run plus a third level (an embedding under the tool) and a failing tool."""
    batch = events(1, level="ERROR", input_="Where is my order?", output="It shipped yesterday.")
    start = datetime.now(UTC) - timedelta(minutes=5)
    batch.append(
        {
            "id": "trace-001-emb",
            "type": "observation-create",
            "timestamp": (start + timedelta(seconds=2.5)).isoformat(),
            "body": {
                "id": "trace-001-emb",
                "traceId": "trace-001",
                "parentObservationId": "trace-001-tool",
                "name": "embed-query",
                "type": "embedding",
                "startTime": (start + timedelta(seconds=2.1)).isoformat(),
                "endTime": (start + timedelta(seconds=2.5)).isoformat(),
            },
        }
    )
    return batch


def test_first_trace_detail(live, session, events):
    page, a = session.page, live.account
    live.ingest(deep_trace(events))
    page.goto(f"{live.url}/project/{a.project_id}/traces")
    page.click("a.row-link:has-text('support-agent')")
    page.wait_for_load_state()
    assert page.url.endswith("/traces/trace-001")

    # Tree: at least three levels, the failing tool in red.
    levels = page.locator("[role=treeitem]").evaluate_all(
        "els => els.map(e => e.getAttribute('aria-level'))"
    )
    assert max(int(x) for x in levels) >= 3
    expect(page.locator("[role=treeitem].ERROR")).to_have_count(1)
    expect(page.locator("[data-body=tree]")).to_be_visible()

    # `t` flips between tree and timeline; the timeline has Gantt bars and a first-token marker.
    page.keyboard.press("t")
    expect(page.locator("[data-body=timeline]")).to_be_visible()
    expect(page.locator("[data-body=tree]")).to_be_hidden()
    assert (
        page.locator(".tl-svg rect.b").count() == 4 and page.locator(".tl-svg line.t").count() == 1
    )
    page.keyboard.press("t")
    expect(page.locator("[data-body=tree]")).to_be_visible()

    # `e` jumps to the first error and shows its message in the pane.
    page.keyboard.press("e")
    expect(page.locator("[role=treeitem][aria-selected=true]")).to_contain_text("lookup_order")
    expect(page.locator("#io-pane h2")).to_have_text("lookup_order")
    expect(page.locator("#io-pane .banner.err")).to_contain_text("order service timed out")
    assert "obs=trace-001-tool" in page.url

    # A generation: model, usage, and cost with its source; the message list in three modes.
    page.click("[role=treeitem] a:has-text('planner')")
    expect(page.locator("#io-pane h2")).to_have_text("planner")
    pane = page.locator("#io-pane")
    expect(pane).to_contain_text("claude-sonnet-4-5")
    expect(pane).to_contain_text("input 900")
    expect(pane.locator(".src.calc")).to_have_text("calculated")
    expect(pane.locator(".msg.msg-user .txt")).to_have_text("Where is my order?")
    page.click("#io-pane a[role=tab]:has-text('JSON')")
    expect(pane.locator(".code pre").first).to_contain_text(
        '[{"role": "user", "content": "Where is my order?"}]'
    )
    page.click("#io-pane a[role=tab]:has-text('Markdown')")
    expect(pane.locator(".md").first).to_be_visible()
    assert "mode=markdown" in page.url and "obs=trace-001-gen" in page.url

    # The agent graph appears for this trace; clicking a node selects its first observation.
    page.click("button[role=tab]:has-text('Graph')")
    expect(page.locator("[data-body=graph]")).to_be_visible()
    page.locator("[data-body=graph] a", has=page.locator("text=lookup_order")).click()
    expect(page.locator("#io-pane h2")).to_have_text("lookup_order")

    # A reload on the pushed URL restores the same selection.
    page.reload()
    expect(page.locator("#io-pane h2")).to_have_text("lookup_order")
    assert session.problems == []
