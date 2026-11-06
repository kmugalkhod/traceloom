"""UI polish pass, traces list and trace detail (AC-1..AC-6, AC-10): measured in a real browser at
fixed viewports, after fonts are ready, with the spec tolerances."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tests.e2e.live_server import browser_page, live_server, make_world, sign_in  # noqa: F401
from tests.factories import DEFAULT_PASSWORD
from tests.helpers_web import seed_trace
from traceloom.ports import ScoreRow

pytestmark = pytest.mark.e2e

BOX = """
(sel) => { const e = document.querySelector(sel); if (!e) { return null; }
  const r = e.getBoundingClientRect();
  return { x: r.x, y: r.y, w: r.width, h: r.height, r: r.right, b: r.bottom }; }
"""
STYLE = """
([sel, props]) => { const e = document.querySelector(sel); if (!e) { return null; }
  const cs = getComputedStyle(e); const out = {};
  for (const p of props) { out[p] = cs[p]; } return out; }
"""
PROBLEMS_IGNORED = ("403", "404")


def seed(container, project_id):
    now = datetime.now(UTC)
    seed_trace(
        container, project_id, "t-ok", timestamp=now - timedelta(minutes=5), name="checkout",
        user_id="usr_ok", session_id="sess-1", tags=["alpha", "beta"], release="2026.11.3",
        version="7", environment="default", cost=0.0123, tokens=900, duration_ms=1200,
        scores=[
            ScoreRow(
                id="s1", trace_id="t-ok", name="quality", value=0.9, data_type="NUMERIC",
                source="ANNOTATION", timestamp=now,
            )
        ],
    )  # fmt: skip
    seed_trace(
        container, project_id, "t-err", timestamp=now - timedelta(minutes=9), name="refund",
        user_id="usr_err", level="ERROR", cost=0.0456, tokens=1500, duration_ms=2400,
    )  # fmt: skip


def open_page(live_server, page, role, path, size=(1440, 900)):  # noqa: F811
    world = make_world(live_server)
    seed(live_server.container, world["project_id"])
    sign_in(page, live_server, world[role], DEFAULT_PASSWORD)
    page.set_viewport_size({"width": size[0], "height": size[1]})
    page.goto(f"{live_server.url}/project/{world['project_id']}{path}")
    page.evaluate("document.fonts.ready")
    return world


def no_console_errors(page):
    assert [p for p in page.problems if not any(c in p for c in PROBLEMS_IGNORED)] == []


# ---- traces list -------------------------------------------------------------------------------


def test_list_toolbar_is_one_row_of_36px_controls(live_server, browser_page):  # noqa: F811
    page = browser_page
    open_page(live_server, page, "viewer", "/traces")
    page.locator("#trace-rows tr").first.wait_for()
    tray = page.evaluate(BOX, "#filters .tbar .seg")
    search = page.evaluate(BOX, "#filters .input-wrap")
    button = page.evaluate(BOX, "#filter-wrap > .btn")
    for box in (tray, search, button):
        assert abs(box["h"] - 36) <= 1, box
    assert (
        max(b["y"] for b in (tray, search, button)) - min(b["y"] for b in (tray, search, button))
        <= 1
    )
    assert search["w"] >= 280
    no_console_errors(page)


def test_list_numbers_rows_scores_and_error_row(live_server, browser_page):  # noqa: F811
    page = browser_page
    open_page(live_server, page, "viewer", "/traces")
    page.locator("#trace-rows tr").nth(1).wait_for()
    for sel in (
        "#trace-rows tr td.num.r:nth-of-type(7)",
        "#trace-rows tr td.c-cost",
        "table.tbl thead th.r:nth-of-type(7)",
    ):
        style = page.evaluate(STYLE, [sel, ["textAlign", "fontVariantNumeric", "fontFamily"]])
        assert style["textAlign"] == "right", (sel, style)
        assert "tabular-nums" in style["fontVariantNumeric"], (sel, style)
        assert "mono" not in style["fontFamily"].lower(), (sel, style)
    heads = page.evaluate(
        "[...document.querySelectorAll('table.tbl thead th')].map(t => [t.textContent.trim(),"
        " getComputedStyle(t).textAlign])"
    )
    by_name = dict(heads)
    assert by_name["Latency"] == "right" and by_name["Cost"] == "right"
    assert by_name["Scores"] == "center"
    # Two-line rows (muted second line) stay between the 44px base and the 56px cap.
    for box in page.evaluate(
        "[...document.querySelectorAll('#trace-rows tr')]"
        ".map(r => r.getBoundingClientRect().height)"
    ):
        assert 43 <= box <= 56, box
    # Scores cells: centred, with a muted dash when empty.
    cells = page.evaluate(
        "[...document.querySelectorAll('#trace-rows tr')].map(r => { const c = r.querySelector("
        "'td.c'); return [r.className, c.textContent.trim(), getComputedStyle(c).textAlign]; })"
    )
    assert all(align == "center" for _, _, align in cells)
    assert [text for cls, text, _ in cells if "ERROR" in cls] == ["—"]
    # AC-3: the error row is not tinted and its numbers keep the normal ink; marker and pill stay.
    rows = page.evaluate(
        """() => [...document.querySelectorAll('#trace-rows tr')].map(r => {
             const td = r.querySelectorAll('td')[2], num = r.querySelector('td.num');
             return { cls: r.className, bg: getComputedStyle(td).backgroundColor,
                      ink: getComputedStyle(num).color,
                      marker: getComputedStyle(r.querySelector('td.lvl'), '::before')
                                .backgroundColor,
                      pill: !!r.querySelector('.lv.ERROR') }; })"""
    )
    err = next(r for r in rows if "ERROR" in r["cls"])
    ok = next(r for r in rows if "ERROR" not in r["cls"])
    assert err["bg"] == ok["bg"] and err["ink"] == ok["ink"]
    assert err["pill"] and err["marker"] not in ("rgba(0, 0, 0, 0)", "transparent")
    assert ok["marker"] in ("rgba(0, 0, 0, 0)", "transparent")
    no_console_errors(page)


def test_list_fits_390px_and_tray_stays_in_its_row(live_server, browser_page):  # noqa: F811
    page = browser_page
    open_page(live_server, page, "viewer", "/traces", size=(390, 844))
    page.locator("#trace-rows tr").first.wait_for()
    assert page.evaluate(
        "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
    )
    assert page.evaluate(
        "(() => { const t = document.querySelector('#filters .seg');"
        " return t.scrollWidth <= t.clientWidth; })()"
    )
    no_console_errors(page)


# ---- trace detail ------------------------------------------------------------------------------


def test_detail_header_breadcrumb_and_split(live_server, browser_page):  # noqa: F811
    page = browser_page
    open_page(live_server, page, "viewer", "/traces/t-ok")
    page.locator(".split .node").first.wait_for()
    head = page.evaluate(BOX, ".th-head")
    assert head["h"] <= 112
    tops = page.evaluate(
        "[...document.querySelectorAll('.mline-row .mline dd')]"
        ".map(e => e.getBoundingClientRect().top)"
    )
    assert len(tops) >= 5 and max(tops) - min(tops) <= 2, tops
    # AC-5: the breadcrumb sits in the context bar, and the title starts close under it.
    assert page.locator(".ctxbar nav[aria-label=Breadcrumb]").count() == 1
    assert page.locator(".th-head nav, .main nav[aria-label=Breadcrumb]").count() == 0
    bar, title = page.evaluate(BOX, ".ctxbar"), page.evaluate(BOX, ".th-head h1")
    assert 0 <= title["y"] - bar["b"] <= 24 + 1
    # AC-6: equal pane tops, 40% tree, a page that does not scroll, aligned duration column.
    tree, detail, split = (page.evaluate(BOX, s) for s in (".tree-pane", ".dpane", ".split"))
    assert abs(tree["y"] - detail["y"]) <= 1
    assert abs(tree["w"] / split["w"] - 0.40) <= 0.02, (tree, split)
    assert page.evaluate("document.documentElement.scrollHeight <= innerHeight + 1")
    for pane in (".tree-pane .panel-h", ".dpane .panel-h"):
        assert abs(page.evaluate(BOX, pane)["h"] - 52) <= 1
    edges = page.evaluate(
        "[...document.querySelectorAll('.node .dur')].map(e => e.getBoundingClientRect().right)"
    )
    assert len(edges) >= 2 and max(edges) - min(edges) <= 1, edges
    rows = page.evaluate(
        "[...document.querySelectorAll('.node')].map(e => e.getBoundingClientRect().height)"
    )
    assert all(abs(h - 36) <= 1 for h in rows)
    assert page.locator(".tree-pane-h kbd, .tree-pane-h .hint").count() == 0
    no_console_errors(page)


def test_detail_tabs_follow_arrow_keys(live_server, browser_page):  # noqa: F811
    page = browser_page
    open_page(live_server, page, "viewer", "/traces/t-ok")
    page.get_by_role("tab", name="Preview").focus()
    names = []
    for _ in range(4):
        page.keyboard.press("ArrowRight")
        names.append(
            page.locator("#io-pane [data-tabs] [aria-selected=true]").inner_text().split("\n")[0]
        )
    assert [n.strip() for n in names] == ["Metadata", "Scores", "Comments", "Preview"]
    no_console_errors(page)


def test_detail_panes_stack_tree_first_at_390px(live_server, browser_page):  # noqa: F811
    page = browser_page
    open_page(live_server, page, "viewer", "/traces/t-ok", size=(390, 844))
    page.locator(".split .node").first.wait_for()
    tree, detail = page.evaluate(BOX, ".tree-pane"), page.evaluate(BOX, ".dpane")
    assert tree["b"] <= detail["y"] and abs(tree["x"] - detail["x"]) <= 1
    assert page.evaluate(
        "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
    )
    no_console_errors(page)


# ---- tags, Viewer and Member -------------------------------------------------------------------

TAG_CHIPS = "#trace-tags .chip"


def chip_texts(page):
    return page.evaluate(
        "[...document.querySelectorAll('#trace-tags .chip')].map(c => c.textContent.trim())"
    )


def test_viewer_sees_tag_chips_only(live_server, browser_page):  # noqa: F811
    page = browser_page
    open_page(live_server, page, "viewer", "/traces/t-ok")
    page.locator(TAG_CHIPS).first.wait_for()
    assert chip_texts(page) == ["alpha", "beta"]
    assert page.locator("#trace-tags button, #trace-tags input").count() == 0
    assert "Edit tags:" not in page.content()
    no_console_errors(page)


def test_member_adds_and_removes_a_tag_inline(live_server, browser_page):  # noqa: F811
    page = browser_page
    open_page(live_server, page, "member", "/traces/t-ok")
    page.locator("#trace-tags button[aria-label='Remove tag alpha']").wait_for()
    assert "Edit tags:" not in page.content()
    assert page.locator("#trace-tags .chip", has_text="alpha").count() == 1
    page.fill("#new-tag", "gamma")
    page.press("#new-tag", "Enter")
    page.locator("#trace-tags .chip", has_text="gamma").wait_for()
    assert page.locator("#trace-tags .chip", has_text="gamma").count() == 1
    page.locator("#trace-tags button[aria-label='Remove tag gamma']").click()
    page.locator("#trace-tags button[aria-label='Remove tag gamma']").wait_for(state="detached")
    assert "gamma" not in " ".join(chip_texts(page))
    no_console_errors(page)
