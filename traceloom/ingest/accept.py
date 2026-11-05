"""Request path of ingestion: validate, persist the raw batch, enqueue, build the 207 body.

Works only against the BlobStore and Queue ports. No trace data is written here.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from traceloom.ingest.schemas import EventError, parse_event
from traceloom.ports import BlobStore, BlobStoreError, Queue

log = logging.getLogger("traceloom.ingest")

PENDING = "pending"
PROCESSED = "processed"


class IngestError(Exception):
    """Base class; the route maps subclasses to HTTP statuses."""


class InvalidBatch(IngestError):  # -> 400
    pass


class BatchTooLarge(IngestError):  # -> 413
    pass


class StorageUnavailable(IngestError):  # -> 503
    pass


@dataclass(frozen=True)
class IngestLimits:
    max_events: int
    max_bytes: int


@dataclass
class AcceptResult:
    successes: list[dict[str, Any]]
    errors: list[dict[str, Any]]
    blob_key: str | None = None

    def body(self) -> dict[str, Any]:
        return {"successes": self.successes, "errors": self.errors}


def pending_key(project_id: str) -> str:
    now = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    return f"{PENDING}/{project_id}/{now}-{uuid.uuid4().hex}.json"


def processed_key(pending: str) -> str:
    return PROCESSED + pending[len(PENDING) :]


def accept_batch(
    project_id: str,
    raw: bytes,
    blobs: BlobStore,
    queue: Queue,
    limits: IngestLimits,
) -> AcceptResult:
    """Validate each event, store the valid ones as one pending blob and enqueue it.

    Raises BatchTooLarge (413), InvalidBatch (400) or StorageUnavailable (503). A failed
    enqueue is not an error: the blob stays pending and the sweeper replays it.
    """
    if len(raw) > limits.max_bytes:
        raise BatchTooLarge(f"Request body exceeds {limits.max_bytes} bytes.")
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise InvalidBatch("Body must be valid JSON.") from exc
    batch = payload.get("batch") if isinstance(payload, dict) else None
    if not isinstance(batch, list):
        raise InvalidBatch('Body must be an object with a "batch" array.')
    if len(batch) > limits.max_events:
        raise BatchTooLarge(f"Batch has more than {limits.max_events} events.")

    successes: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    valid: list[Any] = []
    for index, item in enumerate(batch):
        event_id = item.get("id") if isinstance(item, dict) else None
        label = event_id if isinstance(event_id, str) and event_id else f"index-{index}"
        try:
            parse_event(item)
        except EventError as exc:
            errors.append({"id": label, "status": 400, "message": str(exc)})
        else:
            valid.append(item)
            successes.append({"id": label, "status": 201})

    result = AcceptResult(successes, errors)
    if not valid:
        return result

    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else None
    blob = {"project_id": project_id, "metadata": metadata, "events": valid}
    key = pending_key(project_id)
    try:
        blobs.put(key, json.dumps(blob, separators=(",", ":")).encode())
    except BlobStoreError as exc:
        log.error("blob store write failed: %s", exc)
        raise StorageUnavailable("Storage is unavailable; retry later.") from exc
    result.blob_key = key
    try:
        queue.enqueue({"project_id": project_id, "blob_key": key})
    except Exception:  # durable already; the sweeper re-enqueues pending blobs
        log.exception("enqueue failed for %s; sweeper will replay it", key)
    return result
