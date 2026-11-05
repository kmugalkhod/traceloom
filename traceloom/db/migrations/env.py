from __future__ import annotations

from alembic import context

from traceloom.config import Settings
from traceloom.db import models  # noqa: F401  (registers tables on Base.metadata)
from traceloom.db.base import Base
from traceloom.db.session import make_engine

config = context.config
target_metadata = Base.metadata


def _run(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=connection.dialect.name == "sqlite",
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _run(connection)
        return
    engine = make_engine(Settings().database_url)
    with engine.begin() as conn:
        _run(conn)


if context.is_offline_mode():
    raise RuntimeError("offline migrations are not supported")
run_migrations_online()
