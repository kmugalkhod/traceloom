# ADR-0014: Evaluation execution and the daily cost cap

Status: accepted · Date: 2026-11-09 · Change: sdlc/changes/2026-11-09-phase3-evaluation

## Context
Admins define judge rules that run an LLM over live traces (FR-J3–J8), and Members run UI experiments that call models per dataset item (FR-D7). Four things must hold:
- Ingestion is never slowed or failed by evaluation (NFR-4).
- A retried ingest batch never double-samples or double-creates jobs (FR-J4, NFR-2).
- A judge's own call is visible as a trace with its cost, and never triggers rules itself (FR-J7).
- A per-project, per-UTC-day spend cap stops calls before it is exceeded and records why (FR-J6). This includes running `TRACELOOM_EVAL_CONCURRENCY` calls in parallel.

The spec's working assumption was to measure today's spend as the sum of judge-trace costs read from `TraceStore` (`trace_cost_sum`). Judge traces reach the store only after the ingest worker processes them, and ClickHouse merges are eventual. So that sum lags by seconds to minutes, and parallel jobs would all read the same stale figure.

## Options considered
**Where rules are matched**
1. Matched in the ingest worker after the batch commits, against the merged rows already in memory. (chosen)
   - Pro: no extra read, no new process, and it is naturally replayed with the batch.
   - Con: adds to ingest worker time, so it is budgeted at < 5 ms per trace (NFR-1).
2. A separate periodic scanner that queries new traces.
   - Pro: fully decoupled from ingest.
   - Con: needs a high-water mark per project, and ClickHouse eventual reads make "new" fuzzy.

**How spend is measured for the cap**
1. Sum of judge and experiment trace costs from `TraceStore` (the spec's assumption), plus a pre-call estimate.
   - Pro: no new table.
   - Con: lags ingestion. Parallel calls can each pass the check, so overshoot can reach concurrency × backlog. A ClickHouse query also runs per job.
2. **A meta-DB daily ledger with reservations.** (chosen) `eval_spend_daily(project_id, day, spent_usd, reserved_usd)`, updated with one conditional `UPDATE`.
   - Pro: transactional, exact under concurrency, and an O(1) check.
   - Con: one more small table, and stale reservations need reconciling.
3. An in-memory counter per process.
   - Pro: fastest.
   - Con: wrong in full mode with more than one worker, and lost on restart.

## Decision
- **Matching (after commit).** `ingest/process.py` calls `evals.rules.on_ingested(project_id, trace_rows, observation_rows)` after the TraceStore upsert and aggregate refresh, and before the blob moves to `processed/`. A crash before the move replays the batch, which is safe because job creation is idempotent. Exceptions are logged and swallowed.
  - Rules come from the `Cache` port per project, refreshed every `TRACELOOM_EVAL_RULE_CACHE_SECONDS` (default 10).
  - Objects in `TRACELOOM_EVAL_ENVIRONMENT` and `TRACELOOM_EXPERIMENT_ENVIRONMENT` are skipped (see the open question in ARCHITECTURE.md about the experiment environment).
  - At trigger time a rule is tested in Python against the in-memory row: the target, then the filter predicates the row can answer (name, type, tags, environment, metadata). Score filters, and anything else the row cannot answer, are deferred to the run-time re-check.
- **Deterministic sampling.** An object is sampled when `int(sha256(f"{rule_id}:{object_id}")[:8], 16) % 10_000 < sampling_pct × 100`. The same object always gets the same answer, across retries and processes.
- **Idempotent job creation.** `INSERT … ON CONFLICT DO NOTHING` on the partial unique index `(rule_id, object_type, object_id) WHERE rule_id IS NOT NULL`, with `run_after = now + delay_seconds`, then `enqueue(eval_job, delay_seconds)` (ADR-0013). Later updates to the object hit the conflict and create nothing.
- **Execution (worker, `eval` lane).** The worker runs these steps:
  1. Claim the row (ADR-0013).
  2. Re-check the full filter against the store with the id pinned. If it no longer matches, mark the row `skipped`.
  3. Resolve variables (`evals/mapping.py`, a JSONPath subset).
  4. Reserve the cap.
  5. Call the Phase 2 provider layer, with structured output where the adapter supports it and JSON mode plus a tolerant parse otherwise.
  6. Validate the output against the template schema and price the usage with `compute_cost`.
  7. Write the score and the judge trace, then settle the ledger and the row.
  - For run-item executions (UI experiments), `input` and `expected_output` come from the dataset item version, and `output` from the run item's stored output. The judge therefore never waits for the experiment trace to be ingested.
- **Idempotent score.** `score_id = uuid5(NAMESPACE, execution_id)`, written through `TraceStore.add_score` with `source=EVAL`, `execution_id` and `comment = reasoning`. A re-delivery overwrites the same row (SQL upsert, ClickHouse `ReplacingMergeTree`).
- **Judge traces.** The worker builds a trace + generation event pair (`name = eval:<template>`, environment `TRACELOOM_EVAL_ENVIRONMENT`, usage and cost from step 6) with a pre-generated `judge_trace_id`. It submits them through `traceloom.ingest.accept_batch`, the same blob-first path the SDK uses (ADR-0002, ADR-0006). The execution row stores `judge_trace_id` immediately. Experiment calls do the same in `TRACELOOM_EXPERIMENT_ENVIRONMENT`.
- **Cost cap (ledger with reservation).** The cap is `project_eval_settings.daily_cost_cap_usd`, or `TRACELOOM_EVAL_DAILY_COST_CAP_USD` when it is null.
  1. Before the call, estimate the cost as prompt tokens (counted with the model definition's tokenizer) × input price + `max_tokens` × output price.
  2. Reserve with `UPDATE eval_spend_daily SET reserved_usd = reserved_usd + :est WHERE project_id=:p AND day=:d AND spent_usd + reserved_usd + :est <= :cap`, after an insert-if-missing of the day row. The estimate is also stored on the job row (`estimated_cost`).
  3. If rowcount is 0, mark the job `capped` with "daily cost cap of $X reached" and do not retry it that day.
  4. After the call, in one transaction: `spent_usd += actual`, `reserved_usd -= est`.
  - The sweeper's stuck-job reset (ADR-0013) releases a dead job's reservation.
  - A capped execution can be retried by hand (FR-J8) or replayed the next day. It is not re-queued automatically.
  - The same ledger counts UI experiment model calls (FR-D7).
  - "Today's spend" on the settings page and the cap banner read this ledger.
  - `TraceStore.trace_cost_sum` is not needed for the cap. Keep it only if the plan wants a reconciliation view.
- **Errors.** Error text goes through the Phase 2 secret scrubber before it is stored. A missing variable, an archived config or a missing connection fails without retry. Provider and network errors and invalid output retry up to `TRACELOOM_EVAL_MAX_ATTEMPTS`.

## Consequences
- Overshoot is bounded by how much the estimate is too low. With `max_tokens` used for output it is usually an overestimate, so the cap trips slightly early, never far late. The one exception is a duplicate paid call after a crash (ADR-0013).
- A model with no price definition costs $0 in the ledger, so the cap cannot protect it. Decided (Kunal Mugalkhod, 2026-11-09): allow it and show an "unpriced model, the cost cap does not apply" warning on the rule editor, experiment form, execution log and Evaluation settings.
- Online rules never trigger on `traceloom-eval` or `traceloom-experiment` traces (decided by Kunal Mugalkhod, 2026-11-09).
- Rule matching adds a cached-rules lookup and per-rule predicate checks to the ingest worker, but no extra queries. A trace that matches costs one `INSERT` and one enqueue.
- The Python filter predicate must agree with the SQL filter for the fields it handles. One shared filter test table runs against both.
- The spec's `trace_cost_sum` port method becomes optional. The plan should record that departure.

## Revisit when
- Rules number in the hundreds per project, or matching exceeds the 5 ms budget. Then precompile rules into an index by target name and type.
- Customers want monthly budgets or per-rule caps. Then add `period` and `rule_id` dimensions to the ledger.
- Providers start returning authoritative per-request billing. Then settle the ledger from that figure instead of the token estimate.
