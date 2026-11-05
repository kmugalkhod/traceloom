# ADR-0001: ClickHouse for trace data (full), SQL for lite

Status: accepted · Date: 2026-11-06

## Context
Traces, observations and scores are append-heavy and high-volume (100M+ rows). Reads are analytical: time-bounded lists, percentiles, grouped sums. Organizations, keys, prompts and settings are small and transactional.

## Decision
- In full mode, traces, observations and scores go to **ClickHouse**. Each table is a `ReplacingMergeTree(event_ts)` keyed by `(project_id, …, id)` and partitioned by month. Reads use `FINAL` (or `argMax`) wherever exactness matters: detail pages and dashboards.
- Transactional data lives in Postgres (SQLAlchemy + Alembic).
- Lite mode keeps trace data in the same SQL database, behind the same `TraceStore` port (ADR-0004).

## Alternatives
- Postgres only, with partitioning: simple, but percentile and grouped scans over 10M+ rows miss the < 2 s dashboard target without pre-aggregation. Kept as lite mode.
- TimescaleDB: a better Postgres for time series, but its columnar compression and joins are weaker for wide JSON payloads.

## Consequences
- Two query dialects behind one port. Each method has a lite and a ClickHouse implementation, and both pass a shared contract test suite.
- Eventual consistency: merges happen in the background, so reads must deduplicate (`FINAL`).
