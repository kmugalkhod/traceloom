"""T-32 QA: score analytics page in a real browser — charts, fallbacks, preset, dark, 390px, escaping."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

from tests.e2e.live_server import browser_page, live_server, make_world, sign_in  # noqa: F401
from tests.factories import DEFAULT_PASSWORD
from traceloom.db.models import Score

pytestmark = pytest.mark.e2e

SHOTS = os.environ.get("QA_SHOT_DIR") or os.path.join(
    os.path.dirname(__file__), "..", "..", ".sdlc", "evidence", "t32"
)


def shot(page, name):
    if SHOTS:
        os.makedirs(SHOTS, exist_ok=True)
        page.screenshot(path=f"{SHOTS}/{name}.png", full_page=True)


def seed(live, project_id, name="correct"):
    now = datetime.now(UTC)
    rows = []
    # 30 paired traces: human rows, judge columns -> tt 17, tf 3, ft 4, ff 6
    i = 0
    for (h, j), count in zip(((1, 1), (1, 0), (0, 1), (0, 0)), (17, 3, 4, 6), strict=True):
        for _ in range(count):
            i += 1
            for sid, src, val in ((f"h{i}", "ANNOTATION", h), (f"j{i}", "EVAL", j)):
                rows.append(Score(
                    project_id=project_id, id=sid, trace_id=f"t{i}", name=name, value=val,
                    data_type="BOOLEAN", source=src, environment="production",
                    timestamp=now - timedelta(days=i % 6, hours=1),
                ))  # fmt: skip
    for k in range(12):
        for sid, src, val in (
            (f"ha{k}", "ANNOTATION", k / 11),
            (f"ea{k}", "EVAL", min(1, k / 11 + 0.1)),
        ):
            rows.append(Score(
                project_id=project_id, id=sid, trace_id=f"n{k}",
                name="helpfulness" if src == "ANNOTATION" else "relevance", value=val,
                data_type="NUMERIC", source=src, environment="production",
                timestamp=now - timedelta(days=k % 6, hours=2),
            ))  # fmt: skip
    for k in range(10):
        rows.append(Score(
            project_id=project_id, id=f"x{k}", trace_id=f"x{k}", name="<img src=x onerror=window.pwned=1>",
            value=0.5, data_type="NUMERIC", source="API", environment="production",
            timestamp=now - timedelta(hours=3),
        ))  # fmt: skip
    with live.container.session_factory() as db:
        db.add_all(rows)
        db.commit()


def test_analytics_browser_pass(live_server, browser_page):  # noqa: F811
    world = make_world(live_server)
    seed(live_server, world["project_id"])
    page = browser_page
    url = f"{live_server.url}/project/{world['project_id']}/scores/analytics"
    sign_in(page, live_server, world["viewer"], DEFAULT_PASSWORD)

    page.goto(url)
    page.wait_for_load_state("networkidle")
    shot(page, "1280-default")
    body = page.content()
    assert "Confusion matrix" in body and "n = 30 matched traces" in body
    assert "77%" in body and "23 of 30 match" in body and "0.46" in body
    assert page.evaluate("window.pwned") is None
    assert page.locator("img[src=x]").count() == 0  # score name is escaped
    assert "&lt;img src=x" in body
    # charts rendered by uPlot
    page.wait_for_selector(".an-chart .uplot", timeout=5000)
    # table fallback
    page.locator("details.an-tbl summary").first.click()
    assert page.locator("details.an-tbl[open] table").count() >= 1

    # date range switch
    page.goto(url + "?range=24h")
    shot(page, "1280-24h")
    page.goto(url + "?range=7d")

    # numeric compare
    page.goto(url + "?a=helpfulness&as=ANNOTATION&b=relevance&bs=EVAL")
    page.wait_for_load_state("networkidle")
    assert "Pearson r" in page.content()
    shot(page, "1280-numeric")

    # dark mode + mobile
    page.emulate_media(color_scheme="dark")
    page.goto(url)
    page.wait_for_load_state("networkidle")
    shot(page, "1280-dark")
    page.emulate_media(color_scheme="light")
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(url)
    page.wait_for_load_state("networkidle")
    shot(page, "390")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert [p for p in page.problems if p.startswith(("pageerror", "dialog"))] == []
    assert page.problems == [], page.problems
