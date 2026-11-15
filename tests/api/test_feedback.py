from __future__ import annotations

import base64

import pytest

from tests.helpers_web import seed_trace
from traceloom.db.models import ScoreConfig
from traceloom.ports import ScoreRow
from traceloom.scores import configs as score_configs
from traceloom.scores import feedback

URL = "/api/public/feedback"
ALLOWED = "https://app.example.com"
OTHER = "https://evil.example.org"


@pytest.fixture
def key(make_key, world):
    _, public, secret = make_key(world.project)
    return public, secret


@pytest.fixture
def pk(key):
    return {"X-Traceloom-Public-Key": key[0]}


@pytest.fixture
def thumbs(db, world):
    return score_configs.create_config(
        db, world.project.id, name="thumbs", data_type="BOOLEAN", public_feedback=True
    )


@pytest.fixture
def allowed(db, world):
    feedback.set_feedback_origins(db, world.project.id, [ALLOWED])
    db.commit()


@pytest.fixture(autouse=True)
def trace(container, world):
    seed_trace(container, world.project.id, "t1")


def send(client, pk, origin=ALLOWED, **body):
    body.setdefault("traceId", "t1")
    body.setdefault("name", "thumbs")
    body.setdefault("value", True)
    headers = dict(pk)
    if origin:
        headers["Origin"] = origin
    return client.post(URL, json=body, headers=headers)


def scores(container, world):
    return container.trace_store.get_trace(world.project.id, "t1").scores


def test_allowed_origin_gets_202_cors_and_a_channel_score(
    client, pk, thumbs, allowed, container, world
):
    r = send(client, pk, comment="great")
    assert r.status_code == 202
    assert r.headers["access-control-allow-origin"] == ALLOWED and r.headers["vary"] == "Origin"
    (score,) = scores(container, world)
    assert (score.id, score.name, score.value) == (r.json()["id"], "thumbs", 1.0)
    assert (score.source, score.channel, score.data_type) == ("API", "public_feedback", "BOOLEAN")
    assert score.comment == "great" and score.config_id == thumbs.id and score.author_id is None


def test_disallowed_origin_is_403_without_cors_headers(
    client, pk, thumbs, allowed, container, world
):
    r = send(client, pk, origin=OTHER)
    assert r.status_code == 403
    assert not [h for h in r.headers if h.startswith("access-control")]
    assert scores(container, world) == []
    empty = send(client, pk, origin="null")
    assert empty.status_code == 403


def test_origin_list_is_empty_by_default(client, pk, thumbs):
    assert send(client, pk).status_code == 403


def test_no_origin_header_is_a_server_call_and_allowed(client, pk, thumbs, container, world):
    r = send(client, pk, origin=None)
    assert r.status_code == 202 and "access-control-allow-origin" not in r.headers
    assert len(scores(container, world)) == 1


def test_non_public_unknown_or_archived_config_is_400(client, pk, thumbs, allowed, db, world):
    private = score_configs.create_config(db, world.project.id, name="private", data_type="BOOLEAN")
    assert send(client, pk, name="private").status_code == 400
    assert send(client, pk, name="nope").status_code == 400
    assert send(client, pk, name=None, configId=private.id).status_code == 400
    assert send(client, pk, name=None).status_code == 400
    score_configs.set_archived(db, world.project.id, thumbs.id, True)
    assert send(client, pk).status_code == 400


def test_values_are_checked_against_the_config(client, pk, allowed, db, world):
    score_configs.create_config(
        db,
        world.project.id,
        name="stars",
        data_type="NUMERIC",
        min_value=1,
        max_value=5,
        public_feedback=True,
    )
    score_configs.create_config(
        db,
        world.project.id,
        name="tone",
        data_type="CATEGORICAL",
        categories=["warm", "cold"],
        public_feedback=True,
    )
    assert send(client, pk, name="stars", value=4).status_code == 202
    assert send(client, pk, name="stars", value=9).status_code == 400
    assert send(client, pk, name="tone", value="warm").status_code == 202
    assert send(client, pk, name="tone", value="hot").status_code == 400


def test_comment_cap_and_target(client, pk, thumbs, allowed):
    assert send(client, pk, comment="x" * 1000).status_code == 202
    assert send(client, pk, comment="x" * 1001).status_code == 400
    assert send(client, pk, traceId=None).status_code == 400


def test_same_id_replaces_but_never_another_score(client, pk, thumbs, allowed, container, world):
    assert send(client, pk, id="f1", value=True).status_code == 202
    assert send(client, pk, id="f1", value=False).status_code == 202
    assert [(s.id, s.value) for s in scores(container, world)] == [("f1", 0.0)]
    container.trace_store.add_score(
        world.project.id,
        ScoreRow(
            id="api1",
            name="thumbs",
            timestamp=scores(container, world)[0].timestamp,
            trace_id="t1",
            value=1.0,
        ),
    )
    clash = send(client, pk, id="api1", value=False)
    assert clash.status_code == 400 and "another score" in clash.json()["error"]["message"]


def test_public_key_alone_is_rejected_elsewhere_and_needs_a_valid_key(
    client, pk, key, thumbs, make_key, world, db
):
    assert client.get("/api/public/scores", headers=pk).status_code == 401
    assert client.get("/api/public/score-configs", headers=pk).status_code == 401
    assert client.post("/api/public/ingestion", json={"batch": []}, headers=pk).status_code == 401
    assert client.post(URL, json={}).status_code == 401
    assert (
        client.post(URL, json={}, headers={"X-Traceloom-Public-Key": "tl-pk-nope"}).status_code
        == 401
    )
    # The pk+sk pair is not what this endpoint takes: Basic auth alone gives 401 here.
    basic = {"Authorization": "Basic " + base64.b64encode(f"{key[0]}:{key[1]}".encode()).decode()}
    assert (
        client.post(
            URL, json={"traceId": "t1", "name": "thumbs", "value": True}, headers=basic
        ).status_code
        == 401
    )


def test_a_revoked_key_is_refused(client, pk, thumbs, allowed, db, world, container):
    from sqlalchemy import select

    from traceloom.auth.deps import revoke_api_key
    from traceloom.db.models import ApiKey

    row = db.scalar(select(ApiKey).where(ApiKey.public_key == pk["X-Traceloom-Public-Key"]))
    revoke_api_key(db, container, row.id)
    db.commit()
    assert send(client, pk).status_code == 401


def test_the_key_only_writes_to_its_own_project(
    client, pk, thumbs, allowed, make_org, make_project, container, world
):
    other = make_project(make_org())
    seed_trace(container, other.id, "t1")
    assert send(client, pk).status_code == 202
    assert container.trace_store.get_trace(other.id, "t1").scores == []


def test_the_61st_request_in_a_minute_is_429(client, pk, thumbs, allowed):
    codes = [send(client, pk).status_code for _ in range(61)]
    assert codes[:60] == [202] * 60 and codes[60] == 429
    last = send(client, pk)
    assert last.status_code == 429 and int(last.headers["retry-after"]) >= 1
    assert last.headers["access-control-allow-origin"] == ALLOWED


def test_the_ip_limit_applies_across_keys(
    client, thumbs, allowed, make_key, make_project, container, world, db
):
    container.settings.rate_feedback = "3/minute"
    keys = [{"X-Traceloom-Public-Key": make_key(world.project)[1]} for _ in range(4)]
    codes = [send(client, k).status_code for k in keys]
    assert codes == [202, 202, 202, 429]


def test_preflight_uses_the_union_of_every_projects_origins(
    client, allowed, db, make_org, make_project
):
    other = make_project(make_org())
    feedback.set_feedback_origins(db, other.id, ["https://other.example.net"])
    db.commit()
    for origin in (ALLOWED, "https://other.example.net"):
        r = client.options(URL, headers={"Origin": origin, "Access-Control-Request-Method": "POST"})
        assert r.status_code == 204
        assert r.headers["access-control-allow-origin"] == origin
        assert "POST" in r.headers["access-control-allow-methods"]
        assert "X-Traceloom-Public-Key" in r.headers["access-control-allow-headers"]
    refused = client.options(
        URL, headers={"Origin": OTHER, "Access-Control-Request-Method": "POST"}
    )
    assert refused.status_code == 204 and "access-control-allow-origin" not in refused.headers
    assert "access-control-allow-origin" not in client.options(URL).headers


def test_origin_normalisation_and_validation(db, world):
    assert feedback.normalize_origin("HTTPS://App.Example.com/") == "https://app.example.com"
    assert feedback.normalize_origin("http://localhost:3000") == "http://localhost:3000"
    for bad in (
        "app.example.com",
        "https://a.com/path",
        "ftp://a.com",
        "https://u:p@a.com",
        "https://a.com?x=1",
        "",
    ):
        with pytest.raises(feedback.FeedbackError):
            feedback.normalize_origin(bad)
    assert feedback.set_feedback_origins(
        db, world.project.id, ["https://a.com/", "https://A.com"]
    ) == ["https://a.com"]
    assert feedback.feedback_origins(db, world.project.id) == ["https://a.com"]
    assert db.query(ScoreConfig).count() == 0
