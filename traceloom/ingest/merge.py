"""ADR-0008: idempotent, order-independent, field-level last-writer-wins merge.

Each entity carries `event_ts`, the newest event timestamp applied to it. An event at or after
`event_ts` overwrites with its non-null fields; an older event only fills fields that are still
unset. Maps merge key by key, `tags` union, and applying the same event again changes nothing.
"""

from __future__ import annotations

import copy
import dataclasses
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cache
from typing import Any

from traceloom.cost.pricing import has_derived_total
from traceloom.ingest.schemas import ObservationBody, ParsedEvent, ScoreBody, TraceBody
from traceloom.ports import ObservationRow, ScoreRow, TraceRow

# Event time of derived (stub) data: any real event overwrites it, and it only ever fills gaps.
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass
class Existing:
    traces: Mapping[str, TraceRow] = field(default_factory=dict)
    observations: Mapping[str, ObservationRow] = field(default_factory=dict)
    scores: Mapping[str, ScoreRow] = field(default_factory=dict)

    @classmethod
    def from_store(cls, found: Mapping[str, Mapping[str, Any]]) -> Existing:
        return cls(found.get("trace", {}), found.get("observation", {}), found.get("score", {}))


@dataclass
class MergeResult:
    traces: dict[str, TraceRow] = field(default_factory=dict)
    observations: dict[str, ObservationRow] = field(default_factory=dict)
    scores: dict[str, ScoreRow] = field(default_factory=dict)

    @property
    def trace_ids(self) -> list[str]:
        return list(self.traces)


def observation_trace_id(body: ObservationBody, entity_id: str) -> str:
    return body.trace_id or entity_id


def needed_ids(events: Sequence[ParsedEvent]) -> dict[str, list[str]]:
    """Ids to fetch with TraceStore.get_existing before merging."""
    ids: dict[str, set[str]] = {"trace": set(), "observation": set(), "score": set()}
    for ev in events:
        ids[ev.kind].add(ev.entity_id)
        if ev.kind == "observation":
            ids["trace"].add(observation_trace_id(ev.body, ev.entity_id))
    return {kind: sorted(v) for kind, v in ids.items() if v}


@cache
def _defaults(cls: type) -> dict[str, Any]:
    out = {}
    for f in dataclasses.fields(cls):
        if f.default is not dataclasses.MISSING:
            out[f.name] = f.default
        elif f.default_factory is not dataclasses.MISSING:
            out[f.name] = f.default_factory()
    return out


# Maps merged key by key; input/output are opaque values that are replaced whole.
MERGED_MAPS = {"metadata", "model_parameters"}


def _unset(row: Any, name: str) -> bool:
    value = getattr(row, name)
    return value is None or (name in _defaults(type(row)) and value == _defaults(type(row))[name])


def _is_newer(row: Any, ts: datetime) -> bool:
    return row.event_ts is None or ts >= row.event_ts


def _apply(row: Any, values: Mapping[str, Any], ts: datetime) -> None:
    newer = _is_newer(row, ts)
    for name, value in values.items():
        if value is None:
            continue
        current = getattr(row, name)
        if name == "tags":
            row.tags = list(current) + [t for t in value if t not in current]
        elif name in MERGED_MAPS:
            setattr(row, name, {**current, **value} if newer else {**value, **current})
        elif newer or _unset(row, name):
            setattr(row, name, value)
    if newer:
        row.event_ts = ts


def _apply_earliest(row: Any, name: str, explicit: datetime | None, newer: bool) -> None:
    """start_time / timestamp: a later event cannot move an explicit start later than a
    provisional one, so an older event keeps the earlier value."""
    if explicit is None:
        return
    current = getattr(row, name)
    setattr(row, name, explicit if newer else min(current, explicit))


def _apply_provided_map(row: ObservationRow, name: str, source: str, value, newer: bool) -> None:
    if not value:
        return
    if getattr(row, source) == "provided":
        current = getattr(row, name)
        if has_derived_total(name, current):
            # A derived total goes stale once the parts change, whichever event arrives first;
            # apply_cost derives it again. A provided total is kept like any other key.
            current = {k: v for k, v in current.items() if k != "total"}
        setattr(row, name, {**current, **value} if newer else {**value, **current})
    else:  # replaces calculated / tokenizer-derived data
        setattr(row, name, dict(value))
        setattr(row, source, "provided")


def _trace_values(body: TraceBody) -> dict[str, Any]:
    return {
        "name": body.name,
        "input": body.input,
        "output": body.output,
        "user_id": body.user_id,
        "session_id": body.session_id,
        "tags": body.tags,
        "metadata": body.metadata,
        "release": body.release,
        "version": body.version,
        "environment": body.environment,
        "public": body.public,
    }


def _observation_values(ev: ParsedEvent) -> dict[str, Any]:
    b: ObservationBody = ev.body  # type: ignore[assignment]
    return {
        "parent_observation_id": b.parent_observation_id,
        "type": b.type or (ev.default_type if ev.op == "create" else None),
        "name": b.name,
        "end_time": b.end_time,
        "completion_start_time": b.completion_start_time,
        "input": b.input,
        "output": b.output,
        "metadata": b.metadata,
        "level": b.level,
        "status_message": b.status_message,
        "version": b.version,
        "environment": b.environment,
        "model": b.model,
        "model_parameters": b.model_parameters,
        "prompt_name": b.prompt_name,
        "prompt_version": b.prompt_version,
    }


def _score_values(b: ScoreBody) -> dict[str, Any]:
    value, string_value, data_type = None, None, "NUMERIC"
    if isinstance(b.value, bool):
        value, data_type = float(b.value), "BOOLEAN"
    elif isinstance(b.value, str):
        string_value, data_type = b.value, "CATEGORICAL"
    else:
        value = float(b.value)
    return {
        "trace_id": b.trace_id,
        "observation_id": b.observation_id,
        "session_id": b.session_id,
        "name": b.name,
        "value": value,
        "string_value": string_value,
        "data_type": b.data_type or data_type,
        "comment": b.comment,
        "environment": b.environment,
        "config_id": b.config_id,
    }


class _Folder:
    def __init__(self, existing: Existing) -> None:
        self.existing = existing
        self.result = MergeResult()

    def _row(self, bucket: dict, stored: Mapping, entity_id: str, make) -> Any:
        if entity_id not in bucket:
            bucket[entity_id] = copy.deepcopy(stored[entity_id]) if entity_id in stored else make()
        return bucket[entity_id]

    def trace(self, ev: ParsedEvent) -> None:
        body: TraceBody = ev.body  # type: ignore[assignment]
        row = self._row(
            self.result.traces,
            self.existing.traces,
            ev.entity_id,
            lambda: TraceRow(id=ev.entity_id, timestamp=ev.timestamp),
        )
        _apply_earliest(row, "timestamp", body.timestamp, _is_newer(row, ev.timestamp))
        _apply(row, _trace_values(body), ev.timestamp)

    def observation(self, ev: ParsedEvent) -> None:
        body: ObservationBody = ev.body  # type: ignore[assignment]
        oid = ev.entity_id
        row = self._row(
            self.result.observations,
            self.existing.observations,
            oid,
            lambda: ObservationRow(
                id=oid,
                trace_id=observation_trace_id(body, oid),
                start_time=ev.timestamp,
                type=ev.default_type or "span",
            ),
        )
        newer = _is_newer(row, ev.timestamp)
        if body.trace_id and (newer or row.trace_id == row.id):
            row.trace_id = body.trace_id  # a bare id means "its own trace"; explicit wins
        _apply_earliest(row, "start_time", body.start_time, newer)
        _apply_provided_map(row, "usage_details", "usage_source", body.usage_details, newer)
        _apply_provided_map(row, "cost_details", "cost_source", body.cost_details, newer)
        _apply(row, _observation_values(ev), ev.timestamp)

    def score(self, ev: ParsedEvent) -> None:
        body: ScoreBody = ev.body  # type: ignore[assignment]
        row = self._row(
            self.result.scores,
            self.existing.scores,
            ev.entity_id,
            lambda: ScoreRow(id=ev.entity_id, name=body.name, timestamp=ev.timestamp),
        )
        _apply(row, _score_values(body), ev.timestamp)

    def link_traces(self) -> None:
        """FR-I6: unknown traces get a stub; root spans fill the stub's name/input/output."""
        roots: dict[str, list[ObservationRow]] = {}
        for obs in self.result.observations.values():
            self._row(
                self.result.traces,
                self.existing.traces,
                obs.trace_id,
                lambda obs=obs: TraceRow(
                    id=obs.trace_id,
                    timestamp=obs.start_time,
                    event_ts=EPOCH,
                    environment=obs.environment,
                ),
            )
            if obs.parent_observation_id is None:
                roots.setdefault(obs.trace_id, []).append(obs)
        for trace_id, root_spans in roots.items():
            trace = self.result.traces[trace_id]
            first = min(root_spans, key=lambda o: (o.start_time, o.id))
            _apply(
                trace,
                {"name": first.name, "input": first.input, "output": first.output},
                EPOCH,
            )
        for obs in self.result.observations.values():
            trace = self.result.traces[obs.trace_id]
            if trace.event_ts == EPOCH:  # still a stub: it starts with its earliest observation
                trace.timestamp = min(trace.timestamp, obs.start_time)


def merge_events(events: Sequence[ParsedEvent], existing: Existing) -> MergeResult:
    """Fold events into entity rows. The inputs are not mutated."""
    folder = _Folder(existing)
    ordered = sorted(enumerate(events), key=lambda pair: (pair[1].timestamp, pair[0]))
    for _, ev in ordered:
        getattr(folder, ev.kind)(ev)
    folder.link_traces()
    return folder.result
