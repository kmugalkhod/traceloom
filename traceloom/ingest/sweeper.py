"""Re-enqueues batches that were accepted but never processed (crash, lost queue, failed
enqueue). Processing is idempotent, so replaying a batch is always safe."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from traceloom.ingest.accept import PENDING
from traceloom.ports import BlobStore, Queue
from traceloom.queue_registry import WEBHOOK_DELIVERY

log = logging.getLogger("traceloom.ingest")


class Sweeper:
    def __init__(
        self,
        blobs: BlobStore,
        queue: Queue,
        min_age_seconds: float,
        interval_seconds: float,
        due_deliveries: Callable[[], Iterable[Mapping[str, Any]]] | None = None,
    ) -> None:
        """`due_deliveries` returns the queue messages of webhook deliveries due for (re)delivery,
        read from the DB. It is only wired in lite mode; Redis keeps its own retry schedule."""
        self._blobs = blobs
        self._queue = queue
        self._min_age = min_age_seconds
        self._due_deliveries = due_deliveries
        self._interval = max(1.0, interval_seconds)
        self._recent: dict[str, float] = {}  # blob key -> monotonic time it was last enqueued
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def run_once(self, min_age: float | None = None) -> int:
        """Enqueue pending blobs older than `min_age` seconds (default: the configured age),
        skipping ones this sweeper enqueued within the last half interval. Returns the count."""
        age = self._min_age if min_age is None else min_age
        now, mono = datetime.now(UTC), time.monotonic()
        self._recent = {k: t for k, t in self._recent.items() if mono - t < self._interval / 2}
        enqueued = 0
        for key, modified in self._blobs.list(f"{PENDING}/"):
            parts = key.split("/")
            if len(parts) < 3 or (now - modified).total_seconds() < age or key in self._recent:
                continue
            self._queue.enqueue({"project_id": parts[1], "blob_key": key})
            self._recent[key] = mono
            enqueued += 1
        if enqueued:
            log.info("sweeper re-enqueued %d pending batch(es)", enqueued)
        return enqueued + self._replay_deliveries()

    def set_due_deliveries(self, source: Callable[[], Iterable[Mapping[str, Any]]]) -> None:
        self._due_deliveries = source

    def _replay_deliveries(self) -> int:
        if self._due_deliveries is None:
            return 0
        count = 0
        for message in self._due_deliveries():
            self._queue.enqueue({**message, "type": WEBHOOK_DELIVERY})
            count += 1
        if count:
            log.info("sweeper re-enqueued %d webhook delivery(ies)", count)
        return count

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="traceloom-sweeper", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self.run_once()
            except Exception:
                log.exception("sweeper pass failed")
