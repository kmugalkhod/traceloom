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
