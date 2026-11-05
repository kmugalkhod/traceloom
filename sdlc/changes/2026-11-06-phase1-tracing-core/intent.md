# Intent: Traceloom Phase 1 — tracing core (MVP)

Author: Kunal Mugalkhod · Date: 2026-11-06 · Status: accepted · Record: prompt.md (Product + Recommended architecture + Phase 1)

<!-- Status: draft -> accepted | rejected. Only the product owner changes it. -->

## Problem
Engineers building LLM apps and agents can't see what a run actually did. When a run goes wrong, they can't tell which step failed, what the model saw, how long each step took or what it cost. Team leads have no view of spend, latency or errors per model, user or release. Today they have to read scattered logs or pay for a hosted SaaS. There is no self-hosted, original tool the team owns end to end.

## Proposed outcome
A self-hosted web app, **Traceloom**, plus a Python SDK:
- An engineer adds the SDK (`@observe()`, or the OpenAI / Anthropic / LangChain / LiteLLM wrappers) to an agent. Each run shows up within 5 s as a trace, with a nested tree, a timeline (including time to first token), an agent graph, inputs and outputs, tokens and cost.
- Data can also arrive through the raw ingestion API (batched, idempotent, `207` per event) and through OpenTelemetry OTLP/HTTP with GenAI attributes.
- Accounts, organizations, projects, roles (Owner / Admin / Member / Viewer) and hashed project API keys.
- A traces list with search, filters, date ranges, cursor pagination, saved views and an environment switcher.
- Sessions (chat-like replay), users, a home dashboard of cost, tokens, latency percentiles and errors, and bookmarks, public share links, comments with @mentions, and logged deletes.
- Cost comes from provided `cost_details`, or is calculated from usage and model prices. Built-in prices come from a seed file, users can override them per project, and the UI shows where each cost came from.
- Storage goes through `TraceStore` / `BlobStore` ports with two adapters:
  - **Lite** (default; SQLite for dev and tests, Postgres via `DATABASE_URL`; in-process queue; local folder).
  - **Full** (ClickHouse + MinIO/S3 + Redis queue, run with Docker Compose).
- An architect step writes ARCHITECTURE.md and the ADRs listed in prompt.md. A human approves them before planning.

## Affected users and systems
- AI engineers (debugging), team leads (cost, latency, quality), reviewers (scores and comments in Phase 1; full annotation comes in Phase 3).
- New system (greenfield repo): FastAPI web + API server, worker, Python SDK in `sdk/python`, `examples/` demo agent, and Docker Compose for the full stack.

## Constraints
- Original product. No Langfuse code, text, logos or styling.
- Nothing hardcoded: ports, DB URL, secrets, retention and SMTP come from env or settings. Ship a `.env.example`. Model prices come from a seed file.
- Ingestion never blocks the caller. Accepted events are never lost if the worker or a DB is down.
- Every API and page is project-scoped and permission-checked. Inputs and outputs render escaped. CSRF protection on forms. Rate limits on auth and ingestion.
- Performance targets as in prompt.md: list < 1 s, detail < 500 ms, dashboards < 2 s at 10M observations (full stack), 1,000 events/s on one machine, Compose < 6 GB RAM.
- Stack: Python FastAPI, server-rendered pages + HTMX, SQLAlchemy + Alembic, and pytest + a real-browser test (Playwright) per feature.
- UI: dense and keyboard-friendly, light and dark themes, usable at 1280px and readable at 390px.
- Build mode: agent team (2 developers + QA).

## Out of scope
Phases 2–6: prompt management and playground, score configs, annotation queues, LLM judges, datasets, custom dashboards, alerts, the full public REST API, exports, retention jobs, server-side masking, SSO, CLI, MCP server, the AI gateway, multi-modal media, code evaluators, skills, the JS SDK and SCIM. The data model leaves room for them, but they are not built here.

## Success measure
Phase 1 is done when:
- The demo agent in `examples/agent.py`, instrumented with the SDK, sends a run. It appears within 5 s with the right 3-level tree, timeline, cost and tokens, and its tool error is highlighted.
- Session, user and dashboard numbers match the raw data.
- A Viewer gets 403 on every write and sees no edit buttons. A key from project A can't read project B.
- Acceptance journeys 1–6, 9 and 10 pass. Journey 12 (journeys 1–6 in lite mode) also passes. `pytest` is green.

## Open questions
- ASSUMED: lite mode on SQLite is the test and dev default. Postgres lite (partitioned trace tables) and the full ClickHouse stack are exercised through Docker Compose, with an integration test marker that is skipped when the services are absent.
- ASSUMED: the volume and throughput targets (10M observations, 1,000 events/s) are argued in ARCHITECTURE.md and checked with a load script. Journey 9 (10k traces / 50k observations) is an automated test in lite mode.
- ASSUMED: tokenizer counting uses `tiktoken` for OpenAI-family models where available, and stays empty (no guessing) for others.
- ASSUMED: "published as a package" means an installable `pyproject.toml` in `sdk/python`. Publishing to PyPI is not done in this change.
