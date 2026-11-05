"""Store fixtures shared by contract and API tests (loaded as a pytest plugin from conftest.py)."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from traceloom.adapters.lite.sql_read import SqlReadMixin
from traceloom.db.base import Base
from traceloom.db.models import Observation, Score, Trace
from traceloom.db.session import make_engine, make_session_factory

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


@pytest.fixture
def read_store(lite_session_factory) -> LiteReadStore:
    return LiteReadStore(lite_session_factory)


def _encode_io(fields: dict[str, Any]) -> dict[str, Any]:
    return {
        k: json.dumps(v) if k in ("input", "output") and v is not None else v
        for k, v in fields.items()
    }


@pytest.fixture
def seed(lite_session_factory) -> Callable[..., Trace]:
    """seed(project_id, id, **fields) inserts a trace row; fields are Trace columns.

    input/output may be any JSON value; search_text is derived unless given.
    """

    def add(project_id: str, trace_id: str, observations=(), scores=(), **fields: Any) -> Trace:
        fields.setdefault("timestamp", T0)
        fields.setdefault("name", trace_id)
        fields = _encode_io(fields)
        fields.setdefault(
            "search_text",
            " ".join(str(fields.get(k) or "") for k in ("name", "input", "output")),
        )
        if "metadata" in fields:
            fields["metadata_"] = fields.pop("metadata")
        trace = Trace(project_id=project_id, id=trace_id, **fields)
        with lite_session_factory() as session:
            session.add(trace)
            session.add_all(
                Observation(project_id=project_id, trace_id=trace_id, **_encode_io(o))
                for o in observations
            )
            session.add_all(Score(project_id=project_id, trace_id=trace_id, **s) for s in scores)
            session.commit()
        return trace

    return add


def minutes(n: int) -> datetime:
    return T0 + timedelta(minutes=n)
