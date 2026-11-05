# ADR-0008: Idempotent, order-independent event merge

Status: accepted · Date: 2026-11-06

## Decision
Each entity (trace, observation or score) stores `event_ts`, the newest event timestamp applied to it. To apply an event: if `event.timestamp >= event_ts`, its non-null fields overwrite the stored ones. Otherwise its non-null fields only fill fields that are still null. Map fields are merged key by key, and `tags` are unioned. Applying the same event twice gives the same row. An update that arrives before its create still ends with the fields of the update intact, plus the create's fields filling any gaps.

Observations whose trace does not exist yet create a stub trace. An observation without a `trace_id` gets a trace with the observation's id.

## Consequences
The worker reads the current row before writing, in a batch read per job. In ClickHouse it writes the merged row with the new `event_ts`, and `ReplacingMergeTree` keeps the newest.
