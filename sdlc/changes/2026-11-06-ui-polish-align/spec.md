# Spec: UI polish and alignment pass

From: intent.md (11de8c1) · Status: accepted · Record: none
Skills applied: impeccable (layout, typography, alignment, empty states, a11y); DESIGN.md v3.1 (pinned owner look); REVIEW.md security pass (escaping, role gating unchanged)

## Summary
A presentation-only pass over every web screen. It applies one set of alignment rules: control height, spacing rhythm, numeric columns, table density, panel grid, empty states, and the mobile context row. It also fixes the specific defects in the 2026-11-06 screenshots. Tokens, colours, fonts, routes, data and permissions do not change. DESIGN.md gains a "v3.2 alignment pass" section that records the rules.

## Functional requirements

### Shared rules (S)
- **FR-S1 Control height.**
  - Buttons, inputs, selects, segmented trays, the range picker, the env switcher, icon buttons and filter buttons that share a toolbar row are all 36px tall (30px for `.sm`, 44px at ≤760px). The tray's inner segments are 30px and centred.
  - Icons inside controls are 16px.
- **FR-S2 Spacing rhythm.**
  - All gaps and paddings come from the 4px scale (4/8/12/16/24/32/48).
  - The page header (title + subtitle + actions) sits 24px above the first section, and sections are 24px apart.
  - Panel headers are 52px tall with 16px side padding, and the panel body uses the same 16px side padding. Panel header actions are vertically centred.
- **FR-S3 Numbers.**
  - Latency, cost, tokens, counts, percentages, observation counts and dashboard values render in Inter with `font-variant-numeric: tabular-nums`, right-aligned in tables with a right-aligned header.
  - JetBrains Mono is reserved for ids, keys, model names, code, JSON and editor content.
  - Precision stays as formatting.py produces it today. No value changes.
- **FR-S4 Tables.**
  - Body rows are 44px tall (`--row-h`). A row with a muted second line may grow to fit.
  - Header cells are 12px, weight 500, `--ink-2`, sitting in the existing tinted header bar.
  - First and last cells line up with the panel's 16px inner edge.
  - Status columns hold a status pill.
- **FR-S5 Error rows.**
  - An errored row keeps its left marker and Error pill.
  - The full-row pink tint and the red latency and cost text are removed. The row uses `--surface` with the 3px `--err-bar` marker.
  - Hover and selected states are unchanged.
- **FR-S6 Empty states.**
  - One `.empty` pattern: a 40px tinted icon tile, a 17px title, a muted description of at most 60ch, then the primary action and an optional secondary item (snippet or link).
  - It is centred horizontally in its panel, with a max width of 520px and 48px vertical padding.
  - It applies to Prompts, Playground (no connection), Sessions, Users, Traces (no data and no matches), Dashboard (no data), and the settings lists.
- **FR-S7 Context bar.**
  - On pages without environment or range controls (trace detail, prompts, playground, settings, sessions/users detail), the bar shows the page breadcrumb on the left and the shortcuts and theme buttons on the right.
  - The breadcrumb moves out of the page body into the bar. The page then starts with its title, so there is no empty 64px band.
  - On pages with env/range, the bar is unchanged except for FR-S1 heights.
- **FR-S8 Page header.**
  - Every page uses one header pattern: a 24px title (tracking -0.02em), a muted 14px subtitle on one line where it fits, and actions right-aligned and vertically centred on the title block.
  - At ≤760px, actions wrap below the subtitle and fill the width when there are two of them.

### Traces list (TL)
- **FR-TL1** Segmented filter tray:
  - Lighter: no sunken fill around it. It uses the same tray component as elsewhere, at the FR-S1 height.
  - Counts are muted tabular numbers.
  - The tray sits on one row with search and Filter, all 36px. Search is at least 280px wide on desktop.
- **FR-TL2** Columns:
  - Time: date, with the time on a muted second line.
  - Trace: name, with the mono id · obs count on a second line.
  - User: avatar + id.
  - Tags.
  - Latency, Cost: right-aligned, tabular.
  - Status: pill.
  - Scores: a muted "—" when empty. The header and cells are centred.
  - No column changes.
- **FR-TL3** The stat row is unchanged in content. Its height, padding and icon chip are made identical to the dashboard's (FR-D1).

### Trace detail (TD): a full-height trace workspace
Decided by the owner on 2026-11-07 at design review. The trace detail is a focused workspace, not a long page of stacked panels. It has a compact header and two panes that fill the rest of the viewport and scroll on their own. Comments, Scores and Metadata move into tabs in the right pane. This is an established pattern in LLM-tracing tools, built here in Traceloom's own visual language. Nothing from any competitor is copied.
- **FR-TD1 Compact header.**
  - Line one: title, status pill, and actions (Bookmark, "...") on the right.
  - Line two is one row of inline metrics separated by `·`: latency, cost, tokens, user (link), session (link, when present) and environment. All values are tabular.
  - The tags follow on the same row when they fit, and wrap otherwise.
  - Trace ID, Release and Version (when present) move to the right pane's Metadata tab for the root, and to the "..." copy items. Nothing is dropped.
  - The header is at most 112px tall at 1440px, including the title.
- **FR-TD2 Tags.**
  - Tags show once: chips, each with an × for Members+, plus an inline "+ Add tag" control that becomes a 30px input when activated.
  - The separate "Edit tags:" row is removed.
  - Viewers see the chips only. Add and remove keep their existing endpoints, field names and HTMX targets, and work without JS through the same form posts.
- **FR-TD3 Workspace panes.**
  - At ≥1100px, below the header, two panes fill the remaining viewport height (`calc(100vh - header - context bar - padding)`, min 480px): the tree pane at 40% (min 360px) and the detail pane at 60%.
  - Each pane scrolls internally; the page itself does not scroll at 1440×900 with a normal trace.
  - Both pane tops align, and their headers are 52px.
  - The tree pane header holds the Tree / Timeline / Graph tray on the left and "Jump to first error" (small) on the right. The keyboard hint moves into the buttons' `title` and the shortcuts dialog.
  - Below 1100px the panes stack (tree first, with a max height of 50vh) and the page scrolls.
- **FR-TD4 Tree rows.**
  - Rows are 36px: type icon, name and level pill on the left.
  - Duration and cost are right-aligned in two fixed-width tabular columns, so they line up across rows.
  - An empty cost renders nothing (not "–").
- **FR-TD5 Detail pane.**
  - The header shows the selected observation's type icon, name, level pill and type word on the right. Under it is one inline metrics line: latency, cost, tokens, and model when present.
  - Below that is a tablist (`[data-tabs]`, the same keyboard behaviour as the dashboard): **Preview** (Input, Output), **Metadata** (metadata, plus trace id, release and version for the root), **Scores n**, **Comments n**.
  - Scores and Comments stay trace-level, as today, and keep their existing fragments, forms and permissions.
  - In Preview, each section has a 13px weight-600 label row with a copy button on the right. The block uses `--surface-2` with a 1px `--line` border, radius 10 and padding 12px. The Formatted / JSON / Markdown switch sits in the Preview tab's toolbar.
  - Selecting a tree node keeps the active tab.
- **FR-TD6** The Comments and Scores tab contents use FR-S1 controls. The Scores form puts Name, Type and Value on one row with matched heights, and the select text is never clipped. The separate Comments and Scores panels under the page are removed. Their content lives in the tabs only, so it is not duplicated.

### Dashboard (D)
- **FR-D1 Stat cards.**
  - Every stat card on every page has the same anatomy: a 13px muted label, a tinted 24px icon chip at top-right, a 28px tabular value, and a 13px muted caption.
  - Fixed height: 112px.
  - The dashboard cards gain icon chips (Traces, Cost, Tokens, Error rate) built from existing icons.
- **FR-D2 Grid.**
  - Charts sit on a 12-column grid with a 24px gap. Traces over time spans 8 columns and Error rate by level spans 4.
  - Panels in the same row stretch to equal height.
  - The error-by-level panel fills its height: the stacked bar on top, then the legend rows (dot, word, tabular %, count), then a muted footnote with the total observations.
- **FR-D3 Charts.** Axis labels use Inter at 11px with tabular numbers, colour `--ink-3`. The chart title row matches the panel header (title + muted unit).
- **FR-D4 Breakdown rows.**
  - Name (mono model name) in a fixed 200px column, bar, then a right-aligned tabular value column at 96px.
  - The bars start at the same x on every row.
- **FR-D5 Top users.**
  - A table with columns # (muted, 32px, left-aligned with the panel edge), User (avatar + id), Traces (right, tabular) and Cost (right, tabular). No large indent.
  - The data used must already be available in the existing query result; if traces count is not available, the column is omitted.

### Prompts and playground (PP)
- **FR-PP1** The prompts list and empty state follow FR-S6 and FR-S4. The SDK snippet sits inside the empty state under the actions, at max width 520px.
- **FR-PP2 New/edit prompt.**
  - Labels sit above their fields with 6px between label and control, and 16px between fields.
  - Type (tray) and Tags share a row with tops aligned. Type's label is on the same baseline as Tags' label.
  - The right column panels (Save, Variables, Config) share the FR-S2 header style.
- **FR-PP3** The playground without a connection uses the FR-S6 empty state. With a connection, the "Run with" row and the Prompt/Response panels follow FR-S1 and FR-S2 and their tops align.

### Settings and auth (SA)
- **FR-SA1** Settings tabs are 40px high with a 2px active underline, aligned to the page's left edge (no extra inset). Panels and tables follow FR-S2 and FR-S4. Row "..." menus are right-aligned in a 48px column.
- **FR-SA2 Auth pages** (login, signup, forgot, reset, invite):
  - The form column is vertically centred, 380px wide.
  - The brand panel's headline block is vertically centred, with the tagline footer pinned to the bottom.
  - Inputs are 40px with labels 6px above.
  - The "Forgot password?" link sits on the password label's baseline.

### Mobile (M) — ≤760px
- **FR-M1 Context row.** The environment select and range tray share one row: env flexible, range tray fixed. They wrap to two rows only below 360px.
- **FR-M2** The traces filter tray becomes one horizontally scrollable row of segments (no 2×2 grid). Its scroll is inside the tray, not the page.
- **FR-M3 Trace detail on mobile.** The key/value grid is 3 columns; tags wrap; the tree toolbar wraps to two rows at most; the split pane stacks with the tree first.
- **FR-M4** No page scrolls horizontally at 390px.

## Non-functional requirements
- **NFR-1 Scope.** Only templates, CSS and the existing small JS change, plus DESIGN.md. No new dependencies, no CDN, no inline `style=` for layout (CSS classes only), and no `|safe` on user content.
- **NFR-2 Accessibility.**
  - Contrast stays ≥4.5:1 for text and 3:1 for controls, in both themes.
  - Focus-visible rings stay intact.
  - Moved controls (tags, breadcrumb) keep their accessible names.
  - The DOM order matches the visual order.
- **NFR-3 Dark mode.** Every changed rule uses tokens only, with no new literal colours.
- **NFR-4 Performance.** app.css grows by no more than 15%, and no layout JS is added.
- **NFR-5 No behaviour change.** Routes, forms, HTMX endpoints, role gating and escaping are untouched. Existing tests pass unchanged. If a test asserts on markup that the spec deliberately changes (e.g. the "Edit tags:" text), the plan names it and the reason before any change.

## Design
### Components and changes
- `app.css`:
  - New tokens `--row-h: 44px`, `--ctl-h: 36px`, `--ctl-h-sm: 30px`, `--panel-head-h: 52px`.
  - New utility classes `.num` (tabular, right-aligned), `.kvgrid`, `.empty` (reworked), `.split` (trace pane), `.grid-12`.
  - Remove mono from numeric cells and chart axes.
  - Error-row rules updated.
- `base.html` / context bar partial: render the breadcrumb block on the left when a page provides `crumbs`, and leave the bar empty-left otherwise.
- Templates: dashboard, traces list, trace detail (header, tags, tree, IO), prompts list/new/detail, playground, settings tabs/tables, auth.
- `charts.js`: axis font family and colour come from CSS variables.
- DESIGN.md: a "v3.2 alignment pass" section listing FR-S1..S8 as rules.
- Class names that existing tests assert on stay and keep their meaning:
  - `lv ERROR`, `node ERROR|WARNING`, `ty ty-*`, `cnt`, `sidenav`, `page-h`, `btn-primary`, `item danger`, `ctx-m row-wrap`, `msg msg-user`, `src calc|provided`, `data-menu-toggle`.
  - Tab and menu ARIA.
  - "No context row on detail pages" (mobile) stays true.
  - The existing `.num` class switches from mono to Inter tabular in place, so call sites need no change. `.seg` is the segmented tray referred to above.
### Data and interfaces
None. No route, schema or API change. Template context may gain a `crumbs` list, built from values the routes already have.
### UX
The mockup (`design/mockup.html`) shows the trace detail, traces list and dashboard at 1440px and 390px, built with the real tokens. It comes from the designer (D-1 on the board) and the owner approves it.

## Acceptance criteria
- **AC-1 (FR-S1, FR-TL1):** Given the traces page at 1440px, when the toolbar row is measured, then the tray, search and Filter button have equal heights (36px ±1), and the env select, range tray and icon buttons in the context bar have equal heights.
- **AC-2 (FR-S3, FR-S4, FR-TL2):** Given the traces list, then the Latency and Cost cells and headers have `text-align: right`, a computed `font-variant-numeric` containing `tabular-nums`, a non-monospace font family, and a body row height of 44px ±1.
- **AC-3 (FR-S5):** Given an errored trace row, then its background equals a non-error row's background, its latency text colour equals `--ink`, and it shows the left marker and the Error pill.
- **AC-4 (FR-TD1, FR-TD2):**
  - Given trace detail at 1440×900, then the header (title row plus metrics row) is at most 112px tall, and the inline metrics share one top offset (±2px).
  - The text "Edit tags:" is absent.
  - Each tag appears once.
  - A Member can add and remove a tag. The existing flow test still passes.
  - A Viewer sees no × and no add control.
- **AC-5 (FR-S7):** Given trace detail, then the breadcrumb is inside the context bar, and the page title's top is within 24px of the bar's bottom.
- **AC-6 (FR-TD3, FR-TD4, FR-TD5):**
  - Given trace detail at 1440×900 with the seeded trace, then the two pane tops are equal (±1px).
  - The tree pane width is 40% ±2%.
  - The document does not scroll vertically (`scrollHeight ≤ innerHeight + 1`), and each pane scrolls on its own.
  - The duration cells of all tree rows share a right edge (±1px).
  - The detail pane has the tabs Preview, Metadata, Scores and Comments with working arrow-key navigation.
  - Adding a comment and a score from the tabs works for a Member, using the existing journey tests.
- **AC-7 (FR-D1, FR-D2):** Given the dashboard, then all four stat cards have an icon chip and equal heights, and the two chart panels in the first row have equal heights (±1px).
- **AC-8 (FR-D5):** Given the dashboard's top users, then the rank column's left edge is within 20px of the panel's inner left edge, and cost is right-aligned.
- **AC-9 (FR-S6):** Given an empty project, then the Prompts and Playground empty states are horizontally centred in their panel (the left and right gaps differ by ≤ 4px) and no wider than 520px.
- **AC-10 (FR-M1, FR-M4):** Given 390×844, then the traces, trace detail, dashboard, prompts, playground and settings pages have `scrollWidth ≤ clientWidth`, and the env select and range tray share a top offset.
- **AC-11 (NFR-2, NFR-3):** Given both themes, then the existing contrast and dark-mode tests still pass, and no new literal colour appears in the changed CSS (enforced by a grep test over the new rules' block).
- **AC-12 (NFR-5):** `sdlc.py verify` is green, and the existing e2e journeys show no console errors.

## Open questions carried from intent.md
- Branch base: decided. Work branches from the phase 2 branch, with merge order phase 2 then this (owner accepted the intent with this assumption).
- Reference image: not committed (it is a third-party sample); DESIGN.md already describes its language.

## Flagged concerns
- [x] Trace detail layout: a full-height workspace with right-pane tabs replaces the stacked header grid and the panels below. Decided by Kunal Mugalkhod at the D-1 design review, 2026-11-07.
- [x] Mockup extras not built: the 'Slow' filter tab, the '+x% vs previous' deltas and a sidebar reorder are out of scope, and the nav order stays as today. Decided at the D-1 review, 2026-11-07.
- [~] ASSUMED: removing the full-row error tint and red numbers (FR-S5) is acceptable. Error rows stay findable through the marker, the pill and the Errors filter. This matches the owner's reference (a red amount only in the status pill) and calmer competitor tables.
- [~] ASSUMED: moving the breadcrumb into the context bar (FR-S7) is a layout change, not a navigation change. The links and targets are identical.
- [~] ASSUMED: the tags "Edit tags:" duplication is presentational. Add/remove endpoints are unchanged, so no permission change.
- [~] ASSUMED: if a test asserts on markup this spec deliberately changes, the plan lists the test and the replacement assertion. The test's intent (behaviour) is preserved, never loosened.

## Risks
- **Broad template touch:** risk of regressions in role-gated rendering. Mitigation: the existing permission tests, plus new AC tests run as Viewer and Member.
- **Pixel-tolerance e2e tests can be flaky:** use the ±1–2px tolerances above and fixed viewports, and wait for fonts to load.
- **Phase 2 not yet merged:** if phase 2 changes during its review, this branch rebases onto it.
