"""T-19 QA journey: import wizard, Add to dataset from an observation, bulk add."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import expect

from traceloom.auth.permissions import Role
from traceloom.datasets import service
from traceloom.datasets.service import Actor
from traceloom.db.models import Project, User

pytestmark = pytest.mark.e2e

XSS = "<script>alert(1)</script>"


def csv_bytes(rows=20, bad_row=3):
    lines = ["question,answer,input_json,internal_id"]
    for i in range(1, rows + 1):
        meta = '"{""topic"": refund"' if i == bad_row else f'"{{""topic"": ""t{i}""}}"'
        q = XSS if i == 3 else f"Question {i}"
        lines.append(f'"{q}",Answer {i},{meta},r-{i}')
    return "\n".join(lines).encode()


def test_import_and_add_to_dataset(live, session, events, tmp_path):
    page, a = session.page, live.account
    with live.container.session_factory() as db:
        project = db.get(Project, a.project_id)
        user = db.query(User).filter_by(email=a.email).one()
        ds = service.create_dataset(db, project, Actor.for_user(user, Role.OWNER), name="refunds")
        db.commit()
        ds_id = ds.id
    base = f"{live.url}/project/{a.project_id}/datasets/{ds_id}"

    # --- import wizard
    f = tmp_path / "refunds.csv"
    f.write_bytes(csv_bytes())
    page.goto(base + "/import")
    page.set_input_files("#import-file", str(f))
    page.click("form[data-import-drop] button[type=submit]")
    expect(page.locator("body")).to_contain_text("20 rows")
    page.select_option("select[name=use_0]", "input")
    page.select_option("select[name=use_1]", "expected_output")
    page.select_option("select[name=use_2]", "metadata")
    page.select_option("select[name=use_3]", "ignore")
    page.click("button:has-text('Preview')")
    body = page.locator("body")
    expect(body).to_contain_text("first 5 of 20 rows")
    expect(body).to_contain_text("not valid JSON and will be imported as text")
    expect(body).to_contain_text(XSS)  # literal characters
    assert page.locator("table script").count() == 0
    page.screenshot(path=".sdlc/evidence/t19-preview.png", full_page=True)
    page.click("button:has-text('Import 20 items')")
    expect(page.locator("body")).to_contain_text("Imported 20 items")
    page.screenshot(path=".sdlc/evidence/t19-imported.png", full_page=True)

    # --- add an observation from its detail page
    live.ingest(events(1) + events(2))
    tbase = f"{live.url}/project/{a.project_id}/traces"
    page.goto(f"{tbase}/trace-001?obs=trace-001-gen")
    page.click("[data-add-to-dataset]")
    dlg = page.locator("#add-to-dataset")
    expect(dlg).to_be_visible()
    expect(dlg.locator("#ad-input")).to_have_value(re.compile("question 1"))
    expect(dlg.locator("#ad-expected_output")).to_have_value(re.compile("answer 1"))
    expect(dlg).to_contain_text("trace-001-ge")
    page.screenshot(path=".sdlc/evidence/t19-add-dialog.png")
    dlg.locator("#ad").select_option(label="refunds")
    dlg.locator("button[type=submit]").click()
    page.wait_for_load_state()

    # --- bulk add two traces from the list
    page.goto(tbase)
    boxes = page.locator("[data-bulk-box]")
    expect(boxes).to_have_count(2)
    boxes.nth(0).check()
    boxes.nth(1).check()
    expect(page.locator("[data-bulk-count]")).to_contain_text("2 selected")
    page.click("[data-bulk-go]")
    page.wait_for_load_state()
    dlg = page.locator("#add-to-dataset")
    if dlg.count():
        expect(dlg).to_contain_text("Adding 2 traces")
        dlg.locator("#ad").select_option(label="refunds")
        dlg.locator("button[type=submit]").click()
    else:
        expect(page.locator("body")).to_contain_text("Adding 2 traces")
        page.select_option("#ad", label="refunds")
        page.click("button[type=submit]:has-text('Add items')")
    page.wait_for_load_state()
    page.screenshot(path=".sdlc/evidence/t19-bulk-result.png", full_page=True)

    with live.container.session_factory() as db:
        project = db.get(Project, a.project_id)
        d = service.get_dataset(db, project, ds_id)
        assert service.count_items(db, project, d) == 23  # 20 imported + 1 observation + 2 traces
    assert session.problems == []


def test_dataset_detail_shows_item_count_not_method_repr(live, session):
    a = live.account
    with live.container.session_factory() as db:
        project = db.get(Project, a.project_id)
        user = db.query(User).filter_by(email=a.email).one()
        ds = service.create_dataset(db, project, Actor.for_user(user, Role.OWNER), name="counts")
        db.commit()
        ds_id = ds.id
    session.page.goto(f"{live.url}/project/{a.project_id}/datasets/{ds_id}")
    body = session.page.locator("body")
    expect(body).not_to_contain_text("built-in method")
    expect(body).to_contain_text("0 active items")
