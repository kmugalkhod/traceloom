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

## Prompts and playground

Manage prompts in the web app under **Prompts**: each save creates an immutable version, and labels (`production` is protected, so only Admins move it) point at the version an app should get. Content can include other prompts with `@@@prompt:<name>|label=<label>@@@`. **Playground** runs a prompt against your own provider keys (Settings, LLM connections); set `TRACELOOM_ENCRYPTION_KEY` to a Fernet key first (`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`). In dev an ephemeral key is generated with a warning.

Fetch a prompt from your app with the Python SDK:

```python
import traceloom_sdk

prompt = traceloom_sdk.get_prompt("support", fallback="You help {{name}}.")  # production label
messages = prompt.compile(name="Ann")  # a str for text prompts, a list of messages for chat

with traceloom_sdk.start_generation("answer", model="claude-sonnet-5-5", input=messages,
                                    prompt=prompt):  # links the generation to support vN
    ...
```

`get_prompt` caches for `TRACELOOM_PROMPT_CACHE_TTL` seconds (default 60; `0` disables), serves the old version while refreshing in the background, and uses the fallback when the server is unreachable (a fallback prompt is not linked). It raises `PromptNotFound` only when there is no cached value and no fallback. With the OpenAI and Anthropic wrappers, pass `traceloom_prompt=prompt` to a call to link it. See `examples/prompt_app.py` and the public API under `/api/public/v2/prompts`.

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
