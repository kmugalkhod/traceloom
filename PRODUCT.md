# Product

<!-- impeccable:product-schema 1 -->

<!-- Inferred from sdlc/changes/2026-11-06-phase1-tracing-core/intent.md and spec.md. No interview was possible in this run; every fact below is from those documents. -->

## Platform

web

## Stack
Fixed by CLAUDE.md: FastAPI, server-rendered Jinja pages + HTMX, no inline scripts (CSP `script-src 'self'`), vendored uPlot for charts, server-rendered SVG for the agent graph. No frontend build step.

## Users
- AI engineers debugging an LLM app or agent run: they need to see which step failed, what the model saw, how long each step took and what it cost.
- Team leads watching spend, latency and errors per model, user and release.
- Reviewers who read traces and leave scores and comments.
Roles: Owner, Admin, Member, Viewer. Viewers are read-only and must never see edit controls.

## Product Purpose
Traceloom is a self-hosted LLM-engineering platform. Phase 1 ingests traces from the Python SDK, a raw ingestion API and OTLP, and lets a team debug them (tree, timeline, agent graph, IO), track cost and tokens, replay sessions, and see a dashboard. Success: the demo agent run appears within 5 s with the right tree, timeline, cost and highlighted tool error.

## Positioning
Original, self-hosted and owned end to end by the team. Cost always shows where it came from (provided or calculated). Not a copy of any existing product: no borrowed code, text, logos or styling.

## Operating Context
Used at a desk next to an editor and terminal, for long debugging sessions, often keyboard-first. Also read on a phone at 390px when someone follows a link from an alert. Both light and dark themes (system default plus a toggle).

## Capabilities and Constraints
- Phase 1 screens: auth, shell, onboarding, API keys and models settings, orgs/members/invites, traces list, trace detail, comments/scores/bookmark/tags, public share, delete and audit, sessions, users, dashboard.
- Keyboard: `/` search, `j`/`k` move, `enter` open, `t` tree/timeline, `e` first error, `?` help.
- User content (inputs, outputs, comments) always renders escaped; Markdown is sanitised.
- Controls for actions a role may not perform are not rendered.
- Out of scope: prompts, playground, evals, datasets, custom dashboards, alerts.

## Brand Commitments
Name: Traceloom. No logo exists yet; a placeholder wordmark is used and listed for replacement.

## Evidence on Hand
No customers, benchmarks or testimonials. All trace data in the mockup is synthetic (a support agent demo) and labelled as such.

## Product Principles
1. Density over decoration: show the run, not the chrome.
2. Errors and cost are first-class: red and amber rows, cost with a source, never hidden.
3. Everything addressable: filters live in the URL, rows are keyboard-reachable.
4. User content is untrusted text: always literal, never markup.
5. Hide what a role cannot do instead of showing it disabled.

## Accessibility & Inclusion
WCAG AA contrast, visible focus rings, keyboard-complete, state never conveyed by colour alone (icons and text accompany red and amber).
