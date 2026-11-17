from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text

from traceloom.db import models, session
from traceloom.db.session import make_engine

PHASE3_TABLES = {
    "score_configs",
    "annotation_queues",
    "annotation_items",
    "eval_templates",
    "eval_template_versions",
    "eval_rules",
    "eval_executions",
    "project_eval_settings",
    "eval_spend_daily",
    "datasets",
    "dataset_items",
    "dataset_item_versions",
    "dataset_runs",
    "dataset_run_items",
}
SCORE_COLUMNS = {"queue_id", "execution_id", "channel"}


def _cfg(connection) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(Path(session.__file__).parent / "migrations"))
    cfg.attributes["connection"] = connection
    return cfg


def _insert_score(conn, score_id: str, author: str | None) -> None:
    conn.execute(
        text(
            "insert into scores (project_id, id, name, data_type, source, author_id, timestamp,"
            " environment) values ('p1', :id, 'quality', 'NUMERIC', 'API', :author,"
            " '2026-01-01', 'default')"
        ),
        {"id": score_id, "author": author},
    )


def test_upgrade_from_phase2_backfills_source_and_downgrades(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'm.db'}")
    with engine.begin() as conn:
        command.upgrade(_cfg(conn), "0002")
        _insert_score(conn, "ui", "u1")
        _insert_score(conn, "api", None)
        command.upgrade(_cfg(conn), "head")
    with engine.connect() as conn:
        rows = dict(conn.execute(text("select id, source from scores")).all())
    assert rows == {"ui": "ANNOTATION", "api": "API"}
    insp = sa_inspect(engine)
    assert PHASE3_TABLES <= set(insp.get_table_names())
    assert SCORE_COLUMNS <= {c["name"] for c in insp.get_columns("scores")}
    assert "ix_scores_project_name_timestamp" in {i["name"] for i in insp.get_indexes("scores")}

    with engine.begin() as conn:
        command.downgrade(_cfg(conn), "0002")
    insp = sa_inspect(engine)
    assert not PHASE3_TABLES & set(insp.get_table_names())
    assert not SCORE_COLUMNS & {c["name"] for c in insp.get_columns("scores")}
    engine.dispose()


def test_migrated_schema_matches_models(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'm.db'}")
    with engine.begin() as conn:
        command.upgrade(_cfg(conn), "head")
    insp = sa_inspect(engine)
    for table in models.Base.metadata.sorted_tables:
        assert table.name in insp.get_table_names(), table.name
        migrated = {c["name"] for c in insp.get_columns(table.name)}
        assert migrated == {c.name for c in table.columns}, table.name
        migrated_ix = {i["name"] for i in insp.get_indexes(table.name)}
        assert {i.name for i in table.indexes} <= migrated_ix, table.name
    engine.dispose()
