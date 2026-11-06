"""In-process Queue with one consumer thread.

Tests and the durability journey can `pause()`/`resume()` the consumer, `discard()` queued
messages to simulate a crash, and `drain()` to process everything synchronously.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from collections.abc import Mapping
from typing import Any

from traceloom.queue_registry import INGEST, Handler, HandlerRegistry

log = logging.getLogger("traceloom.queue")


class InProcQueue:
    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._items: deque[Mapping[str, Any]] = deque()
        self._in_flight = 0
        self._paused = False
        self._stopped = True
        self._handler: Handler | None = None
        self._registry = HandlerRegistry()
        self._thread: threading.Thread | None = None

    # --- Queue port -------------------------------------------------------------------------

    def enqueue(self, message: Mapping[str, Any]) -> None:
        with self._cond:
            self._items.append(dict(message))
            self._cond.notify_all()

    def register(self, message_type: str, handler: Handler) -> None:
        """Route messages of `message_type` to `handler` (start() registers the ingest one)."""
        self._registry.register(message_type, handler)

    def start(self, handler: Handler) -> None:
        """`handler` takes messages without a type (ingest batches)."""
        with self._cond:
            if self._thread is not None and self._thread.is_alive():
                return
            self._handler, self._stopped = handler, False
            self._registry.register(INGEST, handler)
        self._thread = threading.Thread(target=self._run, name="traceloom-queue", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the consumer. Queued messages stay queued (see discard())."""
        with self._cond:
            self._stopped = True
            self._cond.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None

    def ping(self) -> bool:
        return not self._stopped and self._thread is not None and self._thread.is_alive()

    # --- Test / ops controls ------------------------------------------------------------------

    def pause(self) -> None:
        """Stop taking new messages; the one in flight (if any) finishes."""
        with self._cond:
            self._paused = True

    def resume(self) -> None:
        with self._cond:
            self._paused = False
            self._cond.notify_all()

    def discard(self) -> int:
        """Drop queued messages, as a process crash would. Returns how many."""
        with self._cond:
            dropped = len(self._items)
            self._items.clear()
            return dropped

    @property
    def pending(self) -> int:
        with self._cond:
            return len(self._items) + self._in_flight

    def drain(self, timeout: float = 30.0) -> None:
        """Process everything queued. With a running, unpaused consumer this waits for it;
        otherwise the calling thread processes the messages itself."""
        if self._handler is None:
            raise RuntimeError("queue has no handler; call start() first")
        with self._cond:
            if self.ping() and not self._paused:
                if not self._cond.wait_for(
                    lambda: not self._items and not self._in_flight, timeout
                ):
                    raise TimeoutError("queue did not drain")
                return
        while True:
            with self._cond:
                if not self._items:
                    return
                item = self._items.popleft()
            self._handle(item)

    # --- internals ----------------------------------------------------------------------------

    def _handle(self, item: Mapping[str, Any]) -> None:
        try:
            self._registry.dispatch(item)
        except Exception:
            # The raw batch stays pending in the blob store; the sweeper replays it.
            log.exception("queue handler failed for %r", dict(item))

    def _run(self) -> None:
        while True:
            with self._cond:
                self._cond.wait_for(lambda: self._stopped or (self._items and not self._paused))
                if self._stopped:
                    return
                item = self._items.popleft()
                self._in_flight += 1
            try:
                self._handle(item)
            finally:
                with self._cond:
                    self._in_flight -= 1
                    self._cond.notify_all()
