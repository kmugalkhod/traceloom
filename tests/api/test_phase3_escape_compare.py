"""Inputs, expected outputs, outputs and run names render escaped on the compare page."""

from __future__ import annotations

from tests.api.compare_helpers import actor, make_run
from traceloom.auth.permissions import Role

XSS = "<script>alert(1)</script>"
ESCAPED = "&lt;script&gt;alert(1)&lt;/script&gt;"


def test_compare_page_escapes_content(
    db, world, container, client, login, make_dataset, make_dataset_item
):
    member = actor(world, Role.MEMBER)
    dataset = make_dataset(world.project)
    items = [make_dataset_item(dataset, {"q": XSS}, XSS)]
    a = make_run(db, world, container, dataset, items, member, f"a {XSS}", [0.9], outputs=[XSS])
    b = make_run(db, world, container, dataset, items, member, "b", [0.1], outputs=[XSS + "x"])
    login(client, world.users[Role.VIEWER], csrf=False)
    r = client.get(f"/project/{world.project.id}/datasets/{dataset.id}/compare?runs={a.id},{b.id}")
    assert r.status_code == 200
    assert XSS not in r.text
    assert r.text.count(ESCAPED) >= 4
