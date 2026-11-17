"""T-29 QA: the progress bar fill is visible once a run completes."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import expect

from tests.e2e.live_server import browser_page, live_server, sign_in  # noqa: F401
from tests.e2e.test_qa_t29_experiments import SlowFake, _seed, _start
from tests.factories import DEFAULT_PASSWORD
from traceloom.llm import providers

pytestmark = pytest.mark.e2e


def test_progress_bar_fill_visible_when_complete(live_server, browser_page):  # noqa: F811
    providers.register("fake", SlowFake())
    try:
        world = _seed(live_server)
        page = browser_page
        sign_in(page, live_server, world["member"], DEFAULT_PASSWORD)
        page.goto(
            f"{live_server.url}/project/{world['project_id']}/datasets/{world['dataset_id']}?tab=runs"
        )
        _start(page, "bar", "v:1")
        page.wait_for_url(re.compile(r"/runs/"))
        expect(page.locator("#run-live")).to_contain_text("Run completed", timeout=150000)
        info = page.evaluate(
            """() => { const d = document.querySelector('.prog .d');
              const r = d.getBoundingClientRect(); const p = document.querySelector('.prog').getBoundingClientRect();
              const c = getComputedStyle(d);
              return {w: r.width, pw: p.width, h: r.height, bg: c.backgroundColor, disp: c.display,
                width: c.width, pdisp: getComputedStyle(d.parentElement).display,
                pbox: getComputedStyle(d.parentElement).boxSizing}; }"""
        )
        print(info, page.evaluate("document.querySelector('.prog').outerHTML"))
        assert info["w"] > info["pw"] * 0.9, info
        assert info["bg"] != "rgba(0, 0, 0, 0)", info
    finally:
        providers.unregister("fake")
