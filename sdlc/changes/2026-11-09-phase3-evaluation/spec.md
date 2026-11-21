# Spec: Traceloom Phase 3 — Evaluation

From: intent.md (b4ac861) · Status: accepted · Record: prompt.md "Phase 3 — Evaluation"
Skills applied: impeccable (UX guidance for the design step); dataviz (analytics and comparison charts); REVIEW.md security pass
Architecture: ARCHITECTURE.md + docs/adr/0001–0012. Expected new ADRs: 0013 (delayed and scheduled jobs), 0014 (evaluation execution and cost cap), 0015 (public feedback endpoint).

## Summary
Phase 3 adds evaluation on top of Phases 1–2 and the v3.2 UI:
- score configs, and scores everywhere (API, SDK, UI, public-key 👍/👎);
- score filters, sorts and an analytics page;
- annotation queues with locking;
- LLM-as-a-judge templates, rules and worker jobs with a daily cost cap;
- datasets with versioned items;
- experiments from the SDK and UI, and a run comparison page.

It extends the existing `scores` table, the queue (adding delayed jobs), the Phase 2 LLM layer and the prompt service. Lite mode stays the test default and the suite stays offline.

## Functional requirements

### Scores and score configs (S)
- **FR-S1 Score configs.** Admins create, edit and archive configs per project. A config holds:
  - `name`, unique per project among non-archived configs;
  - `data_type`: `NUMERIC` (optional `min`/`max`), `CATEGORICAL` (2–20 categories, each a `{label, value}` where value is numeric), or `BOOLEAN`;
  - `description`;
  - `public_feedback` (bool, default false; FR-S4).
  - Archived configs stay readable and keep their scores. They can't be picked for new annotations, queues or judge templates, and they can be unarchived.
  - The data type and categories can't change once scores reference the config. Description, range and archive state can.
- **FR-S2 Validation.** A score that names a `config_id` is checked against that config: data type, range and category. If it fails, the API returns 400 and ingestion records a per-event error. A score without a config is accepted as today (backwards compatible).
- **FR-S3 Creating scores.**
  - Public API: `POST /api/public/scores` (pk+sk), single or batch, plus `GET /api/public/scores?trace_id=&name=&source=&config_id=&from=&to=` and `DELETE /api/public/scores/{id}`.
  - The ingestion batch `score-create` keeps working.
  - SDK: `score()` gains `config_id`, `session_id` and `dataset_run_id`.
  - UI: the trace detail Scores tab, observations and the session page offer config-driven inputs (number field, category select, boolean toggle). Free-form scores stay available. Members+.
  - Each score records `source`: `API` (API/SDK), `ANNOTATION` (UI and queues, with `author_id`) or `EVAL` (judge, with `execution_id`). The migration maps existing rows: `API` stays `API`, and UI-created rows become `ANNOTATION`.
- **FR-S4 Public feedback.**
  - `POST /api/public/feedback` authenticates with the **public key only** (header `X-Traceloom-Public-Key`). The body is `{trace_id, observation_id?, name, value, comment?}`.
  - It is accepted only when `name` matches a non-archived config with `public_feedback=true`, and the value is valid for it.
  - Comments are capped at 1,000 characters.
  - The call creates (or, for the same trace + name + client-supplied `id`, replaces) one `API`-source score with `channel=public_feedback`; analytics can include or exclude this channel.
  - The public key gives no other access: every other endpoint rejects pk-only auth with 401.
  - Rate-limited per key and per client IP (`TRACELOOM_RATE_FEEDBACK`, default `60/minute`).
  - CORS: only origins in the project's **allowed feedback origins** list (project settings) receive CORS headers. The list is empty by default.
  - A static `feedback.js` snippet (no dependencies, under 3 KB) and copy-paste instructions are shown in project settings.
- **FR-S5 Display.**
  - Score chips (name: value, coloured by source) appear on the traces list, the observation rows in the tree, the sessions list, the session detail and the dataset run items.
  - Lists gain a **Scores** column showing the averages of numeric and boolean scores. The column picker chooses which score names to show.
- **FR-S6 Filter and sort.**
  - `TraceQuery` and the observation and session queries gain `score_filters`: a list of `{name, op, value}` where `op` is one of `= != > >= < <=` for numeric/boolean, `in` for categorical, and `exists`/`missing`.
  - They also gain `sort="score:<name>"`, which sorts by the average value; rows with no score sort last.
  - Implemented in the lite SQL and ClickHouse stores, with contract tests.
  - The filter tray gets a Score section, and the filters round-trip in the URL and saved views.
- **FR-S7 Score analytics page** (`/project/{id}/scores/analytics`, Viewer+), for a date range and environment:
  - **Distribution:** per score name, a histogram (numeric, 10 bins over the min–max range or the config range) or a bar chart (categorical/boolean), with counts, mean, median, p10/p90 and n.
  - **Trend:** the mean per hour or day (auto by range) for numeric/boolean, or the share per category for categorical, split by source.
  - **Compare two scores** (A and B, each a name plus an optional source), matched on the same trace (or the same observation when both have one):
    - numeric × numeric: Pearson and Spearman correlation, a scatter plot (≤ 2,000 points sampled), and n matched;
    - categorical/boolean × the same: a confusion matrix with counts, the exact-match agreement %, and Cohen's κ;
    - mixed types: a box plot of numeric values per category.
    - **Human vs judge** is a preset: A = source `ANNOTATION`, B = source `EVAL` for the same name.
  - Each computation states n, and shows "not enough data" below n = 10.

### Annotation queues (A)
- **FR-A1** Members+ create a queue with a name, description and one or more non-archived score configs. Admins can delete a queue. Queues can be edited.
- **FR-A2 Adding items.** Traces, observations or sessions are added:
  - by hand: an "Add to queue" action on the trace detail, observation and session detail pages, and a bulk select on lists;
  - **from a filter**: on a list, "Add all matching to queue" snapshots the current filter result (capped by `TRACELOOM_QUEUE_BULK_MAX`, default 5,000) as items.
  - Duplicate items (the same object in the same queue) are skipped and the count is reported.
  - Items are `pending`, `completed` or `skipped` (skipped items can be reopened).
- **FR-A3 Review workflow.** `/project/{id}/queues/{qid}/review` shows one item: the trace/observation/session view (the IO and tree read-only) beside a score form with one input per queue config, plus an optional comment.
  - Submitting writes `ANNOTATION` scores tagged with `queue_id` and the reviewer, then marks the item completed.
  - Keyboard: `n`/`→` next (completes after saving when the form is dirty and valid), `s` skip, `⌘/ctrl+enter` complete, `?` shows help.
  - A reviewer can also open the queue list and jump to a specific item.
- **FR-A4 Locking.**
  - Fetching the next item atomically claims the oldest pending item that is unlocked or has an expired lock, for this reviewer (`locked_by`, `locked_until = now + TRACELOOM_ANNOTATION_LOCK_MINUTES`, default 10). Two concurrent claims never return the same item.
  - Opening an item locked by someone else shows it read-only, with "Locked by <name> until <time>".
  - Completing or skipping releases the lock. Holding an item renews the lock at each save.
- **FR-A5 Progress.** The queue page shows:
  - total, completed, skipped and pending items, with a progress bar;
  - per-reviewer completed count, median time per item, and the last activity;
  - the item table with filters by status and reviewer.
  - A queue at 100% shows "Queue complete".

### LLM-as-a-judge (J)
- **FR-J1 Evaluator templates.**
  - A template has a name, a prompt with `{{variables}}`, an output schema, a model choice (an LLM connection + model, or "project default") and model params.
  - The output schema has a score of type numeric (with a range), boolean or categorical (with categories), plus `reasoning` (a string).
  - Templates are versioned like prompts: each save is an immutable version, and a rule pins a version or follows the latest.
  - Members+ create and edit templates.
- **FR-J2 Starter library.**
  - Six read-only built-in templates: hallucination, relevance, toxicity, helpfulness, correctness (vs `{{expected_output}}`) and conciseness.
  - Their wording is original to Traceloom.
  - They can be cloned into project templates. Built-ins need a model chosen at rule time.
- **FR-J3 Run rules.** Admin+ create, edit, enable and backfill rules (decided, see concerns). Enabling shows a confirmation naming the provider and model that will receive trace content. A rule holds:
  - a template version;
  - a **target**: `trace`, or `observation` with a name and/or type (`GENERATION`, `SPAN`, `EVENT`, …);
  - a **filter**, using the same filter model as the lists (FR-S6 fields plus name, tags, environment, metadata);
  - **sampling** from 0–100% (default 100);
  - a **delay** in seconds (default 30, so late spans can arrive);
  - a **variable mapping**: each template variable maps to a source (`input`, `output`, `metadata.<key>`, or a child observation by name with its `input`/`output`), an optional JSONPath into it, and a default;
  - a score name (defaults to the template name), an optional `config_id`, and enabled/disabled.
  - Rules are by default applied **only to new data**. A "backfill" action runs a rule over existing matches in a time range, capped by `TRACELOOM_EVAL_BACKFILL_MAX` (default 1,000).
- **FR-J4 Triggering.**
  - When a trace or observation is created or updated by ingestion, enabled rules whose target matches are checked.
  - Sampling uses a deterministic hash of (rule id, object id), so a retried ingest never double-samples.
  - The first match per (rule, object) creates one `eval_job` (`pending`, with `run_after = now + delay`). Later updates don't create more.
  - When the job runs, the filter is re-checked on the latest data. A no-longer-matching job is marked `skipped`.
  - Traces in the judge's own environment (`TRACELOOM_EVAL_ENVIRONMENT`, default `traceloom-eval`) never trigger rules.
- **FR-J5 Execution (worker).**
  - The job resolves the variables, compiles the template and calls the model through the Phase 2 provider layer, with the schema as structured output (or JSON-mode fallback plus parse).
  - It validates the output against the schema and writes one `EVAL` score with `comment = reasoning` and `execution_id`.
  - Retries cover provider/network errors and invalid output: up to `TRACELOOM_EVAL_MAX_ATTEMPTS` (default 3) attempts, with exponential backoff and jitter. A missing variable, an archived config or a missing connection fails without retrying.
- **FR-J6 Daily cost cap.**
  - Each project has a per-day (UTC) judge spend cap in USD: `TRACELOOM_EVAL_DAILY_COST_CAP_USD` (default 10), which Admins can override in project settings. 0 means judges are paused.
  - Before each call, the job checks today's judge spend (the sum of judge trace costs) plus an estimate for this call.
  - If the cap would be exceeded, the job is marked `capped` (not failed) with the reason "daily cost cap of $X reached". It isn't retried that day.
  - The rules page shows a banner while the cap is reached.
- **FR-J7 Judge traces.**
  - Every judge call is recorded as a trace in the same project, in environment `traceloom-eval`, with name `eval:<template>` and the generation's tokens and cost from the Phase 1 cost engine.
  - The score links to it (`execution_id` → execution → `judge_trace_id`).
  - The default traces view hides environment `traceloom-eval` unless the user picks it.
- **FR-J8 Execution log.** `/project/{id}/evals/log` lists executions with rule, template version, target object (linked), status (`pending|running|succeeded|failed|skipped|capped`), attempts, the score value, the judge trace link, cost, duration and the error message (scrubbed of secrets).
  - It can be filtered by rule, status and time.
  - Members+ can retry a failed execution.

### Datasets and experiments (D)
- **FR-D1 Datasets.** Members+ create a dataset (name, unique per project; description; metadata).
  - Each item has `input` (JSON), `expected_output` (JSON, optional), `metadata` (JSON), an optional `source_trace_id`/`source_observation_id`, and a status of `active` or `archived`.
- **FR-D2 Versioned edits.**
  - Every create, edit or archive of an item writes an immutable `dataset_item_versions` row (item id, version, content, actor, time).
  - The dataset's version is the timestamp of its latest change.
  - Item history is viewable, and a run records `dataset_version` (FR-D5) so that items are read as of that version.
- **FR-D3 Import.**
  - CSV upload with column mapping (each column becomes input / expected_output / metadata / ignore; a JSON cell parses as JSON, otherwise it's a string), or a JSON/JSONL array of objects with `input`, `expected_output` and `metadata` keys.
  - Capped by `TRACELOOM_DATASET_IMPORT_MAX_ROWS` (default 10,000) and the upload size limit.
  - A preview of the first 5 rows and the per-row errors is shown before commit.
- **FR-D4 Add from trace.** On trace detail and observation detail, "Add to dataset" opens a dialog prefilled with the object's input as `input` and its output as `expected_output` (both editable), and the metadata. One click saves it with a source link. Bulk add from a list selection is included.
- **FR-D5 Dataset runs.** A run has a name (unique per dataset), description, metadata, `dataset_version`, the prompt version/model used (for UI runs), and status `running|completed|failed`.
  - A run item links (dataset item → trace id, observation id?) and holds the output, latency and cost, taken from the linked trace.
  - Scores on the linked trace count as the run item's scores.
- **FR-D6 SDK experiments.**
  - `traceloom_sdk.get_dataset(name)` returns the items as of the latest version.
  - `traceloom_sdk.run_experiment(dataset, task_fn, evaluators=[…], run_name=None, max_concurrency=4, metadata=None)`:
    - creates a run via `POST /api/public/datasets/{name}/runs`;
    - for each active item, calls `task_fn(item)` inside a trace named `experiment:<run>`, linking the run item;
    - calls each evaluator `fn(input, output, expected_output) -> Evaluation(name, value, comment?)`, recording an `API` score on the item trace with `dataset_run_id`;
    - returns a result object with per-item outputs, scores and errors and the run URL.
  - A task exception is recorded on that item and doesn't stop the run.
  - Public API: dataset create/get/list, items create/list/archive, runs create/get, run items create.
- **FR-D7 UI experiments.**
  - Members+ open "New experiment" on a dataset and pick: a prompt + version (or label), an LLM connection + model + params, a variable mapping (prompt variables ← item input keys), and zero or more evaluator templates (with `{{expected_output}}` available).
  - Submitting creates a run and enqueues one worker job per item. Each item compiles the prompt, calls the model (traced in environment `traceloom-experiment`, linked to the run), and then runs the chosen evaluators as judge jobs on that trace (FR-J5, with no delay), counted toward the daily cost cap.
  - The run page shows live progress (HTMX polling).
  - The cost cap applies to experiment calls too, and capped items are marked.
- **FR-D8 Run comparison page** (`/project/{id}/datasets/{did}/compare?runs=a,b[,c,d]`, up to 4 runs; the first is the baseline):
  - **Aggregates per run:** the mean of each score name (n), success rate, mean and p95 latency, total cost, and a "best" marker per score.
  - **Per-item table:** item input (truncated, expandable), expected output, then per run the output, scores, latency and cost.
  - **Regressions:** an item regresses in run X when any score is lower than the baseline's by more than the threshold (a page control, default 0 → any decrease; boolean true→false counts). Regressed cells are highlighted, a filter shows "only regressed", and there is a count per run.
  - Rows can be sorted by score delta.

### Platform (X)
- **FR-X1 Delayed jobs.**
  - The queue port gains `enqueue(message, delay_seconds=0)` and new message types `eval_job` and `experiment_item`.
  - Jobs are persisted rows (`eval_executions`, `dataset_run_items`) with `run_after`, so a restart doesn't lose them. The sweeper picks up due rows that were never enqueued or were stuck in `running` past a timeout.
  - Lite mode uses a timer lane in the in-process queue; full mode uses Dramatiq's `delay`.
- **FR-X2 New settings** (all in `Settings` and `.env.example`):
  - `TRACELOOM_RATE_FEEDBACK`
  - `TRACELOOM_QUEUE_BULK_MAX`
  - `TRACELOOM_ANNOTATION_LOCK_MINUTES`
  - `TRACELOOM_EVAL_DAILY_COST_CAP_USD`
  - `TRACELOOM_EVAL_MAX_ATTEMPTS`
  - `TRACELOOM_EVAL_ENVIRONMENT`
  - `TRACELOOM_EVAL_BACKFILL_MAX`
  - `TRACELOOM_EVAL_CONCURRENCY` (default 4)
  - `TRACELOOM_DATASET_IMPORT_MAX_ROWS`
  - `TRACELOOM_EXPERIMENT_ENVIRONMENT` (default `traceloom-experiment`)
- **FR-X3 Navigation.**
  - The left nav gains an **Evaluation** group: Scores (analytics), Annotation queues, Evaluators (templates, rules, log) and Datasets.
  - Settings gains a **Score configs** tab (Admin+ edit, Viewer+ read) and an **Evaluation** tab (cost cap, feedback origins and snippet).
- **FR-X4 Audit rows** for: config create/edit/archive, queue delete, template version, rule create/edit/enable/disable/delete/backfill, cost cap change, feedback origins change, dataset delete, item archive, and experiment start.

## Non-functional requirements
- **NFR-1 Performance (lite)**, measured at 50k traces, 200k scores, 50 rules and 10k dataset items:
  - list pages with a score filter: < 1 s;
  - the analytics page (30 days, two-score compare): < 2 s;
  - the queue next-item claim: < 100 ms;
  - the comparison page for 4 runs × 500 items: < 1.5 s (paginated at 100 items).
  - Rule matching adds < 5 ms per ingested trace with 50 rules (rules cached per project, 10 s TTL).
- **NFR-2 Correctness under concurrency.**
  - The lock claim is atomic: `UPDATE … WHERE id = (SELECT … ) AND (locked_until IS NULL OR locked_until < now) RETURNING` on Postgres, and a single-statement conditional update with a retry loop on SQLite. A test runs 20 concurrent claims.
  - Eval job creation is idempotent per (rule, object).
- **NFR-3 Security.**
  - Every new query is project-scoped and permission-checked (matrix below).
  - The pk-only feedback endpoint has its own dependency that never resolves to a full project key.
  - CORS headers go only on `/api/public/feedback`, and only for allowed origins.
  - All user content (dataset JSON, judge reasoning, outputs, CSV cells, comments) renders escaped.
  - CSV import is protected against formula injection on any export path (none in this phase) and parses with the stdlib `csv` module, size-limited.
  - Judge prompts send trace data to the configured provider only (see the concerns).
  - Provider secrets never appear in execution errors.
  - The CSP is unchanged: charts use the vendored uPlot plus static JS; `feedback.js` is served static, with no eval.
- **NFR-4 Reliability.**
  - Judge failures never affect ingestion: rule matching runs after the batch commits, and its errors are logged and swallowed.
  - Jobs are at-least-once. Score writes are idempotent by `execution_id` (re-delivery overwrites the same score id).
  - Experiment runs survive a worker restart (the sweeper re-enqueues pending items).
- **NFR-5 Offline tests.** Judges and experiments are tested with the `FakeProvider`. No network, and the socket guard stays.
- **NFR-6 UX.**
  - Follows DESIGN.md v3.2, light and dark, usable at 1280px and readable at 390px. The review screen stacks below 1100px.
  - Keyboard shortcuts in queues; charts are accessible (a table fallback for each chart, colourblind-safe palette from DESIGN.md tokens).
  - WCAG AA.

## Design

### Components and changes
```
traceloom/
  scores/            configs service (CRUD, validate), service (create/delete/list, source rules), analytics.py (distribution, trend, compare, kappa/pearson/spearman)
  annotation/        queues service (create, add items, add-from-filter, claim/release, complete/skip, stats)
  evals/             templates (built-ins in starter.yaml, versions), rules (match, sample hash, cache), jobs (create, execute, retry, cap), mapping.py (variable extraction, JSONPath subset)
  datasets/          service (datasets, items, versions, import csv/json), runs (create, link, compare, regressions), experiment worker handler
  api/               scores.py, feedback.py (pk-only + CORS), datasets.py
  ingest/process.py  after-commit hook → evals.rules.on_ingested(project, trace_ids, observation_ids)
  web/routers/       scores_analytics.py, settings_scores.py, settings_evaluation.py, queues.py, queue_review.py, evaluators.py, eval_rules.py, eval_log.py, datasets.py, dataset_items.py, dataset_runs.py, run_compare.py
  web/templates/     scores/, queues/, evals/, datasets/, settings/{scores,evaluation}.html, shared _score_chips.html, _score_input.html
  web/static/        analytics.js (uPlot charts), queue_review.js (shortcuts), compare.js, feedback.js (public snippet)
  adapters/lite|full score filters/sorts, analytics queries, observation/session score aggregates
  db/migrations/     0003_evaluation.py; ClickHouse migration for new score columns + indexes
sdk/python/traceloom_sdk/  scores (config_id, session_id, dataset_run_id), datasets.py (get_dataset, DatasetItem), experiments.py (run_experiment, Evaluation, ExperimentResult)
```

### Data and interfaces
**Meta DB (migration 0003):**
- `score_configs`(id, project_id, name, data_type, min_value, max_value, categories JSON, description, public_feedback, archived_at, created_by, created_at, updated_at)
- `scores` gains `queue_id`, `execution_id`, `channel` (NULL or `public_feedback`) and an index (project_id, name, timestamp). Source values are `API|ANNOTATION|EVAL`. The ClickHouse `scores` table gains the same columns.
- `annotation_queues`(id, project_id, name, description, config_ids JSON, created_by, created_at)
- `annotation_items`(id, queue_id, project_id, object_type trace|observation|session, object_id, trace_id, status, locked_by, locked_until, completed_by, completed_at, created_at, duration_ms); unique (queue_id, object_type, object_id); index (queue_id, status, created_at)
- `eval_templates`(id, project_id NULL for built-in, name, created_at), `eval_template_versions`(id, template_id, version, prompt, output_schema JSON, connection_id, model, params JSON, created_by, created_at)
- `eval_rules`(id, project_id, template_id, template_version NULL=latest, target, target_name, target_type, filter JSON, sampling_pct, delay_seconds, mapping JSON, score_name, config_id, enabled, created_by, created_at, updated_at)
- `eval_executions`(id, project_id, rule_id NULL, run_item_id NULL, template_version_id, object_type, object_id, trace_id, status, attempts, run_after, started_at, finished_at, score_id, judge_trace_id, cost, error); unique (rule_id, object_type, object_id) where rule_id is set
- `project_eval_settings`(project_id, daily_cost_cap_usd NULL=env default, feedback_origins JSON)
- `datasets`(id, project_id, name, description, metadata JSON, created_by, created_at, updated_at)
- `dataset_items`(id, dataset_id, project_id, current_version, status, source_trace_id, source_observation_id, created_at, updated_at), `dataset_item_versions`(item_id, version, input JSON, expected_output JSON, metadata JSON, status, actor, created_at)
- `dataset_runs`(id, dataset_id, project_id, name, description, metadata JSON, dataset_version, config JSON (prompt/model/evaluators for UI runs), status, created_by, created_at, finished_at); unique (dataset_id, name)
- `dataset_run_items`(id, run_id, project_id, item_id, item_version, trace_id, observation_id, status, run_after, error, created_at)

**TraceStore port** gains:
- `score_filters` and `sort="score:<name>"` on the trace, observation and session queries;
- `score_aggregates(project_id, object_type, ids, names)`;
- `score_distribution`, `score_trend` and `score_pairs(project_id, a, b, range, limit)` (the stats themselves are computed in Python);
- `trace_cost_sum(project_id, environment, since)` for the cap.
- Lite and ClickHouse implementations, with contract tests.

**Public API (pk+sk unless noted):**
- Scores: `POST/GET /api/public/scores`, `DELETE /api/public/scores/{id}`, `GET /api/public/score-configs`.
- Feedback: `POST /api/public/feedback` (pk only), `OPTIONS` preflight.
- Datasets:
  - `POST/GET /api/public/datasets`, `GET /api/public/datasets/{name}`;
  - `POST/GET /api/public/datasets/{name}/items`, `POST /api/public/dataset-items/{id}/archive`;
  - `POST /api/public/datasets/{name}/runs`, `GET /api/public/datasets/{name}/runs/{run}`;
  - `POST /api/public/dataset-run-items`.

**Queue messages:** `eval_job {execution_id}` and `experiment_item {run_item_id}`, alongside the existing `ingest` and `webhook_delivery`.

### Permissions (additions)
| Action | Viewer | Member | Admin | Owner |
|---|---|---|---|---|
| Read scores, analytics, queues, templates, rules, log, datasets, runs, comparisons | ✓ | ✓ | ✓ | ✓ |
| Create/delete own scores in the UI, review queue items, create/edit queues, add items | | ✓ | ✓ | ✓ |
| Create/edit templates, datasets, items, import, add from trace, run UI experiments, retry executions | | ✓ | ✓ | ✓ |
| Score configs, run rules (create/enable/backfill), cost cap, feedback origins, delete queues/datasets | | | ✓ | ✓ |

### UX
The design step (D-1) produces `design/mockup.html` extending DESIGN.md v3.2, with no new tokens unless needed. It covers these screens:
- **Score chips and inputs:**
  - chips on lists, the tree and sessions (source shown by a small glyph: person / key / judge);
  - a config-driven score input row in the trace Scores tab.
- **Score analytics:**
  - a score picker;
  - a distribution card grid;
  - a trend chart split by source;
  - a compare panel (A/B pickers, a "Human vs judge" preset, scatter or confusion matrix, stat tiles for n, r/ρ, agreement % and κ).
  - Empty state.
- **Annotation queues:**
  - a list with progress bars;
  - the queue detail: progress, per-reviewer table and items table;
  - the **review screen**: the object on the left (IO, compact tree), the score form on the right, a sticky footer with Skip / Complete and shortcut hints, a lock banner, and "Queue complete".
- **Evaluators:**
  - a templates tab (built-ins badged; editor with prompt, variable list, output schema builder, model picker);
  - a rules tab (the list, enabled toggle, sampling and delay, a cap banner);
  - the rule editor (target, a filter builder that reuses the list filter tray, sampling slider, delay, a mapping table per variable with source/path/default, and a preview against a recent matching trace);
  - the execution log table with status pills.
- **Datasets:**
  - the list;
  - the dataset detail with Items | Runs | History tabs;
  - item edit drawer with JSON editors and a version history;
  - an import wizard (upload → map columns → preview → import);
  - "Add to dataset" dialog from trace and observation;
  - a "New experiment" dialog (prompt+version, model, mapping, evaluators) and a run detail with live progress.
- **Run comparison:**
  - run pickers (baseline first);
  - an aggregate table with "best" markers;
  - the per-item table with columns per run, regression highlight, an "only regressed" toggle, a threshold input and sort by delta.
- **Settings:**
  - the Score configs tab (table, create/edit dialog with type-specific fields, archive);
  - the Evaluation tab (cost cap, today's spend, feedback origins, `feedback.js` snippet).

## Acceptance criteria
- **AC-1 (FR-J3–J8; done-1):** Given a rule on traces named `chat` at 50% sampling, 1 s delay, using the built-in relevance template with a fake provider, when 20 `chat` traces are ingested, then after the delay the traces sampled by the deterministic hash (and only those) get an `EVAL` score with reasoning. Each has a judge trace in `traceloom-eval` with cost > 0, and the log shows `succeeded`. Judge traces trigger no rules. A trace named `other` gets no job.
- **AC-2 (FR-J5):** Given the fake provider returns invalid JSON once, then valid output, the execution succeeds on attempt 2. Given it always fails, the execution is `failed` after 3 attempts with a scrubbed error. Ingestion is unaffected in both cases.
- **AC-3 (FR-J6):** Given a cap of $0.01 and fake usage costing $0.006 per call, the first call succeeds, the second is `capped` with the reason, and the rules page shows the banner. Raising the cap lets new jobs run.
- **AC-4 (FR-A1–A5; done-2):** Given a queue with 2 configs and 5 traces added from a filter, when reviewer R1 works it using `⌘↵` and `s` in the browser (4 completed, 1 skipped then reopened and completed), then progress reads 100% / "Queue complete", 10 `ANNOTATION` scores exist with `queue_id`, and R1's stats show 5 completed.
- **AC-5 (FR-A4, NFR-2):** Given 1 pending item and 20 concurrent claims, exactly one succeeds. Given R1 holds an item, R2 sees it read-only. Given the lock expires, R2 can claim it.
- **AC-6 (FR-D1–D7; done-3):** Given a 20-item dataset imported from CSV and a prompt with v1 and v2, when two UI experiments run (v1, v2; fake provider; the correctness evaluator), then both runs complete with 20 linked run items each, every item has a trace in `traceloom-experiment` and an `EVAL` score, and the run records `dataset_version`. The same works via SDK `run_experiment` with a Python evaluator (`API` scores with `dataset_run_id`).
- **AC-7 (FR-D8; done-4):** Given AC-6, where v2 scores higher on average but 3 items score lower than v1, then the comparison page marks v2 best on that score, the "only regressed" filter shows exactly those 3 items, and a threshold above their deltas hides them.
- **AC-8 (FR-S4, NFR-3):**
  - Given a `thumbs` boolean config with `public_feedback=true` and allowed origin `https://app.example`, a pk-only `POST /api/public/feedback` from that origin creates a score and returns CORS headers.
  - The same request from another origin gets no CORS headers.
  - A non-public config name returns 400.
  - Using the pk on `GET /api/public/scores` or the ingestion endpoint returns 401.
  - The 61st request in a minute returns 429.
- **AC-9 (FR-S1, S2):** Given a NUMERIC 0–1 config, a score of 1.5 via the API returns 400. Archiving the config hides it from new-annotation pickers, and its existing scores still show. The data type can't be changed once scores exist.
- **AC-10 (FR-S6, S5):** Given traces with `quality` scores 0.2 / 0.8 / none, filtering `quality >= 0.5` returns one trace and sorting by `score:quality` desc puts unscored last, in both the lite and ClickHouse contract tests. The filter survives a reload via the URL.
- **AC-11 (FR-S7):** Given paired `ANNOTATION` and `EVAL` `correct` (boolean) scores on 30 traces with known agreement, the analytics compare shows the correct confusion matrix counts, agreement % and κ (checked against a hand-computed value), and the distribution and trend charts render with table fallbacks. Pearson and Spearman for a numeric pair match `statistics.correlation` within 1e-9.
- **AC-12 (FR-D2, D3, D4):** Editing an item creates version 2, and a run started before the edit reads version 1. A CSV with a malformed JSON cell is imported as a string, with a warning in the preview. "Add to dataset" from an observation prefills its input and output and links the source.
- **AC-13 (NFR-3):** Dataset input, judge reasoning and a CSV cell containing `<script>alert(1)</script>` render as literal text on the item, log, review and comparison pages, and no dialog fires in the browser test.
- **AC-14 (NFR-5):** The whole default suite passes with no network access and no provider keys.

## Open questions carried from intent.md
- Extend the `scores` table rather than replace it, with sources `API|ANNOTATION|EVAL` → **answered** (FR-S3, Data).
- Sampling decided once, and the delay applied in the queue → **answered** (FR-J4 deterministic hash; FR-X1 persisted `run_after`).
- A per-project cap with an env default and an Admin override → **answered** (FR-J6).
- Agreement uses exact-match %, Cohen's κ, Pearson (and Spearman) → **answered** (FR-S7).
- "Regressed" means lower than the baseline beyond a threshold (default any decrease) → **answered** (FR-D8).
- A 10-minute lock timeout, configurable → **answered** (FR-A4).
- Dataset versions are timestamps, and runs record their version → **answered** (FR-D2, D5).

## Flagged concerns
- [x] CONCERN — policy: security / public API (NFR-3) — owner: Kunal — public-key-only feedback endpoint lets anyone with the page source write feedback scores. **Decided by Kunal Mugalkhod (2026-11-09): option (a)** — only configs marked `public_feedback`, per-project CORS origin allowlist, rate limit per key and per IP; feedback scores carry `source=API` with `channel=public_feedback` so analytics can exclude them (FR-S4). Signed per-trace tokens and open score names were rejected.
- [x] CONCERN — policy: privacy / data egress and cost — owner: Kunal — judge rules send live trace content to a third-party provider and spend money daily. **Decided by Kunal Mugalkhod (2026-11-09): option (a)** — creating, editing, enabling and backfilling rules is Admin+ only, audited, with a confirmation naming the provider and model; Members create templates and use them in UI experiments (FR-J3, permissions table).
- [~] ASSUMED: built-in starter template prompts are written from scratch for Traceloom (no wording copied from other tools) and stored in `traceloom/evals/starter.yaml` — why: CLAUDE.md's original-product rule.
- [~] ASSUMED: judge cost for the cap is the sum of costs of traces in the eval and experiment environments for the project today (UTC), plus a pre-call estimate of (prompt tokens × input price + max_tokens × output price) — why: it reuses the Phase 1 cost engine; the estimate prevents overshoot by a whole call.
- [~] ASSUMED: rule matching runs in the worker right after ingestion commits (the same process as ingest), not in a separate scheduler — why: it fits the existing worker; persisted `eval_executions` rows plus the sweeper cover crashes.
- [~] ASSUMED: the variable-mapping path language is a small JSONPath subset (`$.a.b[0]`, `$.messages[-1].content`), not full JSONPath — why: covers real uses without a new dependency or evaluation risk.
- [~] ASSUMED: evaluators in UI experiments run on the item's trace with `{{expected_output}}` bound from the item version, and their scores are `EVAL` with `dataset_run_id` set — why: one judge path for online and offline evaluation.
- [~] ASSUMED: the analytics stats (Pearson, Spearman, κ, percentiles) are computed in Python from at most 50k paired values fetched by the store (sampled above that, with "sampled" shown) — why: portable across SQLite/Postgres/ClickHouse with no numpy dependency.
- [~] ASSUMED: the traces list hides the `traceloom-eval` and `traceloom-experiment` environments by default (selectable in the environment filter), and dashboard totals exclude them unless selected — why: judge and experiment traffic shouldn't skew production metrics.

## Risks
- **Scope:** five feature areas, larger than Phase 2. Mitigation: team mode, in this order: scores/configs/filters → queues and datasets in parallel → judge engine + delayed jobs → experiments + comparison → analytics. Each "done when" journey lands with its own e2e test.
- **Delayed jobs in two queue backends.** Mitigation: persisted `run_after` rows are the source of truth, the queue is only a wake-up, the sweeper is the fallback, and contract tests run against both queue adapters.
- **Lock races on SQLite.** Mitigation: a single conditional UPDATE with rowcount check, plus a concurrency test.
- **Judge output parsing across providers.** Mitigation: structured output where the adapter supports it, JSON-mode fallback with a tolerant parse, and invalid output counted as a retryable failure.
- **Store parity for score filters and analytics** (SQL vs ClickHouse). Mitigation: shared contract tests, and ClickHouse tests behind `TRACELOOM_IT`.
- **Cost runaway:** a misconfigured 100% rule on high traffic. Mitigation: the daily cap (default $10), sampling, Admin-only rules, and the cap banner.
