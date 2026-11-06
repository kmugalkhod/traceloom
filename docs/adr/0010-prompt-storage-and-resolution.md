# ADR-0010: Prompt storage and resolution

Status: accepted · Date: 2026-11-06

## Context
Phase 2 adds versioned prompts that apps fetch at run time, labels such as `production` that decide which version an app gets, composition (one prompt including another), and per-version metrics from linked generations.

## Decision
- **Immutable versions.** A save always creates the next version (`previous + 1`, starting at 1) in `prompt_versions`. A version is never edited or deleted on its own; only a whole prompt can be deleted (Admin+, audited). Each version holds its content, a JSON config, a required commit message, the author and a timestamp.
- **Labels are pointers.** `prompt_labels(prompt_id, label) -> version` is the only thing that changes when someone "promotes" a prompt. Moving a label is one row update plus an audit row and a webhook event. Protected labels (`production` by default, seeded per project) can only be moved by an Admin or by an API key created with the "may move protected labels" flag.
- **`latest` is computed, never stored.** It means the highest version number, so it cannot be set by hand and cannot drift.
- **Reads default to `production`, then `latest`.** With neither label nor version given, the API and SDK return the `production` version and fall back to the newest version when no `production` label exists.
- **Composition is resolved at read time.** Content may contain `@@@prompt:<name>|label=<l>@@@` or `|version=<n>@@@`. The stored content keeps the reference; fetching (API, SDK, preview) expands it recursively. The depth limit is 5, a cycle or a missing reference is an error that names the reference, and only text prompts can be included. The versions actually used are returned as `dependencies`, and the dependencies seen at save time are stored on the version. Saving rejects a missing reference or a cycle up front. A later label move can still create a cycle, which then fails on read with the same error. The resolver is injected, so the composition code has no database access.
- **Metrics are computed at query time.** A generation is linked to a version through `prompt_name` and `prompt_version` on the observation. Counts, median latency, tokens, cost and score averages for a version are queried from the trace store with an index on `observations(project_id, prompt_name, prompt_version)` (lite) and a skip index on `prompt_name` (ClickHouse). Nothing is counted at ingest.
- **Everything is project-scoped.** Every prompt query filters by `project_id`, and every write checks the actor's permission before writing anything, so a refused label move leaves no audit row and no webhook event.

## Consequences
- Rolling back is a label move, not a data change, and old versions stay inspectable and diffable.
- Reads cost a few small queries (label, version, and one per included prompt). The prompt cache in the SDK absorbs the volume.
- Query-time metrics avoid double bookkeeping. They can be materialised later if the index stops being enough.

## Alternatives considered
- Mutable versions with soft history: rejected, the spec requires immutability and labels cover "change what production gets".
- Materialised per-version counters updated at ingest: rejected for now, as above.
- Expanding composition at save time: rejected, because a referenced prompt moving its label should reach the including prompt without a new version.
