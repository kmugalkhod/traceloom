from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select
from sqlalchemy.orm import Session

from traceloom.db import models, session
from traceloom.db.session import make_engine, upgrade_to_head
from traceloom.services import orgs

NEW_TABLES = {
    "prompts",
    "prompt_versions",
    "prompt_labels",
    "protected_labels",
    "llm_connections",
    "webhooks",
    "webhook_deliveries",
}


def _cfg(connection) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(Path(session.__file__).parent / "migrations"))
    cfg.attributes["connection"] = connection
    return cfg


def test_upgrade_and_downgrade_on_sqlite(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'm.db'}")
    upgrade_to_head(engine)
    insp = sa_inspect(engine)
    assert NEW_TABLES <= set(insp.get_table_names())
    assert "may_move_protected_labels" in {c["name"] for c in insp.get_columns("api_keys")}
    assert "ix_observations_project_prompt" in {i["name"] for i in insp.get_indexes("observations")}

    with engine.begin() as conn:
        command.downgrade(_cfg(conn), "0001")
    insp = sa_inspect(engine)
    assert not NEW_TABLES & set(insp.get_table_names())
    assert "may_move_protected_labels" not in {c["name"] for c in insp.get_columns("api_keys")}
    assert "ix_observations_project_prompt" not in {
        i["name"] for i in insp.get_indexes("observations")
    }

    with engine.begin() as conn:  # and back up again
        command.upgrade(_cfg(conn), "head")
    assert NEW_TABLES <= set(sa_inspect(engine).get_table_names())
    engine.dispose()


def test_existing_projects_get_protected_production(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'm.db'}")
    with engine.begin() as conn:
        command.upgrade(_cfg(conn), "0001")
        conn.exec_driver_sql(
            "insert into organizations (id, name, created_at) values ('o1', 'Org', '2026-01-01')"
        )
        conn.exec_driver_sql("insert into projects (id, org_id, name) values ('p1', 'o1', 'A')")
        conn.exec_driver_sql("insert into projects (id, org_id, name) values ('p2', 'o1', 'B')")
        command.upgrade(_cfg(conn), "head")
    with Session(engine) as db:
        rows = db.execute(select(models.ProtectedLabel)).scalars().all()
        assert {(r.project_id, r.label) for r in rows} == {
            ("p1", "production"),
            ("p2", "production"),
        }
    engine.dispose()


def test_new_project_gets_production_protected(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'm.db'}")
    upgrade_to_head(engine)
    with Session(engine) as db:
        user = models.User(email="a@example.com", password_hash="x")
        db.add(user)
        db.flush()
        _, first = orgs.create_org(db, user, "Org", "First")
        second = orgs.create_project(db, user, orgs.Role.OWNER, first.org_id, "Second")
        db.commit()
        for project in (first, second):
            labels = db.scalars(
                select(models.ProtectedLabel.label).where(
                    models.ProtectedLabel.project_id == project.id
                )
            ).all()
            assert labels == ["production"]
    engine.dispose()


def test_api_key_may_not_move_protected_labels_by_default(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'm.db'}")
    upgrade_to_head(engine)
    with Session(engine) as db:
        db.add(models.Organization(id="o1", name="Org"))
        db.add(models.Project(id="p1", org_id="o1", name="P"))
        db.flush()
        key = models.ApiKey(project_id="p1", public_key="pk", secret_hash="h")
        db.add(key)
        db.commit()
        assert key.may_move_protected_labels is False
    engine.dispose()
