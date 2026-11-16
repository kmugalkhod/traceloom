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

with traceloom_sdk.start_generation(
    "answer", model="claude-sonnet-5-5", input=messages, prompt=prompt
):  # links the generation to support vN
    ...
```

`get_prompt` caches for `TRACELOOM_PROMPT_CACHE_TTL` seconds (default 60; `0` disables), serves the old version while refreshing in the background, and uses the fallback when the server is unreachable (a fallback prompt is not linked). It raises `PromptNotFound` only when there is no cached value and no fallback. With the OpenAI and Anthropic wrappers, pass `traceloom_prompt=prompt` to a call to link it. See `examples/prompt_app.py` and the public API under `/api/public/v2/prompts`.

## Evaluation

Run `traceloom seed-demo` to get a demo project with a score config, an annotation queue, a 20-item dataset and two runs (safe to repeat).

**Score configs.** Settings, Score configs (Admins edit) defines a score: a name, a type (`NUMERIC` with a range, `CATEGORICAL` with labelled values, or `BOOLEAN`) and a description. Scores named after a config are checked against it, and annotation queues and the Scores tab render inputs from it. Archive a config to retire it; its type and categories are locked once scores use it.

**Scoring from code.**

```python
import traceloom_sdk

with traceloom_sdk.start_span("answer") as span:
    span.score("helpfulness", 4, comment="clear and correct")  # on the current trace
traceloom_sdk.score("thumbs", True, trace_id="<trace id>", config_id="<config id>")
```

**Feedback snippet.** Mark a config `public feedback`, add your site under Settings, Evaluation, allowed feedback origins, and paste the `feedback.js` snippet shown there. It posts thumbs or ratings with the public key only (`POST /api/public/feedback`, rate limited by `TRACELOOM_RATE_FEEDBACK`); only configs marked public accept it.

**Judge rules.** Evaluator templates plus rules (Evaluation, Evaluators) score new traces or observations with an LLM judge: a target, a filter, a sampling percentage, a delay (default 30 s) and a variable mapping. Results are `EVAL` scores with the reasoning as the comment. Rules apply to new data only; backfill is capped by `TRACELOOM_EVAL_BACKFILL_MAX` (1,000). Failed calls retry up to `TRACELOOM_EVAL_MAX_ATTEMPTS` (3). Judge traces live in `TRACELOOM_EVAL_ENVIRONMENT` and never trigger rules.

**Daily cost cap.** Each project has a per-day (UTC) judge and experiment spend cap, `TRACELOOM_EVAL_DAILY_COST_CAP_USD` (default 10), which Admins can override in Settings, Evaluation. `0` pauses judges. A call that would exceed it is marked `capped`, not failed, and is not retried that day.

**Datasets.** Datasets hold versioned items (input, expected output, metadata). Create them from a trace ("Add to dataset"), by CSV or JSON import, or through `/api/public/datasets`. A run records the dataset version it used, so results stay comparable after edits.

**Experiments.**

```python
import traceloom_sdk
from traceloom_sdk import Evaluation

def task(item):
    return my_app(item.input["question"])

def exact(input, output, expected_output):
    return Evaluation(name="exact", value=output == expected_output)

result = traceloom_sdk.run_experiment(
    traceloom_sdk.get_dataset("demo-support-questions"), task, evaluators=[exact], run_name="v2"
)
```

Each item runs `task` in a trace linked to the run; evaluator results become `API` scores on that trace. A task error is recorded on the item and never stops the run. In the UI, "New experiment" on a dataset runs a prompt version over every item, and Compare shows up to four runs side by side with regressions highlighted.

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
