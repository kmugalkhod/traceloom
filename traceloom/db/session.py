"""Engine factory, SQLite pragmas and the get_db dependency."""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Iterator
from pathlib import Path

from fastapi import Request
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool


class _Percentile:
    """SQLite aggregate: percentile(value, p) with p in [0, 1], linear interpolation."""

    def __init__(self) -> None:
        self.values: list[float] = []
        self.p = 0.5

    def step(self, value: float | None, p: float) -> None:
        if value is not None:
            self.values.append(float(value))
            self.p = float(p)

    def finalize(self) -> float | None:
        if not self.values:
            return None
        vals = sorted(self.values)
        k = (len(vals) - 1) * self.p
        lo, hi = math.floor(k), math.ceil(k)
        return vals[lo] + (vals[hi] - vals[lo]) * (k - lo)


def _on_sqlite_connect(dbapi_conn: sqlite3.Connection, _record) -> None:
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.close()
    dbapi_conn.create_aggregate("percentile", 2, _Percentile)


def make_engine(url: str) -> Engine:
    parsed = make_url(url)
    if parsed.get_backend_name() != "sqlite":
        return create_engine(url, pool_pre_ping=True)
    kwargs: dict = {"connect_args": {"check_same_thread": False}}
    if parsed.database in (None, "", ":memory:"):
        kwargs["poolclass"] = StaticPool
    else:
        Path(parsed.database).expanduser().parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, **kwargs)
    event.listen(engine, "connect", _on_sqlite_connect)
    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


def get_db(request: Request) -> Iterator[Session]:
    """FastAPI dependency: one session per request, committed on success."""
    session = request.app.state.container.session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def upgrade_to_head(engine: Engine) -> None:
    """Apply Alembic migrations on the given engine (used at lite startup and by `migrate`)."""
    from alembic import command
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    with engine.begin() as connection:
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "head")
