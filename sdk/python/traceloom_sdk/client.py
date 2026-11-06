"""Traceloom client: configuration, bounded queue, background flusher and delivery with retries."""

from __future__ import annotations

import atexit
import datetime as _dt
import hashlib
import json
import logging
import os
import queue
import random
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

import httpx

from traceloom_sdk.prompts import PromptCache

log = logging.getLogger("traceloom")

MASK_FAILED = "<masking failed>"
DEFAULT_HOST = "http://localhost:8000"
INGESTION_PATH = "/api/public/ingestion"
_RETRY_STATUS = {429, 500, 502, 503, 504}

_default_client: Traceloom | None = None
_default_lock = threading.RLock()


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else None


def _env_number(name: str, default: float, cast: Callable[[str], Any]) -> Any:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return cast(raw)
    except ValueError:
        log.warning("ignoring invalid %s=%r", name, raw)
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    return default if raw is None else raw.strip().lower() not in ("0", "false", "no", "off")


def _default(obj: Any) -> Any:
    """json.dumps fallback for values SDK users put in inputs and outputs."""
    if isinstance(obj, (_dt.datetime, _dt.date)):
        return obj.isoformat()
    if isinstance(obj, (set, frozenset, tuple)):
        return list(obj)
    if isinstance(obj, bytes):
        return obj.decode("utf-8", "replace")
    for attr in ("model_dump", "dict", "to_dict"):  # pydantic v2/v1 and similar
        method = getattr(obj, attr, None)
        if callable(method):
            try:
                return method()
            except Exception:
                break
    if hasattr(obj, "__dict__"):
        return {k: v for k, v in vars(obj).items() if not k.startswith("_")}
    return repr(obj)


def utc_iso(when: _dt.datetime | None = None) -> str:
    when = when or _dt.datetime.now(_dt.UTC)
    if when.tzinfo is None:
        when = when.replace(tzinfo=_dt.UTC)
    return when.astimezone(_dt.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_id() -> str:
    return uuid.uuid4().hex


def sampled(trace_id: str, rate: float) -> bool:
    """Deterministic per trace id: the same id always gets the same answer for a given rate."""
    if rate >= 1.0:
        return True
    if rate <= 0.0:
        return False
    bucket = int(hashlib.sha256(trace_id.encode("utf-8")).hexdigest()[:8], 16) / 0x100000000
    return bucket < rate


def get_client() -> Traceloom:
    """The process-wide client: the first one created, or one configured from the environment."""
    global _default_client
    with _default_lock:
        if _default_client is None:
            _default_client = Traceloom()
        return _default_client


def set_default_client(client: Traceloom | None) -> None:
    global _default_client
    with _default_lock:
        _default_client = client


class Traceloom:
    def __init__(
        self,
        host: str | None = None,
        public_key: str | None = None,
        secret_key: str | None = None,
        flush_at: int | None = None,
        flush_interval: float | None = None,
        sample_rate: float | None = None,
        enabled: bool | None = None,
        mask: Callable[[Any], Any] | None = None,
        environment: str | None = None,
        release: str | None = None,
        version: str | None = None,
        timeout: float = 10.0,
        max_queue_size: int = 10000,
        max_retries: int = 3,
        backoff_base: float = 0.5,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.host = (host or _env("TRACELOOM_HOST") or DEFAULT_HOST).rstrip("/")
        self.public_key = public_key or _env("TRACELOOM_PUBLIC_KEY")
        self.secret_key = secret_key or _env("TRACELOOM_SECRET_KEY")
        self.flush_at = max(
            1, flush_at if flush_at is not None else _env_number("TRACELOOM_FLUSH_AT", 50, int)
        )
        self.flush_interval = max(
            0.01,
            flush_interval
            if flush_interval is not None
            else _env_number("TRACELOOM_FLUSH_INTERVAL", 1.0, float),
        )
        rate = (
            sample_rate
            if sample_rate is not None
            else _env_number("TRACELOOM_SAMPLE_RATE", 1.0, float)
        )
        self.sample_rate = min(1.0, max(0.0, rate))
        self.enabled = enabled if enabled is not None else _env_bool("TRACELOOM_ENABLED", True)
        self.mask = mask
        self.environment = environment or _env("TRACELOOM_ENVIRONMENT")
        self.release = release or _env("TRACELOOM_RELEASE")
        self.version = version
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self._transport = transport

        if self.enabled and not (self.public_key and self.secret_key):
            log.warning(
                "Traceloom disabled: set TRACELOOM_PUBLIC_KEY and TRACELOOM_SECRET_KEY"
                " (or pass public_key and secret_key)"
            )
            self.enabled = False

        self._queue: queue.Queue[str] = queue.Queue(maxsize=max(1, max_queue_size))
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._send_lock = threading.Lock()
        self._thread_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._http: httpx.Client | None = None
        self._dropped = 0
        self._atexit_registered = False
        self._prompt_cache = PromptCache()

        global _default_client
        with _default_lock:
            if _default_client is None:
                _default_client = self

    # ---- emitting -------------------------------------------------------------------------

    def is_sampled(self, trace_id: str) -> bool:
        return sampled(trace_id, self.sample_rate)

    def apply_mask(self, value: Any) -> Any:
        if self.mask is None or value is None:
            return value
        try:
            return self.mask(value)
        except Exception:
            log.warning("mask function raised; value replaced", exc_info=True)
            return MASK_FAILED

    def emit(self, event_type: str, body: dict[str, Any], trace_id: str | None) -> None:
        """Queue one event. Never raises."""
        try:
            if not self.enabled:
                return
            if trace_id is not None and not self.is_sampled(trace_id):
                return
            for key in ("input", "output", "metadata"):
                if body.get(key) is not None:
                    body[key] = self.apply_mask(body[key])
            envelope = {
                "id": new_id(),
                "type": event_type,
                "timestamp": utc_iso(),
                "body": {k: v for k, v in body.items() if v is not None},
            }
            try:
                payload = json.dumps(envelope, default=_default)
            except (TypeError, ValueError):
                payload = json.dumps(json.loads(json.dumps(envelope, default=repr)), default=repr)
            try:
                self._queue.put_nowait(payload)
            except queue.Full:
                self._dropped += 1
                if self._dropped == 1 or self._dropped % 100 == 0:
                    log.warning(
                        "Traceloom queue is full; dropped %d event(s) so far", self._dropped
                    )
                return
            self._ensure_thread()
            if self._queue.qsize() >= self.flush_at:
                self._wake.set()
        except Exception:
            log.exception("Traceloom failed to queue an event")

    # ---- background delivery --------------------------------------------------------------

    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        with self._thread_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="traceloom-flusher", daemon=True)
            self._thread.start()
            if not self._atexit_registered:
                atexit.register(self._flush_at_exit)
                self._atexit_registered = True

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(self.flush_interval)
            self._wake.clear()
            if self._stop.is_set():
                break
            try:
                self._drain(time.monotonic() + self.timeout)
            except Exception:
                log.exception("Traceloom flusher failed")

    def _take_batch(self) -> list[str]:
        batch: list[str] = []
        while len(batch) < self.flush_at:
            try:
                batch.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return batch

    def _drain(self, deadline: float) -> bool:
        """Send queued events in batches until empty or the deadline. True when all were sent."""
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not self._send_lock.acquire(timeout=remaining):
            return False
        ok = True
        try:
            while time.monotonic() < deadline:
                batch = self._take_batch()
                if not batch:
                    return ok
                if not self._send(batch, deadline):
                    ok = False
            return ok and self._queue.empty()
        finally:
            self._send_lock.release()

    def _client(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(
                auth=(self.public_key or "", self.secret_key or ""),
                transport=self._transport,
                timeout=self.timeout,
                headers={"User-Agent": "traceloom-sdk-python/0.1.0"},
            )
        return self._http

    def _send(self, batch: list[str], deadline: float) -> bool:
        """POST one batch with up to max_retries retries on 429/5xx/network errors."""
        content = ('{"batch":[' + ",".join(batch) + "]}").encode("utf-8")
        url = self.host + INGESTION_PATH
        last = "no attempt"
        for attempt in range(self.max_retries + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                last = "flush timeout"
                break
            try:
                response = self._client().post(
                    url,
                    content=content,
                    headers={"Content-Type": "application/json"},
                    timeout=min(self.timeout, remaining),
                )
            except httpx.HTTPError as exc:
                last = f"{type(exc).__name__}: {exc}"
            except Exception as exc:  # never let delivery take the thread down
                last = f"{type(exc).__name__}: {exc}"
            else:
                if response.status_code in _RETRY_STATUS:
                    last = f"HTTP {response.status_code}"
                elif response.status_code >= 400:
                    log.warning(
                        "Traceloom rejected a batch of %d event(s): HTTP %d",
                        len(batch),
                        response.status_code,
                    )
                    return False
                else:
                    self._log_event_errors(response)
                    return True
            if attempt < self.max_retries:
                delay = self.backoff_base * (2**attempt) * (0.5 + random.random())
                if self._stop.wait(min(delay, max(0.0, deadline - time.monotonic()))):
                    break
        log.warning("Traceloom dropped a batch of %d event(s): %s", len(batch), last)
        return False

    @staticmethod
    def _log_event_errors(response: httpx.Response) -> None:
        if response.status_code != 207:
            return
        try:
            errors = response.json().get("errors") or []
        except Exception:
            return
        for err in errors[:5]:
            log.warning("Traceloom event rejected: %s", err)

    # ---- public lifecycle -----------------------------------------------------------------

    def flush(self, timeout: float | None = None) -> bool:
        """Send everything queued. Returns True when all events were delivered. Never raises."""
        try:
            deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
            return self._drain(deadline)
        except Exception:
            log.exception("Traceloom flush failed")
            return False

    def shutdown(self, timeout: float | None = None) -> None:
        try:
            self.flush(timeout)
            self._stop.set()
            self._wake.set()
            if self._http is not None:
                self._http.close()
                self._http = None
            if self._atexit_registered:
                atexit.unregister(self._flush_at_exit)
                self._atexit_registered = False
        except Exception:
            log.exception("Traceloom shutdown failed")

    def _flush_at_exit(self) -> None:
        self.flush(min(self.timeout, 5.0))

    # ---- tracing API (delegates to objects.py) --------------------------------------------

    def start_observation(self, name: str | None = None, as_type: str = "span", **fields: Any):
        from traceloom_sdk import objects

        return objects.start(self, as_type, name, fields)

    def start_span(self, name: str | None = None, **fields: Any):
        return self.start_observation(name, "span", **fields)

    def start_generation(self, name: str | None = None, **fields: Any):
        return self.start_observation(name, "generation", **fields)

    def event(self, name: str | None = None, **fields: Any):
        return self.start_observation(name, "event", **fields)

    def score(
        self,
        name: str,
        value: Any,
        trace_id: str | None = None,
        observation_id: str | None = None,
        data_type: str | None = None,
        comment: str | None = None,
    ) -> None:
        from traceloom_sdk import objects

        objects.score(self, name, value, trace_id, observation_id, data_type, comment)

    def update_current_trace(self, **fields: Any) -> None:
        from traceloom_sdk import objects

        objects.update_current_trace(self, fields)

    def update_current_observation(self, **fields: Any) -> None:
        from traceloom_sdk import context

        try:
            current = context.current_observation()
            if current is not None:
                current.update(**fields)
        except Exception:
            log.exception("update_current_observation failed")

    def get_prompt(self, name: str, **options: Any):
        """Fetch a prompt (cached; stale-while-revalidate). Raises PromptNotFound only when
        there is no cached value and no `fallback`. Options: label, version, type,
        cache_ttl_seconds, fallback, max_retries, fetch_timeout_seconds."""
        from traceloom_sdk import prompts

        return prompts.get_prompt(self, name, **options)

    async def aget_prompt(self, name: str, **options: Any):
        import asyncio

        return await asyncio.to_thread(self.get_prompt, name, **options)

    def get_current_trace_id(self) -> str | None:
        from traceloom_sdk import context

        return context.current_trace_id()
