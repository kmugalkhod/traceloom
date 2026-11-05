"""Adapter wiring. Nothing outside this module branches on TRACELOOM_MODE."""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from traceloom.config import Settings

log = logging.getLogger("traceloom")


@dataclass
class Container:
    settings: Settings
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
    container = Container(settings=settings)
    if settings.mode == "full":
        # Imported lazily so lite mode never needs ClickHouse/S3/Redis clients.
        importlib.import_module("traceloom.container_full").configure(container)
    return container
