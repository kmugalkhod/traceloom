"""In-process Queue with one consumer thread, plus optional side lanes.

A message type registered with `workers > 0` gets a lane of its own: a bounded deque served by
that many threads, so slow handlers (webhook deliveries) never hold up ingest batches. A lane
drops a message when it is full or when the same message is already waiting in it; callers that
use lanes must be able to recover a dropped message (webhook deliveries are re-sent by the
sweeper while their row is pending).

Tests and the durability journey can `pause()`/`resume()` the consumers, `discard()` queued
messages to simulate a crash, and `drain()` to process everything synchronously.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from traceloom.queue_registry import INGEST, Handler, HandlerRegistry

log = logging.getLogger("traceloom.queue")


@dataclass
class _Lane:
    workers: int
    capacity: int
    items: deque[Mapping[str, Any]] = field(default_factory=deque)
    threads: list[threading.Thread] = field(default_factory=list)


class InProcQueue:
    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._items: deque[Mapping[str, Any]] = deque()
        self._lanes: dict[str, _Lane] = {}
        self._in_flight = 0
        self._paused = False
        self._stopped = True
        self._handler: Handler | None = None
        self._registry = HandlerRegistry()
        self._thread: threading.Thread | None = None

    # --- Queue port -------------------------------------------------------------------------

    def enqueue(self, message: Mapping[str, Any]) -> None:
        with self._cond:
            lane = self._lanes.get(message.get("type") or INGEST)
            if lane is None:
                self._items.append(dict(message))
            elif len(lane.items) >= lane.capacity:
                log.warning("queue lane %r is full; dropped a message", message.get("type"))
                return
            elif dict(message) in lane.items:
                return  # already waiting (e.g. a sweep re-sent a delivery not yet handled)
            else:
                lane.items.append(dict(message))
            self._cond.notify_all()

    def register(
        self, message_type: str, handler: Handler, *, workers: int = 0, capacity: int = 100
    ) -> None:
        """Route messages of `message_type` to `handler` (start() registers the ingest one).

        With `workers`, the type gets its own lane of that many threads holding at most
        `capacity` waiting messages."""
        self._registry.register(message_type, handler)
        if workers <= 0:
            return
        with self._cond:
            if message_type not in self._lanes:
                self._lanes[message_type] = _Lane(workers, capacity)
            running = not self._stopped
        if running:
            self._start_lane(message_type)

    def start(self, handler: Handler) -> None:
        """`handler` takes messages without a type (ingest batches)."""
        with self._cond:
            if self._thread is not None and self._thread.is_alive():
                return
            self._handler, self._stopped = handler, False
            self._registry.register(INGEST, handler)
        self._thread = threading.Thread(target=self._run, name="traceloom-queue", daemon=True)
        self._thread.start()
        for name in list(self._lanes):
            self._start_lane(name)

    def stop(self) -> None:
        """Stop the consumers. Queued messages stay queued (see discard())."""
        with self._cond:
            self._stopped = True
            self._cond.notify_all()
            threads = [t for lane in self._lanes.values() for t in lane.threads]
            for lane in self._lanes.values():
                lane.threads = []
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None
        for thread in threads:
            thread.join(timeout=10)

    def ping(self) -> bool:
        return not self._stopped and self._thread is not None and self._thread.is_alive()

    # --- Test / ops controls ------------------------------------------------------------------

    def pause(self) -> None:
        """Stop taking new messages; the ones in flight (if any) finish."""
        with self._cond:
            self._paused = True

    def resume(self) -> None:
        with self._cond:
            self._paused = False
            self._cond.notify_all()

    def discard(self) -> int:
        """Drop queued messages, as a process crash would. Returns how many."""
        with self._cond:
            dropped = self._waiting()
            self._items.clear()
            for lane in self._lanes.values():
                lane.items.clear()
            return dropped

    @property
    def pending(self) -> int:
        with self._cond:
            return self._waiting() + self._in_flight

    def drain(self, timeout: float = 30.0) -> None:
        """Process everything queued, lanes included. With running, unpaused consumers this waits
        for them; otherwise the calling thread processes the messages itself."""
        if self._handler is None:
            raise RuntimeError("queue has no handler; call start() first")
        with self._cond:
            if self.ping() and not self._paused:
                if not self._cond.wait_for(
                    lambda: not self._waiting() and not self._in_flight, timeout
                ):
                    raise TimeoutError("queue did not drain")
                return
        while True:
            with self._cond:
                source = next(
                    (q for q in (self._items, *(la.items for la in self._lanes.values())) if q),
                    None,
                )
                if source is None:
                    return
                item = source.popleft()
            self._handle(item)

    # --- internals ----------------------------------------------------------------------------

    def _waiting(self) -> int:
        return len(self._items) + sum(len(lane.items) for lane in self._lanes.values())

    def _handle(self, item: Mapping[str, Any]) -> None:
        try:
            self._registry.dispatch(item)
        except Exception:
            # The raw batch stays pending in the blob store; the sweeper replays it.
            log.exception("queue handler failed for %r", dict(item))

    def _start_lane(self, name: str) -> None:
        with self._cond:
            lane = self._lanes[name]
            lane.threads = [t for t in lane.threads if t.is_alive()]
            missing = lane.workers - len(lane.threads)
            for i in range(missing):
                thread = threading.Thread(
                    target=self._consume,
                    args=(lane.items,),
                    name=f"traceloom-queue-{name}-{i}",
                    daemon=True,
                )
                lane.threads.append(thread)
                thread.start()

    def _run(self) -> None:
        self._consume(self._items)

    def _consume(self, items: deque[Mapping[str, Any]]) -> None:
        while True:
            with self._cond:
                self._cond.wait_for(lambda: self._stopped or (items and not self._paused))
                if self._stopped:
                    return
                item = items.popleft()
                self._in_flight += 1
            try:
                self._handle(item)
            finally:
                with self._cond:
                    self._in_flight -= 1
                    self._cond.notify_all()
