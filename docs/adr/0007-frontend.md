# ADR-0007: Server-rendered Jinja + HTMX, not a SPA

Status: accepted · Date: 2026-11-06

## Decision
Pages are Jinja2 templates with HTMX for partial updates: filters, panes and pagination. A small amount of plain JS handles keyboard shortcuts, the theme toggle and charts. Static assets are **vendored** under `traceloom/static/vendor/` (htmx, uPlot) and never loaded from a CDN, so self-hosted instances work offline and the CSP stays `script-src 'self'`. The tree, timeline and agent graph are rendered as server-side SVG or HTML.

## Alternatives
React/Vite SPA: richer interactions, but it doubles the toolchain (node build, an API for every view). Not needed for a dense, read-mostly tool.

## Consequences
One language and one test stack. Browser tests use Playwright through pytest.
