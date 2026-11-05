# ADR-0004: Lite mode through storage ports

Status: accepted · Date: 2026-11-06

## Decision
Define the protocols `TraceStore`, `BlobStore`, `Queue`, `RateLimiter` and `Cache` in `traceloom/ports.py`. Adapters live in `traceloom/adapters/{lite,full}/`, and `TRACELOOM_MODE` selects them once, in `traceloom/container.py`. Routes and the worker depend only on the ports.

Lite mode runs on SQLite by default (`sqlite:///./data/traceloom.db`, in WAL mode) or on Postgres via `DATABASE_URL`. It uses a local blob folder and an in-process queue, so `uvicorn traceloom.main:app` alone is a complete instance.

## Consequences
- One contract test suite (`tests/contract/`) runs against the lite adapters always, and against the full adapters when `TRACELOOM_IT=1` and Compose services are up.
- SQLite has no percentile function, so the lite adapter registers a Python aggregate on each connection.
