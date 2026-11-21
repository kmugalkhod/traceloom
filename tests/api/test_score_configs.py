from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from tests.api.test_ingestion import auth, event, post, stored
from traceloom.db.models import ScoreConfig
from traceloom.ingest.accept import IngestLimits, accept_batch
from traceloom.scores import configs as cfg
from traceloom.scores import service
from traceloom.scores.service import ScoreError, ScoreNotFound


@pytest.fixture
def headers(world, make_key):
    _, public, secret = make_key(world.project)
    return auth(public, secret)


def score_event(event_id, **body):
    return event(event_id, "score-create", {"traceId": "t1", "name": "quality", **body})


def post_scores(client, container, headers, *events):
    response = post(client, headers, list(events))
    assert response.status_code == 207
    container.queue.drain()
    return response.json()


# --- validation through the ingestion API -----------------------------------------------------


def test_numeric_config_rejects_out_of_range_and_keeps_valid_events(
    client, container, world, headers, make_score_config
):
    config = make_score_config(world.project, "quality", min_value=0.0, max_value=1.0)
    body = post_scores(
        client,
        container,
        headers,
        score_event("ok", id="s-ok", value=0.8, configId=config.id),
        score_event("high", id="s-high", value=1.5, configId=config.id),
        score_event("plain", id="s-plain", value=42),  # no config: accepted as before
    )
    assert [s["id"] for s in body["successes"]] == ["ok", "plain"]
    (error,) = body["errors"]
    assert (error["id"], error["status"]) == ("high", 400)
    assert "above the maximum" in error["message"]
    got = stored(container, world.project.id, score=["s-ok", "s-high", "s-plain"])["score"]
    assert set(got) == {"s-ok", "s-plain"}
    assert got["s-ok"].config_id == config.id


def test_unknown_or_foreign_config_is_a_per_event_error(
    client, container, world, headers, make_org, make_project, make_score_config
):
    other = make_score_config(make_project(make_org()), "theirs")
    body = post_scores(
        client,
        container,
        headers,
        score_event("nope", value=0.5, configId="missing"),
        score_event("foreign", value=0.5, configId=other.id),  # another project's config
    )
    assert [e["id"] for e in body["errors"]] == ["nope", "foreign"]
    assert body["successes"] == []


def test_categorical_and_boolean_values_are_checked(
    client, container, world, headers, make_score_config
):
    tone = make_score_config(
        world.project,
        "tone",
        data_type="CATEGORICAL",
        categories=[{"label": "warm", "value": 1}, {"label": "cold", "value": 0}],
    )
    ok = make_score_config(world.project, "correct", data_type="BOOLEAN")
    body = post_scores(
        client,
        container,
        headers,
        score_event("c1", name="tone", value="warm", configId=tone.id),
        score_event("c2", name="tone", value="angry", configId=tone.id),
        score_event("c3", name="tone", value=1, configId=tone.id),
        score_event("b1", name="correct", value=True, configId=ok.id),
        score_event("b2", name="correct", value=1, configId=ok.id),
        score_event("b3", name="correct", value=2, configId=ok.id),
        score_event("b4", name="correct", value="yes", configId=ok.id),
    )
    assert [s["id"] for s in body["successes"]] == ["c1", "b1", "b2"]
    assert [e["id"] for e in body["errors"]] == ["c2", "c3", "b3", "b4"]


def test_declared_data_type_must_match_the_config(
    client, container, world, headers, make_score_config
):
    config = make_score_config(world.project, "quality")
    body = post_scores(
        client,
        container,
        headers,
        score_event("bad", value=True, dataType="BOOLEAN", configId=config.id),
    )
    assert body["errors"][0]["id"] == "bad" and "does not match" in body["errors"][0]["message"]


def test_archived_config_still_validates_new_scores(
    client, container, world, db, headers, make_score_config
):
    config = make_score_config(world.project, "quality")
    cfg.set_archived(db, world.project.id, config.id, True)
    body = post_scores(client, container, headers, score_event("a", value=2, configId=config.id))
    assert body["errors"][0]["id"] == "a"  # still range-checked
    body = post_scores(client, container, headers, score_event("b", value=0.5, configId=config.id))
    assert body["successes"] == [{"id": "b", "status": 201}]


def test_new_score_fields_are_stored(client, container, world, headers):
    # datasetRunId is the client's (SDK experiments); the server-owned fields only reach storage
    # through the server's own (trusted) ingest path.
    post_scores(client, container, headers, score_event("f0", id="s-r", value=1, datasetRunId="r1"))
    raw = {
        "batch": [
            score_event(
                "f1",
                id="s-f",
                value=1,
                queueId="q1",
                executionId="x1",
                datasetRunId="r1",
                channel="public_feedback",
            )  # fmt: skip
        ]
    }
    s = container.settings
    result = accept_batch(
        world.project.id, json.dumps(raw).encode(), container.blob_store, container.queue,
        IngestLimits(s.ingest_max_events, s.ingest_max_bytes), trusted=True,
    )  # fmt: skip
    assert not result.errors
    container.queue.drain()
    rows = stored(container, world.project.id, score=["s-f", "s-r"])["score"]
    assert rows["s-r"].dataset_run_id == "r1"
    row = rows["s-f"]
    assert (row.queue_id, row.execution_id, row.dataset_run_id, row.channel) == (
        "q1",
        "x1",
        "r1",
        "public_feedback",
    )


def test_channel_other_than_public_feedback_is_rejected(client, container, world, headers):
    body = post_scores(client, container, headers, score_event("c", value=1, channel="other"))
    assert body["errors"][0]["id"] == "c"


# --- config service ---------------------------------------------------------------------------


def never_used(project_id, config_id):
    return False


def test_create_validates_fields(db, world):
    pid = world.project.id
    with pytest.raises(cfg.ConfigError):
        cfg.create_config(db, pid, name=" ", data_type="NUMERIC")
    with pytest.raises(cfg.ConfigError):
        cfg.create_config(db, pid, name="x", data_type="TEXT")
    with pytest.raises(cfg.ConfigError):
        cfg.create_config(db, pid, name="x", data_type="NUMERIC", min_value=2, max_value=1)
    with pytest.raises(cfg.ConfigError):
        cfg.create_config(db, pid, name="x", data_type="CATEGORICAL", categories=[])
    with pytest.raises(cfg.ConfigError):
        cfg.create_config(db, pid, name="x", data_type="CATEGORICAL", categories=["a", "a"])
    with pytest.raises(cfg.ConfigError):
        cfg.create_config(db, pid, name="x", data_type="NUMERIC", bogus=1)
    config = cfg.create_config(db, pid, name="tone", data_type="CATEGORICAL", categories=["a", "b"])
    assert config.categories == [{"label": "a", "value": 0}, {"label": "b", "value": 1}]


def test_names_are_unique_among_live_configs(db, world):
    pid = world.project.id
    first = cfg.create_config(db, pid, name="q", data_type="BOOLEAN")
    with pytest.raises(cfg.ConfigError, match="already exists"):
        cfg.create_config(db, pid, name="q", data_type="NUMERIC")
    cfg.set_archived(db, pid, first.id, True)
    second = cfg.create_config(db, pid, name="q", data_type="NUMERIC")
    with pytest.raises(cfg.ConfigError, match="Another config"):
        cfg.set_archived(db, pid, first.id, False)
    assert [c.id for c in cfg.list_configs(db, pid)] == [second.id]
    assert {c.id for c in cfg.list_configs(db, pid, include_archived=True)} == {first.id, second.id}


def test_type_and_categories_are_locked_once_scores_exist(db, world):
    pid = world.project.id
    config = cfg.create_config(db, pid, name="tone", data_type="CATEGORICAL", categories=["a", "b"])

    def used(project_id, config_id):
        return True

    for change in ({"data_type": "BOOLEAN"}, {"categories": ["a", "b", "c"]}):
        with pytest.raises(cfg.ConfigError, match="cannot change once scores use"):
            cfg.update_config(db, pid, config.id, change, in_use=used)
    # An identical value is not a change; description and the archive state can still change.
    cfg.update_config(db, pid, config.id, {"categories": ["a", "b"]}, in_use=used)
    cfg.update_config(db, pid, config.id, {"description": "How warm"}, in_use=used)
    assert cfg.set_archived(db, pid, config.id, True).archived_at is not None
    # Unused configs can change type.
    cfg.update_config(db, pid, config.id, {"data_type": "BOOLEAN"}, in_use=never_used)
    assert db.get(ScoreConfig, config.id).data_type == "BOOLEAN"


def test_range_can_change_after_use(db, world):
    pid = world.project.id
    config = cfg.create_config(db, pid, name="q", data_type="NUMERIC", min_value=0, max_value=1)
    updated = cfg.update_config(
        db, pid, config.id, {"min_value": 0, "max_value": 10}, in_use=lambda *_: True
    )
    assert (updated.min_value, updated.max_value) == (0, 10)


def test_in_use_follows_stored_scores(client, container, world, headers, make_score_config):
    config = make_score_config(world.project, "quality")
    store = container.trace_store
    assert not store.config_has_scores(world.project.id, config.id)
    post_scores(client, container, headers, score_event("s", value=0.5, configId=config.id))
    assert store.config_has_scores(world.project.id, config.id)
    assert not store.config_has_scores("other-project", config.id)


def test_queries_are_project_scoped(db, world, make_org, make_project):
    other = make_project(make_org())
    mine = cfg.create_config(db, world.project.id, name="q", data_type="BOOLEAN")
    assert cfg.list_configs(db, other.id) == []
    with pytest.raises(cfg.ConfigNotFound):
        cfg.get_config(db, other.id, mine.id)
    with pytest.raises(cfg.ConfigNotFound):
        cfg.update_config(db, other.id, mine.id, {"description": "x"}, in_use=never_used)
    with pytest.raises(cfg.ConfigNotFound):
        cfg.set_archived(db, other.id, mine.id, True)


def test_cache_serves_snapshots_until_invalidated(db, container, world, make_score_config):
    config = make_score_config(world.project, "q", min_value=0.0, max_value=1.0)
    cache = cfg.ConfigCache(container.session_factory, ttl_seconds=60)
    pid = world.project.id
    assert cache.check(pid, config.id, 2) is not None
    cfg.update_config(db, pid, config.id, {"max_value": 5}, in_use=never_used)
    assert cache.check(pid, config.id, 2) is not None  # stale within the ttl
    cache.invalidate(pid, config.id)
    assert cache.check(pid, config.id, 2) is None


# --- score service ----------------------------------------------------------------------------


def test_score_service_create_list_delete(container, world):
    store, pid = container.trace_store, world.project.id
    store.upsert(pid, [_trace()], [], [])
    row = service.create_score(store, pid, name="quality", value=0.7, trace_id="t1", comment="ok")
    assert (row.source, row.data_type, row.value) == ("API", "NUMERIC", 0.7)
    assert [s.id for s in service.list_scores(store, pid, "t1")] == [row.id]
    service.delete_score(store, pid, row.id)
    assert service.list_scores(store, pid, "t1") == []
    with pytest.raises(ScoreNotFound):
        service.delete_score(store, pid, row.id)
    with pytest.raises(ScoreNotFound):
        service.delete_score(store, "other-project", row.id)


def test_score_service_validates_against_config(container, world, make_score_config):
    store, pid = container.trace_store, world.project.id
    config = make_score_config(world.project, "quality")
    cache = cfg.ConfigCache(container.session_factory)
    with pytest.raises(ScoreError, match="above the maximum"):
        service.create_score(
            store, pid, name="quality", value=3, trace_id="t1", config_id=config.id, configs=cache
        )
    with pytest.raises(ScoreError, match="needs a trace"):
        service.create_score(store, pid, name="quality", value=1)


def test_score_service_source_rules(container, world):
    store, pid = container.trace_store, world.project.id
    kwargs = {"name": "q", "value": 1, "trace_id": "t1"}
    with pytest.raises(ScoreError, match="needs an author"):
        service.create_score(store, pid, source="ANNOTATION", **kwargs)
    with pytest.raises(ScoreError, match="execution id"):
        service.create_score(store, pid, source="EVAL", **kwargs)
    with pytest.raises(ScoreError, match="Only annotation"):
        service.create_score(store, pid, source="API", author_id="u1", **kwargs)
    with pytest.raises(ScoreError, match="Only evaluation"):
        service.create_score(store, pid, source="API", execution_id="x1", **kwargs)
    with pytest.raises(ScoreError, match="source must be"):
        service.create_score(store, pid, source="HUMAN", **kwargs)
    ann = service.create_score(
        store, pid, source="ANNOTATION", author_id="u1", queue_id="q1", **kwargs
    )
    ev = service.create_score(store, pid, source="EVAL", execution_id="x1", **kwargs)
    assert (ann.queue_id, ev.execution_id) == ("q1", "x1")


def _trace():
    from traceloom.ports import TraceRow

    return TraceRow(id="t1", timestamp=datetime(2026, 11, 6, tzinfo=UTC))
