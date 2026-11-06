# Spec: Traceloom Phase 2 — prompt management and playground

From: intent.md (b802821) · Status: accepted · Record: prompt.md "Phase 2"
Skills applied: claude-api (Claude model IDs and list prices for the seed and the Anthropic provider adapter); impeccable (UX guidance for the design step); REVIEW.md security pass
Architecture: ARCHITECTURE.md + docs/adr/0001–0009 (Phase 1). New ADRs: 0010 (prompt storage and resolution), 0011 (LLM connections and provider adapters), 0012 (webhook delivery).

## Summary
Add versioned prompt management (UI, public API, SDK, linking, per-version metrics, labels with protected `production`, signed queued webhooks) and an LLM playground (project LLM connections to six providers with encrypted keys, chat editor, streaming, open-from-trace, save-as-version, 4-way compare). Everything is built on the Phase 1 stack and conventions; lite mode stays the test default and the suite stays offline.

## Functional requirements

### Prompts (P)
- **FR-P1** Members+ can create a prompt in a project. The prompt has:
  - a `name`, unique per project, matching `[A-Za-z0-9_.\-/]{1,200}`; `/` builds folders;
  - a `type`: `text` or `chat`. A chat prompt is a list of `{role: system|user|assistant|placeholder, content}`;
  - `tags`.
  - The first save creates version 1.
- **FR-P2** Each save creates a new **immutable** version (`version` = previous + 1) holding:
  - the content;
  - a JSON `config` (an object; for example `model`, `temperature`, `max_tokens`, `tools`, `response_format`);
  - a required commit message;
  - the author and a timestamp.
  - Versions can never be edited or deleted one at a time. Deleting a whole prompt is Admin+ and audited.
- **FR-P3** Variables.
  - Every `{{name}}` in the content (name matches `[A-Za-z_][A-Za-z0-9_]*`) is a variable.
  - The editor highlights them and lists them.
  - `compile(**vars)` substitutes them. Unknown variables are left as-is, and missing ones are reported, never an error.
- **FR-P4** Labels.
  - A label (`[a-z0-9_\-]{1,36}`) points at exactly one version of a prompt.
  - `latest` is automatic: it always means the newest version and can't be set by hand.
  - Moving a label is one click in the UI, or one API call.
  - **Protected labels**: `production` is protected by default, and Admins can mark other labels protected per project. Only Admin+ can move or remove a protected label. Members+ can move the others.
  - Every move writes an audit row (actor, prompt, label, from-version, to-version) and enqueues a webhook (FR-P10).
- **FR-P5** Diff between any two versions of a prompt:
  - text: a line diff; for chat prompts, each message in turn;
  - config: a key-level diff of the JSON.
  - Both are rendered escaped.
- **FR-P6** Composition.
  - Content can include `@@@prompt:<name>|label=<label>@@@` or `@@@prompt:<name>|version=<n>@@@`.
  - The reference is replaced with the referenced prompt's text content when the prompt is fetched (API and SDK) and when it is previewed.
  - Resolution is recursive with a depth limit of 5 and cycle detection. A missing reference or a cycle is an error naming the reference.
  - Only text prompts can be included. In a chat prompt, references inside message content are resolved.
  - The resolved result records the dependency versions it used.
- **FR-P7** The prompts list has:
  - a folder tree (from `/` in names);
  - search on name, tag and content (case-insensitive substring);
  - filters by tag and label;
  - sorting by name or updated time.
  - Each row shows its labels and the latest version.
- **FR-P8** Per-version metrics on the prompt page, for the selected date range and environment:
  - the count of linked generations;
  - median latency, total tokens and total cost;
  - the average of each numeric score on the linked generations' traces;
  - a list of the linked generations (most recent 50, each linking to its trace).
  - A generation is linked when its `prompt_name`/`prompt_version` match (FR-S3).
- **FR-P9** Public API, under HTTP Basic project keys like ingestion:
  - `GET /api/public/v2/prompts/{name}?label=<l>|version=<n>[&resolve=true]`. With neither parameter it returns `production`, falling back to `latest` if there is no `production` label. The response holds name, type, version, prompt (resolved by default), config, labels, tags, commit message and dependencies.
  - `GET /api/public/v2/prompts?name=&label=&tag=&page=&limit=` lists metadata only.
  - `POST /api/public/v2/prompts` creates a new version, creating the prompt if it is new; the body may include `labels`.
  - `PATCH /api/public/v2/prompts/{name}/versions/{version}` sets `newLabels`.
  - Names containing `/` are URL-encoded.
  - Key-based writes act as the project's "API key" actor. Protected labels can't be moved by a key unless the key was created with "may move protected labels" (Admin-only option).
  - The error envelope is the Phase 1 one.
- **FR-P10** Webhooks.
  - Admins configure per project:
    - a URL;
    - a secret, shown once and stored encrypted;
    - enabled/disabled;
    - events: `prompt.label_moved` (and `prompt.version_created`).
  - Each event is enqueued and delivered by the worker:
    - with a JSON body and `X-Traceloom-Signature: t=<unix>,v1=<hex HMAC-SHA256(secret, t.body)>`;
    - with retries on network errors, timeouts and 5xx: 5 attempts with exponential backoff and jitter, 10 s timeout per attempt.
  - The project's webhook page shows each delivery (status, attempts, response code, last error) and a "Send test" button.
  - URLs must be `https://` (or `http://` in dev). Hosts are resolved and refused if they point at private, loopback, link-local or metadata addresses, unless `TRACELOOM_WEBHOOK_ALLOW_PRIVATE=true`. The check runs at save and at every delivery.

### SDK (S) — `traceloom_sdk`
- **FR-S1** `client.get_prompt(name, *, label=None, version=None, type=None, cache_ttl_seconds=None, fallback=None, max_retries=2, fetch_timeout_seconds=5)` returns a `TextPrompt` or `ChatPrompt`.
  - There is an in-process cache keyed by `(name, label|version)`. The default TTL is 60 s, configurable with `TRACELOOM_PROMPT_CACHE_TTL`; `0` disables it.
  - **Stale-while-revalidate**: after the TTL, the stale value is returned straight away and refreshed in a background thread.
  - With no cached value, a fetch failure returns a prompt built from `fallback` (marked `is_fallback=True`). If there is no fallback either, it raises `PromptNotFound`. This is the only SDK call allowed to raise, because the caller has nothing to run otherwise.
  - Fetches never block longer than the timeout × the retries.
- **FR-S2** `prompt.compile(**vars)` returns a `str` (text) or a list of messages (chat) with `{{vars}}` substituted.
  - Prompt objects expose `name`, `version`, `config`, `labels`, `variables` and `is_fallback`.
- **FR-S3** Linking. Passing `prompt=<prompt>` to `start_generation`, `update_current_observation` or `@observe(as_type="generation")` (via `update_current_observation`) sets `prompt_name` and `prompt_version` on the generation.
  - A fallback prompt is not linked.
  - The OpenAI and Anthropic wrappers accept a `traceloom_prompt=` keyword and strip it before calling the provider.
- **FR-S4** Async variants: `await client.aget_prompt(...)`.

### Playground and LLM connections (L)
- **FR-L1** LLM connections (project settings, Admin+). Each connection has:
  - a provider: `anthropic`, `openai`, `azure_openai`, `bedrock`, `google`, or `openai_compatible`;
  - a display name;
  - credentials:
    - an API key (anthropic, openai, google, openai_compatible); or
    - for azure_openai, a key plus endpoint, deployment map and API version; or
    - for bedrock, an access key, secret and region;
  - an optional base URL (required for openai_compatible);
  - a list of custom model names;
  - an optional headers map.
  - Secrets are encrypted at rest with `TRACELOOM_ENCRYPTION_KEY` (FR-X1), shown once as a masked hint (`…last4`), and can be replaced but never read back.
  - Members+ can *use* connections in the playground but can't see or edit their settings.
- **FR-L2** Each provider has a built-in default model list (current Claude, GPT and Gemini models) plus the connection's custom models. A "Test connection" action makes one minimal call and reports success or the provider's error message, with secrets scrubbed.
- **FR-L3** The playground (Members+) is a chat editor with:
  - system, user and assistant messages, which can be reordered, added and removed;
  - `{{variables}}` with an inputs panel;
  - model parameters: temperature, max tokens, top_p, stop. Only the parameters the chosen model supports are shown;
  - tool definitions (JSON schema, editable, with validation);
  - an optional structured-output JSON schema.
- **FR-L4** Run streams the response to the page as it is generated, then shows:
  - the final text and any tool calls;
  - input, output and total tokens;
  - cost (Phase 1 cost engine, `cost_source=calculated`);
  - latency and time to first token.
  - Provider errors show inline without leaking secrets. Stop cancels the stream.
- **FR-L5** "Open in playground" on any generation in the trace detail opens the playground pre-filled with that generation's exact input messages, model, model parameters and tools (from `model_parameters`/`input`). The connection is the project's connection for that provider if one exists; otherwise the user picks one.
- **FR-L6** Save to prompt: save the current messages and config as a new version of an existing chat prompt, or as a new prompt, with a commit message. Members+ can do this, but labels follow FR-P4.
- **FR-L7** Compare: up to 4 variants side by side.
  - Each variant has its own model, parameters and messages, or follows the shared messages.
  - "Run all" runs every variant on the same inputs concurrently and streams each column on its own.
- **FR-L8** By default, playground runs are not stored as traces. A "Trace this run" toggle ingests the run as a trace through the normal pipeline, with environment `playground` and `prompt_name`/`prompt_version` linked when the run started from a prompt.
- **FR-L9** Playground usage limit: `TRACELOOM_PLAYGROUND_RATE` (default `30/minute` per user per project) returns a 429 with a clear message.

### Platform (X)
- **FR-X1** New settings, all in `Settings` and `.env.example`:
  - `TRACELOOM_ENCRYPTION_KEY`: a 32-byte urlsafe-base64 Fernet key. Required outside dev; dev generates an ephemeral one with a startup warning.
  - `TRACELOOM_PROMPT_CACHE_TTL` (SDK side)
  - `TRACELOOM_WEBHOOK_ALLOW_PRIVATE`
  - `TRACELOOM_PLAYGROUND_RATE`
  - `TRACELOOM_LLM_ALLOW_PRIVATE` (FR-L1 base URLs; see the concerns)
  - `TRACELOOM_PROVIDER_TIMEOUT_SECONDS` (default 120)
- **FR-X2** The left nav gains **Prompts** and **Playground**. The settings tabs gain **LLM connections** and **Webhooks**, both Admin+.
- **FR-X3** The model seed gains the current Claude models:
  - `claude-fable-5-1`, `claude-fable-5`: $10 input / $50 output per million tokens; cache read $0.25 for 5.1.
  - `claude-opus-5-5`: $4 / $20; cache read $0.20.
  - `claude-opus-5`, `claude-opus-4-8`, `claude-opus-4-7`, `claude-opus-4-6`: $5 / $25.
  - `claude-sonnet-5-5`, `claude-sonnet-5`: $2 / $10; cache read $0.20 for 5.5.
  - `claude-sonnet-4-6`: $3 / $15.
  - `claude-haiku-4-5`: $1 / $5.
  - All valid from 2026-01-01, as USD per token in the seed's format. The source is the claude-api skill's model table, cached 2026-09-25.

## Non-functional requirements
- **NFR-1 Performance (lite).** At 1k prompts × 20 versions and 50k linked generations:
  - `GET /api/public/v2/prompts/{name}?label=production`: p95 < 50 ms on a warm server;
  - the prompts list page: < 500 ms;
  - the per-version metrics: < 1 s.
  - Playground time-to-first-byte adds < 100 ms over the provider's own.
- **NFR-2 SDK.**
  - `get_prompt` from a warm cache takes < 1 ms with no network call.
  - The SDK never raises from linking or caching paths, and never blocks the host app beyond `fetch_timeout_seconds × (max_retries+1)` on a cold miss.
- **NFR-3 Security.**
  - Every new query is project-scoped and permission-checked (matrix below).
  - Provider keys and webhook secrets are Fernet-encrypted, never logged, never sent to the browser after save, and scrubbed from error messages.
  - SSRF guards apply to webhook URLs and LLM base URLs.
  - Prompt content, diffs, playground messages and provider responses render escaped.
  - Markdown rendering stays sanitised.
  - CSRF on all cookie-authenticated writes, including the playground run endpoint.
  - The CSP is unchanged: streaming uses same-origin `fetch` + `ReadableStream` (or `EventSource`), with no inline script.
  - Audit rows for: prompt create/version, label move, protected-label change, prompt delete, connection create/update/delete, webhook create/update/delete.
- **NFR-4 Reliability.**
  - Webhook delivery is at-least-once and idempotent for receivers (an `X-Traceloom-Delivery` id).
  - Failed deliveries never block label moves.
  - In lite mode, deliveries use the in-process queue and are retried by the sweeper after a restart.
- **NFR-5 Offline tests.**
  - All provider adapters are tested with fake transports. Default `pytest` makes no network calls (the Phase 1 socket guard stays).
  - Optional live provider smoke tests run only with `TRACELOOM_LIVE_LLM=1` and real keys.
- **NFR-6 UX.**
  - Follows DESIGN.md v3, light and dark, usable at 1280px and readable at 390px. The playground compare view stacks columns under 1100px.
  - Keyboard: `cmd/ctrl+enter` runs, `esc` stops.
  - WCAG AA.

## Design

### Components and changes
```
traceloom/
  prompts/            models + service (create_version, move_label, resolve, diff, compile, metrics), composition.py, variables.py
  llm/                connections service, crypto.py (Fernet), providers/{base,anthropic,openai,azure_openai,bedrock,google,openai_compatible}.py, ssrf.py
  webhooks/           service (enqueue, sign, deliver with retry), worker handler
  api/prompts_v2.py   public prompt API
  web/routers/        prompts.py, prompt_detail.py, playground.py (page + POST /run streaming + /save), settings_connections.py, settings_webhooks.py
  web/templates/      prompts/{list,detail,_versions,_diff,_metrics,new}.html, playground/{index,_variant,_message}.html, settings/{connections,webhooks}.html
  web/static/         prompts.js (variable highlight), playground.js (editor state, streaming reader, compare)
  db/migrations/      0002_prompts_playground.py
  seed/models.yaml    Claude 5-family entries (FR-X3)
sdk/python/traceloom_sdk/  prompts.py (TextPrompt, ChatPrompt, cache, compile), client.get_prompt/aget_prompt, linking in objects.py + openai.py/anthropic.py
```
- **Queue messages** gain a `type` discriminator: `ingest` (existing, the default when absent) and `webhook_delivery`. The worker dispatches on it in both lite and full modes.
- **Provider adapters** share one interface: `stream(messages, model, params, tools, response_schema) -> iterator of Delta(text|tool_call|usage|done|error)`. Each adapter uses the provider's official Python SDK where one exists (`anthropic`, `openai` for openai/azure/openai_compatible, `boto3` bedrock-runtime Converse, `google-genai`), lazily imported and declared as runtime dependencies. The Anthropic adapter follows the claude-api skill's current API shapes:
  - adaptive thinking where the model needs it;
  - no `budget_tokens` on 5-family models;
  - `stop_reason == "refusal"` surfaced as an inline message.

### Data and interfaces
**Meta DB (migration 0002):**
- `prompts`(id, project_id, name, type, tags JSON, created_by, created_at, updated_at); unique (project_id, name)
- `prompt_versions`(id, project_id, prompt_id, version, content JSON, config JSON, commit_message, created_by, created_at, dependencies JSON); unique (prompt_id, version)
- `prompt_labels`(project_id, prompt_id, label, version, updated_by, updated_at); pk (prompt_id, label)
- `protected_labels`(project_id, label); `production` is seeded on project create and for existing projects by the migration
- `llm_connections`(id, project_id, provider, name, base_url, config JSON, secret_ciphertext, secret_hint, custom_models JSON, created_by, created_at, updated_at)
- `webhooks`(id, project_id, url, secret_ciphertext, events JSON, enabled, created_by, created_at)
- `webhook_deliveries`(id, webhook_id, project_id, event, payload JSON, status pending|succeeded|failed, attempts, next_attempt_at, last_status_code, last_error, created_at, delivered_at)
- `api_keys` gains `may_move_protected_labels` bool, default false.
- Indexes:
  - `prompt_versions(project_id, prompt_id, version DESC)`
  - `observations(project_id, prompt_name, prompt_version)` in lite. ClickHouse gets a skip index on `prompt_name`.

**TraceStore port:** adds `prompt_metrics(project_id, name, version|None, query) -> PromptMetrics` and `list_prompt_generations(project_id, name, version, limit)` for lite and ClickHouse, with contract tests.

**Webhook payload:** `{"id", "event", "created_at", "project_id", "data": {"prompt", "label", "from_version", "to_version", "actor"}}`.

**Playground run:** `POST /project/{id}/playground/run` (CSRF, Member+) takes the JSON `{connection_id, model, messages, variables, params, tools, response_schema, trace}` and returns a `text/event-stream` of `delta`, `tool_call`, `usage` and `done`/`error` events.

### Permissions (additions)
| Action | Viewer | Member | Admin | Owner |
|---|---|---|---|---|
| Read prompts, versions, diffs, metrics | ✓ | ✓ | ✓ | ✓ |
| Create prompt / version, move unprotected labels, use playground, save from playground | | ✓ | ✓ | ✓ |
| Move/remove protected labels, mark labels protected, delete prompt | | | ✓ | ✓ |
| LLM connections, webhooks (manage) | | | ✓ | ✓ |

### UX
The design step (D-1) produces `design/mockup.html` extending DESIGN.md v3, with no new tokens unless needed. It covers these screens:
- **Prompts list:**
  - a folder tree on the left;
  - search and tag/label filters;
  - a table of name, type, latest version, labels (chips; protected ones show a lock), tags and updated time.
  - Empty state: "Create a prompt" plus an SDK snippet.
- **Prompt detail**, with the version list on the left (version, message, author, time, label chips) and tabs on the right:
  - **Prompt**: content with highlighted `{{vars}}`, the resolved preview, and config;
  - **Diff**: pick two versions;
  - **Metrics**: tiles plus the linked generations table;
  - **Labels**: move by drag or select, with protected ones locked for Members and a confirm dialog.
  - Actions: New version, Open in playground, and Copy SDK snippet.
- **New prompt / new version editor:**
  - a text area, or a chat message list;
  - a config JSON editor with validation;
  - a commit message;
  - a "Set labels" select.
- **Playground**:
  - **Top bar**: connection and model picker, parameters popover, Tools and Schema buttons, Compare toggle (adds up to 4 columns), Run (`⌘↵`), Stop, Save as prompt.
  - **Body**: message list, variables panel, and response pane(s) with live streaming text plus a stats row of tokens, cost, latency and time to first token.
  - **States**: no connections (link to settings for Admins, "ask an Admin" for Members), running, error, refusal.
- **Settings → LLM connections:** a list, then add/edit dialogs per provider with only that provider's fields. The secret field shows `••••last4`, there is a Test connection button, and the custom models editor is a chip input.
- **Settings → Webhooks:** a list, an add dialog (URL, events; the secret is shown once), and a deliveries table with status pills and a "Send test" button.
- **Trace detail:** an "Open in playground" button on generation rows and in the IO pane header (Member+).

## Acceptance criteria
- **AC-1 (FR-S1, S2, P9; done-1):** Given a chat prompt `support` with v2 labelled `production`, when an app calls `get_prompt("support")` and `compile(name="Ann")`, then it gets v2's messages with `Ann` substituted.
- **AC-2 (FR-P4, S1; done-2):** Given AC-1, when an Admin moves `production` to v3 in the UI, then after the cache TTL (set to 1 s in the test) the same app gets v3 with no restart. During the stale window it gets v2, and a refresh happens in the background.
- **AC-3 (FR-S3, P8; done-3):** Given the app passes the prompt to a generation, then the stored generation has `prompt_name=support, prompt_version=2`, and the v2 metrics show count 1 with that generation's tokens and cost. A fallback prompt is not linked.
- **AC-4 (FR-L5, L4; done-4):** Given a traced generation (messages, model `claude-sonnet-5-5`, temperature 0.2, one tool), when a Member clicks "Open in playground" and Run against a fake Anthropic transport, then the request the adapter sends has exactly those messages, model, temperature and tool, the response streams in, and tokens, cost (> 0, calculated) and latency are shown.
- **AC-5 (FR-P4):** Given a Member, when they move `production`, then the result is 403 in the API and UI and no audit row or webhook is created. Moving `staging` succeeds and is audited.
- **AC-6 (FR-P10, NFR-4):** Given a webhook whose receiver returns 500 twice then 200, when a label moves, then the delivery succeeds on attempt 3, carries a valid signature, and the log shows 3 attempts. A webhook URL resolving to 127.0.0.1 is refused at save.
- **AC-7 (FR-L1, NFR-3):**
  - Given a saved connection, its secret is not in the DB in plaintext, not in any page or API response, and not in the logs during a failing test-connection call.
  - Without `TRACELOOM_ENCRYPTION_KEY` outside dev, startup fails.
- **AC-8 (FR-P6):** Given `a` includes `@@@prompt:b|label=production@@@` and `b` includes `a`, then fetching `a` returns a 400 naming the cycle. A valid include resolves, and `dependencies` lists `b`'s version.
- **AC-9 (FR-P5, NFR-3):** Given two versions whose content contains `<script>alert(1)</script>`, then the detail page, the diff, the playground and the list show it as literal text, and no dialog fires in the browser test.
- **AC-10 (FR-L7):** Given 3 variants with different fake-provider models, when Run all is clicked, then all 3 columns stream on their own and each shows its own usage and cost.
- **AC-11 (FR-L* providers):** For each of the six providers, an adapter test against a fake transport checks request mapping (messages, system, params, tools, schema), streaming deltas, usage extraction and error scrubbing.
- **AC-12 (NFR-5):** The whole default suite passes with no network access and no provider keys.

## Open questions carried from intent.md
- Composition depth limit and cycle detection → **answered** (FR-P6: depth 5, cycle error).
- `latest` is automatic and not movable → **answered** (FR-P4).
- Playground runs aren't traced by default, with an opt-in toggle → **answered** (FR-L8).
- The playground reuses the Phase 1 cost engine → **answered** (FR-L4).
- Claude 5 prices in the seed → **answered** (FR-X3; prices from the claude-api skill, cached 2026-09-25).

## Flagged concerns
- [x] CONCERN — policy: security (SSRF, NFR-3) — owner: Kunal — LLM connections need a configurable base URL, and the common real use (a LiteLLM proxy, Ollama, an in-cluster gateway) sits on a private network. Blocking private addresses breaks that; allowing them lets any Admin make the server call internal services. Options:
  - (a) allow private addresses for LLM base URLs by default, with `TRACELOOM_LLM_ALLOW_PRIVATE=true`, and still block link-local/metadata (169.254.0.0/16, fd00:ec2::254) always;
  - (b) block private addresses by default, with the operator opting in via `TRACELOOM_LLM_ALLOW_PRIVATE=true`.
  - Webhooks block private addresses by default either way.
  - **Decided by Kunal Mugalkhod: (a)** — `TRACELOOM_LLM_ALLOW_PRIVATE` defaults to true; link-local and metadata addresses are always blocked.
- [x] CONCERN — policy: cost and roles (Permissions) — owner: Kunal — the playground spends the org's provider credits, and the brief doesn't say who may run it. Options:
  - (a) Members+ can run, rate-limited by `TRACELOOM_PLAYGROUND_RATE`;
  - (b) Admins only;
  - (c) Members+, plus an optional per-connection "Admins only" switch.
  - **Decided by Kunal Mugalkhod: (a)** — Members+ can run the playground, rate-limited by `TRACELOOM_PLAYGROUND_RATE`.
- [x] CONCERN — policy: public API and SDK contract — owner: Kunal — the `@@@prompt:name|label@@@` composition syntax and the `/api/public/v2/prompts` shape are close to other tools' published conventions, which eases migration but is a long-lived public contract. Options:
  - (a) keep the brief's syntax and v2 path, documented as our own;
  - (b) use a distinct syntax such as `{{> prompt:name@label}}`.
  - **Decided by Kunal Mugalkhod: (a)** — keep `@@@prompt:name|label=x@@@` and `/api/public/v2/prompts`, documented in our own words.
- [~] ASSUMED: provider adapters use official SDKs (`anthropic`, `openai`, `boto3`, `google-genai`) as runtime dependencies, lazily imported, so lite installs still run without exercising them — why: correct streaming and auth per provider. The claude-api skill requires the official SDK for Claude calls.
- [~] ASSUMED: encryption uses Fernet from `cryptography` (a new dependency). Key rotation works by listing `TRACELOOM_ENCRYPTION_KEY` as comma-separated keys, newest first (MultiFernet) — why: a standard, audited primitive; rotation without downtime.
- [~] ASSUMED: cache-write prices in the seed are 1.25 × input for the 5-minute TTL. The claude-api skill lists only input, output and some cache-read prices. The ingestion path uses cache-write prices only when usage reports `cache_write` — why: the documented ~1.25× multiplier; flagged in the seed comment with a link to verify.
- [~] ASSUMED: the "Trace this run" ingestion goes through the normal public pipeline with an internal per-project system key, so it gets the same merge, cost and rate limits — why: one code path.
- [~] ASSUMED: prompt metrics are computed at query time from observations, filtered by `prompt_name`/`prompt_version`, using the new index. No denormalised counters — why: the volume target (NFR-1) is met with the index, which avoids double bookkeeping.

## Risks
- **Scope**: two large feature areas and six providers. Mitigation: team mode, with an order that lands prompts + API + SDK (done-1..3) before the playground (done-4), and provider adapters built in parallel against one interface and fake transports.
- **Provider API drift** across six SDKs. Mitigation: one adapter interface, thin per-provider modules, version-pinned dependencies, fake-transport tests per provider, and an optional live smoke test.
- **Streaming under CSP and HTMX**: the playground needs a small static JS module. Mitigation: same-origin fetch streaming, no inline script, and an e2e test.
- **Secrets**: a new class of stored secrets. Mitigation: Fernet with rotation, scrub filters, tests asserting no plaintext in the DB, responses or logs.
- **SSRF** via webhooks and base URLs. Mitigation: a resolve-then-check at save and at send, and a pinned resolved IP for the request.
- **SDK cache correctness** (stale-while-revalidate threads in host apps). Mitigation: one background refresher per key, bounded, daemon threads, and never raising.
