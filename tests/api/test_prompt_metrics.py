"""AC-3: metrics count the generations a real ingest linked to a prompt version."""

from __future__ import annotations

from datetime import timedelta

import pytest

from tests.contract.stores import T0, LiteReadStore
from traceloom.db.models import Observation, Score, Trace
from traceloom.db.session import make_engine, make_session_factory, upgrade_to_head
from traceloom.ports import PromptMetricsQuery

P = "p1"


@pytest.fixture
def store(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'm.db'}")
    upgrade_to_head(engine)
    sessions = make_session_factory(engine)
    with sessions() as s:
        s.add(Trace(project_id=P, id="t1", timestamp=T0, name="t1", environment="production"))
        for oid, version in (("a", 2), ("b", 2), ("c", None)):
            s.add(
                Observation(
                    project_id=P,
                    id=oid,
                    trace_id="t1",
                    type="generation",
                    start_time=T0,
                    end_time=T0 + timedelta(seconds=1),
                    prompt_name="support" if version else None,
                    prompt_version=version,
                    usage_details={"total": 10},
                    cost_details={"total": 0.5},
                )
            )
        s.add(Score(project_id=P, id="s", trace_id="t1", name="q", value=1.0, timestamp=T0))
        s.commit()
    yield LiteReadStore(sessions)
    engine.dispose()


def test_metrics_count_linked_generations(store):
    m = store.prompt_metrics(P, "support", 2, PromptMetricsQuery())
    assert (m.count, m.total_tokens, m.total_cost) == (2, 20, 1.0)
    assert m.median_latency_ms == pytest.approx(1000.0)
    assert m.scores == {"q": 1.0}
    assert store.prompt_metrics(P, "support", 1).count == 0  # a fallback / other version


def test_metrics_are_project_scoped(store):
    assert store.prompt_metrics("p2", "support", 2).count == 0
    assert store.list_prompt_generations("p2", "support", 2) == []
