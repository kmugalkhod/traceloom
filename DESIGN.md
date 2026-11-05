# Traceloom design standard

Source of truth for tokens: the `:root` block in `sdlc/changes/2026-11-06-phase1-tracing-core/design/mockup.html` (copy it into `traceloom/web/static/app.css`). Mode: Operate. Direction: a strip rack, in the manner of air-traffic flight strips. Every run, step, session and user is a ruled strip with a coloured tab; state is read by scanning.

## Principles
1. Density with rhythm: 34px table rows, 30px tree rows, 28px controls. Hairline rules, not boxes inside boxes.
2. Red and amber belong to ERROR and WARNING only. State is never colour alone: icon + word + tint.
3. Numbers are mono and tabular; ids are mono; everything else is Barlow.
4. User content is text. It is shown in the literal style (`.lit` highlight in demos) and never as markup.
5. Controls a role cannot use are not rendered.

## Colour (light / dark)
| Token | Light | Dark | Use |
|---|---|---|---|
| --bg | #e9ece8 | #0c1013 | desk behind panels |
| --surface | #fafbf9 | #151b20 | strips, panels, inputs |
| --surface-2 | #f0f2ee | #1b2227 | table head, hover, side nav |
| --sunken | #e1e5df | #0f1417 | code blocks, tracks |
| --line / --line-strong | #cfd5ce / #a9b3a9 | #2a343b / #46535c | rules / control borders |
| --ink / --ink-2 / --ink-3 | #14191d / #414b52 / #586369 | #e7ecef / #b4bfc6 / #8c99a2 | text, secondary, tertiary (all >= 4.5:1 on surface) |
| --accent-solid / --accent | #1f4fd1 | #3a6df0 / #8fb0ff | primary buttons / links and focus |
| --accent-soft | #dde6fa | #1c2a4d | selection |
| --err-bar, --err-bg, --err, --err-solid | #cf3a32, #fbe2df, #a31d1d, #b42318 | #ef5b52, #3a1716, #ff9b93, #d1382c | ERROR |
| --warn-bar, --warn-bg, --warn | #d99a1d, #fcefcf, #7d4a00 | #e0a526, #33270b, #f2c14e | WARNING |
| --ok, --ok-bg | #17683a, #dff1e5 | #6fd29a, #12301f | success, "provided" source |
Observation types (icon + hue + word): generation `--t-generation` purple, span slate, agent teal, tool rose, chain indigo, retriever green, embedding sky, event olive. Chart series `--s1..--s6`.
Themes: system default via `prefers-color-scheme`, explicit `data-theme="light|dark"` on `<html>` persisted in `localStorage` (and a cookie so the server can render it).

## Type
- UI: Barlow 400/500/600/700. Labels: Barlow Semi Condensed 600, 11px, uppercase, tracking .07em. Data: JetBrains Mono 400-600, tabular numerals.
- Scale (px): 11 label, 12 data, 13 base, 14, 16, 20 page title, 28 hero (auth, error pages).
- Fonts load from Google Fonts in the mockup only. The app must self-host them under `/static/fonts` (CSP `default-src 'self'`) with system fallbacks already in the tokens.

## Spacing, radius, shadow, motion
- Spacing: 2, 4, 6, 8, 12, 16, 24, 32, 48 (`--sp-1..9`).
- Radius: 2 (chips), 4 (controls, panels), 8 (dialogs, popovers).
- Shadow: `--sh-1` panel (1px + 2px blur), `--sh-pop` for popovers and dialogs (offset, soft blur). No glows.
- Motion: 150ms ease-out on button and switch state; a 2px HTMX progress bar and skeleton shimmer for loading. All stop under `prefers-reduced-motion`.

## Components
App shell (top bar 44px, 200px side nav, bottom tab bar under 760px), buttons (primary, default, danger, ghost, icon, small), inputs with inline errors, switch, segmented control, tabs, chips, level badge, source badge (provided / calculated / annotation), score chip `name: value`, panel, banner (err, warn, ok, info), empty state, dialog, popover menu, strip table (level tab, sortable header, cursor row, selected row, two-line strip under 760px), tree row, timeline bar with TTFT marker, agent-graph node, IO viewer (Formatted / JSON / Markdown), comment, stat row, horizontal bar, stacked bar, skeleton, keyboard hint.

## States
Every interactive element has default, hover, focus-visible (2px `--focus` outline, 2px offset), disabled (50% opacity, `aria-disabled`), and where relevant loading, error and selected. Tables and pages have loading (skeleton + progress), empty (what to do next), and error (banner with Retry).

## Responsive
Container-style breakpoints: <=1100px stack split panes and the auth side panel; <=760px replace the side nav by the bottom tab bar, show the env and range row, collapse strip tables to two lines, and raise inputs to 16px / 40px.
