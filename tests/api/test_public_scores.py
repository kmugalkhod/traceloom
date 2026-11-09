from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

import pytest

from tests.helpers_web import seed_trace
from traceloom.scores import configs as score_configs

URL = "/api/public/scores"


def basic(public, secret):
    return {"Authorization": "Basic " + base64.b64encode(f"{public}:{secret}".encode()).decode()}


@pytest.fixture
def auth(make_key, world):
    _, public, secret = make_key(world.project)
    return basic(public, secret)


@pytest.fixture
def config(db, world):
    return score_configs.create_config(
        db, world.project.id, name="quality", data_type="NUMERIC", min_value=0, max_value=1
    )


@pytest.fixture(autouse=True)
def trace(container, world):
    seed_trace(container, world.project.id, "t1")
    seed_trace(container, world.project.id, "t2")


def post(client, auth, **body):
    body.setdefault("traceId", "t1")
    body.setdefault("name", "quality")
    return client.post(URL, json=body, headers=auth)


def test_requires_a_secret_key_pair(client, make_key, world):
    assert client.get(URL).status_code == 401
    _, public, _ = make_key(world.project)
    assert client.get(URL, headers={"X-Traceloom-Public-Key": public}).status_code == 401
    assert client.post(URL, json={"traceId": "t1", "name": "x", "value": 1}).status_code == 401
    assert client.delete(f"{URL}/abc").status_code == 401
    assert client.get("/api/public/score-configs").status_code == 401


def test_post_stores_a_score_without_a_config(client, auth, container, world):
    r = post(client, auth, value=0.7, comment="fine", environment="prod")
    assert r.status_code == 201
    score = container.trace_store.get_trace(world.project.id, "t1").scores[0]
    assert (score.id, score.value, score.source, score.comment) == (
        r.json()["id"],
        0.7,
        "API",
        "fine",
    )
    assert score.environment == "prod"


def test_post_validates_against_the_config(client, auth, config, container, world):
    ok = post(client, auth, value=0.5, configId=config.id)
    assert ok.status_code == 201
    bad = post(client, auth, value=1.5, configId=config.id)
    assert bad.status_code == 400 and "above the maximum" in bad.json()["error"]["message"]
    assert post(client, auth, value=0.5, configId="nope").status_code == 400
    assert post(client, auth, value="high", configId=config.id).status_code == 400
    assert len(container.trace_store.get_trace(world.project.id, "t1").scores) == 1


def test_categorical_and_boolean_configs(client, auth, db, world):
    cat = score_configs.create_config(
        db, world.project.id, name="tone", data_type="CATEGORICAL", categories=["warm", "cold"]
    )
    boolean = score_configs.create_config(db, world.project.id, name="ok", data_type="BOOLEAN")
    assert post(client, auth, name="tone", value="warm", configId=cat.id).status_code == 201
    assert post(client, auth, name="tone", value="hot", configId=cat.id).status_code == 400
    assert post(client, auth, name="ok", value=True, configId=boolean.id).status_code == 201
    assert post(client, auth, name="ok", value="yes", configId=boolean.id).status_code == 400


def test_batch_writes_all_or_nothing(client, auth, config, container, world):
    good = {"traceId": "t1", "name": "quality", "value": 0.1, "configId": config.id}
    bad = {**good, "value": 9}
    r = client.post(URL, json=[good, bad], headers=auth)
    assert r.status_code == 400
    assert r.json()["error"]["details"] == [
        {"index": 1, "message": r.json()["error"]["details"][0]["message"]}
    ]
    assert container.trace_store.get_trace(world.project.id, "t1").scores == []
    ok = client.post(URL, json=[good, {**good, "traceId": "t2"}], headers=auth)
    assert ok.status_code == 201 and len(ok.json()["data"]) == 2
    assert client.post(URL, json=[], headers=auth).status_code == 400
    assert client.post(URL, json=[good] * 101, headers=auth).status_code == 400


def test_clients_cannot_set_server_fields(client, auth):
    for field, value in (("queueId", "q"), ("executionId", "e"), ("channel", "public_feedback")):
        assert post(client, auth, value=1, **{field: value}).status_code == 400


def test_post_replaces_by_id_and_needs_a_target(client, auth, container, world):
    post(client, auth, id="s1", value=1)
    post(client, auth, id="s1", value=2)
    scores = container.trace_store.get_trace(world.project.id, "t1").scores
    assert [(s.id, s.value) for s in scores] == [("s1", 2.0)]
    assert client.post(URL, json={"name": "x", "value": 1}, headers=auth).status_code == 422


def test_get_filters_and_pages(client, auth, container, world):
    now = datetime.now(UTC)
    for i, (trace, name, value) in enumerate([("t1", "a", 1), ("t1", "b", 2), ("t2", "a", 3)]):
        post(client, auth, traceId=trace, name=name, value=value, id=f"s{i}")
    body = client.get(URL, headers=auth).json()
    assert len(body["data"]) == 3 and body["meta"]["nextCursor"] is None
    assert {
        s["id"] for s in client.get(URL, params={"traceId": "t1"}, headers=auth).json()["data"]
    } == {"s0", "s1"}
    assert [
        s["name"] for s in client.get(URL, params={"name": "b"}, headers=auth).json()["data"]
    ] == ["b"]
    assert client.get(URL, params={"source": "EVAL"}, headers=auth).json()["data"] == []
    assert len(client.get(URL, params={"source": "API"}, headers=auth).json()["data"]) == 3
    future = (now + timedelta(days=1)).isoformat()
    assert client.get(URL, params={"from": future}, headers=auth).json()["data"] == []
    past = (now - timedelta(days=1)).isoformat()
    assert (
        len(client.get(URL, params={"from": past, "to": future}, headers=auth).json()["data"]) == 3
    )
    assert client.get(URL, params={"source": "BOGUS"}, headers=auth).status_code == 400

    first = client.get(URL, params={"limit": 2}, headers=auth).json()
    assert len(first["data"]) == 2 and first["meta"]["nextCursor"]
    second = client.get(
        URL, params={"limit": 2, "cursor": first["meta"]["nextCursor"]}, headers=auth
    ).json()
    assert len(second["data"]) == 1 and second["meta"]["nextCursor"] is None
    assert {s["id"] for s in first["data"] + second["data"]} == {"s0", "s1", "s2"}
    assert client.get(URL, params={"cursor": "junk"}, headers=auth).status_code == 400


def test_get_filters_by_config(client, auth, config):
    post(client, auth, value=0.5, configId=config.id)
    post(client, auth, name="other", value=1)
    data = client.get(URL, params={"configId": config.id}, headers=auth).json()["data"]
    assert [s["configId"] for s in data] == [config.id]
    assert data[0]["value"] == 0.5 and data[0]["dataType"] == "NUMERIC"


def test_delete_only_in_the_callers_project(client, auth, make_key, make_project, world, container):
    post(client, auth, id="s1", value=1)
    other = make_project(world.org)
    _, public, secret = make_key(other)
    theirs = basic(public, secret)
    assert client.delete(f"{URL}/s1", headers=theirs).status_code == 404
    assert len(container.trace_store.get_trace(world.project.id, "t1").scores) == 1
    assert client.delete(f"{URL}/s1", headers=auth).status_code == 204
    assert container.trace_store.get_trace(world.project.id, "t1").scores == []
    assert client.delete(f"{URL}/s1", headers=auth).status_code == 404
    assert client.get(URL, headers=theirs).json()["data"] == []


def test_score_configs_list(client, auth, config, db, world, make_key, make_project):
    archived = score_configs.create_config(db, world.project.id, name="old", data_type="BOOLEAN")
    score_configs.set_archived(db, world.project.id, archived.id, True)
    body = client.get("/api/public/score-configs", headers=auth).json()
    assert [c["name"] for c in body["data"]] == ["quality"]
    assert body["data"][0]["maxValue"] == 1 and body["data"][0]["isArchived"] is False
    both = client.get("/api/public/score-configs", params={"includeArchived": "true"}, headers=auth)
    assert {c["name"] for c in both.json()["data"]} == {"quality", "old"}
    _, public, secret = make_key(make_project(world.org))
    assert (
        client.get("/api/public/score-configs", headers=basic(public, secret)).json()["data"] == []
    )


def test_writes_are_rate_limited(settings, client, auth, monkeypatch, container):
    monkeypatch.setattr(container.settings, "rate_ingest", "2/minute", raising=False)
    codes = [post(client, auth, value=1).status_code for _ in range(3)]
    assert codes == [201, 201, 429]
