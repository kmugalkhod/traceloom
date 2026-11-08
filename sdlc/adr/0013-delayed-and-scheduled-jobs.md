# ADR-0013: Delayed and scheduled jobs (persisted rows first, queue as wake-up)

Status: accepted · Date: 2026-11-09 · Change: sdlc/changes/2026-11-09-phase3-evaluation

## Context
Phase 3 adds two job types that must run later, not now:
- `eval_job`: a judge call that runs `delay_seconds` (default 30) after a trace or observation first matches a rule, and retries with backoff on failure (FR-J4, FR-J5).
- `experiment_item`: one dataset item of a UI experiment, run as soon as possible and resumed after a worker restart (FR-D7, NFR-4).

The `Queue` port (ADR-0004) only has `enqueue(message)`. Lite mode runs an in-process queue that forgets everything on restart. Full mode runs one Dramatiq actor (`process_batch`) on one Redis queue (`traceloom-ingest`) with 4 threads. A judge call can take up to `TRACELOOM_PROVIDER_TIMEOUT_SECONDS` (120 s). Webhooks (ADR-0012) already showed the pattern that works here: a row with `next_attempt_at`, plus the sweeper.

## Options considered
1. **Queue-native delay only.** Dramatiq `send_with_options(delay=…)` in full mode, an in-memory timer in lite mode, and nothing in the DB.
   - Pro: no new tables or sweeper code.
   - Con: lite mode loses every delayed job on restart. In full mode a lost Redis message, or a message that runs out of retries, silently drops the job. There is also no execution log to build FR-J8 on.
2. **A DB-polling scheduler only.** Persist rows with `run_after`; a loop polls due rows every N seconds and runs them. No queue involvement.
   - Pro: one mechanism for both modes, durable.
   - Con: latency equals the poll interval. Polling at 1 s to make AC-1's 1 s delay feel right puts constant load on the meta DB. Parallelism also has to be built from scratch, outside the queue's worker threads.
3. **Persisted rows are the source of truth, the queue is a wake-up, and the sweeper is the fallback.** (chosen) This is the webhook pattern (ADR-0012) extended to delays.
   - Pro: durable in both modes, low latency when the queue works, and it reuses the existing sweeper and handler registry.
   - Con: duplicate wake-ups are normal, so every handler must claim its row atomically. There is one more sweeper query.

## Decision
- **Rows.** `eval_executions` and `dataset_run_items` carry `status`, `attempts`, `run_after` and `started_at`, indexed on `(status, run_after)`. A row is created (or rescheduled) in the same transaction that decides the job must run.
- **Port change.** `Queue.enqueue(message, delay_seconds=0)`. Message types `eval_job {execution_id}` and `experiment_item {run_item_id}` join `ingest` and `webhook_delivery` in `queue_registry.py`. Messages carry ids only, never content.
- **Lite adapter.** A **timer lane**: a min-heap of `(due_at, message)` served by one timer thread. When a message is due it moves into an `eval` side lane with `TRACELOOM_EVAL_CONCURRENCY` worker threads (the existing lane mechanism). A full lane or a restart drops messages, and the sweeper recovers them from the rows.
- **Full adapter.** A second Dramatiq actor `run_eval_job` on its own Redis queue `traceloom-eval`, consumed by a second `dramatiq.Worker` in the worker process with `TRACELOOM_EVAL_CONCURRENCY` threads. Delays use `send_with_options(delay=ms)`, which goes through the Redis broker's delay queue. `max_retries=0`: retries are owned by the row (below), not by Dramatiq, so the two retry schedules never stack. Ingest keeps its own queue and threads, so a slow provider never delays ingestion.
- **Claiming.** A handler first runs `UPDATE … SET status='running', started_at=now, attempts=attempts+1 WHERE id=:id AND status='pending' AND run_after <= now`. If rowcount is 0 it returns quietly. That makes duplicate wake-ups harmless.
- **Retries.** A retryable failure sets `status='pending'`, `run_after = now + backoff(attempts)` with jitter, and re-enqueues with the delay. When `attempts` reaches `TRACELOOM_EVAL_MAX_ATTEMPTS` the row is `failed`.
- **Sweeper.** In **both** modes (unlike webhooks), each sweep re-enqueues at most `TRACELOOM_EVAL_SWEEP_BATCH` rows that are either
  - `pending` with `run_after` older than one sweep interval (never enqueued, dropped, or lost), or
  - `running` with `started_at` older than `TRACELOOM_EVAL_STUCK_SECONDS` (worker died mid-call). These are reset to `pending` first, and their cost reservation is released (ADR-0014).

## Consequences
- A restart, a lost Redis message, a full lane or a crashed worker only delays a job by up to one sweep interval plus `TRACELOOM_EVAL_STUCK_SECONDS`.
- At-least-once execution. The judge call itself can run twice if a worker dies after the provider answered but before the row was updated. The score write is idempotent (ADR-0014), but that call's cost is paid twice. This is accepted and bounded by the cap.
- The contract suite gains delayed-enqueue tests for both queue adapters (the full adapter uses Dramatiq's `StubBroker`).
- `.env` gains `TRACELOOM_EVAL_CONCURRENCY`, `TRACELOOM_EVAL_STUCK_SECONDS` and `TRACELOOM_EVAL_SWEEP_BATCH`.

## Revisit when
- Scheduled (cron-like) jobs arrive, for example retention in Phase 4. Add a `schedules` table driven by the same sweeper before reaching for a separate scheduler.
- Pending executions regularly exceed about 100k rows, or the sweeper query shows up in meta-DB load. Then partition the rows by status or move the due-row scan to a dedicated index-only query.
- Experiments need far more parallelism than one worker process provides. Then run the `traceloom-eval` queue in its own Compose service.
