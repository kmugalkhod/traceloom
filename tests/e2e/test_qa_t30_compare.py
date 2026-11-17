"""T-30 QA journey: run comparison page in a real browser (toggle, threshold, sort, 3rd run,
4-run cap, escaping, dark mode, 390px)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.e2e.live_server import browser_page, live_server, make_world, sign_in  # noqa: F401
from tests.factories import DEFAULT_PASSWORD
from traceloom.auth.permissions import Role
from traceloom.datasets import runs, service
from traceloom.datasets.service import Actor
from traceloom.db.base import utcnow
from traceloom.db.models import Project, User
from traceloom.db.models_datasets import DatasetItem
from traceloom.ports import ScoreRow

pytestmark = pytest.mark.e2e

EVIDENCE = Path(__file__).resolve().parents[2] / ".sdlc" / "evidence" / "t30"
XSS = "<script>alert(1)</script>"
# v1 is flat 0.5. v2 wins on average but items 2, 5, 7 regress by 0.05 / 0.2 / 0.4.
V1 = [0.5] * 10
V2 = [0.9, 0.9, 0.45, 0.9, 0.9, 0.3, 0.9, 0.1, 0.9, 0.9]
V3 = [0.6] * 10


def _seed(live, world):
    with live.container.session_factory() as db:
        project = db.get(Project, world["project_id"])
        user = db.query(User).filter_by(email=world["member"]).one()
        actor = Actor.for_user(user, Role.MEMBER)
        ds = service.create_dataset(db, project, actor, name="compare-me")
        items = [
            service.create_item(
                db,
                project,
                actor,
                ds,
                input=XSS if n == 0 else f"question-{n}",
                expected_output=f"expected-{n}",
            )  # fmt: skip
            for n in range(10)
        ]
        db.commit()
        dataset = service.get_dataset(db, project, ds.id)
        made = {}
        for name, values in (("v1", V1), ("v2", V2), ("v3", V3), ("v4", V3), ("v5", V3)):
            run = runs.create_run(db, project, actor, dataset, name=name)
            links = []
            for n, (item, value) in enumerate(zip(items, values, strict=True)):
                tid = f"{name}-{n}"
                out = XSS if (name == "v2" and n == 0) else f"output {name} {n}"
                runs.link_run_item(
                    db, project, actor, run, db.get(DatasetItem, item.id),
                    trace_id=tid, output=out, latency_ms=1200.0, cost=0.01,
                )  # fmt: skip
                links.append((tid, value))
            db.commit()
            for tid, value in links:
                live.container.trace_store.add_score(
                    project.id,
                    ScoreRow(
                        id=f"s-{tid}", name="correct", timestamp=utcnow(), trace_id=tid,
                        value=value, data_type="NUMERIC", source="EVAL", dataset_run_id=run.id,
                    ),
                )  # fmt: skip
            made[name] = run.id
        return ds.id, made


def _rows(page):
    return page.locator("tr[data-reg]")


def test_compare_journey(live_server, browser_page):  # noqa: F811
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    page = browser_page
    world = make_world(live_server)
    did, r = _seed(live_server, world)
    sign_in(page, live_server, world["viewer"], DEFAULT_PASSWORD)
    base = f"{live_server.url}/project/{world['project_id']}/datasets/{did}/compare"

    # 1. two runs: best marker per score, baseline first, all 10 items
    page.set_viewport_size({"width": 1280, "height": 900})
    page.goto(f"{base}?runs={r['v1']},{r['v2']}")
    agg = page.locator("section[aria-label=Aggregates]")
    expect(agg).to_contain_text("Baseline")
    assert agg.locator("td.best-c").count() == 1
    expect(agg.locator("td.best-c")).to_contain_text("Best")
    assert _rows(page).count() == 10
    page.screenshot(path=str(EVIDENCE / "1280-two-runs.png"), full_page=True)

    # 2. escaped input/output: no script element, literal text shown
    assert page.locator("#cmpItems script").count() == 0
    expect(page.locator("#cmpItems")).to_contain_text(XSS)

    # 3. only regressed lists exactly the 3 regressed items (UI toggle)
    page.click("#regT")
    page.wait_for_url(re.compile("regressed=1"))
    assert _rows(page).count() == 3
    text = page.locator("#cmpItems").inner_text()
    for n in (2, 5, 7):
        assert f"question-{n}" in text
    for n in (1, 3, 4, 6, 8, 9):
        assert f"question-{n}" not in text
    page.screenshot(path=str(EVIDENCE / "1280-only-regressed.png"), full_page=True)

    # 4. raising the threshold hides small deltas (0.05 gone at 0.1, 0.2 gone at 0.3)
    page.fill("#th", "0.1")
    page.keyboard.press("Enter")
    page.wait_for_url(re.compile("threshold=0.1"))
    assert _rows(page).count() == 2
    page.fill("#th", "0.3")
    page.keyboard.press("Enter")
    page.wait_for_url(re.compile("threshold=0.3"))
    assert _rows(page).count() == 1
    expect(page.locator("#cmpItems")).to_contain_text("question-7")
    page.goto(f"{base}?runs={r['v1']},{r['v2']}&regressed=1&threshold=0.5")
    expect(page.locator("body")).to_contain_text("No items regressed by more than 0.5")

    # 5. sort by delta: worst first
    page.goto(f"{base}?runs={r['v1']},{r['v2']}&sort=delta")
    order = [int(n) for n in re.findall(r"question-(\d)", page.locator("#cmpItems").inner_text())]
    assert order[:3] == [7, 5, 2]

    # 6. third run + per-run regression counts
    page.goto(f"{base}?runs={r['v1']},{r['v2']},{r['v3']}")
    expect(page.locator(f"[data-regressed='{r['v2']}']")).to_contain_text("3 of 10")
    expect(page.locator(f"[data-regressed='{r['v3']}']")).to_contain_text("0 of 10")
    page.screenshot(path=str(EVIDENCE / "1280-three-runs.png"), full_page=True)

    # 7. at most 4 runs
    ids = ",".join(r[k] for k in ("v1", "v2", "v3", "v4", "v5"))
    page.goto(f"{base}?runs={ids}")
    expect(page.locator("body")).to_contain_text("at most 4 runs")
    assert agg.locator("thead th").count() == 5  # Metric + 4 runs
    expect(agg.locator("thead")).not_to_contain_text("v5")

    # 8. dark mode
    page.goto(f"{base}?runs={r['v1']},{r['v2']},{r['v3']}")
    page.evaluate("document.documentElement.setAttribute('data-theme','dark')")
    page.screenshot(path=str(EVIDENCE / "1280-dark.png"), full_page=True)

    # 9. 390px, light and dark: no horizontal page scroll
    page.set_viewport_size({"width": 390, "height": 844})
    page.evaluate("document.documentElement.removeAttribute('data-theme')")
    page.goto(f"{base}?runs={r['v1']},{r['v2']}")
    page.screenshot(path=str(EVIDENCE / "390-light.png"), full_page=True)
    overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    assert overflow <= 0, f"horizontal page scroll at 390px: {overflow}px"
    page.evaluate("document.documentElement.setAttribute('data-theme','dark')")
    page.screenshot(path=str(EVIDENCE / "390-dark.png"), full_page=True)

    assert [p for p in page.problems if p.startswith(("pageerror", "dialog"))] == []
