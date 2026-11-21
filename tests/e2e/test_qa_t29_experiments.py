"""T-29 QA journey: New experiment dialog, live run progress, trace links, escaping, roles."""

from __future__ import annotations

import re
import time

import pytest
from playwright.sync_api import expect

from tests.api.eval_helpers import make_connection
from tests.e2e.live_server import browser_page, live_server, make_world, sign_in  # noqa: F401
from tests.factories import DEFAULT_PASSWORD
from traceloom.auth.permissions import Role
from traceloom.db.models import Project, User
from traceloom.db.models_datasets import Dataset, DatasetItem, DatasetItemVersion
from traceloom.evals import templates
from traceloom.llm import providers
from traceloom.llm.providers import DoneDelta, TextDelta, UsageDelta
from traceloom.prompts import service as prompts

pytestmark = pytest.mark.e2e

XSS = "<script>alert(1)</script>"


class SlowFake:
    name = "fake"

    def __call__(self, config):
        return self

    def stream(self, messages, model, params=None, tools=None, response_schema=None):
        time.sleep(0.4)
        if response_schema is not None:
            yield TextDelta('{"score": 0.9, "reasoning": "close"}')
        else:
            yield TextDelta(f"answer {XSS}")
        yield UsageDelta(10, 5)
        yield DoneDelta("stop")


class _W:
    def __init__(self, project):
        self.project = project


def _seed(live):
    world = make_world(live)
    with live.container.session_factory() as db:
        templates.seed_builtins(db)
        project = db.get(Project, world["project_id"])
        member = db.query(User).filter_by(email=world["member"]).one()
        make_connection(db, _W(project), live.container).custom_models = ["gpt-4o"]
        ds = Dataset(project_id=project.id, name="qa-20")
        db.add(ds)
        db.flush()
        for n in range(20):
            item = DatasetItem(dataset_id=ds.id, project_id=project.id)
            db.add(item)
            db.flush()
            db.add(
                DatasetItemVersion(
                    item_id=item.id, version=1, project_id=project.id,
                    input={"q": XSS if n == 0 else f"question {n}"},
                    expected_output={"a": f"a{n}"}, metadata_={}, status=item.status,
                )
            )  # fmt: skip
        actor = prompts.Actor.for_user(member, Role.MEMBER)
        for text in ("Answer briefly: {{q}}", "Answer in detail: {{q}}"):
            prompts.create_version(
                db, project, actor, name="qa", content=text, commit_message="v", type="text"
            )
        db.commit()
        world["dataset_id"] = ds.id
    return world


def _start(page, name, ref):
    page.click("button:has-text('New experiment')")
    dlg = page.locator("#new-experiment")
    expect(dlg).to_be_visible()
    dlg.locator("#xn").fill(name)
    dlg.locator("#xv").select_option(ref)
    dlg.locator("#xm").select_option(index=0)
    dlg.locator("label.cbl:has-text('orrectness') input").check()
    page.screenshot(path=".sdlc/evidence/t29-dialog-1280.png")
    dlg.locator("button[type=submit]").click()


def test_member_two_experiments_live_progress(live_server, browser_page):  # noqa: F811
    providers.register("fake", SlowFake())
    try:
        world = _seed(live_server)
        page = browser_page
        sign_in(page, live_server, world["member"], DEFAULT_PASSWORD)
        ds_url = f"{live_server.url}/project/{world['project_id']}/datasets/{world['dataset_id']}"
        runs = []
        for name, ref in (("prompt v1", "v:1"), ("prompt v2", "v:2")):
            page.goto(ds_url + "?tab=runs")
            _start(page, name, ref)
            page.wait_for_url(re.compile(r"/runs/"))
            runs.append(page.url)
            first = page.locator("#run-live .bignum").inner_text()
            page.evaluate("window.__m = 1")
            seen = {first}
            deadline = time.time() + 150
            while time.time() < deadline and page.locator("#run-live[hx-get]").count():
                seen.add(page.locator("#run-live .bignum").inner_text())
                time.sleep(0.3)
            assert page.evaluate("window.__m") == 1, "page reloaded"
            assert len(seen) > 1, f"progress never moved: {seen}"
            expect(page.locator("#run-live")).to_contain_text("Run completed")
            expect(page.locator("#run-live .bignum")).to_have_text("20 of 20")
            page.screenshot(path=f".sdlc/evidence/t29-run-{name[-2:]}-1280.png", full_page=True)
            assert page.locator("#run-live .tbl tbody tr").count() == 20
            links = page.locator("#run-live .tbl a:has-text('Trace')")
            assert links.count() == 20
            assert page.locator("#run-live .tbl script").count() == 0
            expect(page.locator("#run-live .tbl")).to_contain_text(XSS)
            expect(page.locator("#run-live .tbl")).to_contain_text("correctness")
        href = links.first.get_attribute("href")
        page.goto(live_server.url + href)
        assert "/traces/" in page.url
        expect(page.locator("body")).to_contain_text("traceloom-experiment")
        page.set_viewport_size({"width": 390, "height": 800})
        page.goto(runs[0])
        page.screenshot(path=".sdlc/evidence/t29-run-390.png", full_page=True)
        assert page.evaluate("document.documentElement.scrollWidth") <= 392
        page.goto(ds_url + "?tab=runs")
        page.screenshot(path=".sdlc/evidence/t29-runs-tab-390.png", full_page=True)
        assert page.evaluate("document.documentElement.scrollWidth") <= 392
        assert not [p for p in page.problems if p.startswith(("pageerror", "dialog"))]
    finally:
        providers.unregister("fake")


def test_viewer_no_start_control_and_403(live_server, browser_page):  # noqa: F811
    providers.register("fake", SlowFake())
    try:
        world = _seed(live_server)
        page = browser_page
        sign_in(page, live_server, world["viewer"], DEFAULT_PASSWORD)
        ds_url = f"{live_server.url}/project/{world['project_id']}/datasets/{world['dataset_id']}"
        page.goto(ds_url + "?tab=runs")
        assert page.locator("button:has-text('New experiment')").count() == 0
        status = page.evaluate(
            """async (url) => {
              const t = document.cookie.match(/(?:^|; )tl_csrf=([^;]*)/)[1];
              const r = await fetch(url, {method: 'POST', redirect: 'manual', headers: {
                'X-CSRF-Token': t, 'Content-Type': 'application/x-www-form-urlencoded'},
                body: 'name=x&prompt=qa&ref=v:1&connection_id=x&model=m'});
              return r.status; }""",
            ds_url + "/experiments",
        )
        assert status == 403
    finally:
        providers.unregister("fake")
