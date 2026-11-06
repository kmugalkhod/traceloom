"""Dashboard journey: charts draw under the CSP, the filters work, 390px dark mode holds."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tests.e2e.live_server import browser_page, live_server, make_world, sign_in  # noqa: F401
from tests.factories import DEFAULT_PASSWORD
from traceloom.ports import ObservationRow, TraceRow

pytestmark = pytest.mark.e2e

CONTRAST_JS = """
(selectors) => {
  const parse = (c) => c.match(/[\\d.]+/g).map(Number);
  const lum = ([r, g, b]) => {
    const f = (v) => {
      v /= 255;
      return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
    };
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
  };
  const backdrop = (el) => {
    for (let e = el; e; e = e.parentElement) {
      const c = parse(getComputedStyle(e).backgroundColor);
      if (c.length < 4 || c[3] > 0.9) { return c.slice(0, 3); }
    }
    return [255, 255, 255];
  };
  return selectors.map((s) => {
    const el = document.querySelector(s);
    if (!el) { return [s, null]; }
    const fg = parse(getComputedStyle(el).color).slice(0, 3), bg = backdrop(el);
    const [a, b] = [lum(fg), lum(bg)].sort((x, y) => y - x);
    return [s, (a + 0.05) / (b + 0.05)];
  });
}
"""


def seed(container, project_id):
    now = datetime.now(UTC)
    for i, (user, model, cost) in enumerate(
        [("alice", "model-a", 0.5), ("bob", "model-b", 0.2), ("alice", "model-a", 0.3)]
    ):
        start = now - timedelta(hours=i + 1)
        trace = TraceRow(id=f"t{i}", timestamp=start, name="chat", user_id=user, event_ts=start)
        obs = ObservationRow(
            id=f"t{i}-o", trace_id=f"t{i}", start_time=start, end_time=start + timedelta(seconds=2),
            type="generation", model=model, usage_details={"total": 1000},
            cost_details={"total": cost},
            level="ERROR" if i == 1 else "DEFAULT", event_ts=start,
        )  # fmt: skip
        container.trace_store.upsert(project_id, [trace], [obs], [])
        container.trace_store.refresh_trace_aggregates(project_id, [f"t{i}"])


def test_dashboard_charts_filters_and_mobile(live_server, browser_page):  # noqa: F811
    world = make_world(live_server)
    seed(live_server.container, world["project_id"])
    page = browser_page
    sign_in(page, live_server, world["viewer"], DEFAULT_PASSWORD)
    base = f"{live_server.url}/project/{world['project_id']}/dashboard"

    page.goto(base)
    page.wait_for_selector(".chart-box .uplot")  # uPlot drew its canvas
    assert page.locator(".chart-box canvas").count() >= 1
    widths = page.eval_on_selector_all(".hbar .trk i", "els => els.map(e => e.style.width)")
    assert widths and all(w.endswith("%") for w in widths) and "100%" in widths
    assert page.locator(".stat b").first.inner_text() == "3"
    assert [
        p for p in page.problems if "403" not in p and "404" not in p
    ] == []  # no CSP/console errors

    with page.expect_navigation():
        page.get_by_role("button", name="7d").click()
    assert "range=7d" in page.url
    page.get_by_role("button", name="7d").and_(page.locator("[aria-pressed=true]")).wait_for()
    page.goto(base + "?env=staging")
    assert "No traces" in page.content()  # the environment filter reaches every panel

    # 390px, dark, no horizontal scroll, readable text in both themes
    page.set_viewport_size({"width": 390, "height": 800})
    for scheme in ("dark", "light"):
        page.emulate_media(color_scheme=scheme)
        page.goto(base)
        page.wait_for_selector(".chart-box .uplot")
        assert page.evaluate(
            "document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1"
        )
        for selector, ratio in page.evaluate(
            CONTRAST_JS, [".stat .sub", ".panel-h h2", ".stat b", ".hint"]
        ):
            assert ratio is None or ratio >= 4.5, (scheme, selector, ratio)
    assert [p for p in page.problems if "403" not in p and "404" not in p] == []
