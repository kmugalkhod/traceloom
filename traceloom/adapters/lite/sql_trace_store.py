"""Lite TraceStore on the meta database (SQLite or Postgres): the write side.

Reads live in SqlReadMixin and dashboard queries in SqlAnalyticsMixin. Every statement filters
on `project_id`, which is the first parameter of every public method.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterable, Iterator, Mapping, Sequence
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from traceloom.adapters.lite.sql_analytics import SqlAnalyticsMixin
from traceloom.adapters.lite.sql_read import SqlReadMixin, trace_filter_clauses
from traceloom.db.models import Comment, Observation, Score, Trace
from traceloom.ingest.merge import EPOCH
from traceloom.ports import LEVELS, ObservationRow, ScoreRow, TraceQuery, TraceRow

CHUNK = 500
SEARCH_TEXT_LIMIT = 4096
# Written on insert only: recomputed by refresh_trace_aggregates, not by upsert.
AGGREGATE_FIELDS = {"latency_ms", "total_tokens", "total_cost", "max_level", "observation_count"}
JSON_TEXT_FIELDS = {"input", "output"}
FLAG_FIELDS = {"bookmarked": bool, "public": bool, "tags": list}


def dump_json(value: Any) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False, default=str)


def load_json(text: str | None) -> Any:
    if text is None:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return text


def _chunks(items: Sequence[str]) -> Iterator[Sequence[str]]:
    for i in range(0, len(items), CHUNK):
        yield items[i : i + CHUNK]


def _column(name: str) -> str:
    return "metadata_" if name == "metadata" else name


def row_to_model_values(row: Any, skip: Iterable[str] = ()) -> dict[str, Any]:
    values = {}
    for f in dataclasses.fields(row):
        if f.name == "scores" or f.name in skip:
            continue
        value = getattr(row, f.name)
        values[_column(f.name)] = dump_json(value) if f.name in JSON_TEXT_FIELDS else value
    return values


def model_to_row(row_cls: type, model: Any) -> Any:
    kwargs = {}
    for f in dataclasses.fields(row_cls):
        if f.name == "scores":
            continue
        value = getattr(model, _column(f.name))
        kwargs[f.name] = load_json(value) if f.name in JSON_TEXT_FIELDS else value
    return row_cls(**kwargs)


def _text(value: Any) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)


def _tokens(usage: Mapping[str, int]) -> int:
    return (
        int(usage["total"])
        if "total" in usage
        else int(usage.get("input", 0) + usage.get("output", 0))
    )


def _cost(cost: Mapping[str, float]) -> float:
    return float(cost["total"]) if "total" in cost else float(sum(cost.values()))


class SqlTraceStore(SqlReadMixin, SqlAnalyticsMixin):
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    # --- writes -----------------------------------------------------------------------------

    def upsert(
        self,
        project_id: str,
        traces: Sequence[TraceRow],
        observations: Sequence[ObservationRow],
        scores: Sequence[ScoreRow],
    ) -> None:
        """Insert rows, or replace a stored row when the incoming `event_ts` is not older."""
        with self.session_factory.begin() as s:
            self._upsert_kind(s, project_id, Trace, traces, AGGREGATE_FIELDS)
            self._upsert_kind(s, project_id, Observation, observations)
            self._upsert_kind(s, project_id, Score, scores)

    @staticmethod
    def _upsert_kind(s: Session, project_id: str, model_cls, rows, keep_on_update=()) -> None:
        incoming: dict[str, Any] = {}
        for row in rows:  # one entry per id; the newest event_ts wins
            current = incoming.get(row.id)
            if current is None or (row.event_ts or EPOCH) >= (current.event_ts or EPOCH):
                incoming[row.id] = row
        ids = list(incoming)
        for chunk in _chunks(ids):
            stored = {
                m.id: m
                for m in s.scalars(
                    select(model_cls).where(
                        model_cls.project_id == project_id, model_cls.id.in_(chunk)
                    )
                )
            }
            for row_id in chunk:
                row, model = incoming[row_id], stored.get(row_id)
                if model is None:
                    s.add(model_cls(project_id=project_id, **row_to_model_values(row)))
                elif (row.event_ts or EPOCH) >= (model.event_ts or EPOCH):
                    for key, value in row_to_model_values(row, skip=keep_on_update).items():
                        setattr(model, key, value)
        s.flush()

    def add_score(self, project_id: str, score: ScoreRow) -> None:
        self.upsert(project_id, [], [], [score])

    def get_existing(
        self, project_id: str, ids_by_kind: Mapping[str, Sequence[str]]
    ) -> dict[str, dict[str, Any]]:
        kinds = {
            "trace": (Trace, TraceRow),
            "observation": (Observation, ObservationRow),
            "score": (Score, ScoreRow),
        }
        out: dict[str, dict[str, Any]] = {}
        with self.session_factory() as s:
            for kind, ids in ids_by_kind.items():
                model_cls, row_cls = kinds[kind]
                found = out.setdefault(kind, {})
                for chunk in _chunks(list(ids)):
                    for m in s.scalars(
                        select(model_cls).where(
                            model_cls.project_id == project_id, model_cls.id.in_(chunk)
                        )
                    ):
                        found[m.id] = model_to_row(row_cls, m)
        return out

    def refresh_trace_aggregates(self, project_id: str, trace_ids: Sequence[str]) -> None:
        with self.session_factory.begin() as s:
            for chunk in _chunks(list(dict.fromkeys(trace_ids))):
                observed = self._observation_summary(s, project_id, chunk)
                for trace in s.scalars(
                    select(Trace).where(Trace.project_id == project_id, Trace.id.in_(chunk))
                ):
                    summary = observed.get(trace.id)
                    if summary is None:
                        trace.latency_ms, trace.total_tokens, trace.total_cost = None, 0, 0.0
                        trace.max_level, trace.observation_count = "DEFAULT", 0
                    else:
                        (
                            trace.latency_ms,
                            trace.total_tokens,
                            trace.total_cost,
                            trace.max_level,
                            trace.observation_count,
                        ) = summary
                    trace.search_text = " ".join(
                        [
                            trace.name or "",
                            trace.id,
                            _text(load_json(trace.input)),
                            _text(load_json(trace.output)),
                        ]
                    ).lower()[:SEARCH_TEXT_LIMIT]

    @staticmethod
    def _observation_summary(s: Session, project_id: str, trace_ids: Sequence[str]):
        acc: dict[str, list] = {}
        query = select(
            Observation.trace_id,
            Observation.start_time,
            Observation.end_time,
            Observation.level,
            Observation.usage_details,
            Observation.cost_details,
        ).where(Observation.project_id == project_id, Observation.trace_id.in_(trace_ids))
        for trace_id, start, end, level, usage, cost in s.execute(query):
            a = acc.setdefault(trace_id, [start, start, 0, 0.0, 0, 0])
            a[0], a[1] = min(a[0], start), max(a[1], end or start)
            a[2] += _tokens(usage or {})
            a[3] += _cost(cost or {})
            a[4] = max(a[4], LEVELS.index(level) if level in LEVELS else 1)
            a[5] += 1
        return {
            tid: ((last - first).total_seconds() * 1000, tokens, cost, LEVELS[lvl], count)
            for tid, (first, last, tokens, cost, lvl, count) in acc.items()
        }

    def update_trace_flags(self, project_id: str, trace_id: str, **fields: Any) -> bool:
        for name, value in fields.items():
            if name not in FLAG_FIELDS or not isinstance(value, FLAG_FIELDS[name]):
                raise ValueError(f"cannot set {name!r} to {value!r}")
        with self.session_factory.begin() as s:
            trace = s.get(Trace, (project_id, trace_id))
            if trace is None:
                return False
            for name, value in fields.items():
                setattr(trace, name, list(dict.fromkeys(value)) if name == "tags" else value)
            return True

    def delete_traces(self, project_id: str, target: Sequence[str] | TraceQuery) -> int:
        ids = (
            self._trace_ids_for_query(project_id, target)
            if isinstance(target, TraceQuery)
            else list(dict.fromkeys(target))
        )
        deleted = 0
        for chunk in _chunks(ids):  # one short transaction per chunk: writers are not starved
            with self.session_factory.begin() as s:
                s.execute(
                    delete(Comment).where(
                        Comment.project_id == project_id, Comment.trace_id.in_(chunk)
                    )
                )
                s.execute(
                    delete(Score).where(Score.project_id == project_id, Score.trace_id.in_(chunk))
                )
                s.execute(
                    delete(Observation).where(
                        Observation.project_id == project_id, Observation.trace_id.in_(chunk)
                    )
                )
                deleted += s.execute(
                    delete(Trace).where(Trace.project_id == project_id, Trace.id.in_(chunk))
                ).rowcount
        return deleted

    def matching_trace_ids(self, project_id: str, query: TraceQuery) -> list[str]:
        """Ids of every trace matching the filter (for counts and delete-by-filter)."""
        return self._trace_ids_for_query(project_id, query)

    def _trace_ids_for_query(self, project_id: str, query: TraceQuery) -> list[str]:
        """Ids of every trace matching the filter (sort, cursor and page size do not apply)."""
        stmt = select(Trace.id).where(*trace_filter_clauses(project_id, query))
        with self.session_factory() as s:
            return list(s.scalars(stmt))

    # --- helpers for tests and ops ----------------------------------------------------------

    def count_traces(self, project_id: str) -> int:
        with self.session_factory() as s:
            return s.scalar(
                select(func.count()).select_from(Trace).where(Trace.project_id == project_id)
            )


__all__ = ["SqlTraceStore"]
