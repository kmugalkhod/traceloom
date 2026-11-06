"""Ports: the interfaces the app depends on. Adapters live in traceloom/adapters/*.

Every TraceStore method takes `project_id` first. There is no way to query trace data
without naming a project.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

LEVELS = ("DEBUG", "DEFAULT", "WARNING", "ERROR")

# --- Cursors ----------------------------------------------------------------------------------


class InvalidCursor(ValueError):
    pass


def _checksum(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()[:12]


def encode_cursor(sort_value: str | int | float | datetime | None, row_id: str) -> str:
    """Opaque keyset cursor: urlsafe base64 of (sort_value, id) plus an integrity checksum.

    The checksum catches corruption and hand-edited cursors. It is not a secret: every query
    is project-scoped, so a forged cursor can only move within the caller's own data.
    """
    if isinstance(sort_value, datetime):
        value: list[Any] = ["dt", sort_value.isoformat()]
    else:
        value = ["v", sort_value]
    payload = json.dumps([value, row_id], separators=(",", ":")).encode()
    return (
        base64.urlsafe_b64encode(payload + b"." + _checksum(payload).encode()).decode().rstrip("=")
    )


def decode_cursor(token: str) -> tuple[Any, str]:
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        payload, _, tag = raw.rpartition(b".")
        if not payload or tag.decode() != _checksum(payload):
            raise InvalidCursor("bad cursor")
        (kind, value), row_id = json.loads(payload)
        if not isinstance(row_id, str) or kind not in ("dt", "v"):
            raise InvalidCursor("bad cursor")
        return (datetime.fromisoformat(value) if kind == "dt" else value), row_id
    except InvalidCursor:
        raise
    except (ValueError, TypeError, binascii.Error, UnicodeDecodeError) as exc:
        raise InvalidCursor("bad cursor") from exc


# --- Query objects ----------------------------------------------------------------------------


@dataclass(frozen=True)
class TraceQuery:
    q: str | None = None
    name: str | None = None
    user_id: str | None = None
    session_id: str | None = None
    tags: Sequence[str] = ()  # any of
    min_level: str | None = None
    environment: str | None = None
    release: str | None = None
    version: str | None = None
    has_error: bool | None = None
    metadata: Mapping[str, str] = field(default_factory=dict)
    latency_min_ms: float | None = None
    latency_max_ms: float | None = None
    cost_min: float | None = None
    cost_max: float | None = None
    bookmarked: bool | None = None
    from_time: datetime | None = None
    to_time: datetime | None = None
    sort: str = "timestamp"  # timestamp | latency | cost | tokens
    descending: bool = True
    cursor: str | None = None
    limit: int = 50


@dataclass(frozen=True)
class SessionQuery:
    environment: str | None = None
    from_time: datetime | None = None
    to_time: datetime | None = None
    sort: str = "last_trace"  # first_trace | last_trace | traces | cost | duration
    descending: bool = True
    cursor: str | None = None
    limit: int = 50


@dataclass(frozen=True)
class UserQuery:
    environment: str | None = None
    from_time: datetime | None = None
    to_time: datetime | None = None
    sort: str = "last_seen"  # last_seen | first_seen | traces | tokens | cost
    descending: bool = True
    cursor: str | None = None
    limit: int = 50


@dataclass(frozen=True)
class DashboardQuery:
    from_time: datetime
    to_time: datetime
    environment: str | None = None
    bucket: str = "hour"  # hour | day


@dataclass
class Page[T]:
    items: list[T]
    next_cursor: str | None = None


# --- Rows -------------------------------------------------------------------------------------


@dataclass
class TraceRow:
    id: str
    timestamp: datetime
    name: str | None = None
    input: Any = None
    output: Any = None
    user_id: str | None = None
    session_id: str | None = None
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    release: str | None = None
    version: str | None = None
    environment: str = "default"
    public: bool = False
    bookmarked: bool = False
    event_ts: datetime | None = None
    latency_ms: float | None = None
    total_tokens: int = 0
    total_cost: float = 0.0
    max_level: str = "DEFAULT"
    observation_count: int = 0
    search_text: str = ""
    scores: list[ScoreRow] = field(default_factory=list)  # filled by list/detail reads


@dataclass
class ObservationRow:
    id: str
    trace_id: str
    start_time: datetime
    parent_observation_id: str | None = None
    type: str = "span"
    name: str | None = None
    end_time: datetime | None = None
    completion_start_time: datetime | None = None
    input: Any = None
    output: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)
    level: str = "DEFAULT"
    status_message: str | None = None
    version: str | None = None
    environment: str = "default"
    model: str | None = None
    model_parameters: dict[str, Any] = field(default_factory=dict)
    usage_details: dict[str, int] = field(default_factory=dict)
    cost_details: dict[str, float] = field(default_factory=dict)
    cost_source: str = "none"  # provided | calculated | none
    usage_source: str = "none"  # provided | tokenizer | none
    prompt_name: str | None = None
    prompt_version: int | None = None
    event_ts: datetime | None = None


@dataclass
class ScoreRow:
    id: str
    name: str
    timestamp: datetime
    trace_id: str | None = None
    observation_id: str | None = None
    session_id: str | None = None
    dataset_run_id: str | None = None
    value: float | None = None
    string_value: str | None = None
    data_type: str = "NUMERIC"  # NUMERIC | BOOLEAN | CATEGORICAL
    comment: str | None = None
    source: str = "API"  # API | ANNOTATION | EVAL
    config_id: str | None = None
    author_id: str | None = None
    environment: str = "default"
    event_ts: datetime | None = None


@dataclass
class TraceDetail:
    trace: TraceRow
    observations: list[ObservationRow]
    scores: list[ScoreRow]


@dataclass
class SessionRow:
    session_id: str
    first_trace_time: datetime
    last_trace_time: datetime
    trace_count: int
    duration_ms: float
    total_cost: float
    user_ids: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)


@dataclass
class SessionDetail:
    session: SessionRow
    traces: list[TraceRow]  # time order


@dataclass
class UserRow:
    user_id: str
    first_seen: datetime
    last_seen: datetime
    trace_count: int
    total_tokens: int
    total_cost: float


@dataclass
class UserSummary:
    user: UserRow
    daily: list[dict[str, Any]]  # {day, traces, tokens, cost}
    recent_traces: list[TraceRow]
    recent_sessions: list[SessionRow]
    recent_scores: list[ScoreRow]


@dataclass
class DashboardData:
    trace_count: int = 0
    total_cost: float = 0.0
    cost_by_model: list[dict[str, Any]] = field(default_factory=list)  # {model, cost}
    tokens_by_model: list[dict[str, Any]] = field(default_factory=list)  # {model, tokens}
    latency_by_trace_name: list[dict[str, Any]] = field(default_factory=list)  # {name, p50..p99}
    latency_by_model: list[dict[str, Any]] = field(default_factory=list)  # {model, p50..p99}
    level_share: list[dict[str, Any]] = field(default_factory=list)  # {level, count, share}
    top_users_by_cost: list[dict[str, Any]] = field(default_factory=list)  # {user_id, cost}
    traces_over_time: list[dict[str, Any]] = field(default_factory=list)  # {bucket, count}


# --- Ports ------------------------------------------------------------------------------------


@runtime_checkable
class TraceStore(Protocol):
    """Trace, observation and score storage. `project_id` is always the first parameter."""

    def upsert(
        self,
        project_id: str,
        traces: Sequence[TraceRow],
        observations: Sequence[ObservationRow],
        scores: Sequence[ScoreRow],
    ) -> None: ...

    def get_existing(
        self, project_id: str, ids_by_kind: Mapping[str, Sequence[str]]
    ) -> dict[str, dict[str, Any]]:
        """Stored rows (TraceRow / ObservationRow / ScoreRow) keyed kind -> id.

        Kinds: trace, observation, score. Missing ids are simply absent.
        """
        ...

    def add_score(self, project_id: str, score: ScoreRow) -> None: ...

    def refresh_trace_aggregates(self, project_id: str, trace_ids: Sequence[str]) -> None: ...

    def list_traces(self, project_id: str, query: TraceQuery) -> Page[TraceRow]: ...

    def get_trace(self, project_id: str, trace_id: str) -> TraceDetail | None: ...

    def update_trace_flags(self, project_id: str, trace_id: str, **fields: Any) -> bool:
        """Set bookmarked / public / tags. Returns False when the trace does not exist."""
        ...

    def delete_traces(self, project_id: str, target: Sequence[str] | TraceQuery) -> int:
        """Delete by ids or by every trace matching a query. Returns the number deleted."""
        ...

    def list_sessions(self, project_id: str, query: SessionQuery) -> Page[SessionRow]: ...

    def get_session(self, project_id: str, session_id: str) -> SessionDetail | None: ...

    def list_users(self, project_id: str, query: UserQuery) -> Page[UserRow]: ...

    def get_user_summary(self, project_id: str, user_id: str) -> UserSummary | None: ...

    def dashboard(self, project_id: str, query: DashboardQuery) -> DashboardData: ...


class BlobStoreError(Exception):
    """A blob operation failed (disk full, S3 unreachable, ...)."""


class BlobNotFound(BlobStoreError):
    pass


@runtime_checkable
class BlobStore(Protocol):
    def put(self, key: str, data: bytes) -> None: ...

    def get(self, key: str) -> bytes: ...

    def delete(self, key: str) -> None: ...

    def move(self, src: str, dst: str) -> None: ...

    def list(self, prefix: str) -> Iterable[tuple[str, datetime]]:
        """(key, last modified) for every blob under the prefix."""
        ...


@runtime_checkable
class Queue(Protocol):
    def enqueue(self, message: Mapping[str, Any]) -> None: ...

    def start(self, handler: Callable[[Mapping[str, Any]], None]) -> None:
        """Begin consuming messages with `handler`."""
        ...

    def stop(self) -> None: ...

    def ping(self) -> bool: ...


@dataclass(frozen=True)
class RateResult:
    allowed: bool
    remaining: int
    retry_after: float = 0.0


@runtime_checkable
class RateLimiter(Protocol):
    def hit(self, key: str, limit: str) -> RateResult:
        """Count one hit against `limit` ("<count>/<second|minute|hour>")."""
        ...


@runtime_checkable
class Cache(Protocol):
    def get(self, key: str) -> Any | None: ...

    def set(self, key: str, value: Any, ttl_seconds: float | None = None) -> None: ...

    def delete(self, key: str) -> None: ...
