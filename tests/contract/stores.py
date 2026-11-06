"""Store fixtures shared by contract and API tests (loaded as a pytest plugin from conftest.py)."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from tests.contract import conftest_full
from traceloom.adapters.lite.sql_read import SqlReadMixin
from traceloom.db.base import Base
from traceloom.db.models import Observation, Score, Trace
from traceloom.db.session import make_engine, make_session_factory
from traceloom.ports import ObservationRow, ScoreRow, TraceRow

T0 = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


class LiteReadStore(SqlReadMixin):
    """The read side only, over a schema created straight from the models."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory


@pytest.fixture
def lite_session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = make_engine(f"sqlite:///{tmp_path / 'read.db'}")
    Base.metadata.create_all(engine)
    yield make_session_factory(engine)
    engine.dispose()


@pytest.fixture(params=["lite", conftest_full.CLICKHOUSE_PARAM])
def read_store(request, lite_session_factory):
    """The store under test: lite always, ClickHouse when TRACELOOM_IT=1."""
    if request.param == "lite":
        yield LiteReadStore(lite_session_factory)
    else:
        yield from conftest_full.open_clickhouse_store()


def _encode_io(fields: dict[str, Any]) -> dict[str, Any]:
    return {
        k: json.dumps(v) if k in ("input", "output") and v is not None else v
        for k, v in fields.items()
    }


def _lite_kwargs(row: Any) -> dict[str, Any]:
    fields = {f.name: getattr(row, f.name) for f in dataclasses.fields(row) if f.name != "scores"}
    if "metadata" in fields:
        fields["metadata_"] = fields.pop("metadata")
    return _encode_io(fields)


@pytest.fixture
def seed(read_store) -> Callable[..., TraceRow]:
    """seed(project_id, id, observations=(), scores=(), **trace_fields) writes rows directly,
    bypassing ingestion: through the T2 models on lite, a plain INSERT on ClickHouse.

    Trace fields are TraceRow fields; observations and scores are dicts of their row fields.
    search_text is derived from name/input/output unless given.
    """

    def add(project_id: str, trace_id: str, observations=(), scores=(), **fields: Any) -> TraceRow:
        fields.setdefault("timestamp", T0)
        fields.setdefault("name", trace_id)
        fields.setdefault(
            "search_text",
            " ".join(str(fields.get(k) or "") for k in ("name", "input", "output")),
        )
        trace = TraceRow(id=trace_id, **fields)
        obs = [ObservationRow(trace_id=trace_id, **o) for o in observations]
        scs = [ScoreRow(trace_id=trace_id, **s) for s in scores]
        if isinstance(read_store, LiteReadStore):
            with read_store.session_factory() as session:
                session.add(Trace(project_id=project_id, **_lite_kwargs(trace)))
                session.add_all(Observation(project_id=project_id, **_lite_kwargs(o)) for o in obs)
                session.add_all(Score(project_id=project_id, **_lite_kwargs(s)) for s in scs)
                session.commit()
        else:
            read_store._insert("traces", project_id, [trace])
            read_store._insert("observations", project_id, obs)
            read_store._insert("scores", project_id, scs)
        return trace

    return add


def minutes(n: int) -> datetime:
    return T0 + timedelta(minutes=n)
