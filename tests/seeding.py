"""Bulk dataset seeding straight into the lite tables, plus a store with the real mixins."""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from traceloom.adapters.lite.sql_analytics import SqlAnalyticsMixin
from traceloom.adapters.lite.sql_read import SqlReadMixin
from traceloom.db.models import Observation, Score, Trace

T0 = datetime(2026, 11, 1, tzinfo=UTC)
MODELS = ["claude-sonnet-4-5", "gpt-4o", "gemini-2.5-pro", "llama-3.3-70b"]
NAMES = ["chat", "search", "summarize", "agent-run"]
ENVS = ["production", "staging"]
LEVEL_CHOICES = ["DEBUG", "DEFAULT", "DEFAULT", "DEFAULT", "WARNING", "ERROR"]


class AnalyticsStore(SqlReadMixin, SqlAnalyticsMixin):
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory


@dataclass
class Dataset:
    traces: list[dict[str, Any]]
    observations: list[dict[str, Any]]
    scores: list[dict[str, Any]]


def build_dataset(project_id: str, n_traces: int, obs_per_trace: int, seed: int = 1) -> Dataset:
    """Deterministic traces/observations with realistic spread; whole-millisecond durations."""
    rng = random.Random(seed)
    traces, observations, scores = [], [], []
    for i in range(n_traces):
        start = T0 + timedelta(
            seconds=rng.randrange(0, 5 * 86400), milliseconds=rng.randrange(1000)
        )
        env = rng.choice(ENVS)
        trace_tokens, trace_cost, last_end, max_level, count = 0, 0.0, start, 0, 0
        for j in range(obs_per_trace):
            o_start = start + timedelta(milliseconds=rng.randrange(0, 500))
            has_end = rng.random() > 0.05
            o_end = o_start + timedelta(milliseconds=rng.randrange(5, 4000)) if has_end else None
            model = rng.choice(MODELS) if j % 2 == 0 else None
            usage = cost = {}
            if model:
                tin, tout = rng.randrange(10, 3000), rng.randrange(5, 1000)
                usage = {"input": tin, "output": tout, "total": tin + tout}
                total_cost = round((tin * 3 + tout * 15) * 1e-6, 9)
                cost = {"input": tin * 3e-6, "output": tout * 15e-6, "total": total_cost}
                trace_tokens += tin + tout
                trace_cost += total_cost
            level = rng.choice(LEVEL_CHOICES)
            max_level = max(max_level, ("DEBUG", "DEFAULT", "WARNING", "ERROR").index(level))
            count += 1
            last_end = max(last_end, o_end or o_start)
            observations.append(
                dict(
                    project_id=project_id,
                    id=f"o{i}-{j}",
                    trace_id=f"t{i}",
                    type="generation" if model else "span",
                    name=f"step{j}",
                    start_time=o_start,
                    end_time=o_end,
                    level=level,
                    model=model,
                    usage_details=usage,
                    cost_details=cost,
                    environment=env,
                    metadata_={},
                    model_parameters={},
                    cost_source="calculated" if cost else "none",
                    usage_source="provided" if usage else "none",
                )
            )
        traces.append(
            dict(
                project_id=project_id,
                id=f"t{i}",
                name=rng.choice(NAMES),
                timestamp=start,
                user_id=f"user{rng.randrange(40)}" if rng.random() > 0.1 else None,
                session_id=f"s{rng.randrange(n_traces // 5 + 1)}" if rng.random() > 0.3 else None,
                tags=rng.sample(["a", "b", "c"], rng.randrange(0, 3)),
                metadata_={},
                environment=env,
                latency_ms=float(int((last_end - start).total_seconds() * 1000)),
                total_tokens=trace_tokens,
                total_cost=trace_cost,
                max_level=("DEBUG", "DEFAULT", "WARNING", "ERROR")[max_level],
                observation_count=count,
                search_text=f"t{i}",
            )
        )
        if i % 7 == 0:
            scores.append(
                dict(
                    project_id=project_id,
                    id=f"sc{i}",
                    trace_id=f"t{i}",
                    name="quality",
                    value=float(i % 5),
                    data_type="NUMERIC",
                    source="API",
                    timestamp=start,
                    environment=env,
                )
            )
    return Dataset(traces, observations, scores)


def seed(sessions: sessionmaker[Session], data: Dataset) -> None:
    with sessions.begin() as s:
        s.bulk_insert_mappings(Trace, data.traces)
        s.bulk_insert_mappings(Observation, data.observations)
        s.bulk_insert_mappings(Score, data.scores)
