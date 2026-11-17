"""T-19 QA: import wizard preview and Add to dataset dialog at 390px — no sideways page scroll."""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

from tests.e2e.test_qa_t19_dataset_import_add import csv_bytes
from traceloom.auth.permissions import Role
from traceloom.datasets import service
from traceloom.datasets.service import Actor
from traceloom.db.models import Project, User

pytestmark = pytest.mark.e2e


def overflow(page) -> int:
    return page.evaluate(
        "document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )


def test_import_wizard_and_dialog_fit_390px(live, session, events, tmp_path):
    page, a = session.page, live.account
    with live.container.session_factory() as db:
        project = db.get(Project, a.project_id)
        user = db.query(User).filter_by(email=a.email).one()
        ds = service.create_dataset(db, project, Actor.for_user(user, Role.OWNER), name="mobile")
        db.commit()
        ds_id = ds.id
    page.set_viewport_size({"width": 390, "height": 844})
    f = tmp_path / "m.csv"
    f.write_bytes(csv_bytes())
    page.goto(f"{live.url}/project/{a.project_id}/datasets/{ds_id}/import")
    assert overflow(page) <= 0
    page.set_input_files("#import-file", str(f))
    page.click("form[data-import-drop] button[type=submit]")
    expect(page.locator("body")).to_contain_text("20 rows")
    page.select_option("select[name=use_0]", "input")
    page.select_option("select[name=use_3]", "ignore")
    page.screenshot(path=".sdlc/evidence/t19-map-390.png", full_page=True)
    assert overflow(page) <= 0
    page.click("button:has-text('Preview')")
    expect(page.locator("body")).to_contain_text("first 5 of 20 rows")
    page.screenshot(path=".sdlc/evidence/t19-preview-390.png", full_page=True)
    assert overflow(page) <= 0

    live.ingest(events(1))
    page.goto(f"{live.url}/project/{a.project_id}/traces/trace-001?obs=trace-001-gen")
    page.click("[data-add-to-dataset]")
    expect(page.locator("#add-to-dataset")).to_be_visible()
    page.screenshot(path=".sdlc/evidence/t19-add-dialog-390.png")
    assert overflow(page) <= 0
