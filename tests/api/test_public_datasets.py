from __future__ import annotations

import base64

import pytest

from tests.helpers_web import seed_trace

DS = "/api/public/datasets"


def basic(public, secret):
    return {"Authorization": "Basic " + base64.b64encode(f"{public}:{secret}".encode()).decode()}


@pytest.fixture
def auth(make_key, world):
    _, public, secret = make_key(world.project)
    return basic(public, secret)


@pytest.fixture
def dataset(client, auth):
    assert client.post(DS, json={"name": "qa"}, headers=auth).status_code == 201
    return "qa"


def add_item(client, auth, **body):
    body.setdefault("input", {"q": "hi"})
    r = client.post(f"{DS}/qa/items", json=body, headers=auth)
    assert r.status_code == 201, r.text
    return r.json()


def test_rejects_missing_and_pk_only_auth(client, make_key, world, dataset):
    _, public, _ = make_key(world.project)
    pk_only = {"X-Traceloom-Public-Key": public}
    calls = [
        ("get", DS, None),
        ("post", DS, {"name": "x"}),
        ("get", f"{DS}/qa", None),
        ("post", f"{DS}/qa/items", {"input": 1}),
        ("get", f"{DS}/qa/items", None),
        ("post", "/api/public/dataset-items/x/archive", None),
        ("post", f"{DS}/qa/runs", {"name": "r"}),
        ("get", f"{DS}/qa/runs/r", None),
        ("post", "/api/public/dataset-run-items", {"datasetItemId": "a", "traceId": "t"}),
    ]
    for method, url, body in calls:
        for headers in ({}, pk_only):
            r = getattr(client, method)(url, headers=headers, **({"json": body} if body else {}))
            assert r.status_code == 401, (method, url, r.status_code)


def test_dataset_create_get_list_and_duplicate(client, auth, dataset):
    r = client.post(DS, json={"name": "qa"}, headers=auth)
    assert r.status_code == 409
    assert client.get(f"{DS}/qa", headers=auth).json()["name"] == "qa"
    assert client.get(f"{DS}/nope", headers=auth).status_code == 404
    listed = client.get(DS, headers=auth).json()["data"]
    assert [d["name"] for d in listed] == ["qa"]


def test_items_latest_version_and_archive_excluded(client, auth, dataset):
    keep = add_item(client, auth, input={"q": "keep"}, expectedOutput="a")
    gone = add_item(client, auth, input={"q": "gone"})
    r = client.post(f"/api/public/dataset-items/{gone['id']}/archive", headers=auth)
    assert r.status_code == 200 and r.json()["status"] == "archived"
    items = client.get(f"{DS}/qa/items", headers=auth).json()["data"]
    assert [i["id"] for i in items] == [keep["id"]]
    assert items[0]["expectedOutput"] == "a"
    everything = client.get(f"{DS}/qa/items?includeArchived=true", headers=auth).json()["data"]
    assert {i["id"] for i in everything} == {keep["id"], gone["id"]}
    assert client.get(f"{DS}/qa", headers=auth).json()["itemCount"] == 1


def test_item_without_input_is_400(client, auth, dataset):
    assert client.post(f"{DS}/qa/items", json={}, headers=auth).status_code == 400


def test_duplicate_run_name_is_409(client, auth, dataset):
    r = client.post(f"{DS}/qa/runs", json={"name": "r1"}, headers=auth)
    assert r.status_code == 201 and r.json()["status"] == "running"
    assert client.post(f"{DS}/qa/runs", json={"name": "r1"}, headers=auth).status_code == 409
    assert client.get(f"{DS}/qa/runs/r1", headers=auth).json()["name"] == "r1"
    assert client.get(f"{DS}/qa/runs/zzz", headers=auth).status_code == 404


def test_run_item_links_trace_and_takes_output_latency_cost(
    client, auth, container, world, dataset
):
    seed_trace(container, world.project.id, "t1", cost=0.25, output={"a": 1}, duration_ms=120.0)
    item = add_item(client, auth)
    run = client.post(f"{DS}/qa/runs", json={"name": "r1"}, headers=auth).json()
    body = {"runId": run["id"], "datasetItemId": item["id"], "traceId": "t1"}
    r = client.post("/api/public/dataset-run-items", json=body, headers=auth)
    assert r.status_code == 201, r.text
    link = r.json()
    assert link["traceId"] == "t1" and link["datasetItemId"] == item["id"]
    assert link["output"] == {"a": 1}
    assert link["cost"] == pytest.approx(0.25)
    assert link["latencyMs"] is not None
    detail = client.get(f"{DS}/qa/runs/r1", headers=auth).json()
    assert [i["traceId"] for i in detail["runItems"]] == ["t1"]
    # by names instead of run id
    by_name = {"datasetName": "qa", "runName": "r1", "datasetItemId": item["id"], "traceId": "t1"}
    assert (
        client.post("/api/public/dataset-run-items", json=by_name, headers=auth).status_code == 201
    )


def test_run_item_unknown_trace_or_run(client, auth, dataset):
    item = add_item(client, auth)
    run = client.post(f"{DS}/qa/runs", json={"name": "r1"}, headers=auth).json()
    url = "/api/public/dataset-run-items"
    missing_trace = {"runId": run["id"], "datasetItemId": item["id"], "traceId": "nope"}
    assert client.post(url, json=missing_trace, headers=auth).status_code == 404
    no_run = {"datasetItemId": item["id"], "traceId": "nope"}
    assert client.post(url, json=no_run, headers=auth).status_code == 400


def test_other_projects_key_is_isolated(client, auth, make_key, make_project, world, dataset):
    item = add_item(client, auth)
    run = client.post(f"{DS}/qa/runs", json={"name": "r1"}, headers=auth).json()
    _, public, secret = make_key(make_project(world.org))
    other = basic(public, secret)
    assert client.get(DS, headers=other).json()["data"] == []
    assert client.get(f"{DS}/qa", headers=other).status_code == 404
    assert client.get(f"{DS}/qa/items", headers=other).status_code == 404
    assert client.post(f"{DS}/qa/runs", json={"name": "x"}, headers=other).status_code == 404
    assert (
        client.post(f"/api/public/dataset-items/{item['id']}/archive", headers=other).status_code
        == 404
    )
    body = {"runId": run["id"], "datasetItemId": item["id"], "traceId": "t1"}
    assert client.post("/api/public/dataset-run-items", json=body, headers=other).status_code == 404


def test_run_item_records_error_and_run_status_can_finish(client, auth, container, world, dataset):
    seed_trace(container, world.project.id, "t1")
    item = add_item(client, auth)
    run = client.post(f"{DS}/qa/runs", json={"name": "r1"}, headers=auth).json()
    body = {
        "runId": run["id"],
        "datasetItemId": item["id"],
        "traceId": "t1",
        "status": "failed",
        "error": "ValueError: boom",
    }
    link = client.post("/api/public/dataset-run-items", json=body, headers=auth).json()
    assert link["status"] == "failed" and link["error"] == "ValueError: boom"
    r = client.patch(f"{DS}/qa/runs/r1", json={"status": "completed"}, headers=auth)
    assert r.status_code == 200 and r.json()["status"] == "completed"
    assert r.json()["finishedAt"]
    assert (
        client.patch(f"{DS}/qa/runs/r1", json={"status": "bogus"}, headers=auth).status_code == 400
    )
