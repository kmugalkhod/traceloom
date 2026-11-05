# ADR-0002: Blob-first ingestion with a pending sweeper

Status: accepted · Date: 2026-11-06

## Context
Ingestion must never block the caller. Accepted events must survive a worker, Redis or database outage.

## Decision
The web server writes each raw batch to `BlobStore` at `pending/<project>/<yyyy-mm-dd>/<uuid>.json`, enqueues a reference, and returns `207`. The worker processes the batch and moves the blob to `processed/`. A sweeper, running in the worker in full mode or in-process in lite mode, re-enqueues `pending/` blobs older than `TRACELOOM_INGEST_SWEEP_SECONDS`. If the blob write itself fails, the API returns `503`, because the event was never accepted.

## Alternatives
- Direct DB writes in the request: blocks the caller and loses events when the DB is down.
- Queue-only (put events on Redis): Redis memory becomes the buffer, and replay is not possible.

## Consequences
- At-least-once delivery, so processing must be idempotent (ADR-0008).
- Raw batches are kept, which enables replay and debugging. Their retention is a later setting (Phase 4).
