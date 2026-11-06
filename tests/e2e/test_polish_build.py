"""UI polish pass, Prompts and Playground (AC-9): centred empty states, label baselines on the
new-prompt form, 52px panel headers, 36px Run-with controls and aligned playground panels."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from traceloom.auth.permissions import Role
from traceloom.db.models import Project, User
from traceloom.llm import connections, providers
from traceloom.llm.connections import ConnectionPolicy
from traceloom.llm.providers import BaseProvider, DoneDelta, TextDelta
from traceloom.prompts.service import Actor

pytestmark = pytest.mark.e2e

BOXES_JS = """
(sel) => [...document.querySelectorAll(sel)].map((e) => {
  const r = e.getBoundingClientRect();
  return { x: r.x, y: r.y, w: r.width, h: r.height, r: r.right, b: r.bottom };
})
"""


class Quiet(BaseProvider):
    def _stream(self, *args):
        yield TextDelta("ok")
        yield DoneDelta("stop")


def project_url(live, path):
    return f"{live.url}/project/{live.account.project_id}/{path}"


def open_page(session, url):
    page = session.page
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(url)
    page.evaluate("document.fonts.ready")
    return page


def assert_centred(page, panel_sel):
    panel = page.evaluate(BOXES_JS, panel_sel)[0]
    empty = page.evaluate(BOXES_JS, f"{panel_sel} .empty")[0]
    assert empty["w"] <= 521, empty
    left, right = empty["x"] - panel["x"], panel["r"] - empty["r"]
    assert abs(left - right) <= 4, (left, right)
    return empty


def test_empty_states_centred(session, live):
    page = open_page(session, project_url(live, "prompts"))
    page.locator(".empty").wait_for()
    empty = assert_centred(page, ".panel:has(> .empty)")
    action = page.evaluate(BOXES_JS, ".empty .acts .btn")[0]
    snippet = page.evaluate(BOXES_JS, ".empty #sdk-snippet")[0]
    assert snippet["y"] >= action["b"] and snippet["r"] <= empty["r"] + 1  # snippet under actions

    page = open_page(session, project_url(live, "playground"))
    page.locator(".empty").wait_for()
    assert_centred(page, ".panel:has(> .empty)")
    assert page.locator(".empty .acts .btn").is_visible()
    assert session.problems == []


def test_new_prompt_labels_and_panel_headers(session, live):
    page = open_page(session, project_url(live, "prompts/new"))
    page.locator("#tg").wait_for()
    type_lbl, tags_lbl = page.evaluate(BOXES_JS, "#type-lbl, label[for=tg]")
    assert abs(type_lbl["y"] - tags_lbl["y"]) <= 1 and abs(type_lbl["b"] - tags_lbl["b"]) <= 1
    label = page.evaluate(BOXES_JS, "label[for=pn]")[0]
    field = page.evaluate(BOXES_JS, "#pn")[0]
    assert abs(field["y"] - label["b"] - 6) <= 1  # 6px between label and field
    seg, tags = page.evaluate(BOXES_JS, ".field-row .seg, #tg")
    assert abs(seg["h"] - 36) <= 1 and abs(tags["h"] - 36) <= 1
    heads = page.evaluate(BOXES_JS, ".editor .panel > .panel-h, .editor .cfg > summary")
    assert len(heads) >= 4 and all(abs(h["h"] - 52) <= 1 for h in heads), heads
    assert session.problems == []


def test_playground_run_with_row_and_panel_tops(session, live):
    providers.register("anthropic", Quiet)
    try:
        with live.container.session_factory() as db:
            owner = db.scalar(select(User).where(User.email == live.account.email))
            project = db.get(Project, live.account.project_id)
            connections.create(
                db, project, Actor.for_user(owner, Role.OWNER),
                ConnectionPolicy.from_settings(live.container.settings),
                provider="anthropic", name="Claude fake", secrets={"api_key": "sk-ant-fake-123456"},
            )  # fmt: skip
            db.commit()
        page = open_page(session, project_url(live, "playground"))
        page.locator("#pg-connection option").first.wait_for(state="attached")
        controls = page.evaluate(BOXES_JS, ".pgtool .select, .pgtool .input")
        assert len(controls) == 2 and all(abs(c["h"] - 36) <= 1 for c in controls), controls
        prompt, response = page.evaluate(
            BOXES_JS, "#pg-grid > .panel[aria-label=Prompt], #pg-grid > .panel[aria-label=Response]"
        )  # fmt: skip
        assert abs(prompt["y"] - response["y"]) <= 1
        heads = page.evaluate(BOXES_JS, "#pg-grid > .panel > .panel-h")
        assert len(heads) == 2 and all(abs(h["h"] - 52) <= 1 for h in heads), heads
        assert session.problems == []
    finally:
        providers.unregister("anthropic")
