# Traceloom design standard (v2)

Revised after human review: the reference image `HWXC_5naEAIA1bB.jpeg` (repo root) is the pinned look. We take its language only (a floating sidebar, an inner rounded card, soft white panels, status pills, a near-black primary, pastel initial avatars), not its brand or content. v1 (the strip rack) is retired. Tokens live in the `:root` block of `sdlc/changes/2026-11-06-phase1-tracing-core/design/mockup.html`; copy it into `traceloom/web/static/app.css`.

Mode: Operate. Light and dark are both first-class: system default, a toggle, persisted in `localStorage` and a cookie.

## Principles
1. Calm and professional: generous radius, hairline borders, almost no shadow, one near-black primary action per view.
2. Status is a pill: icon + word + tint (Error red, Warning amber, success green). Never colour alone.
3. Numbers use Inter tabular figures; ids and code use JetBrains Mono.
4. User content is always text; controls a role cannot use are not rendered.

## Colour (light / dark)
| Token | Light | Dark | Use |
|---|---|---|---|
| --bg | #e9e9e6 | #08090b | desk behind the app |
| --main | #f8f8f7 | #111316 | the big inner card |
| --surface | #ffffff | #171a1e | panels, inputs, active nav |
| --surface-2 / --sunken | #f2f2f0 / #ececea | #1d2126 / #0d0f12 | table head, tray, code |
| --line / --line-strong | #e5e5e2 / #d3d3cf | #262a30 / #353b43 | panel rules / button borders |
| --field-line | #8d908d | #6b727b | input, checkbox and switch edges (>= 3:1) |
| --ink / --ink-2 / --ink-3 | #111214 / #4b4f54 / #62666b | #f1f2f4 / #b7bdc4 / #8f979f | text (>= 4.5:1 on surface and main) |
| --accent-solid / --on-accent | #111214 / #fff | #f1f2f4 / #111214 | primary button, switch on |
| --accent / --focus / --accent-soft | #2a55d8 / #2a55d8 / #e9eefc | #8fb0ff / #8fb0ff / #1c2740 | links, focus ring, selection |
| --err-bar / --err-bg / --err | #d4413a / #fde8e6 / #b0201c | #ef5b52 / #3a1716 / #ff9b93 | Error |
| --warn-bar / --warn-bg / --warn | #d99a1d / #fdf0d3 / #7d4a00 | #e0a526 / #33270b / #f2c14e | Warning |
| --ok / --ok-bg | #17683a / #e3f4e9 | #6fd29a / #12301f | success, provided cost |
Observation types keep icon + hue + word (`--t-generation`, span, agent, tool, chain, retriever, embedding, event). Chart series `--s1..--s6`. Avatars use five pastel pairs (`.av1..av5`) chosen from the initials.

## Type
Inter 400/500/600/700 for UI (self-host it; the mockup loads Google Fonts). JetBrains Mono for ids and code. Scale (px): 11 label (uppercase, tracked, 500), 12.5 data, 14 base, 15 nav and panel titles, 17, 22 page title, 30 stat numbers and auth titles. Headings carry -0.02em tracking.

## Shape, spacing, depth, motion
Radius: 6 chips and tags, 10 controls, 12 nav and banners, 16 panels, 20 the inner card and dialogs, 999 status pills. Spacing 2/4/6/8/12/16/24/32/48. Depth: `--sh-1` (1px, 2px blur) on panels and buttons; `--sh-pop` on popovers and dialogs. Motion: 150ms ease-out; skeleton shimmer, spinner and the HTMX progress bar stop under `prefers-reduced-motion`.

## Layout
Desktop: 244px floating sidebar (brand tile, org / project picker, Observe and Workspace groups, user card) next to one rounded card holding a context bar (environment, date range, shortcuts, theme) and the page. At <=1100px split panes stack and the sidebar narrows. At <=760px the sidebar becomes a brand + picker row, a bottom tab bar appears, the environment and range show as a row at the top of the page, tables become two-line rows, inputs are 16px / 40px.

## Components
Shell, buttons (primary black, default white, danger, ghost, icon, small), inputs with inline errors, switch, segmented tray (selected = raised white), tabs, tags, status pills, source pills (provided / calculated / annotation), score chip `name: value`, panels, banners, empty states, dialogs, popover menus, tables (tinted header bar, 48px rows, level marker, cursor row, selected row), stat row (label, tinted icon chip, 30px number, caption), tree, timeline with TTFT marker, agent-graph node, IO viewer, comments, bars. States: default, hover, focus-visible (2px ring, 2px offset), disabled (50%, `aria-disabled`), loading, empty, error, selected.
