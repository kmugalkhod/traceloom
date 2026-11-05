"""Pydantic models for ingestion events (FR-I1). Accepts snake_case and camelCase field names."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic.alias_generators import to_camel

from traceloom.ports import LEVELS

OBSERVATION_TYPES = (
    "span",
    "generation",
    "event",
    "agent",
    "tool",
    "chain",
    "retriever",
    "embedding",
    "evaluator",
    "guardrail",
)
# event type -> (entity kind, operation, default observation type)
EVENT_TYPES: dict[str, tuple[str, str, str | None]] = {
    "trace-create": ("trace", "create", None),
    "span-create": ("observation", "create", "span"),
    "span-update": ("observation", "update", "span"),
    "generation-create": ("observation", "create", "generation"),
    "generation-update": ("observation", "update", "generation"),
    "event-create": ("observation", "create", "event"),
    "observation-create": ("observation", "create", None),
    "observation-update": ("observation", "update", None),
    "score-create": ("score", "create", None),
}

Id = Annotated[str, StringConstraints(min_length=1, max_length=200)]
Short = Annotated[str, StringConstraints(max_length=500)]


def _aware(value: Any) -> Any:
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC) if isinstance(value, datetime) else value


class _Body(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")

    @field_validator("*", mode="after")
    @classmethod
    def _utc(cls, v: Any) -> Any:
        return _aware(v)


class TraceBody(_Body):
    id: Id | None = None
    name: Short | None = None
    timestamp: datetime | None = None
    input: Any = None
    output: Any = None
    user_id: Id | None = None
    session_id: Id | None = None
    tags: list[Annotated[str, StringConstraints(max_length=200)]] | None = None
    metadata: dict[str, Any] | None = None
    release: Id | None = None
    version: Id | None = None
    environment: Id | None = None
    public: bool | None = None


class ObservationBody(_Body):
    id: Id | None = None
    trace_id: Id | None = None
    parent_observation_id: Id | None = None
    type: Literal[OBSERVATION_TYPES] | None = None  # type: ignore[valid-type]
    name: Short | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None
    completion_start_time: datetime | None = None
    input: Any = None
    output: Any = None
    metadata: dict[str, Any] | None = None
    level: Literal[LEVELS] | None = None  # type: ignore[valid-type]
    status_message: str | None = None
    version: Id | None = None
    environment: Id | None = None
    model: Id | None = None
    model_parameters: dict[str, Any] | None = None
    usage_details: dict[str, int] | None = None
    cost_details: dict[str, float] | None = None
    prompt_name: Id | None = None
    prompt_version: int | None = None

    @field_validator("usage_details", "cost_details", mode="after")
    @classmethod
    def _non_negative(cls, v: dict | None) -> dict | None:
        if v and any(x < 0 for x in v.values()):
            raise ValueError("must not contain negative values")
        return v


class ScoreBody(_Body):
    id: Id | None = None
    trace_id: Id | None = None
    observation_id: Id | None = None
    session_id: Id | None = None
    name: Short
    value: float | str | bool
    data_type: Literal["NUMERIC", "BOOLEAN", "CATEGORICAL"] | None = None
    comment: str | None = None
    environment: Id | None = None
    config_id: Id | None = None

    @model_validator(mode="after")
    def _needs_target(self) -> ScoreBody:
        if not (self.trace_id or self.session_id):
            raise ValueError("a score needs trace_id or session_id")
        return self


class Envelope(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: Id
    type: str
    timestamp: datetime
    body: dict[str, Any]

    @field_validator("timestamp", mode="after")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        return _aware(v)


@dataclass(frozen=True)
class ParsedEvent:
    id: str
    type: str
    kind: str  # trace | observation | score
    op: str  # create | update
    timestamp: datetime
    body: TraceBody | ObservationBody | ScoreBody
    default_type: str | None = None  # observation type implied by the event type

    @property
    def entity_id(self) -> str:
        return self.body.id or self.id


class EventError(ValueError):
    pass


def _describe(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors()[:3]:
        loc = ".".join(str(p) for p in err["loc"])
        parts.append(f"{loc}: {err['msg']}" if loc else err["msg"])
    return "; ".join(parts)


def parse_event(raw: Any) -> ParsedEvent:
    """Validate one raw event dict. Raises EventError with a short message."""
    if not isinstance(raw, dict):
        raise EventError("event must be an object")
    try:
        env = Envelope.model_validate(raw)
        if env.type not in EVENT_TYPES:
            raise EventError(f"unsupported event type {env.type!r}")
        kind, op, default_type = EVENT_TYPES[env.type]
        body_model = {"trace": TraceBody, "observation": ObservationBody, "score": ScoreBody}[kind]
        body = body_model.model_validate(env.body)
    except ValidationError as exc:
        raise EventError(_describe(exc)) from exc
    return ParsedEvent(env.id, env.type, kind, op, env.timestamp, body, default_type)


__all__ = ["EventError", "ParsedEvent", "parse_event"]
