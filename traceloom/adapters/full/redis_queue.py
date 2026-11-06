"""Queue on Redis through Dramatiq. dramatiq and redis are imported only when this is built.

`enqueue` sends a `process_batch` message; `start(handler)` runs Dramatiq worker threads in this
process, so the web container only enqueues and the worker container consumes. A handler that
raises is retried with backoff; the raw batch also stays pending in the blob store, so the
sweeper can replay it if retries run out.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from traceloom.queue_registry import INGEST, Handler, HandlerRegistry

log = logging.getLogger("traceloom.queue")


class RedisQueue:
    def __init__(
        self,
        redis_url: str = "redis://localhost:6379/0",
        *,
        queue_name: str = "traceloom-ingest",
        broker: Any = None,
        worker_threads: int = 4,
        max_retries: int = 3,
    ) -> None:
        import dramatiq

        if broker is None:
            from dramatiq.brokers.redis import RedisBroker

            broker = RedisBroker(url=redis_url)
        self.broker = broker
        self.queue_name = queue_name
        self._threads = worker_threads
        self._registry = HandlerRegistry()
        self._running = False
        self._worker: Any = None
        self.process_batch = dramatiq.actor(
            self._dispatch,
            actor_name="process_batch",
            queue_name=queue_name,
            broker=broker,
            max_retries=max_retries,
            min_backoff=1000,
            max_backoff=60_000,
        )

    def _dispatch(self, message: dict[str, Any]) -> None:
        if not self._running:
            raise RuntimeError("queue consumer has no handler")
        self._registry.dispatch(message)

    def enqueue(self, message: Mapping[str, Any]) -> None:
        self.process_batch.send(dict(message))

    def register(self, message_type: str, handler: Handler) -> None:
        """Route messages of `message_type` to `handler` (start() registers the ingest one)."""
        self._registry.register(message_type, handler)

    def start(self, handler: Handler) -> None:
        """`handler` takes messages without a type (ingest batches)."""
        if self._worker is not None:
            return
        import dramatiq

        self._registry.register(INGEST, handler)
        self._running = True
        self._worker = dramatiq.Worker(
            self.broker, queues={self.queue_name}, worker_threads=self._threads
        )
        self._worker.start()

    def stop(self) -> None:
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.stop()
        self._running = False

    def ping(self) -> bool:
        client = getattr(self.broker, "client", None)
        if client is None:  # stub broker (tests)
            return True
        return bool(client.ping())
