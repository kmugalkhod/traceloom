from __future__ import annotations

import re

import pytest

from traceloom.auth.permissions import Role
from traceloom.datasets import runs, service
from traceloom.datasets.service import Actor

HTML = {"Accept": "text/html"}


def url(world, suffix=""):
    return f"/project/{world.project.id}/datasets{suffix}"


def post(client, path, **data):
    """POST like a browser: the CSRF token comes from the page the form is on."""
    page = client.get(client.page, headers=HTML)
    data["csrf_token"] = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    return client.post(path, data=data, headers=HTML, follow_redirects=False)


@pytest.fixture
def as_role(client, login, world):
    def go(role):
        login(client, world.users[role], csrf=False)
        client.page = url(world)
        return client

    return go


def member_actor(world, role=Role.MEMBER):
    return Actor.for_user(world.users[role], role)


@pytest.fixture
def dataset(db, world):
    d = service.create_dataset(db, world.project, member_actor(world), name="qa", description="d")
    db.commit()
    return d


def add_item(db, world, dataset, **kw):
    view = service.create_item(db, world.project, member_actor(world), dataset, **kw)
    db.commit()
    return view


def test_member_creates_dataset_and_adds_edits_items(client, as_role, world, db):
    as_role(Role.MEMBER)
    r = post(client, url(world), name="refunds", description="Refund chats")
    assert r.status_code == 303
    detail = r.headers["location"].split("?")[0]
    dataset = service.get_dataset_by_name(db, world.project, "refunds")
    assert detail == url(world, f"/{dataset.id}")

    client.page = detail
    assert (
        post(client, detail + "/items", input='{"q": "one"}', expected_output='"a"').status_code
        == 303
    )
    db.expire_all()
    (item,) = service.items_as_of(db, world.project, dataset)
    for text in ('{"q": "two"}', '{"q": "three"}'):  # each edit adds a version
        r = post(
            client, f"{detail}/items/{item.id}", input=text, expected_output='"a"', metadata="{}"
        )
        assert r.status_code == 303 and "notice=saved" in r.headers["location"]
    db.expire_all()
    assert service.get_item(db, world.project, item.id).current_version == 3

    page = client.get(detail + f"?item={item.id}")
    assert 'id="item-drawer"' in page.text and "Save as v4" in page.text
    for v in ("v1", "v2", "v3"):
        assert f">{v}" in page.text
    history = client.get(detail + "?tab=history").text
    assert history.count("Edited") == 2 and "Created" in history
    assert '<div class="add">' in history and '<div class="del">' in history

    assert post(client, f"{detail}/items/{item.id}/archive").status_code == 303
    db.expire_all()
    assert service.items_as_of(db, world.project, dataset) == []
    archived = client.get(detail + "?status=archived").text
    assert "No archived items" not in archived and "three" in archived
    assert "Archived" in client.get(detail + "?tab=history").text


def test_invalid_json_reopens_the_form_and_changes_nothing(client, as_role, world, db, dataset):
    view = add_item(db, world, dataset, input={"q": 1})
    as_role(Role.MEMBER)
    base = url(world, f"/{dataset.id}")
    client.page = base
    r = post(client, f"{base}/items/{view.id}", input='{"q": }', expected_output="", metadata="")
    assert r.status_code == 400 and "line 1" in r.text and "Nothing is changed yet" in r.text
    assert "{&#34;q&#34;: }" in r.text  # the user's text is kept
    db.expire_all()
    assert service.get_item(db, world.project, view.id).current_version == 1
    assert (
        post(client, base + "/items", input="", expected_output="", metadata="").status_code == 400
    )


def test_duplicate_dataset_name_shows_error(client, as_role, world, dataset):
    as_role(Role.MEMBER)
    r = post(client, url(world), name="qa")
    assert (
        r.status_code == 409
        and "already exists in this project" in r.text
        and "data-autoopen" in r.text
    )


def test_viewer_is_read_only(client, as_role, world, db, dataset):
    view = add_item(db, world, dataset, input={"q": 1})
    as_role(Role.VIEWER)
    base = url(world, f"/{dataset.id}")
    assert "New dataset" not in client.get(url(world)).text
    page = client.get(f"{base}?item={view.id}").text
    assert "Add item" not in page and "Save as" not in page and "Archive item" not in page
    assert "Read-only" in page and "readonly" in page
    client.page = base
    assert client.post(f"{base}/items", data={"input": "1"}).status_code in (401, 403)
    assert client.post(url(world), data={"name": "x"}).status_code in (401, 403)


def test_members_cannot_delete_admins_can(client, as_role, world, db, dataset):
    as_role(Role.MEMBER)
    base = url(world, f"/{dataset.id}")
    assert "Delete dataset" not in client.get(base).text
    assert client.post(f"{base}/delete", data={"confirm_name": "qa"}).status_code == 403
    as_role(Role.ADMIN)
    assert "Delete dataset" in client.get(base).text
    client.page = base
    wrong = post(client, f"{base}/delete", confirm_name="nope")
    assert wrong.status_code == 400 and "Type the dataset name exactly" in wrong.text
    ok = post(client, f"{base}/delete", confirm_name="qa")
    assert ok.status_code == 303 and ok.headers["location"] == url(world)
    db.expire_all()
    assert service.list_datasets(db, world.project) == []


def test_runs_tab_lists_runs(client, as_role, world, db, dataset):
    runs.create_run(db, world.project, member_actor(world), dataset, name="baseline")
    db.commit()
    as_role(Role.VIEWER)
    page = client.get(url(world, f"/{dataset.id}?tab=runs")).text
    assert "baseline" in page and "Running" in page


def test_pages_are_project_scoped(client, as_role, world, db, dataset, make_org, make_project):
    other = make_project(make_org())
    as_role(Role.MEMBER)
    assert client.get(f"/project/{other.id}/datasets/{dataset.id}").status_code in (403, 404)
    view = add_item(db, world, dataset, input=1)
    assert client.get(url(world, f"/missing/items/{view.id}")).status_code == 404
    assert client.get(url(world, f"/{dataset.id}/items/{view.id}")).status_code in (200, 303)


def test_list_page_and_search(client, as_role, world, db, dataset):
    add_item(db, world, dataset, input={"q": "needle here"})
    add_item(db, world, dataset, input={"q": "hay"})
    as_role(Role.MEMBER)
    assert "qa" in client.get(url(world)).text
    page = client.get(url(world, f"/{dataset.id}?q=needle")).text
    assert "needle here" in page and "hay" not in page


INLINE = re.compile(r"\sstyle\s*=|\son[a-z]+\s*=", re.I)


@pytest.mark.parametrize("role", [Role.VIEWER, Role.MEMBER, Role.ADMIN])
def test_dataset_pages_have_no_inline_style_or_handlers(
    client, as_role, world, db, dataset, container, role
):
    """The CSP (default-src 'self') blocks inline style and on*= handlers."""
    from tests.helpers_web import seed_trace

    view = add_item(db, world, dataset, input={"q": 1}, expected_output="a")
    runs.create_run(db, world.project, member_actor(world), dataset, name="r")
    db.commit()
    seed_trace(container, world.project.id, "t-inline", input="x", output="y")
    as_role(role)
    base = url(world, f"/{dataset.id}")
    pages = [
        url(world),
        base,
        f"{base}?tab=runs",
        f"{base}?tab=history",
        f"{base}?status=archived",
        f"{base}?item={view.id}",
    ]
    if role is not Role.VIEWER:
        pid = world.project.id
        pages += [
            f"{base}/import",
            f"/project/{pid}/datasets/add?trace_id=t-inline",
            f"/project/{pid}/datasets/add?trace_id=t-inline&trace_id=t-inline2",
            f"/project/{pid}/traces/t-inline",
            f"/project/{pid}/traces",
        ]
    for path in pages:
        r = client.get(path, headers=HTML)
        assert r.status_code in (200,), (path, r.status_code)
        assert INLINE.search(r.text) is None, path
    if role is not Role.VIEWER:
        fragment = client.get(
            f"/project/{world.project.id}/datasets/add?trace_id=t-inline",
            headers={"HX-Request": "true"},
        )
        assert INLINE.search(fragment.text) is None


def test_item_drawer_layout_matches_design(client, as_role, world, db, dataset):
    """JSON editors carry a status, metadata is one line, Archive sits in the footer."""
    view = add_item(
        db, world, dataset, input={"q": 1}, expected_output={"a": 1}, metadata={"topic": "refund"}
    )
    as_role(Role.MEMBER)
    page = client.get(url(world, f"/{dataset.id}?item={view.id}")).text
    drawer = page.split('id="item-drawer"', 1)[1].split("</dialog>", 1)[0]
    # input and expected output carry a status bar; metadata is a one-line field without one
    assert drawer.count("valid JSON") == 2
    assert "#i-ds-file" in drawer
    footer = drawer.split("<footer", 1)[1].split("</footer>", 1)[0]
    assert "Archive item" in footer
    meta = re.search(r'<textarea id="it-metadata"[^>]*>(.*?)</textarea>', drawer, re.S)
    assert meta and "\n" not in meta.group(1) and "topic" in meta.group(1)
