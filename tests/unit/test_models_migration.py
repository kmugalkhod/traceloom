import inspect
from datetime import UTC, datetime

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Session

from traceloom import ports
from traceloom.db import models
from traceloom.db.base import Base
from traceloom.db.session import make_engine, upgrade_to_head

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'm.db'}")
    upgrade_to_head(engine)
    yield engine
    engine.dispose()


def test_migration_builds_every_table_and_index(engine):
    insp = sa_inspect(engine)
    assert set(insp.get_table_names()) - {"alembic_version"} == set(Base.metadata.tables)
    expected = {
        "traces": {
            "ix_traces_project_timestamp_id",
            "ix_traces_project_session",
            "ix_traces_project_user",
            "ix_traces_project_env_timestamp",
        },
        "observations": {"ix_observations_project_trace", "ix_observations_project_start"},
        "scores": {"ix_scores_project_trace"},
    }
    for table, names in expected.items():
        assert names <= {i["name"] for i in insp.get_indexes(table)}


def test_timestamp_index_is_descending(engine):
    with engine.connect() as conn:
        sql = conn.exec_driver_sql(
            "select sql from sqlite_master where name='ix_traces_project_timestamp_id'"
        ).scalar_one()
    assert "DESC" in sql.upper()


def test_models_match_migration(engine):
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == []


def test_sqlite_pragmas_and_percentile(engine):
    with engine.connect() as conn:
        assert conn.exec_driver_sql("pragma journal_mode").scalar_one() == "wal"
        assert conn.exec_driver_sql("pragma foreign_keys").scalar_one() == 1
        conn.exec_driver_sql("create temp table t(v real)")
        conn.exec_driver_sql("insert into t values (1),(2),(3),(4)")
        assert conn.exec_driver_sql("select percentile(v, 0.5) from t").scalar_one() == 2.5


def _rows():
    user = models.User(id="u1", email="a@example.com", password_hash="x", created_at=NOW)
    org = models.Organization(id="o1", name="Org", created_at=NOW)
    project = models.Project(id="p1", org_id="o1", name="Proj")
    return [
        user,
        org,
        models.Membership(user_id="u1", org_id="o1", role="owner"),
        models.Invite(
            id="i1",
            org_id="o1",
            email="b@example.com",
            role="member",
            token_hash="t1",
            expires_at=NOW,
        ),
        project,
        models.ApiKey(id="k1", project_id="p1", public_key="tl-pk-1", secret_hash="h1"),
        models.PasswordReset(token_hash="r1", user_id="u1", expires_at=NOW),
        models.ModelDefinition(
            id="m1",
            project_id=None,
            model_name="gpt",
            match_pattern="^gpt",
            prices={"input": 1e-6},
            tiers=[],
            valid_from=NOW,
        ),
        models.SavedView(id="v1", project_id="p1", owner_id="u1", name="v", query="q=1"),
        models.UserPref(user_id="u1", project_id="p1", key="columns", value="a,b"),
        models.Comment(
            id="c1",
            project_id="p1",
            object_type="trace",
            object_id="t1",
            trace_id="t1",
            author_id="u1",
            body="hi",
        ),
        models.AuditLog(
            id="a1",
            org_id="o1",
            project_id="p1",
            actor_type="user",
            actor_id="u1",
            action="trace.delete",
            detail={"count": 1},
        ),
        models.Prompt(id="pr1", project_id="p1", name="support/greeting", type="text"),
        models.PromptVersion(
            id="pv1", project_id="p1", prompt_id="pr1", version=1, content="Hi {{name}}"
        ),
        models.PromptLabel(project_id="p1", prompt_id="pr1", label="staging", version=1),
        models.ProtectedLabel(project_id="p1", label="staging"),
        models.LLMConnection(id="lc1", project_id="p1", provider="openai", name="main"),
        models.Webhook(id="wh1", project_id="p1", url="https://example.com/hook"),
        models.WebhookDelivery(id="wd1", webhook_id="wh1", project_id="p1", event="prompt.label"),
        models.Trace(project_id="p1", id="t1", timestamp=NOW, tags=["x"], metadata_={"k": "v"}),
        models.Observation(
            project_id="p1", id="o1", trace_id="t1", start_time=NOW, usage_details={"input": 3}
        ),
        models.Score(project_id="p1", id="s1", trace_id="t1", name="q", value=1.0, timestamp=NOW),
    ]


def test_every_table_round_trips_a_row(engine):
    rows = _rows()
    assert {type(r).__table__.name for r in rows} == set(Base.metadata.tables)
    with Session(engine) as s:
        for row in rows:  # parents first; FK enforcement is on
            s.add(row)
            s.flush()
        s.commit()
    with Session(engine) as s:
        trace = s.get(models.Trace, ("p1", "t1"))
        assert trace.timestamp == NOW and trace.tags == ["x"] and trace.metadata_ == {"k": "v"}
        assert s.get(models.Observation, ("p1", "o1")).usage_details == {"input": 3}
        assert s.get(models.ApiKey, "k1").revoked_at is None
        assert s.get(models.UserPref, ("u1", "p1", "columns")).value == "a,b"


def test_naive_datetimes_rejected(engine):
    with Session(engine) as s, pytest.raises(Exception, match="naive"):
        s.add(models.Organization(id="o9", name="x", created_at=datetime(2026, 1, 1)))
        s.flush()


def test_project_id_is_first_parameter_of_every_trace_store_method():
    methods = [
        (name, fn)
        for name, fn in inspect.getmembers(ports.TraceStore, inspect.isfunction)
        if not name.startswith("_")
    ]
    assert len(methods) >= 11
    for name, fn in methods:
        params = list(inspect.signature(fn).parameters)
        assert params[:2] == ["self", "project_id"], name
