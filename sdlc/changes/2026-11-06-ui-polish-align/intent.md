# Intent: UI polish and alignment pass

Author: Kunal Mugalkhod · Date: 2026-11-06 · Status: accepted · Record: none

<!-- Status: draft -> accepted | rejected. Only the product owner changes it. -->

## Problem
The owner says the current UI "is not aligned" and "not looking professional". Screenshots taken on 2026-11-06 (1440×900 and 390×844, phase 2 branch, seeded demo traces) back this up:
- **Trace detail:**
  - The context bar is empty.
  - The key/value header is ragged: ENV sits alone on its own row, and tags appear twice ("TAGS support · Edit tags: support").
  - The tree panel is narrow, with a lot of empty space under it.
  - The tree toolbar wraps and is cluttered by a keyboard hint.
  - The IO blocks are heavy grey slabs.
  - The Comments and Scores panels have unequal heights.
- **Dashboard:**
  - Its stat cards have no icons, though the Traces page cards do.
  - The Error-by-level panel is half empty.
  - "Top users" ranks have a large left indent.
  - The chart has no title rhythm.
- **Numbers:** some use JetBrains Mono (table latency and cost, chart axes, breakdown) and some use Inter. DESIGN.md says numbers use Inter tabular figures.
- **Empty states** (Prompts, Playground) are left-hugging blocks inside a full-width card.
- **Traces list:** whole error rows tint pink and turn their numbers red. The Scores column is all "–". The segmented filter tray looks heavy.
- **Mobile:** the env/range rows and the stacked segmented tray look crowded.

## Proposed outcome
Every screen looks like one calm, professional product and keeps the look of the owner's reference (the pinned DESIGN.md language). Alignment and consistency rules borrowed from leading LLM-observability and dev tools (Linear, Vercel Geist, Braintrust, LangSmith, Laminar, Sentry, Datadog, PostHog):
- **One control height and spacing rhythm:** 36px controls, 30px small, a 4px grid, 24px section gap. Icons are 16px and centred, and labels share left edges.
- **Numeric columns:** right-aligned, Inter `tabular-nums`, fixed precision per column, right-aligned headers. Mono only for ids and code.
- **Tables:** 44px comfortable rows (down from 48). Error rows get a left marker and a status pill, not a full red row. Tables use a lighter filter bar.
- **Trace detail:**
  - A clean header grid of key/values, with tags inline and editable in place.
  - A balanced split pane: tree and timeline on the left, full height, sticky; span detail on the right.
  - A lighter IO viewer.
  - Comments and Scores stay aligned.
- **Dashboard:** consistent stat cards (icon chip, value, caption) and charts on a strict grid with even panel heights. Readable rank lists.
- **Prompts, playground, settings, auth:** centred, purposeful empty states, aligned tables and forms, consistent page headers.
- **Mobile (390px):** tidy context row, no overflow, two-line table rows.

## Affected users and systems
- All Traceloom web UI users.
- `traceloom/web/templates/**`, `traceloom/web/static/*.css` and the small JS that drives layout.
- DESIGN.md gets a v3.2 section.
- No API, SDK, data or permission changes.

## Constraints
- **Look:** keep the owner-pinned look (reference image, DESIGN.md v3.1 tokens). This is a polish pass, not a re-brand.
- **Originality:** original work only. Never copy Langfuse or any competitor's code, text, logos or styling. Competitors inform patterns only.
- **Behaviour:** no regressions. Existing tests (including e2e layout and journey tests) stay green, user content stays escaped, and role-gated controls stay gated.
- **Accessibility and dark mode:** light and dark both first-class. Contrast ≥ 4.5:1 for text and 3:1 for controls, focus rings stay, and reduced motion is respected.
- **No new runtime dependencies or external CDNs.** Fonts stay self-hosted.

## Out of scope
- A denser "compact mode" toggle, column drag-reorder, the histogram above the traces table, and resizable panes. These are possible follow-ups.
- New features or new data shown. The Phase 3 evaluation UI.
- Changing colour tokens or the brand.

## Success measure
- Before and after screenshots of the 10 main screens at 1440px and 390px. The owner judges them "aligned and professional".
- No horizontal scroll at 390px.
- Numeric columns are right-aligned with tabular figures everywhere.
- Every control in a toolbar row shares one height.
- `sdlc.py verify` is green, with no new console errors in the e2e journeys.

## Open questions
- ASSUMED: branch from `sdlc/2026-11-06-phase2-prompts-playground`, because phase 2 is not merged to main yet and the polish must cover its screens. Merge order: phase 2, then this.
- ASSUMED: the untracked `HWXC_5naEAIA1bB.jpeg` in the repo root is the owner's reference image. It is not committed.
- Decided by owner: direction is polish and align (not a dev-tool redesign); every screen group is in scope; table rows are 44px "comfortable".
