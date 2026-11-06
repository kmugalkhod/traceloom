# Plan: Traceloom Phase 2 — prompt management and playground

From: spec.md (d8fdf0f) · Status: accepted · Accepted by: Kunal Mugalkhod

<!-- Status: draft -> accepted (engineer; tech lead for higher-risk) -> implemented.
     Keep this file in sync with the code: a deviation updates plan.md in the same commit. -->

This builds on Phase 1 (merged on main). FR/NFR/AC ids refer to spec.md. Conventions are in CLAUDE.md. New ADRs: 0010–0012.

## Files that change
**Docs**
- `docs/adr/0010-prompt-storage-and-resolution.md`: new. It covers immutable versions, labels as pointers, `latest` computed (never stored), composition resolved at read time with the dependency versions recorded, and query-time metrics.
- `docs/adr/0011-llm-connections-and-providers.md`: new. It covers one streaming adapter interface, official SDKs lazily imported, Fernet/MultiFernet secrets, and the SSRF policy (private addresses allowed for LLM URLs by default, never metadata).
- `docs/adr/0012-webhook-delivery.md`: new. It covers queued, at-least-once delivery, HMAC-signed bodies, retry with backoff, and the delivery log.
- `.env.example`: modified. New settings (FR-X1) plus `TRACELOOM_DEMO_EMAIL` / `TRACELOOM_DEMO_PASSWORD` (Phase 1 leftover). *A human edits this file, because the path is blocked for agents.* The plan step lists the exact lines to add.
- `README.md`: modified. Adds a prompts and playground quickstart and the SDK `get_prompt` snippet.
- `DESIGN.md`: modified. New patterns only, from D-1.

**Server `traceloom/`**
- `config.py`: modified. Adds `encryption_key` (`TRACELOOM_ENCRYPTION_KEY`; comma-separated keys for rotation; required outside dev), `webhook_allow_private` (default false), `llm_allow_private` (default **true**, per the decision), `playground_rate` (`30/minute`) and `provider_timeout_seconds` (120).
- `db/models.py` and `db/migrations/versions/0002_prompts_playground.py`: new tables (spec *Data*), `api_keys.may_move_protected_labels`, the `observations(project_id, prompt_name, prompt_version)` index, and a `production` protected label seeded for existing projects. `services/orgs.py` seeds it on project create.
- `auth/permissions.py`: modified. New `Perm`s: `PROMPTS_READ` (Viewer), `PROMPTS_WRITE` (Member), `PROMPT_LABELS_PROTECTED` (Admin), `PROMPTS_DELETE` (Admin), `PLAYGROUND_RUN` (Member), `LLM_CONNECTIONS` (Admin), `WEBHOOKS` (Admin).
- `prompts/` (new package):
  - `variables.py`: find and compile `{{var}}`.
  - `composition.py`: parse and resolve `@@@prompt:…@@@`, with a depth of 5 and cycle detection.
  - `diff.py`: line diff and JSON key diff, both as escaped structures.
  - `service.py`: `create_version`, `move_label`, `get` (label/version/latest), `list`, `delete`, `set_protected`. Every write audits and emits a webhook event.
- `llm/` (new package):
  - `crypto.py`: MultiFernet encrypt/decrypt and `hint()`.
  - `ssrf.py`: resolve, classify and pin the IP; shared with webhooks.
  - `connections.py`: CRUD, masked views and test-connection.
  - `providers/base.py`: the `Delta` types, the `Provider` protocol and the registry.
  - `providers/anthropic.py`, `openai.py` (also used by `azure_openai`/`openai_compatible` with different client construction), `bedrock.py` (boto3 `converse_stream`), `google.py` (`google-genai`).
  - `models.py`: the default model lists per provider.
  - `run.py`: renders variables, calls the adapter, accumulates usage, costs it with the Phase 1 `ModelRegistry` + `compute_cost`, and optionally ingests a trace (FR-L8).
- `webhooks/` (new package): `service.py` (configure, enqueue event, sign, test) and `deliver.py` (worker handler with retry/backoff; updates `webhook_deliveries`).
- `ingest/process.py` and `adapters/lite/inproc_queue.py`, `adapters/full/redis_queue.py`: modified. Queue messages get a `type` discriminator (`ingest` default, `webhook_delivery`), and dispatch goes through a small registry. Retries come from the message's own `next_attempt_at`, and the lite sweeper replays due webhook deliveries from the DB.
- `ports.py`, `adapters/lite/sql_analytics.py`, `adapters/full/clickhouse_trace_store.py`: add `prompt_metrics` + `list_prompt_generations`.
- `api/prompts_v2.py`: new. The public prompt API (FR-P9).
- `web/routers/`, all new:
  - `prompts.py`: list, new, folder/tag/search.
  - `prompt_detail.py`: versions, prompt, diff, metrics, labels.
  - `playground.py`: page, `POST /run` streaming `text/event-stream`, `/save`, `/from-observation/{obs_id}`.
  - `settings_connections.py` and `settings_webhooks.py`.
  - `trace_detail.py`: modified, adds the "Open in playground" link.
- `web/context.py`: modified. Adds the Prompts and Playground nav items and the LLM connections and Webhooks settings tabs.
- `web/templates/`: `prompts/*`, `playground/*`, `settings/connections.html`, `settings/webhooks.html`, plus the trace IO pane header button.
- `web/static/prompts.js`: new. Variable highlighting and the label move UI.
- `web/static/playground.js`: new. Editor state, a fetch + ReadableStream SSE reader, Stop through `AbortController`, compare columns and `⌘↵`.
- `web/static/app.css`: new components from D-1.
- `seed/models.yaml`: Claude entries per FR-X3.

**SDK `sdk/python/traceloom_sdk/`**
- `prompts.py`: new. `TextPrompt`, `ChatPrompt`, `PromptCache` (TTL, stale-while-revalidate with one daemon refresher per key, bounded), `compile` and `PromptNotFound`.
- `client.py`: modified. `get_prompt` / `aget_prompt`, honouring `TRACELOOM_PROMPT_CACHE_TTL`.
- `objects.py`, `observe.py`, `openai.py`, `anthropic.py`: modified. A `prompt=` / `traceloom_prompt=` argument for linking; fallback prompts are not linked.
- `sdk/python/tests/test_prompts.py` and `test_prompt_linking.py`: new.

**Examples**
- `examples/prompt_app.py`: new. It fetches `support` from `production`, compiles it, and runs it through the fake LLM with linking. Used by AC-1..3 e2e.

**Tests `tests/`**
- `api/`:
  - `test_prompts_service.py`
  - `test_prompts_api_v2.py`
  - `test_prompt_labels.py`
  - `test_prompt_composition.py`
  - `test_prompt_diff.py`
  - `test_prompt_metrics.py`
  - `test_webhooks.py`
  - `test_llm_connections.py`
  - `test_crypto.py`
  - `test_ssrf.py`
  - `test_playground_run.py`
  - `test_playground_from_observation.py`
  - `test_phase2_permissions.py`
  - `test_phase2_escape.py`
- `unit/providers/test_{anthropic,openai,azure_openai,openai_compatible,bedrock,google}.py`: fake transports (httpx2/httpx MockTransport for the Anthropic/OpenAI/Google SDKs, botocore `Stubber` for Bedrock).
- `contract/test_prompt_metrics_contract.py`: lite always; ClickHouse under `TRACELOOM_IT=1`.
- `e2e/`:
  - `test_journey_prompt_label_switch.py` (AC-1, 2, 3)
  - `test_journey_playground_from_trace.py` (AC-4, AC-9 partial)
  - `test_journey_playground_compare.py` (AC-10)
  - `test_journey_prompt_xss.py` (AC-9)
- `perf/test_prompt_volume.py`: NFR-1.
- `conftest.py`: modified. A fake-provider fixture registered in the provider registry, a `webhook_receiver` fixture (local in-process ASGI app, with the SSRF allow-private override scoped to the test), and an `encryption_key` setting.

## Dependencies
In `pyproject.toml` `[project.dependencies]`:
- `cryptography>=42,<51`: Fernet/MultiFernet. It's already installed transitively; this pins it explicitly.
- `anthropic>=2.0,<3`: moves from the dev deps to runtime (installed 2.6.0, which uses `httpx2`). It's the Anthropic adapter, lazily imported.
- `openai>=3.0,<4`: moves from the dev deps to runtime (installed 3.46.0). It's the OpenAI, Azure and compatible adapters.
- `google-genai>=1.0,<2`: new. The Google adapter, lazily imported.
- `boto3`: already a runtime dependency. The Bedrock adapter uses `bedrock-runtime` `converse_stream`.

SDK `sdk/python/pyproject.toml`: no new dependency. The cache uses the standard library only.

No new vendored JS. The playground uses `fetch` streaming, and uPlot is reused for the metrics sparkline if the design calls for one.

## Order of work
Steps 2–9 depend on step 1. Within one number, steps are independent. UI tasks wait for D-1.

1. **Foundation** (blocks all):
   - config fields
   - migration 0002 + models
   - new perms + matrix test
   - `llm/crypto.py` + `llm/ssrf.py` with tests
   - the queue `type` dispatch registry, with the existing ingestion behaviour unchanged (Phase 1 suite green)
   - ADRs 0010–0012
   - nav and settings-tab stubs
2. **Design D-1** (designer agent): prompts list, prompt detail (four tabs), the editors, playground (single + compare, every state), LLM connections, webhooks, and the "Open in playground" button. Light and dark, at 1280 and 390. The UI tasks in steps 4, 6, 7 and 9 wait for approval.
3. **Prompt core** (FR-P1–P6, P9):
   - `prompts/` package
   - public API v2
   - audit + webhook *events* (enqueue only)
   - Covers AC-5, AC-8 and the API side of AC-1.
4. **Prompt UI** (FR-P1–P7): list, detail, editors, diff, label move with protected-label locks.
5. **SDK prompts** (FR-S1–S4): cache, compile, linking, and the provider wrapper kwarg. Covers AC-1, AC-2 and AC-3 at the SDK level. It's independent of the server code (HTTP only), so it can start right after step 1.
6. **Prompt metrics** (FR-P8): store queries (lite + ClickHouse) and the metrics tab. Covers AC-3 (metrics) and NFR-1.
7. **Webhooks** (FR-P10): settings page, worker delivery, signing, retries, delivery log, SSRF. Covers AC-6.
8. **Providers + run engine** (FR-L1, L2, L4 back end): connections service, six adapters with fake-transport tests, `run.py` with cost, the streaming endpoint and the rate limit. Covers AC-7 and AC-11. The adapters are parallelisable once `providers/base.py` lands.
9. **Playground UI** (FR-L3–L8): editor, streaming client, compare, save-as-version, open-from-trace, "trace this run". Covers AC-4 and AC-10.
10. **Seed + examples + docs**: Claude 5-family seed entries, `examples/prompt_app.py`, README sections, and the `.env.example` lines handed to the human.
11. **Acceptance**:
    - e2e journeys (AC-1–4, 9, 10)
    - the Phase 2 permission sweep (Viewer 403 on every new write)
    - XSS in all new views
    - the perf test (NFR-1)
    - the CLAUDE.md Architecture paragraph updated for `prompts/`, `llm/` and `webhooks/` (proposed to the owner)

## Tests that prove it
| AC | Tests |
|---|---|
| AC-1 | `api/test_prompts_api_v2.py::test_get_production_by_default`, `sdk/python/tests/test_prompts.py::test_get_and_compile_chat`, `e2e/test_journey_prompt_label_switch.py` (runs `examples/prompt_app.py` as a subprocess against the live server) |
| AC-2 | `sdk/.../test_prompts.py::test_stale_while_revalidate` (TTL 1 s, server returns v2 then v3: the first call after TTL returns v2 immediately and the next returns v3); the e2e journey moves the label in the browser as Admin and re-runs the app |
| AC-3 | `sdk/.../test_prompt_linking.py::test_generation_links_prompt`, `::test_fallback_not_linked`; `api/test_prompt_metrics.py::test_metrics_count_linked_generations`; the e2e journey asserts the metrics tab shows 1 |
| AC-4 | `api/test_playground_from_observation.py::test_prefill_matches_generation`; `api/test_playground_run.py::test_anthropic_request_matches_and_streams` (the fake transport captures the request body); `e2e/test_journey_playground_from_trace.py` |
| AC-5 | `api/test_prompt_labels.py::test_member_cannot_move_production` (UI + API, no audit row, no webhook), `::test_member_moves_staging_audited`, `::test_key_needs_protected_flag` |
| AC-6 | `api/test_webhooks.py::test_retry_then_success_signed` (the receiver fixture returns 500, 500, 200), `::test_signature_verifies`, `::test_private_url_refused`, `::test_sweeper_redelivers_after_restart`; `api/test_ssrf.py` (metadata always blocked; private allowed for LLM, blocked for webhooks by default) |
| AC-7 | `api/test_llm_connections.py::test_secret_never_plaintext` (DB row, every page and API response, captured logs during a failing test-connection), `::test_masked_hint`; `api/test_crypto.py::test_rotation`; `::test_startup_requires_key_outside_dev` |
| AC-8 | `api/test_prompt_composition.py::test_cycle_400_names_it`, `::test_depth_limit`, `::test_dependencies_recorded` |
| AC-9 | `api/test_phase2_escape.py` (list, detail, diff, playground pre-fill, provider response); `e2e/test_journey_prompt_xss.py` (a dialog handler fails the test) |
| AC-10 | `api/test_playground_run.py::test_concurrent_variants_independent_usage`; `e2e/test_journey_playground_compare.py` |
| AC-11 | `unit/providers/test_*.py`: one per provider, covering request mapping, streaming deltas, usage, refusal/error scrubbing |
| AC-12 | The whole default suite with the Phase 1 socket guard; no provider keys present |

Also:
- `api/test_phase2_permissions.py` sweeps every new unsafe route as a Viewer (403) and as a Member on the Admin routes (403).
- `perf/test_prompt_volume.py` covers NFR-1.

## Risks
- **Riskiest step: 8 (providers + streaming run engine).** It has six SDKs with different streaming shapes, and Anthropic SDK 2.x uses `httpx2`. Mitigation:
  - one `Delta` interface;
  - each adapter tested against a fake transport built for its SDK's HTTP layer (the Phase 1 `Script` approach already handles `httpx2`);
  - Bedrock via botocore `Stubber`;
  - an optional live smoke test only under `TRACELOOM_LIVE_LLM=1`.
- **Streaming through FastAPI + CSP**:
  - The run endpoint is a plain `def` generator returned via `StreamingResponse`, so the provider SDKs' blocking iterators run in the thread pool and never block the event loop (the Phase 1 review lesson).
  - Client disconnect closes the provider stream.
  - Same-origin `fetch` streaming needs no CSP change.
- **Queue dispatch change touches ingestion.** A missing `type` defaults to `ingest`, and the Phase 1 durability test (AC-8) must stay green before anything else lands.
- **Secrets.**
  - A wrong or rotated `TRACELOOM_ENCRYPTION_KEY` makes stored keys unreadable. Connections then show "key needs re-entry" instead of a 500. MultiFernet handles rotation.
  - Dev's ephemeral key has the same caveat Phase 1 had with the server secret: restarts lose saved provider keys. A startup warning says so.
- **SSRF.** Resolve then check, then connect to the pinned IP. Applied to webhooks at save and send, and to LLM base URLs. Metadata and link-local are always blocked.
- **Migration 0002** adds an index on the observations table, which can be slow on big Postgres tables. The plan uses `CREATE INDEX CONCURRENTLY` on Postgres (autocommit block) and a plain index on SQLite.
- **SDK in host apps.** The refresher threads are daemon, bounded (one per key) and never raise. The cache is per client instance.
- **Merge conflicts in a parallel team** on `db/models.py`, `web/context.py`, `conftest.py` and `ports.py`. Step 1 creates every table, perm, nav item and fixture stub up front. The Phase 1 lesson applies too: developers merge the team branch in right before submitting, and keep one copy of each plan.md deviation.

## Alternatives considered
- **Mutable prompt versions with soft history**: rejected. The spec requires immutability, and labels give the "change what production gets" behaviour.
- **Materialised prompt metrics** (counters updated at ingest): rejected for now. Query-time with an index meets NFR-1 and avoids double bookkeeping (ADR-0010).
- **Raw httpx calls instead of provider SDKs**: rejected. Auth (SigV4, Azure, Google) and streaming formats are what the SDKs exist for, and the claude-api skill requires the official SDK for Claude.
- **A WebSocket for the playground**: rejected. One-way streaming fits SSE over `fetch`, works through proxies, and needs no CSP or HTMX extension.
- **A separate webhook worker process**: rejected. It reuses the existing queue and worker (ADR-0003), with deliveries persisted for replay.
- **A LiteLLM dependency to cover all providers**: rejected. It's a heavy transitive dependency, and its own pricing and abstractions overlap ours. We support it as an OpenAI-compatible endpoint instead.

## Verification target
- `python3 .sdlc/bin/sdlc.py verify` is green: setup, compileall, `pytest -q` (all API, unit, contract-lite, perf, e2e and SDK tests) and `ruff check .`.
- With the server running: `traceloom seed-demo`, create the `support` prompt with `production` on v2, then `python examples/prompt_app.py` prints v2's compiled text and the generation shows `prompt: support v2` in the trace detail. Moving `production` to v3 changes the next run's output within the TTL.
- `GET /api/public/v2/prompts/support` returns `200` with `{name, type, version, prompt, config, labels, tags, dependencies}`.
- The playground streams from a fake provider in the browser, with tokens, cost and latency shown. "Open in playground" on the demo agent's generation pre-fills its messages.
- The UI matches `design/mockup.html` at 1280 and 390, light and dark (QA compares).
- `.env.example` lines for the new settings are handed to the owner.

## Deviations
<!-- Appended during build: date — what changed vs the plan — why. -->
- 2026-11-06 — The `production` protected label is seeded for new projects by a SQLAlchemy `after_insert` mapper event on `Project` (in `db/models.py`), not by explicit code in `services/orgs.py`; `orgs.py` is unchanged. Why: projects are also created by `services/demo.py` and test factories, and one hook covers every path.
- 2026-11-06 — `tests/conftest.py` sets a throwaway `TRACELOOM_ENCRYPTION_KEY` for the whole suite (`os.environ.setdefault`). Why: `Settings` now requires a key outside dev, and many tests build `Settings(TRACELOOM_ENV="test")`.
- 2026-11-06 — Added `adapters/full/clickhouse_migrations.sql` (idempotent `ALTER TABLE observations ADD INDEX IF NOT EXISTS idx_prompt_name`), run by `apply_migrations` after `apply_schema`. Why: `CREATE TABLE IF NOT EXISTS` does not add the prompt_name skip index to existing ClickHouse installs.
