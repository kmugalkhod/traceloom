import base64
import json
import time

import httpx

import traceloom_sdk
from traceloom_sdk import Traceloom
from traceloom_sdk.client import MASK_FAILED, sampled


def test_env_configuration_and_constructor_overrides(monkeypatch):
    monkeypatch.setenv("TRACELOOM_HOST", "https://env.example/")
    monkeypatch.setenv("TRACELOOM_PUBLIC_KEY", "pk-env")
    monkeypatch.setenv("TRACELOOM_SECRET_KEY", "sk-env")
    monkeypatch.setenv("TRACELOOM_FLUSH_AT", "7")
    monkeypatch.setenv("TRACELOOM_FLUSH_INTERVAL", "2.5")
    monkeypatch.setenv("TRACELOOM_SAMPLE_RATE", "0.25")
    c = Traceloom()
    assert (c.host, c.public_key, c.secret_key) == ("https://env.example", "pk-env", "sk-env")
    assert (c.flush_at, c.flush_interval, c.sample_rate, c.enabled) == (7, 2.5, 0.25, True)
    over = Traceloom(host="http://x", flush_at=3, sample_rate=1, enabled=False)
    assert (over.host, over.flush_at, over.sample_rate, over.enabled) == ("http://x", 3, 1.0, False)
    monkeypatch.setenv("TRACELOOM_ENABLED", "false")
    assert Traceloom().enabled is False


def test_bad_env_numbers_fall_back(monkeypatch, sdk_logs):
    monkeypatch.setenv("TRACELOOM_FLUSH_AT", "lots")
    assert Traceloom(public_key="a", secret_key="b").flush_at == 50
    assert "invalid TRACELOOM_FLUSH_AT" in sdk_logs.text


def test_missing_keys_disable_the_client_without_raising(sdk_logs):
    c = Traceloom()
    assert c.enabled is False
    with c.start_span("x"):
        pass
    assert c.flush() is True
    assert "disabled" in sdk_logs.text


def test_flush_sends_the_ingestion_envelope_with_basic_auth(client, recorder):
    with client.start_span("work", input={"q": 1}) as span:
        span.update(output="done")
    assert client.flush() is True
    (request,) = recorder.requests
    assert request.url == "http://traceloom.test/api/public/ingestion"
    token = base64.b64encode(b"pk-test:sk-test").decode()
    assert request.headers["authorization"] == f"Basic {token}"
    batch = json.loads(request.content)["batch"]
    assert [e["type"] for e in batch] == [
        "trace-create",
        "span-create",
        "span-update",
        "trace-create",
    ]
    for event in batch:
        assert set(event) == {"id", "type", "timestamp", "body"}
        assert event["timestamp"].endswith("Z")
    create = batch[1]["body"]
    assert create["name"] == "work" and create["input"] == {"q": 1} and create["traceId"]
    assert batch[2]["body"]["output"] == "done" and "endTime" in batch[2]["body"]


def test_flush_at_triggers_a_send_without_flush(make_client, recorder):
    c = make_client(flush_at=3)
    for i in range(3):
        c.event(f"e{i}")
    deadline = time.time() + 3
    while not recorder.requests and time.time() < deadline:
        time.sleep(0.01)
    assert recorder.requests, "batch was not sent when flush_at was reached"
    assert len(json.loads(recorder.requests[0].content)["batch"]) >= 3


def test_flush_interval_triggers_a_send(make_client, recorder):
    c = make_client(flush_at=1000, flush_interval=0.05)
    c.event("tick")
    deadline = time.time() + 3
    while not recorder.requests and time.time() < deadline:
        time.sleep(0.01)
    assert [e["type"] for e in recorder.events] == ["trace-create", "event-create"]


def test_batches_never_exceed_flush_at(make_client, recorder):
    c = make_client(flush_at=4, flush_interval=60)
    c._wake.clear()
    for i in range(5):
        c.event(f"e{i}")  # 10 events: a trace-create and an event-create each
    c.flush()
    sizes = [len(json.loads(r.content)["batch"]) for r in recorder.requests]
    assert sum(sizes) == 10 and max(sizes) <= 4


def test_500_is_retried_three_times_then_dropped(make_client, recorder, sdk_logs):
    recorder.statuses = [500, 500, 500, 500, 500]
    c = make_client()
    c.event("x")
    assert c.flush() is False
    assert len(recorder.requests) == 4  # first attempt plus 3 retries
    assert "dropped a batch" in sdk_logs.text


def test_retry_succeeds_after_transient_errors(make_client, recorder):
    recorder.statuses = [429, "connect-error", 503]
    c = make_client()
    c.event("x")
    assert c.flush() is True
    assert len(recorder.requests) == 4


def test_client_errors_are_not_retried(make_client, recorder, sdk_logs):
    recorder.statuses = [401]
    c = make_client()
    c.event("x")
    assert c.flush() is False
    assert len(recorder.requests) == 1
    assert "HTTP 401" in sdk_logs.text


def test_backoff_grows_exponentially(make_client, recorder, monkeypatch):
    waits = []
    recorder.statuses = [500, 500, 500, 500]
    c = make_client(backoff_base=1.0)
    monkeypatch.setattr(c._stop, "wait", lambda t=None: waits.append(t) or False)
    c.event("x")
    c.flush(timeout=60)
    assert len(waits) == 3
    for attempt, wait in enumerate(waits):
        assert 0.5 * 2**attempt <= wait <= 1.5 * 2**attempt


def test_unreachable_host_never_raises_and_flush_returns_within_timeout(sdk_logs):
    c = Traceloom(host="http://127.0.0.1:9", public_key="a", secret_key="b", backoff_base=5.0)
    c.event("x")
    started = time.monotonic()
    assert c.flush(timeout=0.5) is False
    assert time.monotonic() - started < 2.0
    c.shutdown(0.1)


def test_unreachable_transport_error_with_long_backoff_respects_timeout(make_client, recorder):
    recorder.statuses = ["connect-error"] * 10
    c = make_client(backoff_base=30.0)
    c.event("x")
    started = time.monotonic()
    assert c.flush(timeout=0.3) is False
    assert time.monotonic() - started < 1.5


def test_full_queue_drops_events_with_a_warning(make_client, recorder, sdk_logs):
    c = make_client(max_queue_size=3, flush_at=1000)
    for i in range(6):
        c.event(f"e{i}")  # 12 events into a queue of 3
    assert c._queue.qsize() == 3
    assert "queue is full" in sdk_logs.text
    c.flush()
    assert len(recorder.events) == 3


def test_mask_applies_to_input_output_and_metadata(make_client, recorder):
    c = make_client(mask=lambda v: "MASKED")
    with c.start_span("s", input={"ssn": "1"}, metadata={"k": "v"}) as span:
        span.update(output="secret")
    c.flush()
    bodies = [e["body"] for e in recorder.events if e["type"].startswith("span")]
    assert bodies[0]["input"] == "MASKED" and bodies[0]["metadata"] == "MASKED"
    assert bodies[1]["output"] == "MASKED"
    assert "secret" not in json.dumps(recorder.events)
    assert (
        next(e for e in recorder.events if e["type"] == "trace-create")["body"]["input"] == "MASKED"
    )


def test_mask_that_raises_gives_placeholder(make_client, recorder):
    def boom(value):
        raise RuntimeError("nope")

    c = make_client(mask=boom)
    c.event("e", input="x", output="y", metadata={"a": 1})
    c.flush()
    body = recorder.of_type("event-create")[0]["body"]
    assert (body["input"], body["output"], body["metadata"]) == (MASK_FAILED,) * 3
    assert MASK_FAILED == "<masking failed>"


def test_sampling_is_deterministic_per_trace_id():
    ids = [f"trace-{i}" for i in range(2000)]
    first = [sampled(i, 0.3) for i in ids]
    assert first == [sampled(i, 0.3) for i in ids]
    assert 0.2 < sum(first) / len(ids) < 0.4
    assert all(sampled(i, 1.0) for i in ids) and not any(sampled(i, 0.0) for i in ids)
    # a lower rate samples a subset of a higher one
    assert all(sampled(i, 0.5) for i in ids if sampled(i, 0.3))


def test_unsampled_traces_send_nothing_for_any_of_their_events(make_client, recorder):
    c = make_client(sample_rate=0.5)
    kept = dropped = 0
    for i in range(60):
        with c.start_span(f"s{i}") as span:
            span.start_generation("g").end()
        kept += c.is_sampled(span.trace_id)
        dropped += not c.is_sampled(span.trace_id)
    c.flush()
    assert kept and dropped
    traces = {e["body"]["id"] for e in recorder.of_type("trace-create")}
    assert len(traces) == kept
    for event in recorder.events:
        trace_id = event["body"].get("traceId") or event["body"]["id"]
        assert c.is_sampled(trace_id)


def test_non_json_values_are_serialised(client, recorder):
    import datetime

    class Thing:
        def __init__(self):
            self.a = 1

    client.event("e", input={"when": datetime.datetime(2026, 1, 1), "thing": Thing(), "s": {1, 2}})
    client.flush()
    body = recorder.of_type("event-create")[0]["body"]
    assert body["input"]["thing"] == {"a": 1} and body["input"]["when"].startswith("2026-01-01")


def test_public_calls_never_raise_when_the_transport_blows_up(make_client):
    def explode(request):
        raise RuntimeError("transport bug")

    c = make_client(transport=httpx.MockTransport(explode))
    with c.start_span("s"):
        pass
    assert c.flush() is False


def test_manual_api_trace_update_and_score(client, recorder):
    with client.start_span("root") as root:
        client.update_current_trace(user_id="u1", session_id="s1", tags=["a"], public=True)
        client.update_current_observation(metadata={"k": 1})
        client.score("quality", 0.9, comment="ok")
        assert client.get_current_trace_id() == root.trace_id
        gen = root.start_generation("llm", model="m", usage_details={"input": 1, "output": 2})
        gen.end(output="hi")
    client.score("explicit", "good", trace_id="t-1")
    client.score("flag", True, trace_id="t-1", observation_id="o-1")
    client.flush()
    trace_updates = [e["body"] for e in recorder.of_type("trace-create") if "userId" in e["body"]]
    assert trace_updates == [{"id": root.trace_id, "userId": "u1", "sessionId": "s1",
                              "tags": ["a"], "public": True}]  # fmt: skip
    scores = {e["body"]["name"]: e["body"] for e in recorder.of_type("score-create")}
    assert (
        scores["quality"]["traceId"] == root.trace_id
        and scores["quality"]["observationId"] == root.id
    )
    assert scores["quality"]["dataType"] == "NUMERIC"
    assert (
        scores["explicit"]["dataType"] == "CATEGORICAL" and scores["flag"]["dataType"] == "BOOLEAN"
    )
    generation = recorder.of_type("generation-create")[0]["body"]
    assert generation["parentObservationId"] == root.id and generation["usageDetails"] == {
        "input": 1,
        "output": 2,
    }
    assert get_trace_id_outside() is None


def get_trace_id_outside():
    return traceloom_sdk.get_current_trace_id()


def test_update_current_trace_outside_a_trace_is_ignored(client, sdk_logs):
    client.update_current_trace(user_id="u")
    client.score("x", 1)
    client.update_current_observation(output="x")
    assert "outside an observation" in sdk_logs.text


def test_module_level_api_uses_the_default_client(client, recorder):
    with traceloom_sdk.start_span("top"):
        traceloom_sdk.event("inner")
        assert traceloom_sdk.get_current_trace_id()
    assert traceloom_sdk.flush() is True
    assert [e["type"] for e in recorder.events].count("event-create") == 1


def test_atexit_flush_is_registered_and_unregistered(make_client, monkeypatch):
    import atexit

    registered = []
    monkeypatch.setattr(atexit, "register", lambda fn, *a: registered.append(fn))
    monkeypatch.setattr(atexit, "unregister", lambda fn: registered.remove(fn))
    c = make_client()
    c.event("x")
    assert registered == [c._flush_at_exit]
    c.shutdown(1)
    assert registered == []
