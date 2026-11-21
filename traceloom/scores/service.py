"""Scores: create, list and delete through the TraceStore port, with source rules.

Sources: `API` (SDK and ingestion, no author), `ANNOTATION` (a person, so `author_id`; scores
from a queue also carry `queue_id`) and `EVAL` (a judge, so `execution_id`).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from traceloom.ports import ScoreRow, TraceStore
from traceloom.scores.configs import ConfigCache

SOURCES = ("API", "ANNOTATION", "EVAL")


class ScoreError(ValueError):
    """A score that cannot be saved; the message is safe to show to the user."""


class ScoreNotFound(LookupError):
    pass


def _check_source(source: str, row: ScoreRow) -> None:
    if source not in SOURCES:
        raise ScoreError(f"source must be one of {', '.join(SOURCES)}.")
    if source == "ANNOTATION" and not row.author_id:
        raise ScoreError("An annotation score needs an author.")
    if source != "ANNOTATION" and (row.author_id or row.queue_id):
        raise ScoreError("Only annotation scores have an author or a queue.")
    if source == "EVAL" and not row.execution_id:
        raise ScoreError("An evaluation score needs an execution id.")
    if source != "EVAL" and row.execution_id:
        raise ScoreError("Only evaluation scores have an execution id.")


def create_score(
    store: TraceStore,
    project_id: str,
    *,
    name: str,
    value: float | str | bool,
    source: str = "API",
    configs: ConfigCache | None = None,
    config_id: str | None = None,
    data_type: str | None = None,
    score_id: str | None = None,
    timestamp: datetime | None = None,
    **fields: Any,
) -> ScoreRow:
    """Validate against the config (when `config_id` is set) and write one score.

    `fields` are the remaining ScoreRow columns (trace_id, comment, author_id, queue_id...).
    Re-using `score_id` replaces that score."""
    if not (fields.get("trace_id") or fields.get("session_id")):
        raise ScoreError("A score needs a trace or a session.")
    if config_id and configs is not None:
        problem = configs.check(project_id, config_id, value, data_type)
        if problem:
            raise ScoreError(problem)
    number, text = None, None
    if isinstance(value, bool):
        number, data_type = float(value), data_type or "BOOLEAN"
    elif isinstance(value, str):
        text, data_type = value, data_type or "CATEGORICAL"
    else:
        number, data_type = float(value), data_type or "NUMERIC"
    row = ScoreRow(
        id=score_id or uuid.uuid4().hex,
        name=name.strip()[:200],
        timestamp=timestamp or datetime.now(UTC),
        value=number,
        string_value=text,
        data_type=data_type,
        source=source,
        config_id=config_id,
        **fields,
    )
    if not row.name:
        raise ScoreError("Give the score a name.")
    _check_source(source, row)
    store.add_score(project_id, row)
    return row


def list_scores(store: TraceStore, project_id: str, trace_id: str) -> list[ScoreRow]:
    detail = store.get_trace(project_id, trace_id)
    return list(detail.scores) if detail else []


def delete_score(store: TraceStore, project_id: str, score_id: str) -> None:
    if not store.delete_score(project_id, score_id):
        raise ScoreNotFound(score_id)
