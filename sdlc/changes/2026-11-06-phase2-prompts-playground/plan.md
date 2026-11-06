# Plan: Traceloom Phase 2 — prompt management and playground

From: spec.md (d8fdf0f) · Status: implemented · Accepted by: Kunal Mugalkhod

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
- 2026-11-06 — Queue dispatch lives in a new `traceloom/queue_registry.py` (`HandlerRegistry`, type constants) used by both queues, and `Sweeper` takes an optional `due_deliveries` callable instead of reading `webhook_deliveries` itself. Why: importing `ingest/process.py` from the adapters would create an import cycle with the container; the webhooks task wires the DB query.
- 2026-11-06 — Provider adapters take an injectable transport (`http_client=` / `client=` / `resolver=`) for fake-transport tests, `llm/providers/base.py` owns secret scrubbing, and `openai_compatible` runs the SSRF check at construction and before each stream without pinning the IP into the SDK connection. `google-genai` is a runtime dependency. Shared test helpers live in `tests/unit/providers/conftest.py` and `openai_helpers.py`.
- 2026-11-06 — Webhook retries are driven by the sweeper, not by delayed queue messages: a failed attempt keeps the row `pending` with a future `next_attempt_at`, and `Sweeper` (wired in both modes through `register_webhook_handler` in `container.py`) re-enqueues due rows; the handler claims a row with a short lease so duplicate messages are skipped. Backoff is therefore at least the sweep interval (`TRACELOOM_INGEST_SWEEP_SECONDS`). `Container` gains `webhook_transport` / `webhook_resolver` test seams.
- 2026-11-06 — The playground API lives in `web/routers/playground_api.py` (the page router `playground.py` stays with the UI task), and pre-fill normalisation is a new `llm/prefill.py`. `GET /from-observation/{obs_id}` takes a required `?trace_id=` because `TraceStore` has no observation-by-id lookup. JSON clients must send `Accept: application/json` to get the error envelope on these `/project/...` paths. The stream is an async generator that pulls each provider chunk with `run_in_threadpool`, checks `request.is_disconnected()` between chunks and cancels the run in `finally` (newer servers report a disconnect only as a failed write, after which Starlette skips background tasks); each adapter closes its SDK stream (and the client it built) in `finally`, because dropping the HTTP connection is how the provider learns the client left; a provider read already blocked in its worker thread ends at its next chunk.
- 2026-11-06 — Include cycles are refused before the write: the prompt service resolves the include graph as it will be after the save or label move (a pending-version overlay on the resolver, root = the written version), so a loop through `latest`, a label or itself fails with a 400 naming the path. Why: QA found saves and label moves that closed a loop were accepted. A flush-then-rollback savepoint was avoided because pysqlite commits when a savepoint is released before any write has begun the transaction.

- 2026-11-06 — Design refresh applied app-wide, requested by Kunal: the v3.1 declutter rules (14px/1.5 body, 24px titles, 48px rows, 36px controls, 24px section gaps) are in `app.css` on `:root` and unscoped, so every Phase 1 page changes. Markup changed only where nothing could be lost: trace detail gets a second details row (Environment, Release, Version); the dashboard Breakdown tab panel and the "..." menus on Phase 1 pages were completed in the review fixes (see below).
- 2026-11-06 — `/prompts/new` is shared by the new-prompt and new-version editors (`?from=<name>&version=<n>` starts a version), and prompt-name routes use a `{name:promptname}` path convertor (`web/prompt_paths.py`) that never matches the literal `new`, so router include order cannot shadow it.
- 2026-11-06 — Compare and Save-as-prompt are self-contained fragments the T-25 page mounts: `GET /playground/compare-panel` and `GET /playground/save-dialog` return HTML that `playground_compare.js` / `playground_save.js` insert on first use, and both read the shared editor through T-25's `window.Playground` API (`getState`, `setState`, `runStream`, `setMode`) and the `pg:action` / `pg:state` events (an earlier draft used a `traceloomPlayground.snapshot()` hook, superseded). `POST /playground/save` gained `mode` (`auto` | `new` | `version`) and returns the new version's `url`. Trace detail shows `prompt: <name> v<n>` and "Open in playground" (`?obs=&trace_id=`) on generations, Member+ only.
- 2026-11-06 — A Load prompt dialog was added (`GET /playground/load-dialog`, `routers/playground_load.py`, `playground/_load.html`, handled in `playground_save.js`): the playground menu's "Load prompt" item had no owner. It lists the project's chat prompts with their latest version, then navigates to `?prompt=<name>&version=<n>`, which T-25 loads.
- 2026-11-06 — Which model parameters the playground offers comes from one rule set, `llm/models.PARAM_RULES` / `supported_params(provider, model)`, handed to the page as `config.paramRules` and evaluated in `playground.js`. Claude 5-family ids (listed, unlisted, dated, `-latest`) and OpenAI reasoning models (o-series, gpt-5) take only `max_tokens`. Why: Claude 5-family models reject sampling parameters, so the adapter drops them. Consequence: AC-4 was amended (spec.md, decided by Kunal Mugalkhod).
- 2026-11-06 — Shared Phase 1 behaviour changed (beyond the CSS refresh): `app.js` places every popup menu with `position: fixed` (right-aligned to its button, flipped above when needed, clamped to an 8px margin, closed on scroll or resize), and menus are keyboard-navigable (focus on the first item, ArrowUp/Down, Escape returns focus). The sidebar gains a "Build" group (Prompts, Playground); on phones the bottom bar is Traces, Prompts, Playground, Dashboard, More, with a More sheet holding Sessions, Users and Settings (`BOTTOM_NAV` / `MORE_NAV` in `web/context.py`, `base.html`). New e2e tests: `tests/e2e/test_menus_viewport.py`, `tests/e2e/test_prompt_menu_keyboard.py`.
- 2026-11-06 — Files beyond "Files that change": `web/prompt_views.py`, `web/prompt_paths.py`, `static/playground_compare.{js,css}`, `playground_save.js`, `connections.js`, `prompt_detail.js`, `pages.css`, `auth/api_keys.py`, `auth/deps.py` (`may_move_protected_labels` on `ResolvedKey`), `web/routers/settings_keys.py` (the key-creation checkbox), `container.py` (`register_webhook_handler`), `_icons.html`, `base.html`. `ingest/process.py` is unchanged: the queue registry replaced the planned edit.
- 2026-11-06 — `claude-haiku-4-5` keeps `valid_from: 2024-01-01` from the Phase 1 seed instead of FR-X3's 2026-01-01, because re-dating would drop pricing for older traces. Decided by the lead at build time.
- 2026-11-06 — The optional live provider smoke tests (`TRACELOOM_LIVE_LLM=1`, NFR-5 / Risks) are deferred. The default suite stays offline with fake transports for all six providers.
- 2026-11-06 — "Tests that prove it" names differ from the plan; the behaviour is covered by renamed tests, among them:
  - `test_get_defaults_to_production_else_latest`
  - `test_chat_compile`
  - `test_stale_while_revalidate_with_one_second_ttl`
  - `test_start_generation_prompt_sets_name_and_version`
  - `test_fallback_prompt_is_not_linked`
  - `test_member_moves_staging_and_it_is_audited`
  - `test_retry_then_success_signed` (covers signature verification)
  - `test_pending_deliveries_are_redelivered_after_restart`
  - `test_secret_is_encrypted_and_only_a_hint_is_exposed` and `test_failing_test_connection_never_reveals_the_secret`
  - `test_hint_shows_last_four`
  - `test_rotated_key_still_decrypts_old_data`
  - `test_startup_fails_outside_dev_without_key`
  - `test_cycle_raises_naming_the_reference` and `test_cyclic_include_is_400_naming_the_cycle`
  - `test_resolve_replaces_and_records_dependency_versions`

- 2026-11-06 — Review fixes (backend and security):
  - **LLM HTTP and SSRF:** LLM provider HTTP goes through `traceloom/llm/http.py`.
    - A pinned httpx/httpx2 transport runs `resolve_target` on every request and connects to the checked IP with the original Host header and SNI.
    - It never follows redirects and ignores proxy environment variables (`trust_env=False`), so a corporate HTTP proxy can't be used for LLM calls.
    - An IP-literal host is checked as written, without a DNS lookup.
    - Bedrock checks its custom endpoint whenever a client is built, but isn't pinned, because boto3 resolves the name itself.
    - The SSRF guard also unwraps NAT64 and 6to4 addresses.
  - **Webhook delivery:**
    - Deliveries run off the ingestion consumer: a 2-thread lane of the in-process queue in lite mode, and a 2-thread `DeliveryPool` in full mode. Each holds at most 100 waiting deliveries and drops beyond that, because the sweeper re-sends pending rows.
    - Each sweep or kick hands out at most 20. The timeout is 10 s with a 3 s connect limit, and the response body is never read.
    - Deliveries are claimed with one conditional UPDATE. 408 and 429 are retried.
    - `webhook.test` is delivered even when the webhook is disabled.
    - Prompt and label writes (web and public API) commit, then kick delivery.
  - **Label races:** a label move that races a concurrent insert uses `INSERT … ON CONFLICT DO NOTHING` and then an update, and lost races return 409 (`PromptConflict`).
  - **Composition limits:** composition is capped at 100 include expansions and 1,000,000 expanded characters per resolution, with lookups memoised per run. A version is capped at 200,000 characters, and public prompt write bodies at 2,000,000 bytes (413).
  - **Token usage:** OpenAI and Google report input as prompt tokens minus cached tokens, so cached tokens are no longer charged twice. `tests/unit/providers/test_openai.py:61` and `test_google.py:88` encoded the double count (input 12 with 4 cached) and now expect 8, a correction, not a loosening. `UsageDelta.total_tokens` includes cache_read and cache_write.
  - **Replayed tool calls:** the demo agent records tool calls with ids, and prefill pairs or synthesises ids for older traces.
  - **Config:** `TRACELOOM_ENCRYPTION_KEY` entries are each validated as Fernet keys at startup.
  - **SDK:** a chat fallback given as a string becomes one user message, and failed stale refreshes back off.
  - **Provider clients:** each is created inside the `try`, so it's always closed.

- 2026-11-06 — Review fixes (UI, decided by Kunal Mugalkhod to include the Phase 1 restructure):
  - **Dashboard:** Cost, Tokens and both latency tables share one tabbed Breakdown panel (roving tabindex, arrows, Home/End, 2×2 tabs at ≤760px).
  - **"..." menus on Phase 1 pages:**
    - traces list: "Save view" is the only primary; saved views, columns and "Delete matching" are in one menu;
    - trace detail: Bookmark stays visible; share, copy ids and delete are in a menu that renders only for HTMX requests, so `/actions` without JavaScript keeps every control inline;
    - session and user detail: "View traces" plus a menu;
    - settings members, invites, keys and models: row menus.
  - **Keys and Models tables** use stacked mobile rows, so their actions stay on screen at 390px.
  - **Menus app-wide:** choosing a menu item now closes the menu and returns focus to its button.
  - **Playground:** shows "Ended early" / "The stream ended early" when a stream closes without `done`, in single and compare mode. The compare menu item reads "Exit compare" while compare is on.
  - **Webhooks panel:** uses `panel-vis`.
  - **Tests:** new tests cover AC-4 as amended (`test_rerun_of_a_claude5_generation_drops_temperature`) and AC-8's legacy loop (`test_api_get_on_a_legacy_loop_in_the_db_is_400_naming_the_cycle`). The vacuous assertion in `test_connections_page.py` now checks the rendered saved-headers element (a correction).
  - **Docs:** DESIGN.md has a "Phase 1 screens as built" section.
  - **Visual check:** 15 of 40 screenshots were viewed.

- 2026-11-06 — Review fixes (second round, after re-review):
  - **Bedrock endpoints:** a custom Bedrock endpoint must be an `https://` host under `.amazonaws.com` / `.amazonaws.com.cn` (which covers regional and VPC-interface endpoints). It's checked at save and at every client build, on top of the SSRF check, and botocore runs with `proxies={}`. Any other Bedrock base URL, including a private or self-hosted gateway, is refused even when private addresses are allowed. Why: botocore re-resolves DNS and honours environment proxies, so the address can't be pinned.
  - **Copy markup:** `data-copy` is always a literal value, and `data-copy-from="#selector"` copies an element's text. A copy chosen from a "..." menu shows its feedback on the menu button. `tests/api/test_trace_detail_page.py:132` and `tests/api/test_api_keys.py:136` follow the markup rename (`data-copy` → `data-copy-from`) and are just as strict.
  - **Playground bodies:** run and save bodies are capped at 2 MB (413) through a shared `json_body` dependency. Run bodies are also bounded to 1M message characters, 100 variables, a 2M-character rendered size, 50 params and 64 tools.
  - **Public prompt writes:** they use `TRACELOOM_RATE_INGEST` per key, in their own bucket, with 429 and Retry-After.
  - **Smaller fixes:** IPv6 Host headers are bracketed, `DeliveryPool` skips a delivery that's already waiting, and composition checks the expanded size on a running total.

## Proposed `.env.example` lines (for the human; agents cannot edit that file)
```
# --- Phase 2: prompts, playground, webhooks ---
# Fernet key(s) for stored provider keys and webhook secrets; comma-separated for rotation, newest first.
# Generate: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# Required outside dev; dev generates an ephemeral key with a warning.
TRACELOOM_ENCRYPTION_KEY=
TRACELOOM_WEBHOOK_ALLOW_PRIVATE=false
TRACELOOM_LLM_ALLOW_PRIVATE=true
TRACELOOM_PLAYGROUND_RATE=30/minute
TRACELOOM_PROVIDER_TIMEOUT_SECONDS=120
# SDK (the app that calls get_prompt): seconds to cache a fetched prompt; 0 disables the cache.
TRACELOOM_PROMPT_CACHE_TTL=60
```
