"""Item outputs and errors from experiments render as text, never as markup (T-29)."""

from __future__ import annotations

from tests.api.test_dataset_runs_pages import HTML, base, env  # noqa: F401
from traceloom.auth.permissions import Role
from traceloom.datasets import runs
from traceloom.datasets.service import Actor
from traceloom.db.models_datasets import DatasetRunItem

PAYLOAD = "<script>alert(1)</script>"
ESCAPED = "&lt;script&gt;alert(1)&lt;/script&gt;"


def test_outputs_errors_and_inputs_are_escaped(client, login, world, env, db, make_dataset_item):  # noqa: F811
    _, dataset = env
    item = make_dataset_item(dataset, {"q": PAYLOAD}, None)
    actor = Actor.for_user(world.users[Role.MEMBER], Role.MEMBER)
    run = runs.create_run(db, world.project, actor, dataset, name=PAYLOAD)
    for status, output, error in (("completed", PAYLOAD, None), ("failed", None, PAYLOAD)):
        db.add(
            DatasetRunItem(
                run_id=run.id,
                project_id=world.project.id,
                item_id=item.id,
                item_version=1,
                status=status,
                output=output,
                error=error,
            )
        )
    db.commit()
    login(client, world.users[Role.VIEWER], csrf=False)
    for suffix in ("", "/progress"):
        text = client.get(f"{base(world, dataset)}/runs/{run.id}{suffix}", headers=HTML).text
        assert PAYLOAD not in text and ESCAPED in text


def test_run_templates_have_no_inline_styles():
    """The CSP blocks inline styles, so the experiment templates must not use them."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "traceloom/web/templates/datasets"
    names = ("_new_experiment.html", "run_detail.html", "_run_progress.html", "_run_status.html")
    for path in [root / n for n in names] + sorted(root.glob("_run*.html")):
        if path.exists():
            assert "style=" not in path.read_text(), path.name
