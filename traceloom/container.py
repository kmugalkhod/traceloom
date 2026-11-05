"""Adapter wiring. Nothing outside this module branches on TRACELOOM_MODE."""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from traceloom.config import Settings

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


def build_container(settings: Settings) -> Container:
    from traceloom.db.session import make_engine, make_session_factory, upgrade_to_head

    engine = make_engine(settings.database_url)
    container = Container(
        settings=settings, engine=engine, session_factory=make_session_factory(engine)
    )
    if settings.mode == "lite":
        container.startup_hooks.append(lambda c: upgrade_to_head(c.engine))
    container.shutdown_hooks.append(lambda c: c.engine.dispose())
    if settings.mode == "full":
        # Imported lazily so lite mode never needs ClickHouse/S3/Redis clients.
        importlib.import_module("traceloom.container_full").configure(container)
    return container
