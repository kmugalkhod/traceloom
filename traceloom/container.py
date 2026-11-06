"""Adapter wiring. Nothing outside this module branches on TRACELOOM_MODE."""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from traceloom.auth.api_keys import ApiKeyCache
from traceloom.config import Settings
from traceloom.cost.registry import ModelRegistry
from traceloom.services.mail import Mailer

log = logging.getLogger("traceloom")


@dataclass
class Container:
    settings: Settings
    # Meta database (Postgres or SQLite).
    engine: Engine | None = None
    session_factory: sessionmaker[Session] | None = None
    # Adapters behind the ports (filled in by build_container / later tasks).
    trace_store: Any = None
    blob_store: Any = None
    queue: Any = None
    limiter: Any = None
    cache: Any = None
    api_key_cache: ApiKeyCache = field(default_factory=ApiKeyCache)
    mailer: Mailer | None = None
    models: ModelRegistry | None = None
    # Lifecycle hooks and health probes registered by feature modules.
    startup_hooks: list[Callable[[Container], None]] = field(default_factory=list)
    shutdown_hooks: list[Callable[[Container], None]] = field(default_factory=list)
    health_checks: dict[str, Callable[[], bool]] = field(default_factory=dict)

    def startup(self) -> None:
        for hook in self.startup_hooks:
            hook(self)

    def shutdown(self) -> None:
        for hook in reversed(self.shutdown_hooks):
            try:
                hook(self)
            except Exception:  # shutdown must run every hook
                log.exception("shutdown hook failed")


def _db_ok(engine: Engine) -> bool:
    with engine.connect() as conn:
        conn.exec_driver_sql("select 1")
    return True


def _blob_ok(container: Container) -> bool:
    next(iter(container.blob_store.list("pending/")), None)
    return True


def build_container(settings: Settings) -> Container:
    from traceloom.adapters.lite.inproc_queue import InProcQueue
    from traceloom.adapters.lite.local_blob import LocalBlobStore
    from traceloom.adapters.lite.memory import InMemoryCache, InMemoryRateLimiter
    from traceloom.adapters.lite.sql_trace_store import SqlTraceStore
    from traceloom.db.session import make_engine, make_session_factory, upgrade_to_head

    engine = make_engine(settings.database_url)
    sessions = make_session_factory(engine)
    container = Container(
        settings=settings,
        engine=engine,
        session_factory=sessions,
        limiter=InMemoryRateLimiter(),
        cache=InMemoryCache(),
        mailer=Mailer(settings),
        models=ModelRegistry(sessions),
    )
    # Hooks run in reverse on shutdown, so the engine is disposed last.
    container.shutdown_hooks.append(lambda c: c.engine.dispose())
    if settings.mode == "lite":
        container.trace_store = SqlTraceStore(container.session_factory)
        container.blob_store = LocalBlobStore(settings.blob_dir)
        container.queue = InProcQueue()
        container.startup_hooks.append(lambda c: upgrade_to_head(c.engine))
        container.shutdown_hooks.append(lambda c: c.queue.stop())
    container.startup_hooks.append(lambda c: c.models.load_seed())
    container.health_checks["db"] = lambda: _db_ok(container.engine)
    container.health_checks["blob"] = lambda: _blob_ok(container)
    container.health_checks["queue"] = lambda: container.queue.ping()
    if settings.mode == "full":
        # Imported lazily so lite mode never needs ClickHouse/S3/Redis clients.
        importlib.import_module("traceloom.container_full").configure(container)
    return container
