# Plan: Traceloom Phase 1 — tracing core (MVP)

From: spec.md (cf751e2) · Status: accepted · Accepted by: Kunal Mugalkhod

<!-- Status: draft -> accepted (engineer; tech lead for higher-risk) -> implemented.
     Keep this file in sync with the code: a deviation updates plan.md in the same commit. -->

Greenfield repo: every file is new. Requirement ids (FR-/NFR-/AC-) refer to spec.md. Architecture: ARCHITECTURE.md and docs/adr/.

## Files that change
**Root**
- `pyproject.toml` — new — distribution `traceloom`, deps, `[dev]` extra, ruff and pytest config (`testpaths = ["tests", "sdk/python/tests"]`, markers `e2e`, `it`, `perf`), console script `traceloom = traceloom.cli:main`
- `.env.example` — new — every setting from `traceloom/config.py`, with comments (FR-X1)
- `Dockerfile`, `docker-compose.yml`, `docker-compose.lite.yml` — new — full stack with `mem_limit` values (FR-X2); `deploy/clickhouse/memory.xml`
- `README.md` — new — quickstart (lite and full), SDK snippet, demo agent, test commands
- `DESIGN.md`, `PRODUCT.md` — new — from the design step (D-1)
- `scripts/loadtest.py` — new — events/s and volume seeding against a running instance (ARCHITECTURE targets)

**Server package `traceloom/`**
- `__init__.py` (version), `config.py` (pydantic-settings `Settings`), `container.py` (adapter wiring by `TRACELOOM_MODE`), `main.py` (app factory, middleware, routers, lifespan: migrate in lite, seed models, start the in-process queue and sweeper)
- `ports.py` — `TraceStore`, `BlobStore`, `Queue`, `RateLimiter`, `Cache` protocols; `TraceQuery`, `SessionQuery`, `DashboardQuery`, `Page`, row dataclasses; cursor encode/decode
- `db/base.py`, `db/models.py` (meta tables + lite trace tables, from spec *Data*), `db/session.py` (engine; SQLite WAL + pragma + percentile aggregate registration), `db/migrations/` (Alembic env + `0001_initial.py`)
- `adapters/lite/sql_trace_store.py`, `local_blob.py`, `inproc_queue.py`, `memory.py` (rate limiter + cache)
- `adapters/full/clickhouse_trace_store.py` (+ `clickhouse_schema.sql`), `s3_blob.py`, `redis_queue.py` (Dramatiq actor `process_batch`), `redis_limiter.py`
- `auth/passwords.py`, `auth/sessions.py`, `auth/csrf.py`, `auth/api_keys.py`, `auth/permissions.py` (matrix + `require(perm)` dependency), `auth/deps.py` (current user, project, role, Basic key auth)
- `ingest/schemas.py` (pydantic event models for every type), `ingest/accept.py` (`accept_batch`: validate → blob → enqueue → 207 body), `ingest/merge.py` (ADR-0008 field-level LWW), `ingest/process.py` (load blob → fold → cost → upsert → aggregates → mark processed), `ingest/otel.py` (OTLP JSON and protobuf → events), `ingest/sweeper.py`
- `cost/registry.py` (load seed, match by regex/scope/valid_from, cache), `cost/pricing.py` (cost calc + tiers), `cost/tokenizers.py` (lazy tiktoken, safe fallback); `seed/models.yaml`
- `services/orgs.py`, `services/invites.py`, `services/mail.py` (SMTP or console + an in-memory outbox for tests), `services/comments.py` (mentions), `services/audit.py`, `services/views.py` (saved views + user prefs), `services/demo.py` (seed-demo)
- `api/public.py` (ingestion, otel, health), `api/errors.py`
- `web/` routers: `auth.py`, `orgs.py`, `settings.py` (general, members, keys, models), `traces.py`, `sessions.py`, `users.py`, `dashboard.py`, `share.py`; `web/render.py` (JSON pretty, chat-shape detection, sanitized Markdown via `markdown-it-py` with HTML disabled), `web/tree.py` (tree, timeline geometry, agent-graph layout → SVG data)
- `web/templates/` — `base.html`, `_macros.html`, auth/*, orgs/*, settings/*, traces/{list,detail,_tree,_timeline,_graph,_io,_comments,_scores}.html, sessions/*, users/*, dashboard.html, share.html, errors/{403,404}.html
- `web/static/` — `app.css` (DESIGN.md tokens, light and dark), `app.js` (theme, shortcuts, copy, column chooser, CSRF header for HTMX, chart init), `vendor/htmx.min.js`, `vendor/uplot.iife.min.js`, `vendor/uplot.min.css`
- `cli.py` — `migrate`, `worker`, `seed-demo`, `serve`

**SDK `sdk/python/`**
- `pyproject.toml` (distribution `traceloom-sdk`, deps: `httpx`; extras `openai`, `anthropic`, `langchain`)
- `traceloom_sdk/__init__.py` (`Traceloom`, `observe`, `get_client`), `client.py` (config, bounded queue, flusher thread, retry/backoff, sampling, mask, atexit), `context.py` (contextvars stack), `observe.py` (decorator for sync/async/generators), `objects.py` (Span/Generation/Event handles), `openai.py`, `anthropic.py`, `langchain.py`, `litellm.py`
- `tests/` (inside the SDK, collected by the root pytest)

**Examples**
- `examples/agent.py`, `examples/fake_llm.py`, `examples/README.md` (FR-P7)

**Tests `tests/`**
- `conftest.py` (lite app on a tmp SQLite DB and tmp blob dir; synchronous queue drain helper `drain()`; user/org/project/key factories; mail outbox; a live-server fixture for e2e via uvicorn in a thread; Playwright browser fixture with Chromium fallback to the `chrome` channel)
- `api/` — `test_auth.py`, `test_orgs_invites.py`, `test_api_keys.py`, `test_permissions.py`, `test_ingestion.py`, `test_merge.py`, `test_otel.py`, `test_cost.py`, `test_traces_api.py`, `test_trace_actions.py`, `test_comments.py`, `test_sessions_users.py`, `test_dashboard.py`, `test_security.py`, `test_durability.py`
- `contract/test_trace_store_contract.py` (lite always; ClickHouse when `TRACELOOM_IT=1`), `contract/test_blob_queue_contract.py`
- `unit/test_clickhouse_sql.py`, `unit/test_s3_blob_fake.py`, `unit/test_redis_queue_fake.py` (full adapters against fakes: `fakeredis`, `moto`)
- `perf/test_volume.py` (AC-7)
- `e2e/test_journey_first_trace.py`, `test_journey_filters.py`, `test_journey_xss.py`, `test_journey_viewer.py`, `test_journey_session.py`, `test_journey_dashboard.py`

## Dependencies
In `pyproject.toml` `[project.dependencies]`:
- `fastapi>=0.115,<1`, `uvicorn[standard]>=0.30`, `jinja2>=3.1`, `python-multipart>=0.0.9` — web
- `pydantic>=2.7,<3`, `pydantic-settings>=2.3,<3`, `email-validator>=2.1` — config and schemas
- `sqlalchemy>=2.0.30,<2.1`, `alembic>=1.13`, `psycopg[binary]>=3.1` — meta DB (Postgres optional at runtime)
- `argon2-cffi>=23.1`, `itsdangerous>=2.2` — auth (ADR-0009)
- `markdown-it-py>=3.0`, `pyyaml>=6.0`, `httpx>=0.27` — rendering, seed file, internal HTTP
- `opentelemetry-proto>=1.27,<2`, `protobuf>=4.25` — OTLP protobuf
- `tiktoken>=0.7` — tokenizer (lazy, optional at runtime)
- `clickhouse-connect>=0.7`, `boto3>=1.34`, `redis>=5.0`, `dramatiq[redis]>=1.17` — full-mode adapters (imported only in full mode)

`[project.optional-dependencies].dev`:
- `pytest>=8.2`, `pytest-asyncio>=0.23`, `pytest-playwright>=0.5`, `playwright>=1.45`, `ruff>=0.5`, `fakeredis>=2.23`, `moto[s3]>=5.0`, `respx>=0.21`, `openai>=1.40`, `anthropic>=0.34`, `langchain-core>=0.2`

SDK `sdk/python/pyproject.toml`: `httpx>=0.27`. The extras pull the provider SDKs.

Vendored static (no package manager): htmx 2.x and uPlot 1.6 (both MIT), license headers kept.

## Order of work
Stories are ordered so the ingestion → detail spine lands first. Each step leaves `sdlc.py verify` green. Steps 2–13 depend on step 1; 7–9 and 13 also need step 4; 11 needs 3, 4, 5 and 10. Within the same number, steps are independent and parallelizable.

1. **Foundation** (blocks all). Covers:
   - `pyproject.toml`, `config.py`, `ports.py`, `db/` + Alembic `0001_initial`, `container.py`, `main.py` skeleton with security-headers/CSP middleware
   - `auth/passwords`, `sessions`, `csrf`, `permissions`, `deps`
   - `tests/conftest.py` with factories and the `drain()` helper
   - `.env.example`, the README stub, `GET /api/public/health`
   - Tests: health, settings load, CSP headers present, permission matrix unit test
2. **Design D-1** (designer agent). DESIGN.md, PRODUCT.md and `design/mockup.html` for every screen in the spec UX. UI tasks of steps 3, 5 and 7–9 wait for approval. Backend tasks don't.
3. **Accounts and projects** (FR-A1–A6). Sign-up, login, logout, reset, mail service, orgs, projects, invites, members and roles, API keys + Basic auth + key cache, auth rate limits. Settings pages get minimal templates first and are restyled once D-1 is approved.
4. **Ingestion core** (FR-I1–I3, I5, I6, NFR-3). Covers:
   - event schemas, `accept_batch`, lite blob + in-process queue, the merge, `process.py`, the sweeper, ingestion rate limit, size limits
   - the lite `SqlTraceStore` write path + aggregates
   - contract tests for writes; AC-2, AC-8
5. **Cost and models** (FR-C1–C5). Seed YAML, registry, pricing with tiers, tokenizer fallback, hooked into `process.py`, the Models settings page (Admin). AC-9.
6. **OTLP** (FR-I4). JSON + protobuf mapping to events through `accept_batch`. AC-10.
7. **Read path + traces UI** (FR-T1–T9, T13). Covers:
   - the `SqlTraceStore` query side (list with filters/search/sort/cursor, detail, flags)
   - `web/render.py`, `web/tree.py`
   - list page (filters, saved views, column chooser, env switcher), detail page (tree, timeline, graph, IO modes, error jump, tags, scores)
   - keyboard shortcuts
   - AC-3, AC-4 (in part)
8. **Sharing, comments, deletes** (FR-T10–T12). Bookmark, public share page, comments with mentions, delete one or by filter, audit log. AC-14.
9. **Sessions, users, dashboard** (FR-S1–S4, FR-D1–D2). Store aggregate queries (percentile aggregate on SQLite), pages and uPlot charts. AC-6, AC-11.
10. **Python SDK** (FR-P1–P6). Independent of the server code (talks HTTP only), so it can start right after step 1:
   - client, flusher, observe, context
   - manual API, mask, the 4 integrations with fake-provider tests
   - AC-12
11. **Demo agent + seed-demo + CLI** (FR-P7, FR-X3). `examples/`, `traceloom seed-demo`, `traceloom migrate|worker`.
12. **Full-mode adapters + Compose** (ADR-0001–0003, FR-X2). Covers:
    - ClickHouse store (schema, upsert with `event_ts`, `FINAL` reads, same query API)
    - S3 blob store, Redis/Dramatiq queue + worker CLI, Redis limiter/cache
    - Dockerfile + Compose with memory limits, `scripts/loadtest.py`
    - unit tests with fakes; contract tests under `TRACELOOM_IT=1`
13. **Acceptance and perf** (AC-1, AC-4, AC-5, AC-7, AC-15). Covers:
    - the e2e Playwright journeys (first trace from `examples/agent.py` against the live server, filters, XSS in every view including share, Viewer read-only, session replay, dashboard)
    - `perf/test_volume.py`
    - the full permissions sweep test that walks every write route as a Viewer
    - README completed; CLAUDE.md Architecture filled

## Tests that prove it
| AC | Tests |
|---|---|
| AC-1 | `e2e/test_journey_first_trace.py`: UI sign-up → org → project → key, run `examples/agent.py` (subprocess, env keys, fake LLM) against the live server, assert the trace is visible ≤ 5 s, ≥ 3 tree levels, a timeline TTFT marker, cost > 0 "calculated", and the "Jump to first error" selection lands on `lookup_order` |
| AC-2 | `api/test_merge.py` (unit: LWW fill/overwrite, tags union, idempotent re-apply); `api/test_ingestion.py::test_duplicate_batch`, `::test_update_before_create`; `contract/test_trace_store_contract.py` same cases |
| AC-3 | `api/test_traces_api.py::test_search_output_word`, `::test_metadata_filter_and_range`; `e2e/test_journey_filters.py` |
| AC-4 | `api/test_security.py::test_script_escaped_*` (list, detail ×3 modes, session, share; asserts `&lt;script&gt;` and no raw tag); `e2e/test_journey_xss.py` (registers a `dialog` handler → fail) |
| AC-5 | `api/test_permissions.py::test_viewer_403_every_write` (iterates the app's route table for unsafe methods), `::test_no_edit_controls_for_viewer`; `::test_key_cross_project` (401/403, B unchanged); `e2e/test_journey_viewer.py` |
| AC-6 | `api/test_sessions_users.py::test_session_replay_order_and_total`; `e2e/test_journey_session.py` |
| AC-7 | `perf/test_volume.py` (bulk-seeds 10k/50k via store `upsert`, times the list page 1 + filtered page 2 < 1 s, detail < 500 ms, dashboard < 2 s) |
| AC-8 | `api/test_durability.py::test_queue_stopped_then_replayed_exactly_once` (consumer paused, 3 batches → 207, zero rows; resume + sweeper → counts exact, re-run sweeper → unchanged) |
| AC-9 | `api/test_cost.py` (provided, calculated, project override, tier above threshold, no match → none, tokenizer fallback with a stub tokenizer) |
| AC-10 | `api/test_otel.py` (JSON fixture + the same spans encoded as protobuf → identical observations; ERROR status) |
| AC-11 | `api/test_dashboard.py::test_numbers_match_raw` (computes every figure in Python from the seeded rows) |
| AC-12 | `sdk/python/tests/test_client.py` (unreachable host, 500 retry, never raises, flush timeout, sampling, mask raising), `test_observe.py` (sync/async/gen/async-gen nesting, exception → ERROR), `test_openai.py`, `test_anthropic.py` (fake transports via `respx`, stream TTFT), `test_langchain.py`, `test_litellm.py` |
| AC-13 | `api/test_auth.py::test_reset_invalidates_sessions`, `api/test_orgs_invites.py::test_invite_role`, `::test_last_owner_protected` |
| AC-14 | `api/test_trace_actions.py::test_share_toggle`, `::test_delete_by_filter_audited`; `api/test_comments.py::test_mention_sends_mail` |
| AC-15 | The whole default suite runs with `TRACELOOM_MODE=lite`, no services (conftest asserts no Redis/CH env is used) |

Also: `api/test_security.py` covers CSRF rejection without a token, login rate limit → 429, ingestion limit → 429, secret never in the DB in plaintext, and session cookie flags. `unit/test_clickhouse_sql.py` covers ClickHouse query building and the merge row shape.

## Risks
- **Riskiest step: 4 (ingestion core + merge + durability).** Everything else reads what it writes. Mitigation: it lands as its own story with merge unit tests and contract tests first. The `drain()` helper makes async processing deterministic in tests, and the sweeper is idempotent.
- **What it could break:** this is greenfield, so the risk is inconsistency between the lite and ClickHouse stores. Mitigation: one contract suite parametrized over adapters.
- **Parallel-team conflicts** on the shared files `db/models.py`, `main.py` (router registration) and `conftest.py`. Mitigation: step 1 creates every table, every router module stub and every fixture up front, so later stories add code in their own modules.
- **SQLite performance** for AC-7: substring search over 10k traces and percentile aggregates. Mitigation: a denormalized `search_text` (truncated to 4 KB) and aggregate columns on `traces`; the indexes from the spec; WAL. Fallback: an FTS5 virtual table (recorded as a deviation if used).
- **Playwright flakiness** and browser availability. Mitigation: a cached Chromium or the `chrome` channel; one live server per session; `expect` auto-waits; no sleeps.
- **OTLP protobuf version skew.** Pin `opentelemetry-proto` <2. The test round-trips a fixture built with the same package.
- **The SDK breaking host apps.** Fault-injection tests. Every public SDK entry point is wrapped in a `try/except` that logs.
- **Security:** CSRF/HTMX wiring and the CSP blocking vendored scripts. Mitigation: security tests in step 1, and every template uses external JS only (no inline handlers).
- **Migrations:** one initial Alembic revision, with lite applying it at startup. Postgres partitioning is deferred (spec concern 1).

## Alternatives considered
- **A SPA frontend:** rejected per ADR-0007.
- **Trace aggregates computed at query time** (join observations): rejected, because it fails the list < 1 s target at scale. Aggregates are denormalized at ingest instead.
- **Event-sourced storage** (keep every event, merge at read): rejected for Phase 1, because reads get complex. The ADR-0008 field-level LWW plus raw blobs gives replay without it.
- **Building the full adapters first:** rejected. Lite is the test default and the faster path to a working journey. Full mode follows the same contract.
- **SQLite FTS5 from the start:** deferred, because `LIKE` on `search_text` meets AC-7 at the tested volume.
- **One mega-story per area:** rejected in favour of the 13-step order above, so 2 developers can work in parallel after step 1.

## Verification target
- `python3 .sdlc/bin/sdlc.py verify` green: setup, compileall, `pytest -q` (all API, contract-lite, unit, perf, e2e and SDK tests), and `ruff check .`.
- `.venv/bin/uvicorn traceloom.main:app --port 8000`, then `traceloom seed-demo`, then `python examples/agent.py`: the trace is visible at `/project/<id>/traces/<trace_id>` within 5 s with tree, timeline, graph, cost and the error highlighted.
- `GET /api/public/health` → `200 {"status":"ok"}`. Ingestion → `207` with `successes`/`errors`.
- The UI matches `design/mockup.html` at 1280px and 390px, in light and dark (QA compares them).
- `docker compose config` validates, and the sum of `mem_limit` values is < 6 GB.

## Deviations
<!-- Appended during build: date — what changed vs the plan — why. -->
- 2026-11-06 — S-1 foundation:
  - `Settings` has `TRACELOOM_ENV` (`dev|test|production`, default `dev`); a missing `TRACELOOM_SECRET_KEY` fails startup outside `dev`, and in `dev` an ephemeral key is generated. Cookies are `Secure` in production unless `TRACELOOM_COOKIE_SECURE` overrides.
  - `Container` also carries `engine`, `session_factory`, `api_key_cache` and `mailer`. In lite mode `build_container` wires the in-memory limiter and cache and a startup hook that runs Alembic `upgrade head`.
  - Cursors are `base64(json[(sort_value, id)] + checksum)`: integrity-checked, not secret (all queries are project-scoped). Added a `UserQuery` dataclass next to `SessionQuery`.
  - CSRF is a signed double-submit token (`tl_csrf` cookie, `X-CSRF-Token` header or `csrf_token` form field). Requests with HTTP Basic auth and `/api/public/*` are exempt. `SessionMiddleware` lives in `auth/sessions.py`.
  - The mail console fallback logs the message (FR-A2 needs the reset link) with API secrets, `SMTP_PASSWORD` and `TRACELOOM_SECRET_KEY` scrubbed.
  - The route-table permission sweep enumerates routes from the OpenAPI schema (FastAPI nests included routers privately), so routes with `include_in_schema=False` are not swept.
  - `ruff` excludes `.sdlc/` (governance scripts).
- 2026-11-06 — S-3 ingestion, cost, OTLP-ready core:
  - `TraceStore.get_existing` returns row dataclasses keyed kind -> id; `add_score` was added to the port, and `BlobStoreError` / `BlobNotFound` to `ports.py`. `SqlTraceStore.upsert` replaces a stored row when the incoming `event_ts` is not older and never overwrites the computed aggregates; field-level merging happens in `ingest/merge.py` (ADR-0008), not in the adapter. Delete-by-filter needs the read path (`_trace_ids_for_query`, raises until then).
  - Merge nuances: `input`/`output` are replaced whole (only `metadata` and `model_parameters` merge by key). Provided `usage_details` / `cost_details` replace derived (tokenizer / calculated) data. A bare observation id is provisional: an explicit `trace_id` later wins. Stub traces carry `event_ts` = epoch, so any real trace event overwrites them.
  - `Container` also has `models` (ModelRegistry, seeded at startup in both modes) and `sweeper`. In lite mode startup runs migrate, then seed, then starts the queue consumer, replays pending blobs and starts the sweeper. The in-process queue has `pause/resume/discard/drain`.
  - The model seed has no Claude 5-family entries because no list prices were available. Add them to `traceloom/seed/models.yaml` or as project definitions.
- 2026-11-06 — S-4 trace explorer (T-14, T-15, T-18):
  - `SqlReadMixin` expects the host store to expose `session_factory` (a sessionmaker) and exports `trace_filter_clauses(project_id, query)` for delete-by-query. Trace and observation `input`/`output` are stored as JSON text.
  - Read-contract fixtures live in `tests/contract/stores.py` (a pytest plugin registered in `tests/conftest.py`); `read_store` is parametrized lite always, ClickHouse via `tests/contract/conftest_full.py` under the `it` marker.
  - ClickHouse stores JSON values as JSON text, reads token/cost totals from the `total` key of `usage_details`/`cost_details`, orders tables by `(project_id, id)` (no partitioning, so replacement is never split across partitions), and indexes `lower(search_text|name|id)` with ngram bloom filters. `adapters/full/*.sql` is package data.
  - Markdown rendering disables the image rule as well as raw HTML, so viewing a trace never makes a remote request.
- 2026-11-06 — S-6 SDK, demo agent, CLI (T-24..T-27):
  - SDK sends camelCase bodies and emits `trace-create` at the root observation, a `*-create` at start and a `*-update` at end (plus a `trace-create` carrying the root output). Observation types other than span/generation/event use `observation-create/update`. `sdk/python/pyproject.toml` carries its own ruff config (py39 target) because the SDK supports Python 3.9+.
  - Usage keys are `input`, `output`, `total`, `cache_read`, `cache_write` (the cost engine's keys); OpenAI cached tokens are split out of `input`.
  - Provider SDK tests use scripted `httpx2`/`httpx` transports (`tests/conftest.py: Script`) instead of respx, which cannot see the provider SDKs' own `httpx2`.
  - `seed-demo` derives the demo key pair from the server secret (`TRACELOOM_SERVER_SECRET`, via Settings; the SDK still uses `TRACELOOM_PUBLIC_KEY`/`TRACELOOM_SECRET_KEY`) so reruns print the same keys; `traceloom worker` runs `container.startup()` (consumer and sweeper hooks) until SIGINT/SIGTERM.
- 2026-11-06 — S-5 analytics: dashboard token/cost totals read the `total` key of `usage_details` / `cost_details` (tokens fall back to input + output when `total` is absent; costs need `total`, which `cost/pricing.py` always derives). Per-model figures filter on the observation's own `start_time` and environment, trace figures on the trace timestamp. Session duration is max(trace start + latency) − min(trace start). Breakdowns are capped at 50 rows and top users at 10. The Postgres branches of the dialect helpers (`percentile_cont`, `date_trunc`, epoch extraction) are written but not exercised by the suite. Tests seed rows directly (`tests/seeding.py`) through a store built from `SqlReadMixin` + `SqlAnalyticsMixin`.
- 2026-11-06 — S-3 follow-ups after the merge with S-4/S-5:
  - `apply_cost` derives `usage_details["total"]` (input + output) when provided usage lacks it, so lite aggregates and the ClickHouse `total` reads agree. The merge drops a stale derived `total` from stored usage/cost when a newer event supplies parts without one.
  - `SqlTraceStore._trace_ids_for_query` selects ids with `trace_filter_clauses`, so `delete_traces(project_id, TraceQuery)` deletes every match (sort, cursor and page size are ignored) together with its observations, scores and comments.
- 2026-11-06 — S-2 accounts, orgs and settings UI (T-5 to T-8), built to the approved v3 design:
  - `app.css` is the mockup stylesheet minus the gallery chrome, plus a few utility classes in place of the mockup's inline `style=` attributes (the CSP blocks inline styles). Fonts (Inter, JetBrains Mono, SIL OFL) and htmx 2.0.4 are vendored under `web/static`; htmx runs with `includeIndicatorStyles` and `allowEval` off. Dialogs are native `<dialog>` elements driven by `app.js` (`data-dialog-open`). The theme is stored in `localStorage` and the `tl_theme` cookie.
  - New files beyond the task lists: `web/templating.py` (Jinja + `render`), `web/pages.py` (403/404 inside the shell when signed in), `base_auth.html`, `_icons.html`, `_help.html`, `settings/base.html`, `settings/_tabs.html`. `api/errors.py` and `main.py` changed: static mount, HTML error pages.
  - URL scheme: `/project/{id}/settings[/members|/keys|/models]`; org actions under `/orgs/{org_id}/...`; `/invite/{token}`. Signing in lands on `/project/{id}/traces`, which belongs to the traces UI task. The environment switcher is included with `ignore missing`; the date-range control is a template block.
  - Members: the "Joined" column from the mockup is omitted (`memberships` has no created-at column and the schema is not changed here). Self-removal (leave) goes through the remove route, so any member can leave, and the last Owner cannot. An invite is only good for the address it was sent to. The Models tab and page are Admin+ (the design shows a 403 panel for Viewers); the API keys tab and page are Admin+.
  - The permission sweep exempts `POST /orgs/new` (any signed-in user may create an organization). The Audit log tab arrives with the share/delete/audit task.
- 2026-11-06 — The server's secret env var is `TRACELOOM_SERVER_SECRET` (was `TRACELOOM_SECRET_KEY`, which clashed with the SDK's variable for the project API secret). Decided by Kunal Mugalkhod. `Settings.secret_key` keeps its attribute name; startup still fails outside dev when it is unset. Earlier entries in this list that mention the old name describe the state at the time.
- 2026-11-06 — S-6 full mode (T-28):
  - `container_full.configure` sets `container.start_consumer`; only `traceloom worker` calls it, so the web process enqueues and never consumes. The Dramatiq worker threads run in the worker process (no separate `dramatiq` CLI).
  - `BlobStoreError`/`BlobNotFound` are declared in `ports.py` identically to T-10 so the S3 store raises the same errors as the local one.
  - Compose uses `mem_limit` (total 4.8 GB); `TRACELOOM_SECRET_KEY` defaults to empty, which works only with `TRACELOOM_ENV=dev`.
  - The blob/queue IT contract is a separate `tests/contract/test_full_blob_queue_it.py` (the T-10 contract file is not on this branch); fold it into the shared parametrization on merge.
- 2026-11-06 — S-2 rework after QA (T-5 to T-8): templates import `_macros.html` with context, because `csrf()` reads `csrf_token` from the page context (without it every rendered form carried an empty token; the earlier tests posted the token in a header and missed it, and a test now posts the token read from the page). The 404 and 403 pages follow the mockup copy (cap line, heading, separate paragraphs, no echo of the framework's default detail). `traceloom/logging_setup.py` adds a stderr handler for the `traceloom` logger at INFO when no logging is configured, so the console mail fallback shows reset and invite links under uvicorn. At <=760px the settings tabs wrap and the members and pending-invite rows stack inside their card. Merged the team branch (compose files, README and the full-mode wiring test use `TRACELOOM_SERVER_SECRET`).
