"""Journey 4: a <script> payload is literal text in the list and in all three IO modes."""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e

PAYLOAD = "<script>alert(1)</script>"
IMG = "<img src=x onerror=alert(1)>"


def test_script_payload_is_text_everywhere(live, session, events):
    page, a = session.page, live.account
    live.ingest(
        events(
            1,
            name=PAYLOAD,
            input_=PAYLOAD,
            output=f"{PAYLOAD}\n\n{IMG}",
            tags=(PAYLOAD,),
            user=PAYLOAD,
        )
    )
    base = f"{live.url}/project/{a.project_id}/traces"

    page.goto(base)
    expect(page.locator("tbody#trace-rows")).to_contain_text(PAYLOAD)  # visible as characters
    page.goto(f"{base}?q=alert")  # the search box and the chips echo user input too
    expect(page.locator("tbody#trace-rows tr[data-row]")).to_have_count(1)
    page.goto(f"{base}?tag={PAYLOAD}")
    expect(page.locator("tbody#trace-rows tr[data-row]")).to_have_count(1)

    for selection in ("", "&obs=trace-001-gen"):
        for mode in ("formatted", "json", "markdown"):
            page.goto(f"{base}/trace-001?mode={mode}{selection}")
            pane = page.locator("#io-pane")
            expect(pane).to_contain_text(PAYLOAD)
            assert pane.locator("script, img").count() == 0, (
                f"an element was created in {mode} mode"
            )
    page.wait_for_timeout(300)  # give a bad payload time to fire
    assert [p for p in session.problems if p.startswith("dialog")] == []
    assert session.problems == []
