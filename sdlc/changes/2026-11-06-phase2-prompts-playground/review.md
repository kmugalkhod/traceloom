# Review: Traceloom Phase 2 — prompt management and playground

Reviewed: 2026-11-06 · Against: spec.md, plan.md, REVIEW.md · Diff: main..907da96 (first pass on main..de54e0e; two re-reviews of the fix commits de54e0e..907da96)

## Tally
Important: 0 · Nit: 12 · Pre-existing: 1

## Important
None open. Across three review rounds, 11 Important findings were raised. Each was checked against the code by the lead, fixed with a failing test first, and confirmed by a re-review:
- [security] **LLM connections SSRF:** the SSRF check result was discarded, the SDKs re-resolved DNS (rebinding), and redirects were followed. Fixed: `llm/http.py`, a pinned transport per request with Host/SNI, re-checked on every hop. NAT64 and 6to4 are unwrapped.
- [security] **Prompt composition fan-out DoS:** 20 references × 5 levels was about 3M expansions. Fixed: 100 expansions and 1M characters per resolution (running total), memoised lookups, 200k characters per version, 2 MB public write bodies.
- [security] **Bedrock custom endpoint** (found at re-review): botocore re-resolves DNS and honours env proxies. Fixed: only `https://*.amazonaws.com(.cn)` endpoints, and `proxies={}`.
- [bugs] **Cached tokens charged twice** for OpenAI, Azure, compatible endpoints and Google. Fixed: input = prompt − cached. Two provider tests encoded the double count and were corrected.
- [bugs] **"Send test" failed on a disabled webhook.** Fixed.
- [bugs] **Webhook delivery shared the single ingestion thread,** so a slow endpoint stalled ingestion. Fixed: a separate bounded lane or pool, a 10 s timeout with a 3 s connect limit, 20 per sweep.
- [compliance] **AC-4 couldn't hold:** Claude 5-family models reject temperature. AC-4 was amended by Kunal Mugalkhod, and `test_rerun_of_a_claude5_generation_drops_temperature` was added.
- [compliance] **Plan deviations were missing:** `PARAM_RULES`, the app.js menu and nav changes, the unlisted files. They're now recorded.
- [compliance] **Shared Phase 1 behaviour changed without a record.** Now recorded, and the owner-requested Phase 1 restructure (dashboard Breakdown tabs, "..." menus) is built.

QA during the build also caught and fixed:
- the include-cycle hole (T-5);
- the playground disconnect not stopping the provider stream (T-23, two rounds; root cause: adapters didn't close the SDK stream);
- tool messages dropped on replay (T-25);
- mobile menu and layout bugs (T-7, T-9, T-24, T-27);
- the compare view hiding the shared editor (T-26).

## Nits
1. [security] Redirects: the provider SDKs set `follow_redirects=True` per request, so redirects are followed. Each hop is re-resolved and re-checked by the pinned transport (metadata and link-local are always blocked; private is allowed by policy). Docs that say "never follows redirects" are imprecise, but there's no bypass.
2. [security] Webhook receivers get no guidance to verify with a constant-time compare and reject stale timestamps, and there's no rotate-secret action (only delete and recreate).
3. [security] Webhook audit detail stores the full URL, which may include query-string tokens. Store the host and path only.
4. [security] Each playground run holds a thread-pool thread for up to 120 s, so the per-start rate limit doesn't cap concurrent runs. Add a per-user concurrent-run cap.
5. [bugs] The SDK cold cache with the server down blocks each `get_prompt` for up to timeout × (retries+1), because fallbacks aren't negative-cached. The spec allows it.

7 more nits:
- the web prompt editor forms have no body-size cap (content is capped at 200k characters);
- the "..." menu item contexts lack open-state screenshots for some Phase 1 pages;
- hard-coded hex colours for provider tiles in `app.css`;
- the trace-detail Environment/Release/Version row was added beyond the mockup;
- ClickHouse queries for prompt metrics are untested against a real server (`TRACELOOM_IT=1`);
- the live provider smoke tests are deferred;
- `ruff format --check` flags 3 test files (verify runs `ruff check` only).

## Pre-existing
- [compliance] `claude-haiku-4-5` keeps `valid_from: 2024-01-01` from the Phase 1 seed, against FR-X3's 2026-01-01. Kept deliberately, and recorded in plan.md.

## Plan conformance
The diff matches plan.md including its Deviations section. The section covers:
- the app-wide design refresh and Phase 1 restructure (requested by Kunal);
- the queue registry, the ClickHouse migrations file and cycle-check-before-write;
- `PARAM_RULES`, the webhook retry-by-sweep and the Load prompt dialog;
- shared menu and nav behaviour, renamed tests and unlisted files;
- three rounds of review fixes.

Spec AC-4 and AC-8 are amended with the owner's decision.

No test was weakened, skipped or deleted to get green. Four existing assertions changed, each a correction or a markup rename, and each is recorded:
- two provider tests that encoded the cached-token double charge;
- two `data-copy` → `data-copy-from` assertions.
One Phase 1 sign-up test that asserted the old insecure invite behaviour was replaced in Phase 1.

**Verify on 907da96:** setup PASS, build PASS, test PASS (1587 passed, 35 skipped — `TRACELOOM_IT=1` integration tests), lint PASS.

**Open for the owner:**
- add the `.env.example` lines listed at the end of plan.md;
- revoke the GitHub token pasted in chat;
- don't commit `HWXC_5naEAIA1bB.jpeg`.

## CLAUDE.md updates
These are proposals for the owner. The lead didn't edit CLAUDE.md.
- **Architecture:** add these lines:
  - `traceloom/prompts/`: versioned prompts, labels, composition, diff.
  - `traceloom/llm/`: LLM connections, Fernet crypto, SSRF guard and pinned HTTP, six provider adapters, run engine.
  - `traceloom/webhooks/`: signed, queued, retried delivery.
  - `traceloom/queue_registry.py`: message-type dispatch.
- **Things Claude gets wrong** (each seen in this change):
  - "Claude 5-family models take no temperature/top_p; when a spec AC can't hold for a model, amend the AC with the owner, don't silently test a different model."
  - "Outbound HTTP to user-supplied URLs goes through `llm/http.py` (pinned, re-checked per hop) or the webhook sender — never a bare SDK client."
  - "Any change to shared behaviour (app.js, base templates, CSS tokens) goes in plan Deviations."
  - "Teammates: run verify in the foreground; never `git stash` a shared working tree."
