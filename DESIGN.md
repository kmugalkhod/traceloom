# Traceloom design standard (v3.1)

Revised after human review and matching the approved v3 mockup, then given a calmer layout in v3.1 (see "Layout pass" below; borders and the 14px base are unchanged): a neutral SaaS dashboard reference supplied by the owner is the pinned look. We take its language only (a floating sidebar, an inner rounded card, soft white panels, status pills, a near-black primary, pastel initial avatars), not its brand or content. v1 (the strip rack) is retired. Tokens live in the `:root` block of `sdlc/changes/2026-11-06-phase1-tracing-core/design/mockup.html`; copy it into `traceloom/web/static/app.css`. Phase 2 adds components only (see the last section; CSS in `sdlc/changes/2026-11-06-phase2-prompts-playground/design/mockup.html`) and changes no token.

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

## Phase 2 additions (prompts, playground, connections, webhooks)
No token, font, radius or shadow changed. Nav: a **Build** group (Prompts, Playground) sits between Observe and Project; on mobile the bottom bar is Traces, Prompts, Playground, Dashboard, More (More holds Sessions, Users, Settings). Settings tabs gain LLM connections and Webhooks, rendered for Admins only.
- **Label pill** (`.lab`): 999 radius, word + optional `vN`. `production` is green with a lock (protected); `latest` is dashed outline; others use `--accent-soft`; "No production label" is amber with an icon.
- **Version pill** (`.vp`): mono `vN`, radius 7; current/selected uses `--accent-soft`. **Type pill** and **tag** reuse chip sizing.
- **Editor** (`.ed`): mono 12.5px, line gutter, `--field-line` border, 2px focus ring. `{{variable}}` is `.var` (accent-soft fill, accent text, outline, weight 600); `@@@prompt:...@@@` is `.inc` (tool hue, same treatment). Highlights always carry an outline, never colour alone.
- **Message card** (`.mcard`): role select, grip, up/down/remove icon buttons; assistant header uses `--accent-soft`; placeholder messages are muted.
- **Diff** (`.diff`, `.cdiff`): line rows with old/new numbers and a `+` or minus sign column on `--ok-bg` / `--err-bg`; config diff is key, old, arrow, new. All values are text.
- **Output panel**: streaming text with a block caret (stops under reduced motion), a status chip (Streaming, Done, Stopped, Failed), a Stop button (`.btn-stop`: outlined red) that replaces Run while streaming, and a 6-cell **usage row** (`.usage`: input, output, total tokens, cost with the `calculated` source pill, latency, time to first token; pending values render muted).
- **Compare columns** (`.cols`): letter badge (A to D, series colours plus the letter), model select, params line, own output and usage. 3 in a row at wide widths, 2 by 2 for 4, stacked at <=1100px.
- **Connection row** (`.conn`): provider tile (pastel pair), name, provider pill, key hint `…last4`, last-test pill, actions. Secrets are only ever a masked field with Replace key.
- **Secret-once** (`.secret` inside an ok banner), **delivery status** reuses `.lv` pills (Delivered, Retrying n of 5, Failed), **toast** (`.toast`, near-black, bottom right, with Undo).
- A disabled control a role cannot use stays visible only where the role can reasonably ask for it (production move for Members, with the reason under it); everywhere else it is not rendered.

## Layout pass (v3.1): calmer layout, v3 structure kept
Revision 3 after review: the v3 borders, radii, panel and table-header styling are unchanged (a borderless pass looked unfinished and is withdrawn). Less crowding comes from layout: fewer things visible at once, secondary actions in menus, optional fields collapsed, a consistent 24px rhythm. In the mockup the changes sit under `.v4`; in `app.css` put the tokens on `:root` and the rules unscoped.

**Token changes (small)**
| Token | v3 | v3.1 |
|---|---|---|
| `--fs-base` | 14 | 14 (kept), line-height 1.5 (`--lh`) |
| `--fs-2xl` page title | 30 | 24 (stat numbers 28) |
| Control height | 34 / 38 | 36 (inputs and buttons), 30 small, 44 on mobile |
| Table row | 48 | 48 (flat 52, roomy 56) |
| Section gap | 16 | 24 |
| Page padding | 24 / 32 | 24 (20 at <=1100, 14 mobile) |

**Rules**
- Panels, panel headers, table-header bars, stat dividers, tabs, dialogs: exactly as v3 (hairline borders, `--sh-1`, 16 radius).
- One primary action per view; rare actions (saved views, columns, delete matching, public share, copy, delete prompt) live in a "..." menu (`.pop`). Destructive actions are menu items in `--err`; the red button only appears in the confirm dialog.
- Optional fields are collapsed (`details`): prompt Config, playground Settings, label history, older versions.
- Header key-values: the primary six (trace id, user, session, latency, cost, tokens) on the first row; Environment, Release, Version on a second "details" row (nothing is dropped).
- Tables keep their columns on desktop; rows with several secondary values show one muted second line. Mobile tables become two-line rows.
- Dashboard: Cost, Tokens and both latency tables share one tabbed "Breakdown" panel (tabs Cost, Tokens, Latency by model, Latency by trace; each tab panel keeps the old panel name as its label); the stat row, traces over time, error rate and top users stay visible. Tabs are a `[data-tabs]` tablist handled in `app.js`: roving tabindex, Left/Right wrap, Home/End, the panel itself is focusable. At <=760 the four tabs sit in a 2 by 2 grid.
- No cramped multi-row toolbars: filters sit on one row; at <=760 they wrap into full-width controls.

**Phase 1 screens as built**
- "..." menus use the app-wide `data-menu` markup (`.pop`, `role="menu"`, items `role="menuitem"`), placed `position:fixed` by `app.js` so no panel clips them. Choosing an item closes the menu and returns focus to the "..." button (so a dialog it opens hands focus back there); Up/Down walk the items, including checkboxes inside label items; Escape closes. Viewers never get a menu whose only purpose is writing.
- Traces list: `Save view` is the one primary (Members and up); "..." holds Saved views (apply, edit, delete), the column chooser and, for Admins, `Delete matching traces…` in `--err`, which opens the existing type-the-count confirm page. The menu has two columns on desktop and stacks at <=760. The button's text names its contents ("More actions: saved views, columns, delete matching").
- Trace detail: `Bookmark` stays visible; "..." holds Public share (the switch), Copy share link (when public), Copy trace / session / user id, and `Delete trace…` (Admins) which opens the confirm dialog with the red button. Viewers get no menu (the trace id row keeps its copy icon). The `/actions` fragment on its own (no JavaScript) still renders every control inline.
- Session and user detail: `View traces` plus "..." (copy id; a session also links its users). Sessions and Users lists have no secondary actions, so no menu.
- Settings: per-row "..." on API keys (Copy public key, `Revoke…`), Models (Edit, `Delete definition`; built-ins keep a visible `Override`), Members (Copy email, `Remove` / `Leave`) and invites (Copy email, `Revoke invite`). Role selects stay inline. Keys and Models tables stack into two-line rows at <=760 so the menu stays on screen. The Danger zone keeps its red button.

**Phase 2 screen rules**
- Prompt detail: title, one-line meta, `New version` (the only primary) and a "..." menu; tabs Versions, Compare, Preview, Metrics. Versions are a rail (v-number, label pills, commit line, author) beside the selected version (title, labels, `Set production`, content, metadata, collapsed Label history).
- Playground: one row "Run with" (connection, model), then two aligned equal panels (Prompt, Response). Header actions: "..." (Compare variants, which reads "Exit compare" while compare is on; Load prompt; Save as prompt), `Settings` (opens a panel with Parameters, Tools, Schema), `Run` (becomes Stop). A stream that closes without its final `done` event shows the status "Ended early" and a warning "The stream ended early", in single and compare mode, never "Done".
- Compare: 24px gaps; each column is a panel with a 3px top bar in its series colour plus a letter badge, model select, params, status chip, output and a one-line usage summary.
- Settings lists (connections, webhooks): one row per item with a muted second line, status pill, ghost `Test` / `Send test`, and a "..." menu; dialogs use a Provider select.
- Prompt Config is collapsed by default on the new prompt and version editors.
