# Intent: Traceloom Phase 2 — prompt management and playground

Author: Kunal Mugalkhod · Date: 2026-11-06 · Status: accepted · Record: prompt.md "Phase 2 — Prompt management and playground"

<!-- Status: draft -> accepted | rejected. Only the product owner changes it. -->

## Problem
Traceloom (Phase 1, merged on main) shows what an LLM app did, but teams still keep prompts in code.
- **Shipping:** changing a prompt means a code change and a deploy.
- **History:** there is no history of which prompt text and config produced which result.
- **Ownership:** non-engineers can't safely edit or roll back prompts.
- **Reproducing:** when a trace shows a bad generation, there is no way to replay it with a different prompt or model, short of copying it into a provider console by hand.

## Proposed outcome
**Prompts.** Teams manage text and chat prompts in Traceloom.
- **Versions:** each save is an immutable version with a commit message, `{{variables}}` and a JSON config (model, temperature, tools, …).
- **Labels:** `production`, `staging` or custom labels move between versions in one click. `latest` is automatic, only Admins can move protected labels such as `production`, and every move is audited and fires a webhook.
- **Diff:** any two versions can be compared, text and config.
- **Composition:** a prompt can include another by reference (`@@@prompt:name|label@@@`), resolved on fetch.
- **Organising:** folders, tags and search.
- **Public API:** `GET /api/public/v2/prompts/{name}?label=|version=`, plus create, list and update labels.
- **SDK:**
  - `get_prompt(name, label=…)` with a local cache (configurable TTL, stale-while-revalidate, and a fallback prompt when the server is down).
  - `prompt.compile(**vars)`.
  - Passing the prompt to a generation links the two.
- **Per-version metrics:** linked-generation count, median latency, tokens, cost and average scores, plus the list of linked generations.

**Playground.**
- **LLM connections:** project-level connections to Anthropic, OpenAI, Azure OpenAI, AWS Bedrock, Google, and any OpenAI-compatible base URL (e.g. a LiteLLM proxy). Each has an encrypted API key and custom models.
- **Chat editor:** system, user and assistant messages, variables, model parameters, tool definitions and a structured-output schema.
- **Responses:** streamed, with tokens, cost and latency.
- **Open in playground:** any generation in a trace opens in the playground with its exact messages and parameters.
- **Save as prompt:** the playground saves as a new prompt version.
- **Compare:** up to 4 model or prompt variants side by side on the same input.

## Affected users and systems
- **Users:**
  - Developers fetch prompts via the SDK/API.
  - Product people and prompt engineers edit, label and compare prompts in the UI and playground.
  - Admins hold protected labels and LLM connections.
  - Viewers get read-only access.
- **Systems:**
  - The Traceloom web server: new prompt, label, connection and playground pages and APIs.
  - The meta DB: new tables for prompts, versions, labels, connections and webhooks.
  - The worker queue: webhook delivery.
  - The Python SDK: `get_prompt`, `compile`, linking.
  - Generations: the existing `prompt_name` / `prompt_version` columns are linked.
  - Outbound calls to LLM providers and webhook URLs.

## Constraints
- **Project ruleset (CLAUDE.md):** every query is project-scoped and permission-checked, user content renders escaped, nothing is hardcoded, and lite mode is the default in tests. Original product: no Langfuse code, text or styling.
- **UI:** follow the approved v3 design (DESIGN.md) and the existing CSP: no inline script or style.
- **Provider keys:** encrypted at rest with a **separate `TRACELOOM_ENCRYPTION_KEY`**, required outside dev. They are never shown again after saving and never logged.
- **Providers:** all six are in scope. Tests use fake providers and transports and make no network calls, so default `pytest` stays offline.
- **Webhooks:** delivered **through the worker queue with retries and backoff, HMAC-signed bodies and a delivery log**. Destination URLs are SSRF-guarded (no private, loopback or link-local addresses unless explicitly allowed by config).
- **Labels and history:** protected labels can only be moved by Admin+. Prompt versions are immutable.
- **SDK safety:** the SDK must never break the host app. A fetch failure falls back to cache or to the fallback prompt.

## Out of scope
- Phase 3+ work: datasets, experiments, evaluators and LLM-as-judge, the AI gateway, the JS SDK, and multi-modal prompts.
- Prompt A/B traffic splitting, and approval workflows beyond protected labels.
- Billing or quotas for playground usage.

## Success measure
The "Phase 2 done when" journeys, each covered by an automated test (API, plus browser where UI is involved):
1. An app fetches the `production` prompt through the SDK.
2. Moving the label changes what the app gets, with no deploy, within the cache TTL.
3. The fetched prompt is linked on its generations, and the per-version metrics count them.
4. The playground reproduces a traced generation: same messages and parameters, run against a fake provider.

Also:
- a label move by a non-Admin on a protected label returns 403;
- a webhook fires, is signed and is retried on failure;
- provider keys are never stored or shown in plaintext.

## Open questions
- ASSUMED: prompt composition resolves recursively with a depth limit and cycle detection. A missing reference is an error on fetch, not silently empty.
- ASSUMED: `latest` always points to the newest version and cannot be moved by hand.
- ASSUMED: playground runs are not traced into the project by default. A per-run "trace this run" toggle traces it with environment `playground`.
- ASSUMED: provider token usage and cost in the playground reuse the Phase 1 cost engine and model definitions.
- ASSUMED: Claude 5 model prices get added to `seed/models.yaml` in this change, because the playground's cost display needs them (a gap left by Phase 1).
