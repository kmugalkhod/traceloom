"""OTLP/HTTP traces -> Traceloom ingestion events (FR-I4).

JSON and protobuf bodies are both parsed into the same `ExportTraceServiceRequest` message and
converted by one code path, so the same spans give identical observations either way.
"""

from __future__ import annotations

import base64
import binascii
import json
import zlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from google.protobuf import json_format
from google.protobuf.message import DecodeError
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2 as service
from opentelemetry.proto.common.v1 import common_pb2 as common
from opentelemetry.proto.trace.v1 import trace_pb2 as trace

from traceloom.ingest.schemas import OBSERVATION_TYPES

JSON_TYPE = "application/json"
PROTOBUF_TYPE = "application/x-protobuf"
ERROR_FALLBACK_MESSAGE = "Span ended with an error status."
# gen_ai.request.* keys that are not model parameters
_NOT_PARAMS = {"model"}
_CONTENT_PREFIXES = (
    "gen_ai.prompt",
    "gen_ai.completion",
    "gen_ai.input.messages",
    "gen_ai.output.messages",
)


class OtlpError(ValueError):
    """The whole request cannot be parsed (HTTP 400)."""


class PayloadTooLarge(OtlpError):
    pass


@dataclass
class Converted:
    events: list[dict[str, Any]] = field(default_factory=list)
    rejected: int = 0
    errors: list[str] = field(default_factory=list)

    def reject(self, message: str) -> None:
        self.rejected += 1
        if len(self.errors) < 5:
            self.errors.append(message)


# --- decoding the request body -------------------------------------------------------------------


def decompress(body: bytes, encoding: str, max_bytes: int) -> bytes:
    """Undo Content-Encoding (exporters usually gzip), refusing to inflate past max_bytes."""
    encoding = (encoding or "identity").lower()
    if encoding == "identity":
        return body
    if encoding != "gzip":
        raise OtlpError(f"Unsupported Content-Encoding {encoding!r}.")
    inflater = zlib.decompressobj(wbits=31)
    try:
        out = inflater.decompress(body, max_bytes + 1)
        if len(out) > max_bytes or inflater.unconsumed_tail:
            raise PayloadTooLarge(f"Request body exceeds {max_bytes} bytes once decompressed.")
        return out + inflater.flush()
    except zlib.error as exc:
        raise OtlpError("Body is not valid gzip.") from exc


def _hex_to_b64(value: Any) -> Any:
    """OTLP/JSON writes trace and span ids as hex; proto3 JSON expects base64 for bytes."""
    if isinstance(value, str):
        try:
            return base64.b64encode(bytes.fromhex(value)).decode()
        except ValueError:
            return value
    return value


def parse_json(body: bytes) -> tuple[service.ExportTraceServiceRequest, Converted]:
    """Parse OTLP/JSON span by span, so one malformed span does not sink the request."""
    try:
        doc = json.loads(body)
    except ValueError as exc:
        raise OtlpError("Body is not valid JSON.") from exc
    if not isinstance(doc, dict):
        raise OtlpError("Body must be a JSON object.")
    request = service.ExportTraceServiceRequest()
    problems = Converted()
    for rs in _list(doc.get("resourceSpans")):
        resource_spans = request.resource_spans.add()
        _merge(
            resource_spans.resource, rs.get("resource") if isinstance(rs, dict) else None, problems
        )
        for ss in _list(rs.get("scopeSpans") if isinstance(rs, dict) else None):
            scope_spans = resource_spans.scope_spans.add()
            _merge(scope_spans.scope, ss.get("scope") if isinstance(ss, dict) else None, problems)
            for raw in _list(ss.get("spans") if isinstance(ss, dict) else None):
                span = scope_spans.spans.add()
                try:
                    if not isinstance(raw, dict):
                        raise json_format.ParseError("span must be an object")
                    fixed = {**raw}
                    for key in ("traceId", "spanId", "parentSpanId"):
                        if key in fixed:
                            fixed[key] = _hex_to_b64(fixed[key])
                    json_format.ParseDict(fixed, span, ignore_unknown_fields=True)
                except (json_format.ParseError, ValueError, TypeError, binascii.Error) as exc:
                    scope_spans.spans.pop()
                    problems.reject(f"unparseable span: {exc}")
    return request, problems


def _list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _merge(message: Any, value: Any, problems: Converted) -> None:
    if not isinstance(value, dict):
        return
    try:
        json_format.ParseDict(value, message, ignore_unknown_fields=True)
    except (json_format.ParseError, ValueError, TypeError):
        pass  # unreadable resource/scope attributes only cost us the metadata


def parse_protobuf(body: bytes) -> service.ExportTraceServiceRequest:
    request = service.ExportTraceServiceRequest()
    try:
        request.ParseFromString(body)
    except DecodeError as exc:
        raise OtlpError("Body is not a valid OTLP protobuf message.") from exc
    return request


# --- attribute helpers --------------------------------------------------------------------------


def any_value(v: common.AnyValue) -> Any:
    kind = v.WhichOneof("value")
    if kind == "string_value":
        return v.string_value
    if kind == "bool_value":
        return v.bool_value
    if kind == "int_value":
        return v.int_value
    if kind == "double_value":
        return v.double_value
    if kind == "array_value":
        return [any_value(x) for x in v.array_value.values]
    if kind == "kvlist_value":
        return {kv.key: any_value(kv.value) for kv in v.kvlist_value.values}
    if kind == "bytes_value":
        return v.bytes_value.hex()
    return None


def attributes(items: Any) -> dict[str, Any]:
    return {kv.key: any_value(kv.value) for kv in items}


def _json_or_text(value: Any) -> Any:
    """Message attributes arrive as JSON text from many instrumentations."""
    if isinstance(value, str) and value[:1] in "[{":
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _nanos(value: int) -> datetime | None:
    if value <= 0:
        return None
    return datetime.fromtimestamp(value // 1_000_000_000, UTC).replace(
        microsecond=(value % 1_000_000_000) // 1000
    )


def _tags(value: Any) -> list[str] | None:
    if isinstance(value, str):
        value = [t.strip() for t in value.split(",")]
    if isinstance(value, list):
        tags = [str(t) for t in value if str(t).strip()]
        return tags or None
    return None


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# --- gen_ai: generation fields ------------------------------------------------------------------


def _indexed_messages(attrs: dict[str, Any], prefix: str) -> list[dict[str, Any]]:
    """gen_ai.prompt.0.role / gen_ai.prompt.0.content ... -> [{role, content}, ...]"""
    messages: dict[int, dict[str, Any]] = {}
    for key, value in attrs.items():
        if key.startswith(prefix + "."):
            index, _, field_name = key[len(prefix) + 1 :].partition(".")
            if index.isdigit() and field_name:
                messages.setdefault(int(index), {})[field_name] = value
    return [messages[i] for i in sorted(messages)]


def _event_messages(span: trace.Span, names: dict[str, str | None]) -> Any:
    out = []
    for event in span.events:
        if event.name not in names:
            continue
        a = attributes(event.attributes)
        content = a.get("content", a.get("gen_ai.prompt", a.get("gen_ai.completion")))
        if content is None and "message" in a:
            content = a["message"]
        role = names[event.name] or a.get("role")
        out.append(
            {"role": role, "content": _json_or_text(content)} if role else _json_or_text(content)
        )
    # A lone role-less event (the legacy prompt/completion events) is the value itself.
    return out[0] if len(out) == 1 and not isinstance(out[0], dict) else out


_INPUT_EVENTS = {
    "gen_ai.system.message": "system",
    "gen_ai.user.message": "user",
    "gen_ai.assistant.message": "assistant",
    "gen_ai.tool.message": "tool",
    "gen_ai.content.prompt": None,
}
_OUTPUT_EVENTS = {"gen_ai.choice": "assistant", "gen_ai.content.completion": None}


def generation_fields(span: trace.Span, attrs: dict[str, Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    model = attrs.get("gen_ai.response.model") or attrs.get("gen_ai.request.model")
    if model:
        fields["model"] = str(model)
    params = {
        key[len("gen_ai.request.") :]: value
        for key, value in attrs.items()
        if key.startswith("gen_ai.request.") and key[len("gen_ai.request.") :] not in _NOT_PARAMS
    }
    if params:
        fields["model_parameters"] = params
    tokens_in = _int(
        attrs.get("gen_ai.usage.input_tokens", attrs.get("gen_ai.usage.prompt_tokens"))
    )
    tokens_out = _int(
        attrs.get("gen_ai.usage.output_tokens", attrs.get("gen_ai.usage.completion_tokens"))
    )
    usage = {
        k: v for k, v in (("input", tokens_in), ("output", tokens_out)) if v is not None and v >= 0
    }
    if usage:
        usage["total"] = usage.get("input", 0) + usage.get("output", 0)
        fields["usage_details"] = usage

    prompt = (
        _json_or_text(attrs.get("gen_ai.input.messages"))
        or _indexed_messages(attrs, "gen_ai.prompt")
        or _json_or_text(attrs.get("gen_ai.prompt"))
        or _event_messages(span, _INPUT_EVENTS)
    )
    completion = (
        _json_or_text(attrs.get("gen_ai.output.messages"))
        or _indexed_messages(attrs, "gen_ai.completion")
        or _json_or_text(attrs.get("gen_ai.completion"))
        or _event_messages(span, _OUTPUT_EVENTS)
    )
    if prompt:
        fields["input"] = prompt
    if completion:
        fields["output"] = completion
    return fields


# --- spans -> events ----------------------------------------------------------------------------


def _span_event(
    span: trace.Span, resource: dict[str, Any], scope: Any
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The observation event for one span, and the trace-level fields found on it."""
    if len(span.trace_id) != 16 or not any(span.trace_id):
        raise ValueError("trace_id must be 16 non-zero bytes")
    if len(span.span_id) != 8 or not any(span.span_id):
        raise ValueError("span_id must be 8 non-zero bytes")
    start, end = _nanos(span.start_time_unix_nano), _nanos(span.end_time_unix_nano)
    if start is None:
        raise ValueError("start_time_unix_nano is required")

    attrs = attributes(span.attributes)
    span_id, trace_id = span.span_id.hex(), span.trace_id.hex()
    declared = attrs.get("traceloom.observation.type")
    is_gen = any(k.startswith("gen_ai.") for k in attrs)
    obs_type = declared if declared in OBSERVATION_TYPES else ("generation" if is_gen else "span")

    body: dict[str, Any] = {
        "id": span_id,
        "traceId": trace_id,
        "name": span.name,
        "startTime": start.isoformat(),
        "type": obs_type,
    }
    if span.parent_span_id:
        body["parentObservationId"] = span.parent_span_id.hex()
    if end:
        body["endTime"] = end.isoformat()
    if span.status.code == trace.Status.STATUS_CODE_ERROR:
        body["level"] = "ERROR"
        body["statusMessage"] = span.status.message or ERROR_FALLBACK_MESSAGE
    elif span.status.message:
        body["statusMessage"] = span.status.message
    if is_gen or obs_type == "generation":
        for key, value in generation_fields(span, attrs).items():
            body[_camel(key)] = value
    for key, target in (("traceloom.input", "input"), ("traceloom.output", "output")):
        if key in attrs and target not in body:
            body[target] = _json_or_text(attrs[key])

    trace_fields = {
        "userId": attrs.get("traceloom.user_id", attrs.get("user.id")),
        "sessionId": attrs.get("traceloom.session_id", attrs.get("session.id")),
        "tags": _tags(attrs.get("traceloom.tags")),
        "environment": attrs.get("traceloom.environment"),
        "release": attrs.get("traceloom.release"),
        "version": attrs.get("traceloom.version"),
    }
    trace_fields = {
        k: str(v) if k != "tags" else v for k, v in trace_fields.items() if v not in (None, "")
    }
    if "environment" in trace_fields:
        body["environment"] = trace_fields["environment"]
    if "version" in trace_fields:
        body["version"] = trace_fields["version"]

    metadata: dict[str, Any] = {}
    if resource:
        metadata["resource"] = resource
    scope_meta = {k: v for k, v in (("name", scope.name), ("version", scope.version)) if v}
    scope_attrs = attributes(scope.attributes)
    if scope_attrs:
        scope_meta["attributes"] = scope_attrs
    if scope_meta:
        metadata["scope"] = scope_meta
    plain = {k: v for k, v in attrs.items() if not k.startswith(_CONTENT_PREFIXES)}
    if plain:  # message content already became input/output; do not store it twice
        metadata["attributes"] = plain
    if metadata:
        body["metadata"] = metadata

    kind = {"generation": "generation-create", "span": "span-create"}.get(
        obs_type, "observation-create"
    )
    event = {
        "id": f"otlp-{span_id}",
        "type": kind,
        "timestamp": (end or start).isoformat(),
        "body": body,
    }
    return event, {"_trace_id": trace_id, "_start": start, **trace_fields}


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(p.title() for p in rest)


def convert(
    request: service.ExportTraceServiceRequest, problems: Converted | None = None
) -> Converted:
    """All spans in the request as ingestion events. Spans that cannot be converted are counted
    as rejected and skipped; the rest still go through."""
    result = problems or Converted()
    traces: dict[str, dict[str, Any]] = {}
    for rs in request.resource_spans:
        resource = attributes(rs.resource.attributes)
        for ss in rs.scope_spans:
            for span in ss.spans:
                try:
                    event, trace_fields = _span_event(span, resource, ss.scope)
                except ValueError as exc:
                    result.reject(f"invalid span {span.name or span.span_id.hex() or '?'}: {exc}")
                    continue
                result.events.append(event)
                merged = traces.setdefault(trace_fields.pop("_trace_id"), {})
                start = trace_fields.pop("_start")
                merged["_start"] = min(merged.get("_start", start), start)
                for key, value in trace_fields.items():
                    merged.setdefault(key, value)
    for trace_id, fields in traces.items():
        start = fields.pop("_start")
        if fields:  # only spans that carry trace-level attributes need an explicit trace event
            result.events.append(
                {
                    "id": f"otlp-trace-{trace_id}",
                    "type": "trace-create",
                    "timestamp": start.isoformat(),
                    "body": {"id": trace_id, "timestamp": start.isoformat(), **fields},
                }
            )
    return result


# --- response -----------------------------------------------------------------------------------


def response_body(content_type: str, rejected: int, message: str) -> tuple[bytes, str]:
    """ExportTraceServiceResponse in the format the client used."""
    response = service.ExportTraceServiceResponse()
    if rejected:
        response.partial_success.rejected_spans = rejected
        response.partial_success.error_message = message
    if content_type == PROTOBUF_TYPE:
        return response.SerializeToString(), PROTOBUF_TYPE
    return json_format.MessageToJson(
        response, preserving_proto_field_name=False
    ).encode(), JSON_TYPE
