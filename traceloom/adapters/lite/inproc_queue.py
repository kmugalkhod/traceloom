"""In-process Queue with one consumer thread, plus optional side lanes.

A message type registered with `workers > 0` gets a lane of its own: a bounded deque served by
that many threads, so slow handlers (webhook deliveries) never hold up ingest batches. A lane
drops a message when it is full or when the same message is already waiting in it; callers that
use lanes must be able to recover a dropped message (webhook deliveries are re-sent by the
sweeper while their row is pending).

Tests and the durability journey can `pause()`/`resume()` the consumers, `discard()` queued
messages to simulate a crash, and `drain()` to process everything synchronously.

`enqueue(message, delay_seconds)` parks a delayed message on a heap; a timer thread moves it to
its lane once due. The eval types (`eval_job`, `experiment_item`) share one `eval` lane served by
`eval_concurrency` workers, apart from ingest and webhook deliveries.
"""

from __future__ import annotations

import heapq
import itertools
import logging
import threading
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from traceloom.queue_registry import EVAL_LANE, EVAL_TYPES, INGEST, Handler, HandlerRegistry

log = logging.getLogger("traceloom.queue")


@dataclass
class _Lane:
    workers: int
    capacity: int
    items: deque[Mapping[str, Any]] = field(default_factory=deque)
    threads: list[threading.Thread] = field(default_factory=list)


EVAL_LANE_CAPACITY = 10_000


class InProcQueue:
    def __init__(self, *, eval_concurrency: int = 4) -> None:
        self._cond = threading.Condition()
        self._items: deque[Mapping[str, Any]] = deque()
        self._delayed: list[tuple[float, int, dict[str, Any]]] = []  # heap of (due, seq, message)
        self._seq = itertools.count()
        self._timer: threading.Thread | None = None
        self._eval_concurrency = max(1, eval_concurrency)
        self._lanes: dict[str, _Lane] = {}
        self._in_flight = 0
        self._paused = False
        self._stopped = True
        self._handler: Handler | None = None
        self._registry = HandlerRegistry()
        self._thread: threading.Thread | None = None

    # --- Queue port -------------------------------------------------------------------------

    def enqueue(self, message: Mapping[str, Any], delay_seconds: float = 0) -> None:
        with self._cond:
            if delay_seconds > 0:
                due = time.monotonic() + delay_seconds
                heapq.heappush(self._delayed, (due, next(self._seq), dict(message)))
                self._cond.notify_all()  # the timer may need a shorter wait
                return
            self._put(message)

    def _put(self, message: Mapping[str, Any]) -> None:
        """Append to the message's lane (caller holds the lock)."""
        lane = self._lanes.get(_lane_name(message.get("type") or INGEST))
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
        lane_name = _lane_name(message_type)
        if message_type in EVAL_TYPES:  # all eval types share one lane
            workers, capacity = self._eval_concurrency, EVAL_LANE_CAPACITY
        if workers <= 0:
            return
        with self._cond:
            if lane_name not in self._lanes:
                self._lanes[lane_name] = _Lane(workers, capacity)
            running = not self._stopped
        if running:
            self._start_lane(lane_name)

    def start(self, handler: Handler) -> None:
        """`handler` takes messages without a type (ingest batches)."""
        with self._cond:
            if self._thread is not None and self._thread.is_alive():
                return
            self._handler, self._stopped = handler, False
            self._registry.register(INGEST, handler)
        self._thread = threading.Thread(target=self._run, name="traceloom-queue", daemon=True)
        self._thread.start()
        self._timer = threading.Thread(target=self._run_timer, name="traceloom-timer", daemon=True)
        self._timer.start()
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
        if self._timer is not None:
            self._timer.join(timeout=10)
            self._timer = None
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
            self._delayed.clear()
            for lane in self._lanes.values():
                lane.items.clear()
            return dropped

    @property
    def pending(self) -> int:
        with self._cond:
            return self._waiting() + self._in_flight

    def drain(self, timeout: float = 30.0) -> None:
        """Process everything queued, lanes and delayed messages included. With running, unpaused
        consumers this waits for them (and for delays to elapse); otherwise the calling thread
        processes the messages itself, delayed ones immediately."""
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
                while self._delayed:
                    self._put(heapq.heappop(self._delayed)[2])
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
        return len(self._items) + len(self._delayed) + sum(len(lane.items) for lane in self._lanes.values())

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

    def _run_timer(self) -> None:
        """Move delayed messages onto their lanes as they fall due."""
        with self._cond:
            while not self._stopped:
                if not self._delayed:
                    self._cond.wait()
                    continue
                wait = self._delayed[0][0] - time.monotonic()
                if wait > 0:
                    self._cond.wait(wait)
                    continue
                self._put(heapq.heappop(self._delayed)[2])

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


def _lane_name(message_type: str) -> str:
    return EVAL_LANE if message_type in EVAL_TYPES else message_type
