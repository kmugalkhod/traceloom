"""AC-13: dataset content renders as literal text on every dataset page."""

from __future__ import annotations

import pytest

from traceloom.auth.permissions import Role
from traceloom.datasets import runs, service
from traceloom.datasets.service import Actor

XSS = "<script>alert(1)</script>"
ESCAPED = "&lt;script&gt;alert(1)&lt;/script&gt;"


@pytest.fixture
def seeded(db, world):
    actor = Actor.for_user(world.users[Role.MEMBER], Role.MEMBER)
    dataset = service.create_dataset(
        db, world.project, actor, name=f"ds {XSS}", description=f"about {XSS}"
    )
    view = service.create_item(
        db,
        world.project,
        actor,
        dataset,
        input={"question": XSS},
        expected_output=XSS,
        metadata={"note": XSS},
        source_trace_id=f"t-{XSS}",
    )
    item = service.get_item(db, world.project, view.id)
    service.update_item(db, world.project, actor, item, input={"question": XSS + "2"})
    runs.create_run(db, world.project, actor, dataset, name=f"run {XSS}", description=XSS)
    db.commit()
    return dataset, view


@pytest.mark.parametrize("role", [Role.VIEWER, Role.MEMBER, Role.ADMIN])
def test_dataset_pages_escape_content(client, login, world, seeded, role):
    dataset, item = seeded
    login(client, world.users[role], csrf=False)
    base = f"/project/{world.project.id}/datasets"
    pages = [
        base,
        f"{base}/{dataset.id}",
        f"{base}/{dataset.id}?status=archived",
        f"{base}/{dataset.id}?q=alert",
        f"{base}/{dataset.id}?tab=runs",
        f"{base}/{dataset.id}?tab=history",
        f"{base}/{dataset.id}?item={item.id}",
    ]
    for path in pages:
        r = client.get(path)
        assert r.status_code == 200, path
        assert XSS not in r.text, path
    assert ESCAPED in client.get(f"{base}/{dataset.id}").text
    drawer = client.get(f"{base}/{dataset.id}?item={item.id}").text
    assert drawer.count(ESCAPED) >= 3  # input, expected output and metadata editors
