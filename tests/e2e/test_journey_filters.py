"""Journey 3: search, metadata filter and date range in the URL, column choice, saved views."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e


def row_ids(page) -> list[str]:
    hrefs = page.locator("tbody#trace-rows tr[data-row]").evaluate_all(
        "rows => rows.map(r => r.dataset.href)"
    )
    return [h.rsplit("/", 1)[1] for h in hrefs]


def test_filters_columns_and_saved_views(live, session, events):
    page, a = session.page, live.account
    batch = []
    batch += events(1, output="the invoice was paid", region="eu")
    batch += events(2, output="a zebra crossed the road", region="eu", minutes_ago=10)
    batch += events(3, output="nothing to see", region="us", minutes_ago=20)
    batch += events(
        4, output="ancient history", region="eu", minutes_ago=60 * 24 * 3
    )  # outside 24 h
    live.ingest(batch)

    list_url = f"{live.url}/project/{a.project_id}/traces"
    page.goto(list_url)
    assert row_ids(page) == ["trace-001", "trace-002", "trace-003"]  # default range is 24 h

    # Search for a word that appears in only one output; all state lands in the URL.
    page.fill("input[name=q]", "zebra")
    page.press("input[name=q]", "Enter")
    page.wait_for_load_state()
    assert "q=zebra" in page.url and row_ids(page) == ["trace-002"]

    # Add the metadata filter from the filter panel, plus the 24 h range.
    page.goto(list_url)
    page.click("button[data-menu-toggle=filter-menu]")
    page.fill("#f-mkey", "region")
    page.fill("#f-mval", "eu")
    page.click("button:has-text('Apply filters')")
    page.wait_for_load_state()
    assert "metadata.region=eu" in page.url
    assert row_ids(page) == ["trace-001", "trace-002"]
    assert page.locator(".chip.accent", has_text="metadata.region = eu").count() == 1
    page.click("a:has-text('7d')")
    page.wait_for_load_state()
    assert "range=7d" in page.url and row_ids(page) == ["trace-001", "trace-002", "trace-004"]
    filtered_url = page.url
    page.reload()
    assert page.url == filtered_url and row_ids(page) == [
        "trace-001",
        "trace-002",
        "trace-004",
    ]  # reload restores it

    # Hide a column and reload: the choice is remembered for this user and project.
    assert page.locator("th", has_text="Latency").count() == 1
    page.click("button:has-text('Columns')")
    page.locator("label.item.check", has_text="Latency").click()
    page.wait_for_load_state()
    assert page.locator("th", has_text="Latency").count() == 0
    page.goto(list_url)
    assert page.locator("th", has_text="Latency").count() == 0

    # Save the filtered list as a view, then apply it from a fresh page.
    page.goto(filtered_url)
    page.click("button:has-text('Save view')")
    page.fill("#view-name", "EU this week")
    page.click("dialog[open] button:has-text('Save view')")
    page.wait_for_load_state()
    assert "metadata.region=eu" in page.url and "range=7d" in page.url
    page.goto(list_url)
    assert row_ids(page) == ["trace-001", "trace-002", "trace-003"]
    page.click("button:has-text('Saved views')")
    page.click("a[role=menuitem]:has-text('EU this week')")
    page.wait_for_load_state()
    assert page.url == filtered_url and row_ids(page) == ["trace-001", "trace-002", "trace-004"]
    assert session.problems == []


def test_load_more_and_the_environment_switcher(live, session, events):
    page, a = session.page, live.account
    batch = []
    for i in range(60):
        batch += events(i, minutes_ago=1 + i % 50)
    live.ingest(batch)
    page.goto(f"{live.url}/project/{a.project_id}/traces")
    assert len(row_ids(page)) == 50 and "Showing 1–50 of 60" in page.locator("#pager").inner_text()
    page.click("#pager a:has-text('Load more')")
    expect(page.locator("tbody#trace-rows tr[data-row]")).to_have_count(
        60
    )  # polls; no eval under the CSP
    ids = row_ids(page)
    assert len(ids) == 60 and len(set(ids)) == 60  # no duplicates, no URL change needed
    assert "Showing 1–60 of 60" in page.locator("#pager").inner_text()
    assert page.locator("#pager a").count() == 0
    page.click("button[data-menu-toggle=env-menu]")
    assert re.search(r"All environments", page.locator("#env-menu").inner_text())
    assert session.problems == []
