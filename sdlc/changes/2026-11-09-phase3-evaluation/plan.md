# Plan: Traceloom Phase 3 — Evaluation

From: spec.md (ef3129a) · Status: accepted · Accepted by: Kunal Mugalkhod

<!-- Status: draft -> accepted (engineer; tech lead for higher-risk) -> implemented.
     Keep this file in sync with the code: a deviation updates plan.md in the same commit. -->

This change is stacked on `team/2026-11-06-ui-polish-align` (Phase 1 + Phase 2 + the v3.2 UI). FR/NFR/AC ids refer to spec.md. The approved architecture is `sdlc/ARCHITECTURE.md` with ADRs 0013–0015 (`sdlc/adr/`). Conventions are in CLAUDE.md. Team mode: two developers, one QA agent and the designer (D-1).

## Files that change
**Docs**
- `sdlc/ARCHITECTURE.md` and `sdlc/adr/0013–0015`: already approved (commit a7572be). Unchanged unless the build deviates.
- `ARCHITECTURE.md` (repo root): modified. Adds one pointer line at the top: "Superseded for Phase 3+ by sdlc/ARCHITECTURE.md; ADRs 0013+ live in sdlc/adr/."
- `README.md`: modified. Adds an "Evaluation" section covering score configs, `score()`, the feedback snippet, judge rules and the cost cap, datasets, and `run_experiment`.
- `DESIGN.md`: modified. Adds a "Phase 3 additions" section (new patterns only) from D-1.
- `.env.example`: *a human edits this file, because the path is blocked for agents.* The exact lines are at the end of this plan.

**Server `traceloom/`**
- `config.py`: modified. Adds the FR-X2 settings plus the ADR-0013/14 extras:
  - `rate_feedback` (`60/minute`)
  - `queue_bulk_max` (5000)
  - `annotation_lock_minutes` (10)
  - `eval_daily_cost_cap_usd` (10.0)
  - `eval_max_attempts` (3)
  - `eval_environment` (`traceloom-eval`)
  - `experiment_environment` (`traceloom-experiment`)
  - `eval_backfill_max` (1000)
  - `eval_concurrency` (4)
  - `dataset_import_max_rows` (10000)
  - `eval_rule_cache_seconds` (10)
  - `eval_stuck_seconds` (600)
  - `eval_sweep_batch` (200)
- `db/models.py` and `db/migrations/versions/0003_evaluation.py`: new.
  - All spec *Data* tables, plus `eval_spend_daily(project_id, day, spent_usd, reserved_usd)` (ADR-0014).
  - `eval_executions.estimated_cost`.
  - `eval_rules.name`, `connection_id` and `model` (T-25): rules show a name, and a built-in template names no model, so the rule chooses one.
  - `dataset_run_items.output/latency_ms/cost/estimated_cost`.
  - `scores.queue_id/execution_id/channel` and the index `(project_id, name, timestamp)`.
  - A data migration sets `source='ANNOTATION'` where `author_id IS NOT NULL`.
- `adapters/full/clickhouse_migrations.sql` / `clickhouse_schema.sql`: `scores` gains `queue_id`, `execution_id`, `channel` and `dataset_run_id` (if missing), plus a skip index on `name`.
- `auth/permissions.py`: modified. New `Perm`s:
  - `EVAL_READ` (Viewer)
  - `SCORES_WRITE` (Member; reuses ANNOTATE semantics for UI scores)
  - `QUEUES_WRITE` (Member)
  - `EVAL_TEMPLATES_WRITE` (Member)
  - `DATASETS_WRITE` (Member)
  - `EXPERIMENTS_RUN` (Member)
  - `SCORE_CONFIGS` (Admin)
  - `EVAL_RULES` (Admin)
  - `EVAL_SETTINGS` (Admin)
  - `QUEUES_DELETE` (Admin)
  - `DATASETS_DELETE` (Admin)
- `auth/deps.py`: modified. Adds a `feedback_key` dependency that resolves `X-Traceloom-Public-Key` to a `FeedbackPrincipal`, a distinct type no other route accepts. `project_key` keeps rejecting pk-only requests with 401.
- `ports.py`:
  - `Queue.enqueue(message, delay_seconds=0)`;
  - `TraceQuery`/observation/session queries gain `score_filters` and `sort="score:<name>"`;
  - the `TraceStore` methods `score_aggregates`, `score_distribution`, `score_trend` and `score_pairs`;
  - `ScoreFilter` and the result dataclasses.
- `adapters/lite/inproc_queue.py`: modified. A timer lane (heap + condition variable) feeds the `eval` side lane with `eval_concurrency` workers (ADR-0013).
- `adapters/full/redis_queue.py`: modified. `delay` via Dramatiq, and a separate `traceloom-eval` queue and actor with `max_retries=0` (the row owns retries).
- `adapters/lite/sql_read.py`, `sql_analytics.py`, `adapters/full/clickhouse_trace_store.py`: score filters, sorts, aggregates, distribution, trend and pairs. The traces list hides the eval and experiment environments unless they're selected.
- `ingest/process.py`: modified. After commit, calls `evals.rules.on_ingested(container, project_id, trace_ids, observation_ids)` inside `try/except` with logging (NFR-4).
- `ingest/sweeper.py`: modified. `set_due_jobs(source)` re-enqueues due or stuck `eval_executions` and `dataset_run_items` (both modes).
- `ingest/schemas.py` / `merge.py` / `accept.py`: modified. `ScoreBody` gains `queue_id`, `execution_id`, `dataset_run_id` and `channel`; merge maps them onto `ScoreRow`. Config validation (FR-S2) runs in `accept_batch` through the config cache (`scores.configs.ConfigCache`, an optional `score_check` hook wired by `api/ingestion.py`), because a per-event error can only reach the client at accept time.
- `ports.py` (TraceStore/ScoreRow, T-5): `ScoreRow` gains `queue_id`, `execution_id`, `channel`; `TraceStore` gains `delete_score` and `config_has_scores` (lite + ClickHouse). The ClickHouse column list for the new score fields is left to the ClickHouse schema task.
- `scores/` (new package):
  - `configs.py`: CRUD, archive, immutable type once used, validate.
  - `service.py`: create, list, delete, source rules.
  - `analytics.py`: histogram, percentiles, trend bucketing, Pearson, Spearman, confusion matrix, agreement, Cohen's κ. Pure Python with no new dependency.
- `annotation/` (new package):
  - `queues.py`: create, edit, add items, add-from-filter, stats.
  - `claim.py`: atomic claim, renew, release, complete, skip (NFR-2).
- `evals/` (new package):
  - `starter.yaml`: six original templates.
  - `templates.py`: versions, built-ins seeded and read-only.
  - `mapping.py`: the JSONPath subset and variable resolution.
  - `rules.py`: rule cache, target match, deterministic sampling `sha256(rule_id:object_id)`, idempotent execution insert, delayed enqueue, environment exclusions for eval and experiment.
  - `spend.py`: ledger reserve, settle and release, and the pre-call estimate (ADR-0014).
  - `runner.py`: the `eval_job` handler (re-check filter, compile, provider call with schema, parse/validate, score via `accept_batch`, judge trace via `accept_batch`, retries with backoff, capped/skipped/failed states, error scrubbing).
  - `backfill.py`.
  - `rule_service.py` (T-25): rule validation, audited writes, enable confirmation, preview and 24 h outcomes for the Evaluators pages.
- `datasets/` (new package):
  - `service.py`: datasets, items, versions, archive, add-from-trace.
  - `importer.py`: CSV/JSON/JSONL, mapping, preview, limits.
  - `runs.py`: create, link, status, `dataset_version` reads.
  - `experiment.py`: the `experiment_item` handler (compile prompt version, provider call traced in the experiment environment, then evaluator executions with no delay).
  - `compare.py`: aggregates, per-item matrix, regressions with threshold.
- `api/scores.py`, `api/feedback.py` (per-route CORS from the union of allowlists, a per-project origin check returning 403, per-key and per-IP limits, a 202 response; ADR-0015), `api/datasets.py`.
- `main.py`: registers the new routers and the `eval_job`/`experiment_item` handlers, and wires `sweeper.set_due_jobs`.
- `web/context.py`: an **Evaluation** nav group (Scores, Annotation queues, Evaluators, Datasets), and the settings tabs **Score configs** and **Evaluation**.
- `web/trace_filters.py`: a score section in the filter model, with URL round-trip and saved views.
- `web/routers/`, all new: `scores_analytics.py`, `settings_scores.py`, `settings_evaluation.py`, `queues.py`, `queue_review.py`, `evaluators.py`, `eval_rules.py`, `eval_log.py`, `datasets.py`, `dataset_items.py`, `dataset_runs.py`, `run_compare.py`. Modified: `trace_actions.py` (config-driven score inputs, "Add to queue", "Add to dataset"), `traces.py`, `sessions.py` (score column/chips, bulk add).
- `web/templates/`: new `scores/`, `queues/`, `evals/` and `datasets/` folders, `settings/scores.html`, `settings/evaluation.html`, `_score_chips.html` and `_score_input.html`. Modified: trace detail Scores tab, list rows, session pages.
- `web/static/`:
  - `analytics.js`: uPlot histogram, trend and scatter, with table fallbacks.
  - `queue_review.js`: shortcuts and lock renewal.
  - `compare.js`: regressed filter, threshold, sort.
  - `rule_editor.js`: mapping table and preview.
  - `dataset_import.js`
  - `feedback.js`: the public snippet, under 3 KB, no dependencies.
  - CSS in `ui/` per DESIGN.md v3.2.

**SDK `sdk/python/traceloom_sdk/`**
- `objects.py`: modified. `score()` gains `config_id`, `session_id` and `dataset_run_id`.
- `datasets.py`: new. `get_dataset`, `Dataset` and `DatasetItem`, with `create_dataset` / `create_item` helpers.
- `experiments.py`: new. `run_experiment`, `Evaluation` and `ExperimentResult`, using a thread pool with `max_concurrency`. A task error is recorded per item and never raised.
- `__init__.py`: exports.

**Tests**: listed under *Tests that prove it*. The shared fixtures go in `tests/conftest.py` and `tests/factories.py`: score config, queue, dataset and rule factories, plus an `eval_clock` fixture that makes the delay lane immediate.

## Dependencies
None. Statistics use the standard library (`statistics.correlation`, with ranks for Spearman). CSV uses `csv`, and the JSONPath subset is hand-written. uPlot is already vendored. SDK: no new dependency.

## Order of work
Step 1 blocks everything. Within one number, steps are independent. UI tasks wait for D-1.

1. **Foundation** (blocks all):
   - config fields
   - migration 0003 + models, including the ClickHouse DDL
   - new perms + matrix test
   - `Queue.enqueue(delay_seconds)` in both adapters + the lite timer lane + the full `traceloom-eval` queue + the sweeper `set_due_jobs` hook (ADR-0013, with contract tests)
   - empty packages `scores/`, `annotation/`, `evals/`, `datasets/`
   - nav group + settings-tab stubs
   - the root `ARCHITECTURE.md` pointer
   - factories and fixtures
   - The Phase 1/2 suite stays green.
2. **Design D-1** (designer agent): every screen in spec *UX*, light and dark, at 1280 and 390. UI tasks in steps 4, 6, 8, 10, 11 and 12 wait for approval.
3. **Scores core** (FR-S1–S3, S6 back end):
   - config service + validation in ingest
   - public scores API
   - score filters and sorts in lite and ClickHouse stores + contract tests
   - SDK `score()` params
   - Covers AC-9 and AC-10 (store side).
4. **Scores UI** (FR-S1, S3, S5, S6 UI): Settings → Score configs, config-driven inputs on trace/observation/session, chips and the score column, score section in the filter tray. Covers AC-10 (URL round-trip).
5. **Public feedback** (FR-S4, ADR-0015): `feedback_key`, the route + CORS + limits, Settings → Evaluation origins + snippet, and `feedback.js`. Covers AC-8.
6. **Annotation queues** (FR-A1–A5): services, atomic claim, pages, review screen + shortcuts, add-from-filter on lists. Covers AC-4 and AC-5.
7. **Datasets core** (FR-D1–D5 back end, D6 SDK): services, item versions, importer, public datasets API, SDK `get_dataset`/`run_experiment`. Covers AC-12 (back end) and the SDK half of AC-6.
8. **Datasets UI** (FR-D1–D4): list, detail tabs, item drawer + history, import wizard, "Add to dataset" from trace/observation/list.
9. **Judge engine** (FR-J1, J2, J4–J7):
   - templates + starter library
   - mapping
   - rule matcher hooked into ingest
   - spend ledger
   - runner with retries, judge traces, capped/skipped states
   - Covers AC-1 (back end), AC-2 and AC-3.
10. **Evaluators UI** (FR-J1, J3, J6, J8):
    - templates tab and editor
    - rules tab, editor with mapping preview and the Admin confirm naming provider and model
    - the cap banner and unpriced-model warning
    - the execution log + retry
    - backfill
    - Covers the UI half of AC-1 and AC-3.
11. **UI experiments + run pages** (FR-D7): the "New experiment" dialog, the `experiment_item` handler, evaluator executions, run detail with live progress. Covers AC-6 (UI). Depends on 7 and 9.
12. **Run comparison** (FR-D8): `compare.py` + page. Covers AC-7. Depends on 11.
13. **Score analytics** (FR-S7): `analytics.py` (unit-tested against hand-computed values), store queries, page + charts with table fallbacks. Covers AC-11. Depends on 3; best after 9, so the human-vs-judge data exists in the demo.
14. **Acceptance and docs**:
    - e2e journeys for the four "done when" items (AC-1, AC-4, AC-6, AC-7) plus AC-8 and AC-13
    - the Phase 3 permission sweep
    - XSS on all new views
    - perf tests (NFR-1)
    - `seed-demo` extended with a score config, a queue, a dataset and two runs
    - README and DESIGN.md sections
    - the `.env.example` lines handed to the owner

## Tests that prove it
| AC | Tests |
|---|---|
| AC-1 | `api/test_eval_rules.py::test_rule_scores_sampled_traces` (20 traces, the deterministic sample set computed in the test, an `EVAL` score + reasoning, a judge trace in `traceloom-eval` with cost > 0, log `succeeded`, a non-matching name gets no job, judge and experiment traces trigger nothing); `e2e/test_journey_judge_rule.py` (an Admin creates the rule in the UI, ingests via the SDK, and sees scores and the log) |
| AC-2 | `api/test_eval_runner.py::test_invalid_output_retried`, `::test_permanent_failure_after_max_attempts_scrubbed`, `::test_ingest_unaffected_by_judge_error` |
| AC-3 | `api/test_eval_cost_cap.py::test_second_call_capped`, `::test_raise_cap_resumes`, `::test_banner_shown`; `unit/test_eval_spend.py::test_concurrent_reservations_never_exceed_cap` (20 threads) |
| AC-4 | `api/test_annotation_queues.py::test_add_from_filter_dedup`, `::test_complete_writes_scores_with_queue_id`, `::test_stats`; `e2e/test_journey_annotation_queue.py` (shortcuts `⌘↵` and `s`, reopen skipped, "Queue complete") |
| AC-5 | `api/test_annotation_claim.py::test_twenty_concurrent_claims_one_wins`, `::test_locked_item_read_only_for_other`, `::test_expired_lock_reclaimable` |
| AC-6 | `api/test_experiments_ui.py::test_two_prompt_versions_on_20_items` (the `experiment_item` handler, fake provider, linked traces, `EVAL` scores, `dataset_version`); `sdk/python/tests/test_experiments.py::test_run_experiment_links_and_scores`, `::test_task_error_recorded_not_raised`; `e2e/test_journey_experiment_compare.py` (CSV import → two runs → compare) |
| AC-7 | `api/test_run_compare.py::test_best_marker_and_regressed_items`, `::test_threshold_hides_small_deltas`, `::test_boolean_true_to_false_regresses`; asserted in the browser by the AC-6 e2e journey |
| AC-8 | `api/test_feedback.py::test_pk_only_creates_score_with_cors`, `::test_other_origin_no_cors_403`, `::test_non_public_config_400`, `::test_pk_rejected_elsewhere_401`, `::test_rate_limit_429`, `::test_preflight_union_allowlist` |
| AC-9 | `api/test_score_configs.py::test_out_of_range_400`, `::test_archive_hides_from_pickers_keeps_scores`, `::test_type_immutable_once_used`; `unit/test_ingest_schemas.py` additions |
| AC-10 | `contract/test_score_filter_contract.py` (lite always, ClickHouse under `TRACELOOM_IT`): filter ops, `exists`/`missing`, `score:<name>` sort with unscored last; `api/test_trace_list_page.py::test_score_filter_url_roundtrip` |
| AC-11 | `unit/test_score_analytics.py` (κ, agreement, confusion matrix vs hand-computed; Pearson/Spearman vs `statistics.correlation` within 1e-9; percentiles; n < 10 → not enough data); `api/test_score_analytics_page.py` (charts + table fallbacks render) |
| AC-12 | `api/test_datasets.py::test_item_versions_and_run_reads_old_version`, `::test_add_from_observation_prefill`; `api/test_dataset_import.py::test_csv_malformed_json_kept_as_string_with_warning`, `::test_row_limit` |
| AC-13 | `api/test_phase3_escape.py` (item, log, review, comparison, analytics labels); `e2e/test_journey_eval_xss.py` (a dialog handler fails the test) |
| AC-14 | The whole default suite with the socket guard; no provider keys present |

Also:
- `api/test_phase3_permissions.py` sweeps every new unsafe route as a Viewer (403) and as a Member on the Admin routes (configs, rules, cap, origins, deletes → 403).
- `api/test_phase3_migration.py` checks that 0003 upgrades a Phase 2 DB and maps the existing score sources.
- `contract/test_blob_queue_contract.py` gains delayed-enqueue cases.
- `perf/test_eval_volume.py` covers NFR-1.

## Risks
- **Riskiest step: 1 (delayed jobs in both queue adapters + sweeper).** Every evaluation path depends on it, and it touches the ingest worker. Mitigation:
  - the rows are the source of truth, so a lost wake-up only delays a job until the next sweep;
  - contract tests on both adapters;
  - the Phase 1 durability test (AC-8 Phase 1) must stay green before anything else lands;
  - the eval lane is separate from the ingest lane in lite, and a separate Dramatiq queue in full.
- **Rule matching in the ingest path** could slow or break ingestion. Mitigation: it runs after commit, wrapped in try/except, with a 10 s rule cache, one indexed insert per match and `ON CONFLICT DO NOTHING`. The perf test asserts < 5 ms per trace with 50 rules.
- **Lock races on SQLite** (single writer, no `SKIP LOCKED`). Mitigation: a single conditional `UPDATE … WHERE id=? AND (locked_until IS NULL OR locked_until < ?)` with a rowcount check and a bounded retry over candidate ids; `FOR UPDATE SKIP LOCKED` on Postgres; a 20-thread test on both.
- **Cost cap overshoot.** Mitigation: an atomic reservation in `eval_spend_daily` before each call, settled with the actual cost after (ADR-0014). Unpriced models are allowed with a visible warning (owner decision).
- **Judge output parsing across six providers.** Mitigation: the schema goes to the provider as structured output where the adapter supports it, with a JSON-mode fallback and a tolerant parse (first JSON object). Invalid output is retryable, and it's covered with the `FakeProvider`.
- **Migration 0003** touches `scores` (new columns, index, source backfill). On big Postgres tables the index uses `CREATE INDEX CONCURRENTLY` in an autocommit block (the Phase 2 pattern), and the source backfill is batched.
- **Public feedback endpoint** is a new unauthenticated-ish surface. Mitigation: a distinct principal type, a narrow body schema, config allowlist, origin check, two rate limits, and no trace-existence oracle (ADR-0015).
- **Merge conflicts in a parallel team** on `db/models.py`, `ports.py`, `web/context.py`, `conftest.py`, `factories.py` and `trace_actions.py`. Step 1 creates every table, perm, nav item, port signature and fixture stub up front. Developers merge the team branch in right before submitting, and plan.md deviations are kept as one copy each.
- **Stacked branch.** If UI polish changes during its own review, this branch rebases onto it. Phase 3 merges only after UI polish and Phase 2.

## Alternatives considered
- **Sum of judge-trace costs for the cap** (the spec's assumption): rejected for an exact meta-DB ledger with reservations. Trace costs lag ingestion and are only eventually consistent in ClickHouse, so parallel jobs would overshoot (ADR-0014, approved).
- **A separate eval worker service**: rejected for now. A separate queue/lane inside the existing worker isolates judge latency from ingest without a new deployment unit (ADR-0013, "Revisit when").
- **numpy/scipy for statistics**: rejected. The standard library covers Pearson, Spearman (via ranks), percentiles and κ at ≤ 50k points, and lite installs stay small.
- **A full JSONPath library** for variable mapping: rejected. A small subset covers real uses with no evaluation surface.
- **Signed per-trace feedback tokens**: rejected by the owner (spec concern 1).
- **Storing experiment outputs only on traces**: rejected. Run items also keep output, latency and cost, so evaluators and comparison don't wait on trace ingestion (ARCHITECTURE.md).

## Verification target
- `python3 .sdlc/bin/sdlc.py verify` is green: setup, compileall, `pytest -q` (API, unit, contract-lite, perf, e2e and SDK tests) and `ruff check .`.
- With the server running and `traceloom seed-demo`:
  - an Admin creates a relevance rule on `chat` traces at 100% with a fake/dev connection, and the demo agent's next traces get `EVAL` scores, judge traces in `traceloom-eval`, and log rows `succeeded`;
  - a Member works the demo queue to "Queue complete" with keyboard shortcuts;
  - two UI experiments on the 20-item demo dataset finish, and the comparison page marks a winner and lists the regressed items;
  - `curl -X POST /api/public/feedback -H 'X-Traceloom-Public-Key: tl-pk-…' -H 'Origin: <allowed>'` returns 202 with CORS headers, and the score appears on the trace.
- `GET /api/public/scores?trace_id=…` returns `200` with `{data:[{id,name,value,string_value,data_type,source,config_id,…}], meta}`.
- The UI matches `design/mockup.html` at 1280 and 390, light and dark (QA compares).
- The `.env.example` lines below are handed to the owner.

## Deviations
- 2026-11-09 — spec FR-J6 / [~] cost assumption: the cap uses the `eval_spend_daily` ledger with reservations instead of `TraceStore.trace_cost_sum`, per approved ADR-0014. `trace_cost_sum` is not built.
- 2026-11-09 — spec FR-S4 / AC-8: a POST from a non-allowed origin returns 403 (stricter than "no CORS headers"), and accepted feedback returns 202 without checking that the trace exists, per approved ADR-0015.
- 2026-11-09 — FR-J4: online rules also ignore `traceloom-experiment` traces (owner decision at the architecture gate).

## Proposed `.env.example` lines (for the human; agents cannot edit that file)
```
# --- Phase 3: evaluation ---
# Public-key 👍/👎 feedback endpoint: requests per minute per key and per client IP.
TRACELOOM_RATE_FEEDBACK=60/minute
# Max items one "add all matching to queue" can add.
TRACELOOM_QUEUE_BULK_MAX=5000
# Minutes an annotation item stays locked to a reviewer without activity.
TRACELOOM_ANNOTATION_LOCK_MINUTES=10
# Default per-project daily (UTC) spend cap for LLM judges and UI experiments, in USD. 0 pauses judges.
TRACELOOM_EVAL_DAILY_COST_CAP_USD=10
TRACELOOM_EVAL_MAX_ATTEMPTS=3
TRACELOOM_EVAL_CONCURRENCY=4
TRACELOOM_EVAL_BACKFILL_MAX=1000
# Environments that hold judge and experiment traces; hidden from lists by default, never trigger rules.
TRACELOOM_EVAL_ENVIRONMENT=traceloom-eval
TRACELOOM_EXPERIMENT_ENVIRONMENT=traceloom-experiment
TRACELOOM_EVAL_RULE_CACHE_SECONDS=10
TRACELOOM_EVAL_STUCK_SECONDS=600
TRACELOOM_EVAL_SWEEP_BATCH=200
TRACELOOM_DATASET_IMPORT_MAX_ROWS=10000
```
