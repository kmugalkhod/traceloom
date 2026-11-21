from __future__ import annotations

import io
import re

import pytest

from tests.helpers_web import seed_trace
from traceloom.auth.permissions import Role
from traceloom.datasets import service
from traceloom.datasets.service import Actor

HTML = {"Accept": "text/html"}
XSS = "<script>alert(1)</script>"


def csv_file(rows=20, bad_row=3):
    lines = ["question,answer,input_json,internal_id"]
    for i in range(1, rows + 1):
        meta = '"{""topic"": refund"' if i == bad_row else f'"{{""topic"": ""t{i}""}}"'
        question = XSS if i == 3 else f"Question {i}"
        lines.append(f'"{question}",Answer {i},{meta},r-{i}')
    return "\n".join(lines).encode()


@pytest.fixture
def member(client, login, world):
    login(client, world.users[Role.MEMBER])  # sets the X-CSRF-Token header, as app.js does
    return client


@pytest.fixture
def dataset(db, world):
    actor = Actor.for_user(world.users[Role.MEMBER], Role.MEMBER)
    d = service.create_dataset(db, world.project, actor, name="refunds")
    db.commit()
    return d


def base(world, dataset):
    return f"/project/{world.project.id}/datasets/{dataset.id}"


def token_of(client, path):
    html = client.get(path, headers=HTML).text
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def upload(client, world, dataset, data, name="refunds.csv"):
    path = base(world, dataset) + "/import"
    csrf = token_of(client, path)
    return client.post(
        path,
        data={"csrf_token": csrf},
        files={"file": (name, io.BytesIO(data), "text/csv")},
        headers=HTML,
        follow_redirects=False,
    )


MAP = {"use_0": "input", "use_1": "expected_output", "use_2": "metadata", "use_3": "ignore"}


def test_wizard_previews_and_imports_csv_with_bad_json_cell(member, world, dataset, db):
    r = upload(member, world, dataset, csv_file())
    assert r.status_code == 303
    step2 = r.headers["location"]
    page = member.get(step2).text
    assert "Map columns" in page and "20 rows · 4 columns" in page
    assert page.count("<select") == 4

    path = step2 + "/preview"
    preview = member.post(path, data={"csrf_token": token_of(member, step2), **MAP})
    assert preview.status_code == 200
    html = preview.text
    assert "first 5 of 20 rows" in html
    assert "1 cell is not valid JSON and will be imported as text" in html
    assert "input_json: not valid JSON, kept as text." in html
    assert XSS not in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html  # literal text
    assert "Import 20 items" in html

    done = member.post(step2 + "/run", data={"csrf_token": token_of(member, step2), **MAP})
    assert done.status_code == 200 and "Imported 20 items" in done.text
    db.expire_all()
    items = service.items_as_of(db, world.project, dataset)
    assert len(items) == 20
    assert items[2].input == XSS
    assert items[2].metadata == {"input_json": '{"topic": refund'}
    assert items[0].expected_output == "Answer 1"
    assert items[0].metadata == {"topic": "t1"}
    # The parked upload is gone once the import is done.
    assert member.get(step2).status_code == 404


def test_mapping_needs_exactly_one_input(member, world, dataset):
    step2 = upload(member, world, dataset, csv_file(5)).headers["location"]
    csrf = token_of(member, step2)
    none = member.post(step2 + "/preview", data={"csrf_token": csrf, "use_0": "ignore"})
    assert none.status_code == 400 and "Choose which one column is the input" in none.text
    two = member.post(
        step2 + "/preview", data={"csrf_token": csrf, "use_0": "input", "use_1": "input"}
    )
    assert two.status_code == 400


def test_upload_limits_and_bad_files(member, world, dataset, container, monkeypatch):
    monkeypatch.setattr(container.settings, "dataset_import_max_rows", 10, raising=False)
    big = upload(member, world, dataset, csv_file(12))
    assert big.status_code == 400 and "more than 10 rows" in big.text
    assert upload(member, world, dataset, b"x", name="notes.txt").status_code == 400
    assert upload(member, world, dataset, b"", name="empty.csv").status_code == 400
    jsonl = upload(member, world, dataset, b'{"input": "a"}\n{"input": "b"}\n', name="d.jsonl")
    assert jsonl.status_code == 303


def test_unknown_token_is_404(member, world, dataset):
    assert member.get(base(world, dataset) + "/import/" + "0" * 32).status_code == 404
    assert member.get(base(world, dataset) + "/import/../../etc").status_code in (404, 307, 308)


def test_viewer_cannot_import(client, login, world, dataset):
    login(client, world.users[Role.VIEWER], csrf=False)
    assert client.get(base(world, dataset) + "/import").status_code == 403


def test_add_observation_to_dataset_prefills_and_links_source(
    member, world, dataset, db, container
):
    seed_trace(container, world.project.id, "t1", input={"q": "hi"}, output="hello")
    store = container.trace_store
    obs = next(o for o in store.get_trace(world.project.id, "t1").observations if o.name == "step1")
    url = f"/project/{world.project.id}/datasets/add?trace_id=t1&observation_id={obs.id}"

    fragment = member.get(url, headers={"HX-Request": "true"})
    assert (
        fragment.status_code == 200
        and "<dialog" in fragment.text
        and "data-autoopen" in fragment.text
    )
    assert "refunds" in fragment.text
    page = member.get(url, headers=HTML).text
    assert "<html" in page and 'name="observation_id"' in page

    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
    r = member.post(
        url.split("?")[0],
        data={
            "csrf_token": csrf,
            "dataset_id": dataset.id,
            "trace_id": "t1",
            "observation_id": obs.id,
            "input": '{"q": "edited"}',
            "expected_output": '"hello"',
            "metadata": "",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303 and "notice=traceadded" in r.headers["location"]
    db.expire_all()
    (item,) = service.items_as_of(db, world.project, dataset)
    assert (item.input, item.expected_output) == ({"q": "edited"}, "hello")
    assert (item.source_trace_id, item.source_observation_id) == ("t1", obs.id)


def test_add_trace_prefills_its_input_and_output(member, world, dataset, container):
    seed_trace(container, world.project.id, "t2", input={"q": "trace in"}, output="trace out")
    page = member.get(f"/project/{world.project.id}/datasets/add?trace_id=t2", headers=HTML).text
    assert "trace in" in page and "trace out" in page


def test_invalid_json_keeps_the_form_open(member, world, dataset, container):
    seed_trace(container, world.project.id, "t3", input="x")
    url = f"/project/{world.project.id}/datasets/add"
    page = member.get(url + "?trace_id=t3", headers=HTML).text
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
    r = member.post(
        url,
        data={"csrf_token": csrf, "dataset_id": dataset.id, "trace_id": "t3",
              "input": '{"a": ', "expected_output": "", "metadata": ""},
    )  # fmt: skip
    assert r.status_code == 400 and "Input: line 1" in r.text and "{&#34;a&#34;: " in r.text


def test_bulk_add_two_traces_and_new_dataset(member, world, db, container):
    seed_trace(container, world.project.id, "b1", input={"q": 1}, output="o1")
    seed_trace(container, world.project.id, "b2", input={"q": 2}, output="o2")
    url = f"/project/{world.project.id}/datasets/add"
    page = member.get(url + "?trace_id=b1&trace_id=b2", headers=HTML).text
    assert "Adding 2 traces" in page
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
    r = member.post(
        url,
        data={"csrf_token": csrf, "new_dataset": "bulk", "trace_id": ["b1", "b2"]},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "notice=bulk&added=2&skipped=0" in r.headers["location"]
    dataset = service.get_dataset_by_name(db, world.project, "bulk")
    assert {i.source_trace_id for i in service.items_as_of(db, world.project, dataset)} == {
        "b1",
        "b2",
    }
    assert "Added 2 traces as items" in member.get(r.headers["location"]).text


def test_viewer_cannot_add_and_pages_show_the_controls(client, login, world, dataset, container):
    seed_trace(container, world.project.id, "t9", input="x")
    login(client, world.users[Role.VIEWER], csrf=False)
    assert client.get(f"/project/{world.project.id}/datasets/add?trace_id=t9").status_code == 403
    detail = client.get(f"/project/{world.project.id}/traces/t9").text
    assert "Add to dataset" not in detail
    assert "data-bulk-add" not in client.get(f"/project/{world.project.id}/traces").text
    login(client, world.users[Role.MEMBER], csrf=False)
    assert "Add to dataset" in client.get(f"/project/{world.project.id}/traces/t9").text
    assert "data-bulk-add" in client.get(f"/project/{world.project.id}/traces").text


def test_qa_bulk_add_skips_a_trace_from_another_project(
    member, world, db, container, make_org, make_project
):
    other = make_project(make_org())
    seed_trace(container, world.project.id, "mine", input={"q": 1}, output="o")
    seed_trace(container, other.id, "theirs", input={"secret": 1}, output="s")
    url = f"/project/{world.project.id}/datasets/add"
    csrf = re.search(
        r'name="csrf_token" value="([^"]+)"', member.get(url + "?trace_id=mine", headers=HTML).text
    ).group(1)
    member.post(
        url,
        data={"csrf_token": csrf, "new_dataset": "scoped", "trace_id": ["mine", "theirs"]},
        follow_redirects=False,
    )
    dataset = service.get_dataset_by_name(db, world.project, "scoped")
    ids = {i.source_trace_id for i in service.items_as_of(db, world.project, dataset)}
    assert ids == {"mine"}


def test_qa_import_without_csrf_token_is_rejected(member, world, dataset):
    r = member.post(
        base(world, dataset) + "/import",
        data={},
        files={"file": ("x.csv", io.BytesIO(b"a\n1\n"), "text/csv")},
        headers={**HTML, "X-CSRF-Token": ""},
        follow_redirects=False,
    )
    assert r.status_code in (400, 403)
