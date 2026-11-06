from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from traceloom.auth.deps import revoke_api_key
from traceloom.config import Settings
from traceloom.main import create_app
from traceloom.ports import BlobStoreError

T0 = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)


def ts(seconds=0) -> str:
    return (T0 + timedelta(seconds=seconds)).isoformat()


def event(id_, type_, body, at=0):
    return {"id": id_, "type": type_, "timestamp": ts(at), "body": body}


def auth(public, secret) -> dict:
    return {"Authorization": "Basic " + base64.b64encode(f"{public}:{secret}".encode()).decode()}


@pytest.fixture
def key(world, make_key):
    row, public, secret = make_key(world.project)
    return row, auth(public, secret)


@pytest.fixture
def batch():
    return [
        event("e1", "trace-create", {"id": "t1", "name": "run", "tags": ["a"]}),
        event(
            "e2",
            "generation-create",
            {
                "id": "g1",
                "traceId": "t1",
                "model": "claude-sonnet-4-5",
                "startTime": ts(1),
                "endTime": ts(3),
                "usageDetails": {"input": 1000, "output": 500},
            },
            at=1,
        ),
        {"id": "bad", "type": "nope", "timestamp": ts(), "body": {}},
    ]


def post(client, headers, batch, **extra):
    return client.post("/api/public/ingestion", json={"batch": batch, **extra}, headers=headers)


def stored(container, project_id, **ids):
    return container.trace_store.get_existing(project_id, ids)


def test_207_with_successes_and_one_error(client, container, world, key, batch):
    r = post(client, key[1], batch)
    assert r.status_code == 207
    body = r.json()
    assert body["successes"] == [{"id": "e1", "status": 201}, {"id": "e2", "status": 201}]
    assert [(e["id"], e["status"]) for e in body["errors"]] == [("bad", 400)]
    container.queue.drain()
    got = stored(container, world.project.id, trace=["t1"], observation=["g1"])
    assert (
        got["trace"]["t1"].name == "run" and got["observation"]["g1"].model == "claude-sonnet-4-5"
    )


def test_duplicate_batch(client, container, world, key, batch):
    for _ in range(2):
        assert post(client, key[1], batch).status_code == 207
        container.queue.drain()
    assert container.trace_store.count_traces(world.project.id) == 1
    got = stored(container, world.project.id, trace=["t1"], observation=["g1"])
    trace = got["trace"]["t1"]
    assert (trace.observation_count, trace.tags) == (1, ["a"])


def test_update_before_create(client, container, world, key):
    update = event(
        "u1",
        "generation-update",
        {"id": "g1", "traceId": "t1", "output": "answer", "endTime": ts(9)},
        at=9,
    )
    create = event(
        "c1",
        "generation-create",
        {"id": "g1", "traceId": "t1", "model": "gpt-4o", "input": "question", "startTime": ts(1)},
        at=1,
    )
    post(client, key[1], [update])
    container.queue.drain()
    post(client, key[1], [create])
    container.queue.drain()
    obs = stored(container, world.project.id, observation=["g1"])["observation"]["g1"]
    assert (obs.output, obs.input, obs.model) == ("answer", "question", "gpt-4o")
    assert obs.start_time == T0 + timedelta(seconds=1) and obs.end_time == T0 + timedelta(seconds=9)


def test_generation_with_usage_gets_calculated_cost_and_trace_totals(
    client, container, world, key, batch
):
    post(client, key[1], batch)
    container.queue.drain()
    got = stored(container, world.project.id, trace=["t1"], observation=["g1"])
    obs, trace = got["observation"]["g1"], got["trace"]["t1"]
    assert obs.cost_source == "calculated"
    assert obs.cost_details["total"] == pytest.approx(1000 * 3e-6 + 500 * 15e-6)
    assert trace.total_cost == pytest.approx(obs.cost_details["total"])
    assert trace.total_tokens == 1500 and trace.latency_ms == 2000.0


def test_provided_cost_is_kept(client, container, world, key):
    post(
        client,
        key[1],
        [
            event(
                "g",
                "generation-create",
                {
                    "id": "g1",
                    "traceId": "t1",
                    "model": "gpt-4o",
                    "usageDetails": {"input": 10},
                    "costDetails": {"total": 0.42},
                },
            )
        ],
    )
    container.queue.drain()
    obs = stored(container, world.project.id, observation=["g1"])["observation"]["g1"]
    assert (obs.cost_source, obs.cost_details["total"]) == ("provided", 0.42)


def test_orphan_observation_creates_stub_trace(client, container, world, key):
    post(client, key[1], [event("o", "span-create", {"id": "s1", "name": "root", "input": "hi"})])
    container.queue.drain()
    trace = stored(container, world.project.id, trace=["s1"])["trace"]["s1"]
    assert (trace.name, trace.input, trace.observation_count) == ("root", "hi", 1)


# --- status mapping ---


@pytest.mark.parametrize("headers", [{}, auth("tl-pk-nope", "tl-sk-nope")])
def test_bad_key_is_401(client, headers, batch):
    r = post(client, headers, batch)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"


def test_revoked_key_is_401_immediately(client, container, db, key, batch):
    assert post(client, key[1], batch).status_code == 207
    assert revoke_api_key(db, container, key[0].id)
    db.commit()
    assert post(client, key[1], batch).status_code == 401


def test_invalid_envelope_is_400(client, key):
    r = client.post("/api/public/ingestion", content=b"not json", headers=key[1])
    assert r.status_code == 400 and r.json()["error"]["code"] == "bad_request"
    assert client.post("/api/public/ingestion", json={"nope": 1}, headers=key[1]).status_code == 400


def test_blob_failure_is_503_and_nothing_is_queued(client, container, key, batch, monkeypatch):
    def boom(*a, **k):
        raise BlobStoreError("disk full")

    monkeypatch.setattr(container.blob_store, "put", boom)
    r = post(client, key[1], batch)
    assert r.status_code == 503 and r.json()["error"]["code"] == "unavailable"
    assert container.queue.pending == 0


@pytest.fixture
def small(settings: Settings):
    def build(**env):
        return TestClient(create_app(Settings(**{**settings.model_dump(by_alias=False), **env})))

    return build


def test_413_over_event_count(small, key, batch):
    with small(ingest_max_events=3) as c:
        assert post(c, key[1], batch).status_code == 207
        assert post(c, key[1], batch + batch).status_code == 413


def test_413_over_bytes_with_and_without_content_length(small, key, batch):
    with small(ingest_max_bytes=300) as c:
        r = post(c, key[1], [event("x", "trace-create", {"name": "n" * 1000})])
        assert r.status_code == 413 and r.json()["error"]["code"] == "payload_too_large"

        def chunks():
            yield b'{"batch": ['
            yield b" " * 1000

        assert c.post("/api/public/ingestion", content=chunks(), headers=key[1]).status_code == 413


def test_429_over_the_rate_limit_is_per_key(small, world, make_key, key, batch):
    other = auth(*make_key(world.project)[1:])
    with small(rate_ingest="2/minute") as c:
        assert [post(c, key[1], batch).status_code for _ in range(3)] == [207, 207, 429]
        r = post(c, key[1], batch)
        assert r.headers["retry-after"] and r.json()["error"]["code"] == "rate_limited"
        assert post(c, other, batch).status_code == 207


# --- isolation ---


def test_key_of_one_project_never_changes_another(
    client, container, world, key, make_project, make_key
):
    other_project = make_project(world.org)
    _, pk, sk = make_key(other_project)
    victim = [event("v1", "trace-create", {"id": "shared", "name": "victim"})]
    post(client, auth(pk, sk), victim)
    container.queue.drain()
    attack = [
        event("a1", "trace-create", {"id": "shared", "name": "attacker"}, at=60),
        event("a2", "span-create", {"id": "s", "traceId": "shared"}, at=60),
    ]
    assert post(client, key[1], attack).status_code == 207
    container.queue.drain()
    mine = stored(container, world.project.id, trace=["shared"])["trace"]["shared"]
    theirs = stored(container, other_project.id, trace=["shared"], observation=["s"])
    assert mine.name == "attacker"
    assert theirs["trace"]["shared"].name == "victim" and theirs["observation"] == {}
    assert theirs["trace"]["shared"].observation_count == 0


def test_deep_health_reports_storage_and_queue(client):
    body = client.get("/api/public/health?deep=1").json()
    assert body["checks"] == {"db": "ok", "blob": "ok", "queue": "ok"}
