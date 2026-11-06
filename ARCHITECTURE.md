# Traceloom architecture

Status: accepted (Phase 1 architect step) · Decisions: `docs/adr/` · Diagram: `traceloom-architecture.drawio`

This confirms the recommended architecture in `prompt.md`, with the refinements recorded as ADRs. Each target from the brief is restated below with the argument for why this design meets it.

## Shape

```
SDK / OTLP / browser ──► web (FastAPI + Jinja/HTMX)
                           │ 1. auth key (cache)  2. BlobStore.put(raw batch)  3. Queue.enqueue(ref)  4. 207
                           ▼
        BlobStore (local folder | S3/MinIO) ◄── worker ──► TraceStore (SQL lite | ClickHouse)
                    Queue (in-process | Redis/Dramatiq) ──┘        ▲
                                                                    │ list/detail/dashboard queries
        MetaStore: SQLAlchemy on SQLite (dev) or Postgres ── users, orgs, projects, keys, models, views, comments, audit
```

Every process imports one core package (`traceloom`). Three **ports** isolate the storage stack (ADR-0004):

| Port | Lite adapter (default) | Full adapter |
|---|---|---|
| `TraceStore`: traces, observations, scores, and their aggregates | SQLAlchemy tables in the meta DB (SQLite or Postgres) | ClickHouse `ReplacingMergeTree` tables |
| `BlobStore`: raw batches (later media and exports) | local folder (`TRACELOOM_BLOB_DIR`) | S3-compatible (MinIO locally) |
| `Queue`: batch references | in-process asyncio worker inside the web process | Redis + Dramatiq, with a separate `traceloom worker` process |
| `RateLimiter` / `Cache` | in-memory | Redis |

`TRACELOOM_MODE=lite|full` picks the adapters. Nothing else in the code branches on mode.

## Key flows
1. **Ingest** (ADR-0002, ADR-0008). Validate the envelope, check the key, then write the raw batch to `BlobStore` under `pending/`. Enqueue `{project_id, blob_key}` and return `207` with a per-event status. The worker loads the blob and validates each event. It folds events into entity rows with field-level last-writer-wins by event timestamp, computes cost, upserts into `TraceStore`, recomputes the aggregates of every touched trace, and then moves the blob to `processed/`. A **sweeper** re-enqueues `pending/` blobs older than `TRACELOOM_INGEST_SWEEP_SECONDS`. This covers enqueue failures and worker crashes.
2. **Read.** Pages and APIs call `TraceStore` query methods. Every method takes `project_id` as its first argument and uses keyset (cursor) pagination on `(timestamp, id)`, always bounded by a time range.
3. **Cost** (in the worker). Use the provided `cost_details` if present. Otherwise match the model with the project's definitions first, then global ones, by regex and `valid_from`. Multiply usage by the price per key. If usage is missing, count tokens with the definition's tokenizer. Store `cost_source = provided|calculated|none`.

## Targets and why they hold

| Target | Argument | How it is checked |
|---|---|---|
| Ingestion never blocks | The request path does one blob write and one enqueue: O(batch bytes), with no DB writes for trace data. | API test: `207` returns before the worker runs. |
| 1,000 events/s on one machine | At the SDK default of 50 events per batch, 1,000 events/s is 20 requests/s. Each request costs one key-cache hit, one blob PUT and one Redis LPUSH, so one uvicorn worker handles it. The worker writes to ClickHouse in batched `INSERT`s, and ClickHouse sustains 10⁵+ rows/s. Lite on SQLite (WAL, one transaction per batch) is sized for dev and small teams (about 2k events/s measured target). | `scripts/loadtest.py` reports events/s. |
| Accepted events never lost | The blob is durable **before** the `207`. Processing is idempotent (ADR-0008), so a crash anywhere means replay from `pending/`. If Redis or the worker is down, the sweeper picks the work up later. If ClickHouse is down, the job fails, the blob stays in `pending/`, and it is retried. | Journey 10 test: queue stopped → `207` → restart → each event exactly once. |
| List < 1 s @ 10M obs | Trace aggregates (latency, tokens, cost, max level, observation count) are denormalized onto the trace row at ingest, so the list never joins observations. ClickHouse `traces` is `ORDER BY (project_id, toDate(timestamp), id)`, `PARTITION BY toYYYYMM(timestamp)`, and the list is always time-bounded. Text search uses `ngrambf_v1` skip indexes on name, input and output. | Journey 9 (lite, 10k/50k) in pytest; `loadtest.py --volume` for full. |
| Detail < 500 ms | `observations` is `ORDER BY (project_id, toDate(start_time), trace_id, id)` with a bloom-filter index on `trace_id`. The query is bounded by trace timestamp ± `TRACELOOM_TRACE_WINDOW_HOURS`. | Timed API test. |
| Dashboards < 2 s | Dashboard queries hit the trace and observation tables with project + time-range prefix pruning. They use ClickHouse `quantiles()` and grouped sums, with at most about 8 queries per page, run concurrently. | Timed API test (lite); load script (full). |
| 100M+ by adding storage | ClickHouse partitions by month and scales by adding disks or shards. Raw blobs live in object storage. No schema in the app layer assumes a single node. | ADR-0001. |
| Compose < 6 GB | The `mem_limit` values in `docker-compose.yml` add up to 5.5 GB: ClickHouse 2.5 GB (`max_server_memory_usage_to_ram_ratio`), Postgres 768 MB, Redis 256 MB, MinIO 512 MB, web 768 MB, worker 768 MB. | `docker compose config` check in the README. |
| Lite mode without ClickHouse or S3 | The default mode is lite, and all of pytest runs in lite mode. | Journey 12 = the whole default suite. |

## Security model
- Sessions use signed, HttpOnly, SameSite=Lax cookies. Passwords are hashed with argon2id. CSRF is checked with a per-session token on every unsafe cookie-authenticated request: form field or `X-CSRF-Token` header, which HTMX sends.
- Project API keys: public key `tl-pk-<random>`, secret key `tl-sk-<random>`. Only `HMAC-SHA256(secret, TRACELOOM_SERVER_SECRET)` is stored (ADR-0009). Auth is HTTP Basic.
- One dependency resolves `(user|key) → project → role` for every route, then checks a permission from a single matrix (`traceloom/auth/permissions.py`).
- Templates autoescape. User content is rendered as text, JSON (escaped) or Markdown (sanitised, with no raw HTML). A strict CSP (`script-src 'self'`) blocks inline scripts.

## Phase-ahead decisions
ADR-0005 (code-evaluator sandbox) and ADR-0006 (gateway as a separate process) are decided now so Phase 1 leaves room for them. Neither is built in Phase 1.
