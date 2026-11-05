# ADR-0006: The AI gateway is a separate process (Phase 5)

Status: accepted · Date: 2026-11-06

## Decision
The gateway is its own ASGI app (`traceloom.gateway:app`) and its own Compose service. It shares the core package and publishes traces through the same blob-plus-queue path as the SDK. Streaming proxy load and provider latency then never hold UI or ingestion workers.

## Consequences
Phase 1 keeps ingestion logic in a reusable function (`traceloom.ingest.accept_batch`) so the gateway can call it in-process.
