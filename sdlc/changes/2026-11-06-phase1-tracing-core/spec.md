# Spec: Traceloom Phase 1 — tracing core (MVP)

From: intent.md (7f1bdc4) · Status: accepted · Record: prompt.md
Skills applied: none project-level (no `.claude/skills/`); impeccable (UX guidance for the design step); REVIEW.md security pass
Architecture: `ARCHITECTURE.md` + `docs/adr/0001–0009` (proposed, approved with this spec)

## Summary
Build the first runnable Traceloom: a FastAPI web/API server with a worker, lite and full storage adapters behind ports, accounts with role-based access, batched and OTLP ingestion, cost calculation, the traces/sessions/users/dashboard UI, and the `traceloom-sdk` Python package with integrations and a demo agent.

## Functional requirements

### Accounts, orgs, projects (A)
- **FR-A1** A visitor can sign up with email + password (min 10 chars). They can then log in and log out. Open by default; `TRACELOOM_SIGNUP_ENABLED=false` restricts sign-up to invited emails.
- **FR-A2** Password reset: request by email → a single-use token valid for `TRACELOOM_RESET_TOKEN_MINUTES` → set a new password. Every session is invalidated. Mail goes over SMTP when `SMTP_HOST` is set; otherwise it is logged to the console. The response is the same whether or not the email exists.
- **FR-A3** A logged-in user can create an organization (and becomes its Owner) and create projects inside it. A user can belong to many organizations, with one role in each.
- **FR-A4** Owners and Admins can invite by email with a role. The invite link (token, expiring) adds an existing user to the org, or leads a new user through sign-up into it. Admins can change roles and remove members. Only an Owner can grant Owner or delete the org. The last Owner cannot leave or be demoted.
- **FR-A5** Roles and permissions follow the matrix in *Design → Permissions*. Every page and API checks it server-side. Buttons for disallowed actions are not rendered.
- **FR-A6** Admins can create project API keys with a note. The pair is `tl-pk-…` / `tl-sk-…`, and the secret is shown once. Keys are listed by public key, note, created date and last-used date, and can be revoked. A revoked key fails within 1 s, because revoke evicts it from the cache.

### Ingestion (I)
- **FR-I1** `POST /api/public/ingestion` with HTTP Basic `pk:sk` and body `{"batch":[{"id","type","timestamp","body"}], "metadata"?}`. Supported types: `trace-create`, `span-create`, `span-update`, `generation-create`, `generation-update`, `event-create`, `score-create`. Also accepted: `observation-create` / `observation-update` with any observation type in `body.type` (agent, tool, chain, retriever, embedding, evaluator, guardrail).
- **FR-I2** Returns `207` with `{"successes":[{"id","status":201}], "errors":[{"id","status":400,"message"}]}`. Events that fail schema validation are reported as errors and not queued. Valid events are queued. If there are more events than `TRACELOOM_INGEST_MAX_EVENTS` or more bytes than `TRACELOOM_INGEST_MAX_BYTES`, the response is `413`. A bad key gets `401`. A blob-store failure gets `503`.
- **FR-I3** Processing is asynchronous and idempotent, and order-independent per ADR-0008. The same batch sent twice gives no duplicates. An update before its create ends with the merged, correct row.
- **FR-I4** `POST /api/public/otel/v1/traces` accepts OTLP/HTTP in JSON (`application/json`) and protobuf (`application/x-protobuf`).
  - Spans map to observations: span id → id, trace id → trace id, parent span id → parent. Resource and scope attributes go into metadata.
  - `gen_ai.*` attributes make a span a `generation`: `gen_ai.request.model` / `gen_ai.response.model` → model, `gen_ai.usage.input_tokens` / `output_tokens` (and legacy `prompt_tokens` / `completion_tokens`) → usage_details, `gen_ai.request.*` params → model_parameters, and prompt/completion attributes and events → input/output.
  - The status code ERROR becomes level ERROR plus a status message.
  - `traceloom.*` attributes map user_id, session_id, tags, environment, release and the observation type.
  - The response is OTLP `ExportTraceServiceResponse` with `partialSuccess`.
- **FR-I5** `GET /api/public/health` returns `{"status":"ok","version"}`, plus storage and queue checks when `?deep=1`.
- **FR-I6** A root span with no explicit trace fields creates the trace from that span (name, input, output). Observations that point at an unknown trace create a stub trace.

### Cost and tokens (C)
- **FR-C1** Built-in model definitions load from `traceloom/seed/models.yaml` at startup, upserted as global definitions. Each has a match regex, a tokenizer (optional), prices per usage key (USD per token), an optional price tier above an input-token threshold, and `valid_from`. The seed covers current Claude, GPT, Gemini and Llama families.
- **FR-C2** Admins can add, edit and delete project definitions on a Models settings page. A project definition overrides a global one when both match. Among same-scope matches, the one with the latest `valid_from ≤ generation start` wins.
- **FR-C3** If `cost_details` is provided, it is used and `cost_source=provided`. Otherwise, if usage plus a matching definition is available, cost = Σ usage[key] × price[key], with `total` derived, and `cost_source=calculated`. Otherwise `cost_source=none`.
- **FR-C4** If `usage_details` is missing and the definition has a tokenizer, the worker counts input and output tokens with it and marks `usage_source=tokenizer`.
- **FR-C5** The UI shows cost with its source ("provided" / "calculated") on generations, plus rolled-up totals on traces.

### Traces UI (T)
- **FR-T1** Traces list for the project, with these columns: time, name, user, session, tags, latency, total tokens, total cost, level, scores, environment, release. Also:
  - a column chooser, with the choice persisted per user
  - sort on time, latency, cost and tokens
  - cursor pagination (page size 50)
- **FR-T2** Search (`q`) does a case-insensitive substring match on name, id, input and output.
  - Filters: name, user, session, tag (any of), level ≥, environment, release, version, `has_error`, `metadata.<key> = <value>`, latency range, cost range, bookmarked.
  - A date range (1h / 24h / 7d / 30d / custom; default 24h).
  - All state lives in the URL query string.
- **FR-T3** Saved views: name the current query string and store it per project (visible to all members, editable by its creator or an Admin). Applying a view restores the URL.
- **FR-T4** An environment switcher in the top bar filters every list, dashboard and detail-adjacent query. The selection persists per user per project.
- **FR-T5** Trace detail:
  - **Header:** name, trace id and observation id with copy buttons, user (link), session (link), tags (editable by Member+), latency, cost, tokens, environment, release, version, bookmark toggle, share toggle.
- **FR-T6** Left pane:
  - **Tree:** nested by parent, each row with a type icon, name, duration, cost and a level colour.
  - **Timeline:** Gantt bars on a shared time axis, with time to first token shown as a marker on generations.
  - Toggle with the `t` key and a segmented control. DEBUG is hidden by default and can be shown with a toggle.
- **FR-T7** Right pane for the selected observation, or the trace itself:
  - input, output and metadata, in three modes: Formatted (pretty JSON, or a chat transcript when the value is a list of `{role, content}` messages), JSON (raw, escaped) and Markdown (sanitised)
  - for generations, also model, parameters, usage, cost (with source) and the status message
- **FR-T8** Errors are highlighted: ERROR rows in red, WARNING in amber. A "Jump to first error" button (key `e`) selects the first ERROR observation.
- **FR-T9** Agent graph tab, shown when the trace has agent, tool or chain observations. Nodes are the distinct steps (type + name), with call count and total duration. Edges are parent→child and sibling-sequence transitions. Clicking a node selects that step's first observation.
- **FR-T10** Bookmark (Member+). Public share on/off (Admin+). The public link `/share/{project_id}/{trace_id}` renders a read-only detail without login, and returns 404 when sharing is off.
- **FR-T11** Comments on traces and observations (Member+), in Markdown, sanitised. `@email` or `@name` of a project member creates a mention, which notifies that member by email (console in dev). Authors can delete their own comments, and Admins can delete any.
- **FR-T12** Delete one trace, or every trace that matches the current filter (with a confirm dialog that shows the count). Deleting removes the trace's observations, scores and comments. Each delete writes an audit-log row (actor, action, project, trace ids or filter, count, time).
- **FR-T13** Members can add a score to a trace or observation from the detail page: name, numeric, boolean or categorical value, and a comment. The score's source is ANNOTATION and its author is the current user. Scores appear in the list's scores column as `name: value` chips.

### Sessions and users (S)
- **FR-S1** Sessions list: session id, first and last trace time, trace count, duration (last end − first start), total cost, distinct users and tags. Sorted, filtered by date range and environment, with cursor pagination.
- **FR-S2** Session page: every trace of the session in time order, as a chat-like replay. Each turn shows the trace's input (user side) and output (assistant side), with cost and latency per turn and a link to the trace. Totals appear at the top.
- **FR-S3** Users list: user id, first and last seen, trace count, total tokens, total cost.
- **FR-S4** User page: a usage-over-time chart (traces, tokens, cost per day), plus that user's traces, sessions and scores (recent 50 each, linking to filtered lists).

### Dashboard (D)
- **FR-D1** The home dashboard for the selected date range and environment shows:
  - trace count
  - total cost
  - cost by model
  - tokens by model
  - latency p50 / p90 / p95 / p99 per trace name and per model
  - error rate by level (share of observations at each level)
  - top 10 users by cost
  - traces over time (bucketed by range)
- **FR-D2** Every dashboard number equals the result of the same aggregation over the raw rows. A test checks this.

### Python SDK (P) — `sdk/python`, distribution `traceloom-sdk`, import `traceloom_sdk`
- **FR-P1** `Traceloom()` reads `TRACELOOM_HOST`, `TRACELOOM_PUBLIC_KEY`, `TRACELOOM_SECRET_KEY`, `TRACELOOM_FLUSH_AT` (default 50), `TRACELOOM_FLUSH_INTERVAL` (default 1 s), `TRACELOOM_SAMPLE_RATE` (default 1.0) and `TRACELOOM_ENABLED`. Constructor arguments override them.
- **FR-P2** A background thread batches events and flushes by size or interval. `flush()` is public and runs `atexit`. It retries 429/5xx and network errors with exponential backoff and jitter (max 3). Sampling is decided per trace (deterministic on the trace id).
  - The SDK never raises into user code. Errors are logged to the `traceloom` logger. The queue is bounded, and when it is full new events are dropped with a warning.
- **FR-P3** `@observe(name=None, as_type="span", capture_input=True, capture_output=True)` works on sync and async functions and on sync and async generators. Generator output is collected when the generator is exhausted. Nesting uses `contextvars`. Exceptions set level ERROR and a status message, then re-raise.
- **FR-P4** Manual API:
  - `start_span(...)`, `start_generation(...)` and `start_observation(as_type=...)`, as context managers or with `.end()`
  - `event(...)`
  - `update_current_trace(user_id, session_id, tags, metadata, name, release, version, environment, public)`
  - `update_current_observation(...)`
  - `score(name, value, trace_id=None, observation_id=None, data_type=None, comment=None)`
  - `get_current_trace_id()`
- **FR-P5** Integrations: each captures the model, parameters, input messages, output, usage and time to first token (on streams).
  - `traceloom_sdk.openai.wrap(client)`: chat completions and responses, sync, async and stream.
  - `traceloom_sdk.anthropic.wrap(client)`: messages, sync, async and stream.
  - `traceloom_sdk.langchain.CallbackHandler`: chains, LLMs, chat models, tools and retrievers mapped to observation types.
  - `traceloom_sdk.litellm.callback`: works as a `litellm.success_callback` / `failure_callback` function, with no hard dependency on litellm.
- **FR-P6** `Traceloom(mask=fn)`: `fn(data)` is applied to every input, output and metadata value before it is queued. If `fn` raises, the value is replaced with `"<masking failed>"`.
- **FR-P7** `examples/agent.py` runs a support agent: the planner LLM chooses tools, a search tool, a calculator tool and a failing `lookup_order` tool, and a final answer. The result is a 3+ level tree with an agent span, tool spans, generations and one tool ERROR. It runs offline against a built-in fake LLM, with realistic model names and usage, or against Anthropic when `ANTHROPIC_API_KEY` is set. `examples/README.md` explains both.

### Platform (X)
- **FR-X1** All settings come from env, documented in `.env.example`. Nothing is hardcoded: ports, `DATABASE_URL`, `TRACELOOM_SECRET_KEY` (startup fails in non-dev if it is unset), mode, Redis, S3, ClickHouse, SMTP, limits and rate limits.
- **FR-X2** `docker-compose.yml` runs web, worker, Postgres, Redis, ClickHouse and MinIO with memory limits totalling under 6 GB. `docker-compose.lite.yml` runs the web server alone.
- **FR-X3** `traceloom` CLI entry points: `traceloom migrate`, `traceloom worker` and `traceloom seed-demo`. `seed-demo` creates a demo user, org, project and key and prints them, for QA.

## Non-functional requirements
- **NFR-1 Performance (lite, tested):** with 10k traces and 50k observations in SQLite, the traces list API and page < 1 s, trace detail < 500 ms and the dashboard < 2 s. Full-mode targets are argued in ARCHITECTURE.md and checked with `scripts/loadtest.py`.
- **NFR-2 Freshness:** an SDK run is visible in the UI ≤ 5 s after `flush()`. The lite in-process queue processes immediately.
- **NFR-3 Durability:** journey 10 is enforced. With the queue consumer stopped, ingestion still returns 207. After restart, every event appears exactly once.
- **NFR-4 Security:**
  - project scoping on every query (`project_id` is the first parameter of every `TraceStore` method)
  - the permission matrix
  - CSRF on all cookie-authenticated unsafe requests
  - rate limits: auth `TRACELOOM_RATE_AUTH` (default `10/minute` per IP+email), ingestion `TRACELOOM_RATE_INGEST` (default `600/minute` per key)
  - hashed secrets
  - autoescape and sanitised Markdown
  - CSP `default-src 'self'; script-src 'self'; object-src 'none'; frame-ancestors 'none'`
  - security headers
  - secrets and passwords never logged
- **NFR-5 UX:** dense developer-tool layout, light and dark themes (system default plus a toggle), keyboard shortcuts (`/` search, `j`/`k` move in lists and trees, `enter` open, `t` tree/timeline, `e` first error, `?` help), usable at 1280px and readable at 390px. WCAG AA contrast, visible focus rings.
- **NFR-6 Tests:**
  - API tests for every FR group
  - at least one Playwright browser test per feature journey (sign-up → key, trace list → detail, session replay, dashboard, Viewer read-only, XSS)
  - a contract test suite for the store ports
  - `pytest -q` green in lite mode, with no network access needed

## Design

### Components and changes (greenfield)
```
traceloom/
  main.py            app factory, middleware (sessions, CSRF, CSP, rate limit), routers
  config.py          pydantic-settings Settings (all env)
  container.py       builds adapters from TRACELOOM_MODE
  ports.py           TraceStore, BlobStore, Queue, RateLimiter, Cache protocols + query/filter dataclasses
  adapters/lite/     sql_trace_store.py, local_blob.py, inproc_queue.py, memory_limiter.py
  adapters/full/     clickhouse_trace_store.py, s3_blob.py, redis_queue.py (dramatiq), redis_limiter.py
  db/                SQLAlchemy models (meta + lite trace tables), session, alembic/
  auth/              passwords, sessions, csrf, api keys, permissions matrix, deps
  ingest/            schemas (pydantic event models), accept.py (request path), process.py (merge + cost), otel.py, sweeper.py
  cost/              model registry, pricing, tokenizers
  services/          orgs, invites, mail, comments, audit, saved views
  web/               routers per area (auth, orgs, settings, traces, sessions, users, dashboard, share) + templates/ + static/
  api/               public API routers (ingestion, otel, health) + internal JSON for HTMX
  cli.py             migrate / worker / seed-demo
  seed/models.yaml
sdk/python/          pyproject.toml, traceloom_sdk/{client,observe,context,openai,anthropic,langchain,litellm}.py, tests/
examples/            agent.py, fake_llm.py, README.md
tests/               api/, contract/, e2e/ (playwright), perf/, conftest.py
docker-compose.yml, docker-compose.lite.yml, Dockerfile, .env.example, scripts/loadtest.py
```

### Data and interfaces
**Meta DB (SQLAlchemy):**
- `users`(id, email unique, name, password_hash, session_version, created_at)
- `organizations`(id, name, created_at)
- `memberships`(user_id, org_id, role)
- `invites`(id, org_id, email, role, token_hash, expires_at, accepted_at)
- `projects`(id, org_id, name)
- `api_keys`(id, project_id, public_key unique, secret_hash unique, note, created_at, last_used_at, revoked_at)
- `password_resets`(token_hash, user_id, expires_at, used_at)
- `model_definitions`(id, project_id null=global, model_name, match_pattern, tokenizer, prices JSON, tiers JSON, valid_from, source)
- `saved_views`(id, project_id, owner_id, name, query)
- `user_prefs`(user_id, project_id, key, value)
- `comments`(id, project_id, object_type, object_id, trace_id, author_id, body, created_at)
- `audit_log`(id, org_id, project_id, actor_type, actor_id, action, target, detail JSON, created_at)

**Trace data (TraceStore; lite SQL tables / ClickHouse tables, same columns):**
- `traces`(project_id, id, name, timestamp, input, output, user_id, session_id, tags, metadata, release, version, environment, public, bookmarked, event_ts, latency_ms, total_tokens, total_cost, max_level, observation_count, search_text)
- `observations`(project_id, id, trace_id, parent_observation_id, type, name, start_time, end_time, completion_start_time, input, output, metadata, level, status_message, version, environment, model, model_parameters, usage_details, cost_details, cost_source, usage_source, prompt_name, prompt_version, event_ts)
- `scores`(project_id, id, trace_id, observation_id, session_id, dataset_run_id, name, value, string_value, data_type, comment, source, config_id, author_id, timestamp, environment, event_ts)
- Lite indexes:
  - traces: `(project_id, timestamp DESC, id)`, `(project_id, session_id)`, `(project_id, user_id)`, `(project_id, environment, timestamp)`
  - observations: `(project_id, trace_id)`, `(project_id, start_time)`
  - scores: `(project_id, trace_id)`

**TraceStore port (excerpt):**
- `upsert(project_id, traces, observations, scores)`
- `get_existing(project_id, ids_by_kind)`
- `refresh_trace_aggregates(project_id, trace_ids)`
- `list_traces(project_id, TraceQuery) -> Page[TraceRow]`
- `get_trace(project_id, id) -> TraceDetail`
- `update_trace_flags(project_id, id, **fields)`
- `delete_traces(project_id, ids | TraceQuery) -> int`
- `list_sessions` / `get_session`
- `list_users` / `get_user_summary`
- `dashboard(project_id, DashboardQuery) -> DashboardData`

**Public API:** as in FR-I1/I4/I5. Error envelope: `{"error":{"code","message"}}`. Cursors are opaque base64 of `(sort_value, id)`.

### Permissions
| Action | Viewer | Member | Admin | Owner |
|---|---|---|---|---|
| Read traces, sessions, users, dashboard, comments | ✓ | ✓ | ✓ | ✓ |
| Add score / comment, bookmark, edit tags, saved views (own) | | ✓ | ✓ | ✓ |
| Toggle public share, delete traces | | | ✓ | ✓ |
| Project settings, API keys, model definitions, invites, roles, create project | | | ✓ | ✓ |
| Grant Owner, delete org | | | | ✓ |

### UX
Screens (detailed in the design step's `design/mockup.html` and `DESIGN.md`):
- **Auth:** sign-up, log-in, forgot and reset password, accept invite.
- **Shell:** a left nav (Dashboard, Traces, Sessions, Users, Settings), a top bar (org/project picker, environment switcher, date range, theme toggle, user menu).
- **Onboarding:** an empty project shows "Create an API key" plus an SDK snippet.
- **Traces list:** a filter bar, search, the column chooser, saved views, a dense table, row hover/selection with `j`/`k`, and a "Load more" cursor.
- **Trace detail:** a split pane (resizable at ≥1280px, stacked at 390px), with Tree / Timeline / Graph tabs on the left and an IO viewer with mode tabs on the right, plus comments and scores below.
- **Sessions list + replay, users list + user page, dashboard grid, settings** (General, Members & invites, API keys, Models).
- **States:** empty, loading (HTMX indicator), error (inline banner), 403 page, 404 page.

## Acceptance criteria
- **AC-1 (FR-A1, A3, A6, P7; journey 1):** Given a new visitor, when they sign up, create an org, a project and a key, and run `examples/agent.py` with those keys, then within 5 s the trace appears in the list. Its detail shows a ≥3-level tree, a timeline with a time-to-first-token marker, non-zero calculated cost and tokens, and the failing tool row highlighted, with "Jump to first error" selecting it.
- **AC-2 (FR-I3; journey 2):** Given a batch, when it is sent twice, then each entity exists once. Given a `generation-update` sent before its `generation-create`, then the final row has both the update's output/end_time and the create's model/input.
- **AC-3 (FR-T2; journey 3):** Given traces with distinct outputs, when searching a word that appears only in one output, then only that trace is listed. When adding `metadata.region = eu` plus a date range, then only matching traces in range are listed.
- **AC-4 (NFR-4; journey 4):** Given an input containing `<script>alert(1)</script>`, then the list, the detail (all three view modes), the session replay and the public share page show it as literal text, and no dialog fires in the browser test.
- **AC-5 (FR-A5; journey 5):** Given a Viewer, when they call each write endpoint, then each returns 403, and the detail page renders no edit, delete, share, tag or score controls. Given keys of project A, when reading or ingesting into project B, then the result is 401/403 and B's data is unchanged.
- **AC-6 (FR-S2; journey 6):** Given a session of 5 traces ingested out of order, then the replay lists them in timestamp order and the total cost equals the sum of their costs.
- **AC-7 (NFR-1; journey 9):** Given 10k traces and 50k observations seeded in lite mode, then the first list page and a filtered next page each return in < 1 s, detail in < 500 ms and the dashboard in < 2 s.
- **AC-8 (NFR-3; journey 10):** Given the queue consumer stopped, when 3 batches are ingested, then all return 207 and no trace rows exist yet. When the consumer and sweeper restart, then every event appears exactly once.
- **AC-9 (FR-C1–C4):** Given a generation with `cost_details`, then its cost is the provided cost with source "provided". Given only usage and a seeded model, then cost = usage × price with source "calculated". Given a project override for that model, then the override price is used.
- **AC-10 (FR-I4):** Given an OTLP JSON payload and the same payload in protobuf, with `gen_ai.*` attributes, then each produces a generation with model, usage and parent links. A span with ERROR status has level ERROR.
- **AC-11 (FR-D1, D2):** Given known seeded data, then each dashboard figure equals a direct computation over the raw rows.
- **AC-12 (FR-P2–P6):** Given an unreachable host, the SDK never raises, and `flush()` returns within its timeout. Given `@observe` on sync, async, generator and async-generator functions, the nesting is correct. The mask function is applied. The OpenAI, Anthropic, LangChain and LiteLLM integrations, tested against fakes, produce generations with usage and time to first token.
- **AC-13 (FR-A2, A4):** A password reset logs out old sessions. An invite joins with the invited role. The last Owner cannot be demoted.
- **AC-14 (FR-T10–T12):** The share toggle makes `/share/...` reachable without login, and turning it off returns 404. An @mention sends a mail (captured in tests). Deleting by filter removes exactly the matched traces and writes an audit row.
- **AC-15 (journey 12):** The whole default pytest suite runs in lite mode with no ClickHouse, S3 or Redis.

## Open questions carried from intent.md
- Lite on SQLite is the default for tests and dev → **answered** (FR-X1, AC-15).
- Volume targets: lite is tested at 10k/50k (AC-7). Full mode is argued and load-scripted (ARCHITECTURE.md) → **answered**.
- Tokenizer: `tiktoken` for OpenAI-family models, loaded lazily. If its encoding can't be loaded (offline), the usage stays empty → **answered** (FR-C4).
- SDK packaging: installable `sdk/python/pyproject.toml`, with no PyPI publish → **answered**.

## Flagged concerns
- [x] CONCERN — policy: prompt.md "lite mode … traces in partitioned tables" — owner: Kunal — Postgres declarative partitioning of the trace tables under SQLAlchemy/Alembic adds real migration complexity, and SQLite (the dev default) has no partitions. Options: (a) Phase 1 uses plain tables with composite `(project_id, time)` indexes on Postgres, and partitioning lands with retention in Phase 4. (b) Build monthly partitions on Postgres now with raw DDL in the migration. — decided by Kunal Mugalkhod: (a) plain tables + composite indexes now; Postgres partitioning lands with retention in Phase 4.
- [x] CONCERN — policy: prompt.md "pytest must pass" + Tests non-negotiable — owner: Kunal — the full-stack adapters (ClickHouse, S3, Redis/Dramatiq) can't run inside a plain `pytest` without Docker. Options: (a) the contract tests for the full adapters run only with `TRACELOOM_IT=1` against `docker compose up`, and default pytest covers lite plus the adapter unit tests with fakes. (b) Default pytest starts the containers (testcontainers), which makes verify need Docker and run for minutes. — decided by Kunal Mugalkhod: (a) full-adapter contract tests are opt-in via `TRACELOOM_IT=1` against docker compose; default pytest = lite + adapter unit tests with fakes.
- [x] CONCERN — policy: security (Non-negotiables) — owner: Kunal — open self-sign-up lets anyone who can reach the instance create an account and orgs. Options: (a) `TRACELOOM_SIGNUP_ENABLED` defaults to true, and when false only invited emails can sign up. (b) Default false, except that the very first user can always sign up. — decided by Kunal Mugalkhod: (a) `TRACELOOM_SIGNUP_ENABLED` defaults to true; when false, only invited emails can sign up.
- [x] CONCERN — policy: security/PII + role definitions — owner: Kunal — a public share link exposes trace inputs and outputs (possibly PII) without auth, and the brief doesn't say which role may toggle it or delete traces. Options: (a) Admin+ for share and delete, Member+ for bookmark, tags, scores and comments. (b) Member+ for share as well; delete stays Admin+. — decided by Kunal Mugalkhod: (a) Admin+ toggles public share and deletes traces; Member+ bookmarks, edits tags, adds scores and comments.
- [~] ASSUMED: the ingestion envelope uses `{batch:[{id,type,timestamp,body}]}`, the de-facto shape of LLM tracing SDKs. It is our own schema and docs, with no copied code or text — why: it keeps migration easy for users.
- [~] ASSUMED: Playwright reuses the locally cached Chromium (or the system Chrome channel). The e2e fixture runs `playwright install chromium` once if neither is present — why: the verify setup step can't install browsers.
- [~] ASSUMED: "full-text search" in Phase 1 is a case-insensitive substring search (`LIKE` in lite, ngram bloom-filter skip indexes in ClickHouse), not ranked FTS — why: it meets journey 3 and the latency targets.
- [~] ASSUMED: the agent graph is laid out server-side as SVG with a simple layered layout, with no graph library — why: CSP `script-src 'self'` and a small footprint.
- [~] ASSUMED: dashboard charts use vendored uPlot (MIT, ~45 KB) — why: "a small chart library", and it works offline.

## Risks
- **Scope size.** This is the largest single change, with 11 feature areas, and is mitigated by team mode and a story order that delivers the ingestion → detail spine first.
- **Merge correctness** across out-of-order events, and the ClickHouse `FINAL` semantics. The contract tests share cases between both adapters.
- **OTLP protobuf parsing** needs `opentelemetry-proto`; there is version-skew risk, so the dependency is pinned.
- **The SDK must never break the host app.** This is covered by fault-injection tests (unreachable host, 500s, slow server, a mask that raises).
- **Lite performance** of SQLite substring search at 50k observations. The trace-level `search_text` column keeps search on the traces table.
