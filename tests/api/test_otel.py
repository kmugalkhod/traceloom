from __future__ import annotations

import base64
import gzip
import json
from datetime import UTC, datetime

import pytest
from google.protobuf import json_format
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2 as service
from opentelemetry.proto.common.v1 import common_pb2 as common
from opentelemetry.proto.resource.v1 import resource_pb2 as resource
from opentelemetry.proto.trace.v1 import trace_pb2 as trace

from traceloom.auth.permissions import Role
from traceloom.config import Settings
from traceloom.main import create_app

URL = "/api/public/otel/v1/traces"
BASE = int(datetime(2026, 11, 6, 12, 0, tzinfo=UTC).timestamp() * 1e9)
TRACE_ID = bytes.fromhex("0af7651916cd43dd8448eb211c80319c")
ROOT, GEN, LEGACY, TOOL = (
    bytes.fromhex(x)
    for x in ("b7ad6b7169203331", "00f067aa0ba902b7", "1122334455667788", "99aabbccddeeff00")
)


def value(v):
    if isinstance(v, bool):
        return common.AnyValue(bool_value=v)
    if isinstance(v, int):
        return common.AnyValue(int_value=v)
    if isinstance(v, float):
        return common.AnyValue(double_value=v)
    if isinstance(v, list):
        return common.AnyValue(array_value=common.ArrayValue(values=[value(x) for x in v]))
    return common.AnyValue(string_value=str(v))


def attrs(**kv):
    return [common.KeyValue(key=k.replace("__", "."), value=value(v)) for k, v in kv.items()]


def kv(d):
    return [common.KeyValue(key=k, value=value(v)) for k, v in d.items()]


def make_request() -> service.ExportTraceServiceRequest:
    root = trace.Span(
        trace_id=TRACE_ID,
        span_id=ROOT,
        name="support-agent",
        start_time_unix_nano=BASE,
        end_time_unix_nano=BASE + 5_000_000_000,
        attributes=kv(
            {
                "traceloom.observation.type": "agent",
                "traceloom.user_id": "usr_maria",
                "traceloom.session_id": "sess-1",
                "traceloom.tags": ["support", "eu"],
                "traceloom.environment": "production",
                "traceloom.release": "v1.2",
            }
        ),
    )
    gen = trace.Span(
        trace_id=TRACE_ID,
        span_id=GEN,
        parent_span_id=ROOT,
        name="planner",
        start_time_unix_nano=BASE + 100_000_000,
        end_time_unix_nano=BASE + 1_500_000_000,
        attributes=kv(
            {
                "gen_ai.system": "anthropic",
                "gen_ai.request.model": "claude-sonnet-4-5",
                "gen_ai.response.model": "claude-sonnet-4-5-20250929",
                "gen_ai.request.temperature": 0.2,
                "gen_ai.request.max_tokens": 512,
                "gen_ai.usage.input_tokens": 1000,
                "gen_ai.usage.output_tokens": 250,
                "gen_ai.prompt.0.role": "system",
                "gen_ai.prompt.0.content": "Be brief.",
                "gen_ai.prompt.1.role": "user",
                "gen_ai.prompt.1.content": "Where is my order?",
                "gen_ai.completion.0.role": "assistant",
                "gen_ai.completion.0.content": "Let me look.",
            }
        ),
    )
    legacy = trace.Span(
        trace_id=TRACE_ID,
        span_id=LEGACY,
        parent_span_id=ROOT,
        name="final-answer",
        start_time_unix_nano=BASE + 2_000_000_000,
        end_time_unix_nano=BASE + 4_000_000_000,
        attributes=kv(
            {
                "gen_ai.request.model": "gpt-4o",
                "gen_ai.usage.prompt_tokens": 40,
                "gen_ai.usage.completion_tokens": 8,
            }
        ),
        events=[
            trace.Span.Event(
                name="gen_ai.content.prompt", attributes=kv({"gen_ai.prompt": "Summarise."})
            ),
            trace.Span.Event(
                name="gen_ai.content.completion", attributes=kv({"gen_ai.completion": "Done."})
            ),
        ],
    )
    tool = trace.Span(
        trace_id=TRACE_ID,
        span_id=TOOL,
        parent_span_id=ROOT,
        name="lookup_order",
        start_time_unix_nano=BASE + 1_600_000_000,
        end_time_unix_nano=BASE + 1_900_000_000,
        attributes=kv({"order.id": "A-1", "traceloom.observation.type": "tool"}),
        status=trace.Status(code=trace.Status.STATUS_CODE_ERROR, message="order service timed out"),
    )
    scope = trace.ScopeSpans(
        scope=common.InstrumentationScope(name="my-lib", version="0.3"),
        spans=[root, gen, legacy, tool],
    )
    return service.ExportTraceServiceRequest(
        resource_spans=[
            trace.ResourceSpans(
                resource=resource.Resource(
                    attributes=kv({"service.name": "support-agent", "service.version": "1.2"})
                ),
                scope_spans=[scope],
            )
        ]
    )


def as_json(request) -> dict:
    """What an OTLP/JSON exporter sends: camelCase, ids as hex strings."""
    doc = json_format.MessageToDict(request)
    for rs in doc["resourceSpans"]:
        for ss in rs["scopeSpans"]:
            for span in ss["spans"]:
                for k in ("traceId", "spanId", "parentSpanId"):
                    if k in span:
                        span[k] = base64.b64decode(span[k]).hex()
    return doc


def auth(public, secret):
    return {"Authorization": "Basic " + base64.b64encode(f"{public}:{secret}".encode()).decode()}


def send_json(client, headers, doc, **kw):
    return client.post(
        URL, content=json.dumps(doc), headers={**headers, "Content-Type": "application/json", **kw}
    )


def send_pb(client, headers, request, **kw):
    return client.post(
        URL,
        content=request.SerializeToString(),
        headers={**headers, "Content-Type": "application/x-protobuf", **kw},
    )


@pytest.fixture
def key(world, make_key):
    _, public, secret = make_key(world.project)
    return auth(public, secret)


def stored(container, project_id):
    ids = {"observation": [s.hex() for s in (ROOT, GEN, LEGACY, TOOL)], "trace": [TRACE_ID.hex()]}
    return container.trace_store.get_existing(project_id, ids)


def test_json_and_protobuf_give_identical_observations(
    client, container, world, make_key, make_project
):
    other = make_project(world.org)
    _, pk1, sk1 = make_key(world.project)
    _, pk2, sk2 = make_key(other)
    req = make_request()
    assert send_json(client, auth(pk1, sk1), as_json(req)).status_code == 200
    assert send_pb(client, auth(pk2, sk2), req).status_code == 200
    container.queue.drain()
    a, b = stored(container, world.project.id), stored(container, other.id)
    assert len(a["observation"]) == 4
    assert a["observation"] == b["observation"] and a["trace"] == b["trace"]


def test_spans_become_generations_with_model_usage_params_and_parent_links(
    client, container, world, key
):
    send_pb(client, key, make_request())
    container.queue.drain()
    obs = stored(container, world.project.id)["observation"]
    gen = obs[GEN.hex()]
    assert gen.type == "generation" and gen.name == "planner" and gen.trace_id == TRACE_ID.hex()
    assert gen.parent_observation_id == ROOT.hex()
    assert gen.model == "claude-sonnet-4-5-20250929"  # the model that actually answered
    assert (
        gen.usage_details == {"input": 1000, "output": 250, "total": 1250}
        and gen.usage_source == "provided"
    )
    assert gen.model_parameters == {"temperature": 0.2, "max_tokens": 512}
    assert gen.input == [
        {"role": "system", "content": "Be brief."},
        {"role": "user", "content": "Where is my order?"},
    ]
    assert gen.output == [{"role": "assistant", "content": "Let me look."}]
    assert (
        gen.cost_source == "calculated" and gen.cost_details["total"] > 0
    )  # cost engine ran on it
    assert (gen.end_time - gen.start_time).total_seconds() == pytest.approx(1.4)
    assert obs[ROOT.hex()].parent_observation_id is None and obs[ROOT.hex()].type == "agent"


def test_legacy_token_keys_and_content_events(client, container, world, key):
    send_json(client, key, as_json(make_request()))
    container.queue.drain()
    legacy = stored(container, world.project.id)["observation"][LEGACY.hex()]
    assert legacy.type == "generation" and legacy.model == "gpt-4o"
    assert legacy.usage_details == {"input": 40, "output": 8, "total": 48}
    assert legacy.input == "Summarise." and legacy.output == "Done."


def test_error_status_gives_level_error_and_a_message(client, container, world, key):
    send_pb(client, key, make_request())
    container.queue.drain()
    obs = stored(container, world.project.id)["observation"]
    tool = obs[TOOL.hex()]
    assert (
        tool.type == "tool"
        and tool.level == "ERROR"
        and tool.status_message == "order service timed out"
    )
    assert obs[GEN.hex()].level == "DEFAULT" and obs[GEN.hex()].status_message is None
    assert stored(container, world.project.id)["trace"][TRACE_ID.hex()].max_level == "ERROR"


def test_error_status_without_a_message_still_has_one(client, container, world, key):
    req = make_request()
    req.resource_spans[0].scope_spans[0].spans[3].status.message = ""
    send_pb(client, key, req)
    container.queue.drain()
    assert stored(container, world.project.id)["observation"][TOOL.hex()].status_message


def test_traceloom_attributes_populate_the_trace(client, container, world, key):
    send_pb(client, key, make_request())
    container.queue.drain()
    t = stored(container, world.project.id)["trace"][TRACE_ID.hex()]
    assert (t.user_id, t.session_id, t.environment, t.release) == (
        "usr_maria",
        "sess-1",
        "production",
        "v1.2",
    )
    assert sorted(t.tags) == ["eu", "support"]
    assert t.name == "support-agent"  # from the root span (FR-I6)
    assert t.observation_count == 4 and t.total_tokens == 1250 + 48
    assert t.timestamp.timestamp() == pytest.approx(BASE / 1e9)


def test_resource_scope_and_span_attributes_go_to_metadata(client, container, world, key):
    send_pb(client, key, make_request())
    container.queue.drain()
    meta = stored(container, world.project.id)["observation"][TOOL.hex()].metadata
    assert meta["resource"] == {"service.name": "support-agent", "service.version": "1.2"}
    assert meta["scope"] == {"name": "my-lib", "version": "0.3"}
    assert meta["attributes"]["order.id"] == "A-1"
    gen_meta = stored(container, world.project.id)["observation"][GEN.hex()].metadata
    assert (
        "gen_ai.prompt.0.content" not in gen_meta["attributes"]
    )  # content is input/output, not metadata
    assert gen_meta["attributes"]["gen_ai.system"] == "anthropic"


def test_resending_the_same_export_changes_nothing(client, container, world, key):
    req = make_request()
    send_pb(client, key, req)
    container.queue.drain()
    before = stored(container, world.project.id)
    send_json(client, key, as_json(req))
    container.queue.drain()
    assert stored(container, world.project.id) == before
    assert container.trace_store.count_traces(world.project.id) == 1


# --- partial success ---


def test_unparseable_json_spans_are_reported_not_fatal(client, container, world, key):
    doc = as_json(make_request())
    spans = doc["resourceSpans"][0]["scopeSpans"][0]["spans"]
    spans.append({"traceId": "zz", "spanId": "nothex", "name": "broken", "startTimeUnixNano": "1"})
    spans.append("not a span")
    spans.append(
        {"traceId": TRACE_ID.hex(), "spanId": "0102030405060708", "name": "no-start"}
    )  # no start time
    spans.append(
        {
            "traceId": TRACE_ID.hex(),
            "spanId": "0102030405060709",
            "name": "bad-time",
            "startTimeUnixNano": {"x": 1},
        }
    )
    r = send_json(client, key, doc)
    assert r.status_code == 200
    ps = r.json()["partialSuccess"]
    assert ps["rejectedSpans"] == "4" and ps["errorMessage"]
    container.queue.drain()
    assert (
        len(stored(container, world.project.id)["observation"]) == 4
    )  # the good spans got through


def test_invalid_protobuf_spans_are_reported_in_a_protobuf_response(client, container, world, key):
    req = make_request()
    req.resource_spans[0].scope_spans[0].spans.append(
        trace.Span(trace_id=b"short", span_id=ROOT, name="bad-ids", start_time_unix_nano=BASE)
    )
    r = send_pb(client, key, req)
    assert r.status_code == 200 and r.headers["content-type"] == "application/x-protobuf"
    out = service.ExportTraceServiceResponse.FromString(r.content)
    assert (
        out.partial_success.rejected_spans == 1 and "bad-ids" in out.partial_success.error_message
    )
    container.queue.drain()
    assert len(stored(container, world.project.id)["observation"]) == 4


def test_full_success_is_an_empty_response_in_the_clients_format(client, key):
    j = send_json(client, key, as_json(make_request()))
    assert (
        j.status_code == 200
        and j.headers["content-type"].startswith("application/json")
        and j.json() == {}
    )
    p = send_pb(client, key, make_request())
    assert p.headers["content-type"] == "application/x-protobuf"
    assert (
        service.ExportTraceServiceResponse.FromString(p.content).partial_success.rejected_spans == 0
    )


def test_an_empty_export_is_fine(client, key):
    assert send_json(client, key, {"resourceSpans": []}).json() == {}
    assert send_pb(client, key, service.ExportTraceServiceRequest()).status_code == 200


# --- request-level errors ---


@pytest.mark.parametrize("headers", [{}, auth("tl-pk-nope", "tl-sk-nope")])
def test_bad_key_is_401(client, headers):
    r = send_json(client, headers, as_json(make_request()))
    assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized"
    assert send_pb(client, headers, make_request()).status_code == 401


def test_revoked_key_is_401(client, container, world, make_key, db):
    from traceloom.auth.deps import revoke_api_key

    row, public, secret = make_key(world.project)
    assert send_pb(client, auth(public, secret), make_request()).status_code == 200
    revoke_api_key(db, container, row.id)
    db.commit()
    assert send_pb(client, auth(public, secret), make_request()).status_code == 401


def test_cross_project_keys_never_write_into_another_project(
    client, container, world, make_key, make_project
):
    other = make_project(world.org)
    _, pk, sk = make_key(other)
    send_pb(client, auth(pk, sk), make_request())
    container.queue.drain()
    assert stored(container, world.project.id)["observation"] == {}


@pytest.mark.parametrize("content_type", ["text/plain", "application/xml", ""])
def test_unsupported_content_type_is_415(client, key, content_type):
    r = client.post(URL, content=b"{}", headers={**key, "Content-Type": content_type})
    assert r.status_code == 415 and r.json()["error"]["code"] == "error"


def test_garbage_bodies_are_400(client, key):
    assert (
        client.post(
            URL, content=b"not json", headers={**key, "Content-Type": "application/json"}
        ).status_code
        == 400
    )
    assert (
        client.post(
            URL, content=b"[]", headers={**key, "Content-Type": "application/json"}
        ).status_code
        == 400
    )
    assert (
        client.post(
            URL,
            content=b"\xff\xff\xff\xff",
            headers={**key, "Content-Type": "application/x-protobuf"},
        ).status_code
        == 400
    )


def test_gzip_bodies_are_accepted(client, container, world, key):
    body = gzip.compress(make_request().SerializeToString())
    r = client.post(
        URL,
        content=body,
        headers={**key, "Content-Type": "application/x-protobuf", "Content-Encoding": "gzip"},
    )
    assert r.status_code == 200
    container.queue.drain()
    assert len(stored(container, world.project.id)["observation"]) == 4
    bad = client.post(
        URL,
        content=b"not gzip",
        headers={**key, "Content-Type": "application/json", "Content-Encoding": "gzip"},
    )
    assert bad.status_code == 400
    deflate = client.post(
        URL,
        content=b"x",
        headers={**key, "Content-Type": "application/json", "Content-Encoding": "br"},
    )
    assert deflate.status_code == 400


def test_size_limits_cover_gzip_bombs_too(settings, world, make_key):
    from fastapi.testclient import TestClient

    small = Settings(**{**settings.model_dump(by_alias=False), "ingest_max_bytes": 2000})
    with TestClient(create_app(small)) as c:
        sf = c.app.state.container.session_factory
        from traceloom.auth.api_keys import generate_key_pair, hash_secret
        from traceloom.db.models import ApiKey

        pk, sk = generate_key_pair()
        with sf.begin() as s:
            s.add(
                ApiKey(
                    project_id=world.project.id,
                    public_key=pk,
                    secret_hash=hash_secret(sk, small.secret_key),
                )
            )
        headers = {**auth(pk, sk), "Content-Type": "application/x-protobuf"}
        assert (
            c.post(URL, content=make_request().SerializeToString() * 3, headers=headers).status_code
            == 413
        )
        bomb = gzip.compress(b"\x00" * 1_000_000)
        assert (
            c.post(URL, content=bomb, headers={**headers, "Content-Encoding": "gzip"}).status_code
            == 413
        )


def test_storage_failure_is_503(client, container, key, monkeypatch):
    from traceloom.ports import BlobStoreError

    def boom(*a, **k):
        raise BlobStoreError("disk full")

    monkeypatch.setattr(container.blob_store, "put", boom)
    assert send_pb(client, key, make_request()).status_code == 503


def test_span_text_is_stored_as_text_and_escaped_on_the_way_out(client, container, world, key):
    evil = "<script>alert(1)</script>"
    req = make_request()
    req.resource_spans[0].scope_spans[0].spans[0].name = evil
    send_pb(client, key, req)
    container.queue.drain()
    assert (
        stored(container, world.project.id)["observation"][ROOT.hex()].name == evil
    )  # never mangled


def test_all_users_roles_irrelevant_for_key_auth(client, world, key, login):
    login(client, world.user(Role.VIEWER))  # a browser session grants nothing here
    assert send_pb(client, {}, make_request()).status_code == 401
