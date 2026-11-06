# Traceloom

Self-hosted LLM-engineering platform: ingest traces from LLM apps and agents, debug them, track cost, manage prompts and evaluate quality.

## Quickstart (lite mode)

```
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]' -e ./sdk/python
cp .env.example .env
.venv/bin/uvicorn traceloom.main:app --port 8000
curl localhost:8000/api/public/health
```

Lite mode needs only SQLite (or Postgres), an in-process queue and a local blob folder. All settings are documented in `.env.example`.

## Tests

```
.venv/bin/pytest -q
.venv/bin/ruff check .
```

## Docker

Lite mode (web server only, SQLite and a local blob folder in a volume):

```bash
docker compose -f docker-compose.lite.yml up --build
```

## Full mode

Full mode runs web, worker, Postgres, Redis, ClickHouse and MinIO. Memory limits total 4.8 GB
(web 768 MB, worker 768 MB, Postgres 512 MB, Redis 256 MB, ClickHouse 2 GB, MinIO 512 MB).

```bash
export TRACELOOM_SERVER_SECRET=$(openssl rand -hex 32)   # keep it: API key hashes depend on it
docker compose up --build
docker compose exec web traceloom seed-demo
```

- `web` runs `traceloom migrate`, then serves on port 8000. It accepts events, stores each batch
  in MinIO (S3) and enqueues it on Redis.
- `worker` (`traceloom worker`) consumes the queue with Dramatiq, writes to ClickHouse and runs
  the sweeper, which replays batches that were stored but never processed.
- Set `TRACELOOM_ENV=production`, `POSTGRES_PASSWORD`, `MINIO_ROOT_PASSWORD` and
  `CLICKHOUSE_PASSWORD` before exposing it. All settings are environment variables (`.env.example`).
- `deploy/clickhouse/memory.xml` keeps ClickHouse inside its limit.
- The boto3, redis, dramatiq and ClickHouse clients load only when `TRACELOOM_MODE=full`.

Integration tests for the full adapters need the services running:
`docker compose up -d minio redis clickhouse` then `TRACELOOM_IT=1 .venv/bin/pytest -m it`.

Load test: `python scripts/loadtest.py --help` lists the target volumes (lite: 10k traces and 50k
observations; full: 1,000 events/s sustained).
