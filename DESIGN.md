# Traceloom design standard (v3.2)

Revised after human review and matching the approved v3 mockup, then given a calmer layout in v3.1 (see "Layout pass" below; borders and the 14px base are unchanged): a neutral SaaS dashboard reference supplied by the owner is the pinned look. We take its language only (a floating sidebar, an inner rounded card, soft white panels, status pills, a near-black primary, pastel initial avatars), not its brand or content. v1 (the strip rack) is retired. Tokens live in the `:root` block of `sdlc/changes/2026-11-06-phase1-tracing-core/design/mockup.html`; copy it into `traceloom/web/static/app.css`. Phase 2 and Phase 3 add components only (see the last sections; CSS in `sdlc/changes/2026-11-06-phase2-prompts-playground/design/mockup.html` and `sdlc/changes/2026-11-09-phase3-evaluation/design/mockup.html`) and change no token.

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

## v3.2 alignment pass (FR-S1..S8): rules, no new colour, font, radius or shadow
Presentation only. The reference mockup is `sdlc/changes/2026-11-06-ui-polish-align/design/mockup.html`. New tokens: `--row-h: 44px`, `--ctl-h: 36px`, `--ctl-h-sm: 30px`, `--panel-head-h: 52px`. Every changed rule uses tokens only.
- **S1 Control height.** Buttons, inputs, selects, trays, the range picker, the env switcher, icon buttons and filter buttons in one toolbar row are 36px (30px `.sm`, 44px at <=760px). Tray segments are 30px, centred in a 36px tray with a 2px inset. Icons in controls are 16px.
- **S2 Spacing rhythm.** Only 4/8/12/16/24/32/48. The page header sits 24px above the first section; sections are 24px apart. Panel headers are 52px with 16px side padding, and the body uses the same 16px. Header actions are vertically centred.
- **S3 Numbers.** Latency, cost, tokens, counts, percentages and dashboard values are Inter with `font-variant-numeric: tabular-nums`, right-aligned in tables under a right-aligned header. JetBrains Mono is only for ids, keys, model names, code, JSON and editors. `.num` switches from mono to Inter in place. Chart axis labels are Inter 11px in `--ink-3`, read from `--font-ui` and `--ink-3` in `charts.js` (canvas text cannot take `font-variant-numeric`, so the axis uses Inter's own figures).
- **S4 Tables.** Body rows 44px (a muted second line may grow the row). Header cells 12px, weight 500, `--ink-2` in the tinted bar. First and last cells align to the panel's 16px inner edge. Status columns hold a pill.
- **S5 Error rows.** Keep the 3px `--err-bar` marker and the Error pill. Remove the pink row tint and the red latency and cost text: the row is `--surface`, the numbers `--ink`. Hover and selected are unchanged.
- **S6 Empty state.** One `.empty`: 40px tinted icon tile, 17px title, muted text up to 60ch, primary action, optional secondary item. Centred in its panel, max-width 520px, 48px vertical padding. Used by Prompts, Playground (no connection), Sessions, Users, Traces, Dashboard and settings lists.
- **S7 Context bar.** 56px. With env or range controls it shows them (36px). Without them (trace detail, prompts, playground, settings, session and user detail) it shows the breadcrumb on the left, then the shortcuts and theme buttons. The breadcrumb leaves the page body, so the title starts 24px below the bar. On mobile the crumbs are hidden (`display:none`, not duplicated) and the env select and range tray share one row.
- **S8 Page header.** 24px title (-0.02em), 14px muted subtitle on one line where it fits, actions right-aligned and centred on the title block. At <=760px actions wrap below and fill the width when there are two.
- **Stat card** (all pages): 112px, 13px label, 24px tinted icon chip top-right, 28px tabular value, 13px caption.
- **Dashboard grid:** 12 columns, 24px gap; Traces over time 8, Error rate by level 4; same-row panels stretch. Breakdown rows: 200px name, bar, 96px right-aligned value. Top users: #, User, Traces, Cost with no large indent.
- **Trace workspace (trace detail):** a full-height page, not a stack of panels; at 1440x900 it does not scroll. Compact header of at most 112px: line 1 title, status pill, Bookmark and "..."; line 2 one inline metrics row separated by "·" (latency, cost, tokens, user link, session link, environment, all tabular) followed by the tag chips and "+ Add tag", wrapping if needed. Tags show once, with x for Members up and the inline add (a 30px field) for Members up; Viewers see chips only. Trace ID, Release and Version live in the detail pane's Metadata tab (root) and as copy items in "...". Two panes fill the rest and scroll independently: tree 40% (min 360px) with a 52px header holding the Tree/Timeline/Graph tray and "Jump to first error", rows 36px with fixed 72px right-aligned duration and cost columns; detail pane with a 52px header (type icon, name, level pill, type word right), one inline metrics line (latency, cost, tokens, model) and tabs Preview (Input/Output blocks with copy buttons and a Formatted/JSON/Markdown switch), Metadata, Scores n, Comments n. Scores and Comments are trace-level and appear only in those tabs. IO blocks: 1px `--line` border, radius 10, `--surface-2`, 12px padding, 13px/600 label row with the copy button on the right. Below 1100px the panes stack, tree first at max 50vh, and the page scrolls.
- **Lists:** no "Slow" filter and no "+x% vs previous" deltas (they need new data).
- **Settings and auth:** tabs 40px with a 2px active underline starting at the page edge; row menus in a 48px right-aligned column. Auth form column 380px and vertically centred, inputs 40px, "Forgot password?" on the Password label's baseline.

## Phase 3 additions (evaluation: scores, queues, judges, datasets, experiments)
No token, font, radius or shadow changed. CSS for every item below is in the "PHASE 3 ADDITIONS" block of `sdlc/changes/2026-11-09-phase3-evaluation/design/mockup.html`. Mode stays Operate.
- **Nav:** an **Evaluation** group (Scores = analytics, Annotation queues, Evaluators, Datasets) sits between Build and Project. On mobile the bottom bar is unchanged and More holds the group. Settings tabs gain **Score configs** and **Evaluation** after Audit log.
- **Score chip** (`.sc`): `name: value`, 22px, radius 6, tabular figures, tinted by source with a glyph, never colour alone: API = neutral + key, Annotation (`.ann`) = `--accent-soft` + person, Eval (`.eval`) = 12% `--t-generation` mix + scales. Archived config = dashed (`.arch`); public feedback = API chip with a small `feedback` tag (`.fb`). Lists show at most 3 chips then `+n`.
- **Statuses** reuse `.pill` and add `.run` (accent-soft + spinner), `.capd` (warn, cost icon), `.skip` (dashed), `.pend`. Execution: Pending, Running, Succeeded, Failed, Skipped, Capped. Queue item: Pending, Completed, Skipped, Locked. Run item adds nothing new.
- **Progress bar** (`.prog`): 8px (6 thin), radius 999, segments completed `--ok`, skipped `--warn-bar`, active `--accent`, track `--sunken`; always paired with a text count and `role="progressbar"`. **Meter** (`.meter`) for spend vs cap turns `--warn-bar` when full.
- **Config-driven score input** (`.sin`): name + type hint, then a control by data type: number input (range hint), category buttons (`.cats`, `1-9` keys), boolean pair (`.bool`, `t`/`f`), plus a Save per row (Scores tab) or one footer action (review). Errors use `.field-error` text under the control.
- **Review screen** (`.rv`): object column (IO + compact read-only tree) and 400px score form, sticky footer (`.rvfoot`) with shortcut hints and Skip / Complete. Below 1100px it stacks; the footer stays sticky. Lock state is an `info` banner and disabled controls.
- **Banners** gain `.info` and `.ok`; **dialogs** (`.dlg`, 560 or 760 wide, radius 20, header/body/footer) and a right **drawer** (`.drawer`, 520) share the `--sh-pop` shadow; **toast** (`.toast`) holds results and Undo; **selection bar** (`.selbar`, near-black) appears on lists when rows are selected.
- **Filter tray score section** (`.frow`): score, operator, value, remove. Active conditions show as `.chip.f` chips and in the URL.
- **JSON editor** (`.jed`): mono 12.5, bar with name and validity, `.bad` outline plus message on a parse error. **Version rail** (`.ver`) and **line diff** (`.dl`) for item and template history. **Stepper** (`.steps`) for the import wizard. **Drop zone** (`.drop`).
- **Rule editor:** target radio cards (`.tgt`), sampling range, a variable mapping table (`.mapt`) with `.var` chips from Phase 2, and a preview panel (`.prev`). **Judge reasoning** (`.reason`) is a violet left-rule quote, text only.
- **Analytics:** distribution card (`.dcard`: histogram or bars, 5-up stat strip, "View as table" `details`), trend line with source told apart by solid vs dashed line and glyph, compare panel with preset button, confusion matrix (`.cmat`, cell tint by share, `agree` or `differ` word on each cell), stat tiles (`.tiles`), `Not enough data` block (`.nd`) below n = 10. Series use `--s1..--s6`.
- **Run comparison:** letter badges A-D (`.ltr`, `--s1..--s4`), baseline header underlined, aggregate table with a `Best` pill (`.best`, ok) and delta chips (`.dlt` up/dn with arrow and sign), per-item table (`.cmpt`) with a sticky item column, regressed cells on `--err-bg` with arrow and "true to false" or `-0.30` text, `Only regressed` switch, threshold and sort.
- Controls a role cannot use are not rendered (queue/dataset delete, rule create/enable/backfill, retry, New experiment, score inputs for Viewers, config edit). Disabled controls appear only to explain a lock or a not-yet-valid form.
