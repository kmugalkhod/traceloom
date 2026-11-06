# ADR-0011: LLM connections and provider adapters

Status: accepted · Date: 2026-11-06

## Context
The playground runs prompts against real models. A project needs to store provider credentials safely, reach six kinds of providers through one interface, and let operators point at private gateways (a LiteLLM proxy, Ollama) without letting the server be used to reach internal services or cloud metadata.

## Decision
- **One streaming adapter interface.** Every provider implements `stream(messages, model, params, tools, response_schema)` and yields `Delta` values (text, tool call, usage, done, error). Providers are `anthropic`, `openai`, `azure_openai`, `openai_compatible`, `bedrock` and `google`. The OpenAI adapter also serves Azure and OpenAI-compatible endpoints with different client construction.
- **Official SDKs, imported lazily.** `anthropic`, `openai`, `boto3` (Bedrock Converse) and `google-genai` are runtime dependencies, imported inside the adapter that needs them, so importing the app never loads a provider SDK. Tests use fake transports, so the suite needs no network and no keys.
- **Secrets are encrypted with Fernet.** Provider keys and webhook secrets are stored with `MultiFernet` using `TRACELOOM_ENCRYPTION_KEY`, one or more urlsafe-base64 keys separated by commas, newest first. New data uses the first key and any listed key decrypts, so rotation needs no downtime. The key is required outside dev; dev generates an ephemeral one with a startup warning. A secret is shown once as a masked hint (`…last4`), can be replaced and is never read back or logged. When the key is wrong or rotated away, the connection shows "key needs re-entry" instead of failing with a 500.
- **SSRF policy for base URLs.** One guard (`llm/ssrf.py`) is shared with webhooks. It resolves the host once, classifies every address, and connects to the pinned IP so a second DNS answer cannot swap the target. Link-local, cloud metadata (169.254.169.254 and friends, `fd00:ec2::254`), unspecified, multicast and reserved addresses are always refused. Private and loopback addresses are allowed for LLM base URLs by default (`TRACELOOM_LLM_ALLOW_PRIVATE=true`, decided by the project owner) because a private gateway is the common real use. Webhooks refuse private addresses by default (`TRACELOOM_WEBHOOK_ALLOW_PRIVATE=false`).
- **Costing reuses Phase 1.** A playground run renders variables, streams the adapter, accumulates usage and prices it with the existing model registry and `compute_cost`. It can optionally ingest the run as a trace.
- **Limits.** Members and above can run the playground, limited per user and project by `TRACELOOM_PLAYGROUND_RATE` (default `30/minute`). Each provider call has `TRACELOOM_PROVIDER_TIMEOUT_SECONDS` (default 120). Error messages are scrubbed of secrets before they reach the browser.

## Consequences
- Adding a provider means one adapter and a fake-transport test, with no change to the UI or the run code.
- Four more runtime dependencies. They are imported lazily, so a deployment that never uses a provider never loads it.
- Operators who do not want the server reaching private networks set `TRACELOOM_LLM_ALLOW_PRIVATE=false`.

## Alternatives considered
- Raw HTTP calls instead of provider SDKs: rejected, because authentication (SigV4, Azure, Google) and streaming formats are what the SDKs exist for.
- Blocking private addresses by default: rejected, it breaks the common self-hosted gateway case. Link-local and metadata stay blocked either way.
- A WebSocket for streaming: rejected, SSE over `fetch` is one-way, works through proxies and needs no CSP change.
