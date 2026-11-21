# ADR-0015: Public feedback endpoint

Status: accepted · Date: 2026-11-09 · Change: sdlc/changes/2026-11-09-phase3-evaluation

## Context
End users of a customer's app send 👍/👎 straight from the browser, so the request can only carry the project's **public** key, which anyone can read in the page source (FR-S4). The product owner decided (2026-11-09, option a) that feedback:
- is accepted only for score configs marked `public_feedback`,
- gets CORS headers only for origins in a per-project allowlist, and
- is rate-limited per key and per client IP.

Signed per-trace tokens and open score names were rejected. This ADR records how that decision is built, not whether to make it.

Two facts shape the build:
- A CORS preflight (`OPTIONS`) carries the *names* of custom headers but not their values. A preflight for a request using `X-Traceloom-Public-Key` therefore cannot tell which project it is for.
- CORS is enforced by browsers only. `curl` ignores it, so CORS is not an authentication control.

## Options considered
**Authentication**
1. Reuse `project_key` and let it accept a pk with no secret.
   - Pro: the least code.
   - Con: one bug away from pk-only reads on every route.
2. **A separate dependency `feedback_key` returning a distinct `FeedbackPrincipal` type.** (chosen) Routes that take `ResolvedKey` cannot accept it, and `project_key` keeps rejecting pk-only with 401.

**Preflight with an unknown project**
1. Put the pk in the query string so the preflight can resolve the project.
   - Pro: an exact per-project answer.
   - Con: changes the spec's header contract, and puts the key into access logs.
2. **Answer the preflight from the union of all projects' allowlists (cached), and enforce the project's own list on the POST.** (chosen)
   - Pro: keeps the header contract.
   - Con: a preflight can reveal that an origin is allowed by *some* project.
3. Echo any origin on the preflight.
   - Con: breaks "only allowed origins receive CORS headers".

**Write path**
1. Write directly to `TraceStore` in the request.
   - Con: an anonymous caller drives synchronous trace-store writes.
2. **Validate, then hand a `score-create` event to `accept_batch`.** (chosen) This is the blob-first ingest path (ADR-0002): one blob PUT and one enqueue, durable, and no trace-store write on the public request path.

## Decision
- **Route.** `POST /api/public/feedback` and `OPTIONS /api/public/feedback`, with the body `{id?, trace_id, observation_id?, name, value, comment?}`.
- **`feedback_key` dependency.**
  - It reads `X-Traceloom-Public-Key`, resolves it to the project through the existing key cache (a revoked key fails), and returns `FeedbackPrincipal(project_id, key_id)`.
  - It never reads `Authorization` and never yields `ResolvedKey`.
  - An Authorization header with a pk and no secret keeps getting 401 from `project_key` everywhere else (AC-8).
- **Checks, in order** (all before any write):
  1. Rate limits through the `RateLimiter` port: `feedback:key:<key_id>` and `feedback:ip:<client_ip>`, each at `TRACELOOM_RATE_FEEDBACK`. Over the limit returns 429 with `Retry-After`.
  2. If the request has an `Origin` header and that origin is not in the project's `feedback_origins`, return 403 with no CORS headers.
  3. `name` must match a non-archived config with `public_feedback=true`, and the value must be valid for it (FR-S2). Otherwise 400.
  4. The comment must be at most 1,000 characters.
- **Write.**
  - `score_id = uuid5(NAMESPACE, f"{project_id}:{trace_id}:{name}:{client_id}")` when the client sends an `id`, and a random id otherwise. The same trace, name and client id therefore replace the earlier score through the ADR-0008 merge.
  - The score carries `source=API` and `channel=public_feedback`.
  - The endpoint responds `202`. It does not check that `trace_id` exists, because a trace can arrive after its feedback (ADR-0008 stub semantics) and an existence check would let anyone probe trace ids.
- **CORS.**
  - The preflight is handled by the route itself, not Starlette's global `CORSMiddleware`.
  - An allowed origin receives `Access-Control-Allow-Origin: <origin>`, `Vary: Origin`, `Allow-Methods: POST`, `Allow-Headers: Content-Type, X-Traceloom-Public-Key` and `Max-Age: 600`. Credentials are never allowed.
  - A disallowed origin gets a plain 204 with no CORS headers.
  - "Allowed" means: for the preflight, the origin is in the union set cached from all `project_eval_settings.feedback_origins` (refreshed on change and on TTL); for the POST, it is in the project's own list.
  - No other route emits CORS headers.
- **Client IP.** The direct peer, as the auth limiter does today. Behind a proxy, operators run uvicorn with `--proxy-headers` and set `FORWARDED_ALLOW_IPS` to the proxy, so `X-Forwarded-For` is trusted only from it.
- **Snippet.** `traceloom/web/static/feedback.js`: no dependencies, under 3 KB, no `eval`, served static. It is loaded cross-origin by `<script src>`, which needs no CORS. Project settings show copy-paste instructions, including the customer-side CSP `connect-src` entry it needs.

## Consequences
- Anyone holding the pk can still script feedback from outside a browser. What limits the damage is the gating (only `public_feedback` configs, a narrow shape), the per-key and per-IP limits, and the `channel` tag that lets analytics exclude feedback. This is the accepted risk from the owner's decision.
- An empty allowlist (the default) means browsers get no CORS headers, so browser feedback stays off until an Admin adds an origin.
- Feedback is eventually visible (the queue lag), like any ingested score.

## Revisit when
- Abuse shows up despite the limits. Then add the rejected signed per-trace token as an opt-in "strict" mode.
- A JS SDK ships (Phase 4+). Then fold the snippet into it, keeping the endpoint contract.
- Many projects configure origins and the union preflight set grows large. Then move to an indexed lookup.
