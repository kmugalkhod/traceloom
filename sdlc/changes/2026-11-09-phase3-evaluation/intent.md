# Intent: Traceloom Phase 3 — Evaluation

Author: Kunal Mugalkhod · Date: 2026-11-09 · Status: accepted · Record: prompt.md "Phase 3 — Evaluation" (lines 265–310)

<!-- Status: draft -> accepted | rejected. Only the product owner changes it. -->

## Problem
Traceloom (Phase 1 tracing, Phase 2 prompts and playground, plus the UI polish pass) shows what an LLM app did and which prompt produced it. It cannot yet say whether the output was **good**.
- **No quality signal:** scores can be ingested (a `scores` table exists), but nothing defines what a score means, end users can't send feedback, and scores can't be filtered, sorted or analysed.
- **No human review:** domain experts have no structured way to work through traces and label them, so labelling happens in spreadsheets.
- **No automatic grading:** nobody can grade live traffic at scale, so regressions surface as user complaints.
- **No offline testing:** a prompt or model change can't be tested against a fixed set of inputs before it ships, and nothing shows which cases got worse.

## Proposed outcome
**Scores**
- **Score configs:** name, data type (numeric, categorical, boolean), range or categories, and a description. Configs can be archived.
- **Creating scores:** through the public API, the SDK and the UI.
- **End-user feedback:** a browser snippet or endpoint lets an end user send 👍/👎 using only the public key.
- **Where scores appear:** on traces, observations, sessions and lists, where they can be filtered and sorted.
- **Score analytics page:**
  - the distribution of each score
  - its trend over time
  - two scores compared: correlation (numeric), a confusion matrix (categorical), and agreement between human and LLM-judge scores

**Annotation queues**
- A queue has a description and the score configs reviewers fill in.
- Traces, observations or sessions are added by hand or in bulk from a list filter.
- Reviewers work item by item, with keyboard shortcuts for next, skip and complete. An item is locked to one reviewer at a time.
- Each queue shows its progress and per-reviewer stats.

**LLM-as-a-judge (online evals)**
- **Evaluator templates:** a prompt, an output schema (score plus reasoning) and a model from the project's LLM connections.
  - A starter library ships with hallucination, relevance, toxicity, helpfulness, correctness against an expected output, and conciseness.
  - Users can create their own templates and version them.
- **Run rules:**
  - target: traces, or observations of a given name or type
  - a filter
  - a sampling percentage
  - a delay
  - variable mapping from input, output, metadata or a child observation
- **Execution:**
  - Jobs run in the worker with retries and a configurable per-project daily cost cap.
  - Each run writes a score with source `EVAL`.
  - The judge's own LLM call is recorded as a trace, so its cost is visible.
  - An execution log shows each job's status and any errors.

**Datasets and experiments**
- **Datasets:** items with input, expected output and metadata. Edits are versioned, and items can be archived.
  - Items can be imported from CSV or JSON, or added from a trace or observation in one click.
- **SDK experiments:** `run_experiment(dataset, task_fn, evaluators=[…])` creates a dataset run, and each item's trace is linked to it.
- **UI experiments:** pick a dataset, a prompt version, a model and evaluators, and the worker runs it.
- **Run comparison page:** runs side by side per item (output, scores, latency, cost), aggregate scores per run, and the items that regressed.

## Affected users and systems
- **Users:**
  - Developers send scores and run experiments through the SDK and API.
  - Domain experts and reviewers work annotation queues.
  - Prompt engineers and product people set up judges, datasets and UI experiments, and read the analytics.
  - End users send feedback from the customer's app.
  - Admins manage configs, cost caps and LLM connections. Viewers are read-only.
- **Systems:**
  - The web server: new pages and APIs for score configs, analytics, queues, evaluators, datasets and runs.
  - The meta DB: new tables, plus columns on the existing `scores` table.
  - `TraceStore` in both lite and full (ClickHouse) mode: score filters and sorts on lists, plus analytics queries.
  - The worker: judge jobs and UI experiment runs.
  - The Phase 2 LLM layer: connections and providers.
  - The prompt service: the prompt versions used by experiments.
  - The Python SDK: scores, datasets and `run_experiment`.

## Constraints
- **Project ruleset (CLAUDE.md):**
  - Every query is project-scoped and permission-checked.
  - User content renders escaped.
  - Nothing is hardcoded, including model names, cost caps and sampling.
  - Lite mode is the default in tests.
  - This is an original product: no Langfuse code, text, logos or styling.
- **UI:** follow the current DESIGN.md (v3.2, after the UI polish pass) and the CSP: no inline script or style.
- **Public feedback endpoint:** it accepts only a narrow score shape and is rate-limited. The public key can't read data or write anything except feedback scores.
- **Offline tests:** judge and experiment tests use the fake provider and make no network calls.
- **Judge safety:** the per-day cost cap stops jobs once it is reached and records why. Retries are bounded, and a judge failure never affects ingestion.
- **Data model:** dataset item edits keep history, existing scores keep working (configs are optional), and a migration handles existing data.
- **Branching:** this change is stacked on `team/2026-11-06-ui-polish-align`, which contains Phase 1, Phase 2 and the UI polish. It merges after those two.

## Out of scope
- Phase 4+ work: custom dashboards, the AI gateway, the JS SDK, and export or retention policies.
- A JS/TS SDK. The browser feedback snippet is a small standalone script, not an SDK.
- Human-in-the-loop prompt optimisation, auto-tuning and RL-style training loops.
- Multi-modal judges such as image inputs.

## Success measure
The "Phase 3 done when" journeys, each covered by an automated test (API, plus browser where UI is involved):
1. A judge rule scores sampled live traces automatically: an `EVAL` score is created, the judge trace is visible with its cost, and the execution log shows success.
2. A reviewer finishes an annotation queue: items are locked to one reviewer, the shortcuts work, and progress reaches 100%.
3. Two prompt versions run on a 20-item dataset, as two runs from the UI or SDK, with traces linked.
4. The comparison page shows which version wins on aggregate scores and lists the items that got worse.

Also:
- 👍/👎 sent with only a public key creates a score, and the same key can't read anything.
- The daily cost cap stops further judge jobs and logs the reason.
- The analytics page shows the distribution, the trend, and a comparison of two scores, with a confusion matrix and agreement figures.

## Open questions
- ASSUMED: the existing `scores` table is extended (config id, author, queue id, execution id) rather than replaced, and the existing `source` values become `API`, `ANNOTATION` and `EVAL`.
- ASSUMED: sampling is decided once per trace or observation at ingest, and the delay is applied in the worker queue (lite mode: an in-process delayed queue; full mode: a Dramatiq delay).
- ASSUMED: the daily cost cap is per project. A global default comes from env, and Admins override it in project settings.
- ASSUMED: the agreement metric is the share of exact matches for categorical scores and Cohen's kappa, with Pearson correlation for numeric scores.
- ASSUMED: "regressed" means an item's score in the candidate run is lower than in the baseline run by more than a threshold the user sets (default: any decrease).
- ASSUMED: the annotation lock expires after a configurable timeout (default 10 min), so an abandoned item returns to the queue.
- ASSUMED: dataset versions are snapshots by timestamp. A run records the dataset version it used.
