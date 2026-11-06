"""Worker side of ingestion: raw batch -> merged rows with cost -> TraceStore."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

from traceloom.container import Container
from traceloom.cost.pricing import apply_cost
from traceloom.ingest.accept import processed_key
from traceloom.ingest.merge import Existing, merge_events, needed_ids
from traceloom.ingest.schemas import EventError, ParsedEvent, parse_event
from traceloom.ports import BlobNotFound

log = logging.getLogger("traceloom.ingest")


def load_events(project_id: str, raw: bytes) -> list[ParsedEvent]:
    doc = json.loads(raw)
    if doc.get("project_id") != project_id:
        raise ValueError("blob belongs to a different project")
    events = []
    for item in doc.get("events", []):
        try:
            events.append(parse_event(item))
        except EventError as exc:  # accept_batch only stores valid events; be defensive anyway
            log.warning("skipping invalid stored event: %s", exc)
    return events


def process_message(container: Container, message: Mapping[str, Any]) -> None:
    """Apply one pending batch. Safe to run again for the same batch (at-least-once delivery):
    the merge is idempotent and the blob only moves to processed/ at the end."""
    project_id, blob_key = message["project_id"], message["blob_key"]
    blobs, store = container.blob_store, container.trace_store
    try:
        raw = blobs.get(blob_key)
    except BlobNotFound:
        return  # already processed by an earlier delivery
    events = load_events(project_id, raw)
    if events:
        existing = Existing.from_store(store.get_existing(project_id, needed_ids(events)))
        merged = merge_events(events, existing)
        for obs in merged.observations.values():
            apply_cost(obs, project_id, container.models)
        store.upsert(
            project_id,
            list(merged.traces.values()),
            list(merged.observations.values()),
            list(merged.scores.values()),
        )
        store.refresh_trace_aggregates(project_id, merged.trace_ids)
    blobs.move(blob_key, processed_key(blob_key))
