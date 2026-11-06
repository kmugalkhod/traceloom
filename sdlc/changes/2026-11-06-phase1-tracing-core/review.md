# Review: Traceloom Phase 1 — tracing core (MVP)

Reviewed: 2026-11-06 · Against: spec.md, plan.md, REVIEW.md · Diff: main..a57f71c (first pass on main..9d0ffd2; re-review of the fix commits 9d0ffd2..a57f71c)

## Tally
Important: 0 · Nit: 18 · Pre-existing: 1

## Important
None open. The first pass found 8 Important findings. Each was checked against the code by the lead, fixed with a failing test first, and confirmed fixed by a focused re-review (bugs and security) of the fix commits:
- [bugs] `web/routers/trace_delete.py`, `trace_actions.py`, `settings_models.py`, `traces.py` — async routes did blocking SQLite work on the event loop. Every other request stalled while one waited on the write lock, which is the likely cause of the ~30 s sign-up hang QA saw. Fixed: plain `def` routes plus a shared `form_data` dependency (22b62a7..06ec613). `tests/api/test_event_loop.py`.
- [bugs] `adapters/lite/sql_trace_store.py` `delete_traces` — the write lock was held for the whole delete-by-filter. Fixed: one commit per chunk.
- [bugs] `auth/deps.py` — the API-key `last_used_at` write held the lock for the whole upload of a slow ingestion request. Fixed: its own short transaction.
- [bugs] `ingest/merge.py`, `cost/pricing.py` — the derived usage/cost `total` depended on event arrival order (ADR-0008). Fixed: re-derived after every merge.
- [security] `web/routers/auth.py`, `services/accounts.py` — anyone could sign up as an invited address without the invite token and get the invited role (including Owner), and invite-only sign-up revealed which addresses had invites. Fixed: a membership comes only from a verified token, with one generic refusal message.
- [security] `web/routers/settings_models.py` `_save` — an Admin could read another project's model definition through the error page (IDOR). Fixed: scoped 404 before parsing.
- [compliance] `tests/e2e/test_journey_first_trace.py` — the AC-1 journey test was missing, so nothing proved that real SDK output is accepted and rendered by the server. Added; it passes.
- [compliance] `plan.md` — Deviations had duplicate blocks, a stale entry and unrecorded file-layout changes. Fixed.

## Nits
1. [security] `web/routers/trace_actions.py` `_highlight` — @mention substitution runs on rendered HTML, so a member name inside a link `href` corrupts the attribute. It isn't exploitable (escaped, and the CSP blocks it). Do the substitution on markdown-it text tokens instead.
2. [security] `services/mail.py` — the console mail fallback keys on an empty `SMTP_HOST`, not on `TRACELOOM_ENV`, so a production deploy without SMTP logs live reset and invite links. FR-A2 says mail falls back to the console, so this follows the spec. Recommend: print links only in `dev`, and warn elsewhere.
3. [security] `config.py` — `seed-demo` creates `demo@example.com` / `demo-password-123` by default. Running it on a public instance leaves a known-password account. Recommend: a random password that is printed, or refuse to seed outside dev.
4. [bugs] `ingest/process.py` — the worker's read and its upsert are separate transactions, and the upsert writes `bookmarked`/`tags`, so a bookmark or tag removal made in between can be overwritten (lost update, small window).
5. [security] `services/invites.py` `accept` — check-then-act. Two simultaneous accepts by the same user can return a 500 (duplicate membership); they can't grant access to anyone else. Recommend an atomic claim, as for reset tokens.

13 more nits:
- reset and invite tokens in URL paths and query strings reach the access log
- stateless logout: a stolen cookie works until it expires
- deeply nested JSON gives a 500, not a 400
- stub-trace name and ghost-stub edge cases for clients that omit `traceId`
- no quarantine for a permanently bad blob in the in-process queue
- the SDK's `TracedStream` isn't finished when user code `break`s without closing it, and `observe` buffers every generator item
- the AC-7 perf test times store calls, not the HTTP routes
- a provided total that equals the derived sum is treated as derived
- chunked delete isn't atomic across chunks, so a failure can leave a partial delete
- `_stamp_last_used` shares one connection under the in-memory `StaticPool`
- `money()` renders a tiny negative value as "< $0.000001"
- a project rename writes no audit row
- the members page has no "Joined" column (recorded deviation)

## Pre-existing
- [security] `services/accounts.py` — with open sign-up, "An account with this email already exists" reveals which addresses are registered (rate-limited).

## Plan conformance
The diff matches plan.md once its Deviations section is included. That section is now complete, with no duplicates, and covers:
- the file layout
- test files that landed under other names
- the server-secret rename (decided by Kunal Mugalkhod)
- the v3 design deviations: no previous-period comparison, no Joined column, timeline duration in the label column
- the review fixes, including the one replaced test (`test_signup_disabled_lets_an_invited_email_register` asserted the insecure invite behaviour; it is replaced by a stricter test)

No test was weakened, skipped or deleted to get green. The compliance pass checked the history.

Open items for the owner:
- `.env.example` couldn't be read by the reviewers or the lead (blocked path). Confirm it lists every `Settings` field, including `TRACELOOM_SERVER_SECRET`, `TRACELOOM_DEMO_EMAIL` and `TRACELOOM_DEMO_PASSWORD`.
- `seed/models.yaml` has no Claude 5 prices, so Claude 5 generations show no calculated cost until they are added, either to the seed or as project model definitions.
- The untracked `HWXC_5naEAIA1bB.jpeg` (the owner's reference image) must not be committed.

Verify on a57f71c: setup PASS, build PASS, test PASS (841 passed, 29 skipped — `TRACELOOM_IT=1` integration tests), lint PASS.

## CLAUDE.md updates
These are proposals for the owner to accept. The lead did not edit CLAUDE.md.
- Architecture is stale. It lists only `traceloom/`, `sdk/python/` and `sdlc/changes/`. Proposed additions:
  - `traceloom/{ports.py, adapters/lite|full, ingest, cost, services, auth, db, web/routers, web/templates}`
  - `examples/`
  - `tests/{api,contract,e2e,perf,unit}`
  - `ARCHITECTURE.md`, `docs/adr/`, `DESIGN.md`
  - the `traceloom` CLI (`migrate`, `worker`, `seed-demo`)
- Commands: add `TRACELOOM_IT=1 .venv/bin/pytest -m it` (full-adapter contracts, needs `docker compose up`) and `traceloom seed-demo`.
- Things Claude gets wrong, each seen in this change:
  - "The server secret is `TRACELOOM_SERVER_SECRET`; `TRACELOOM_SECRET_KEY` is the SDK's project API secret."
  - "Jinja macros that call `csrf()` must be imported `with context`; test forms by posting the token read from the rendered page, never via a header."
  - "Routes that touch the DB are plain `def` (use the `form_data` dependency for forms); never do sync DB work in `async def`."
  - "Never `db.get()` a project-owned row by id in a router; scope it to `access.project.id` before using it, including on error paths."
  - "CSP forbids inline style and script; use `data-*` attributes and static JS."
  - "When merging branches, keep one copy of each plan.md Deviations entry."
