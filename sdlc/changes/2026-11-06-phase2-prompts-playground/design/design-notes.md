# Design notes: Phase 2 prompts and playground

Mockup: `design/mockup.html` (open in a browser; Light/Dark switch at the top, jump links per task). Frames at 1280px and in 390px phone frames use container queries, so what you see is the real responsive layout. A live Run / Stop demo is at the bottom of the page.

## Direction
Brownfield: the approved v3 look (floating sidebar, inner rounded card, white panels, near-black primary, status pills) is unchanged and extended with a quiet, text-first prompt-engineering surface. Versions, labels and streaming output are the new visual vocabulary; every new component is built from existing tokens.

## Screens
Prompts list (folder tree, search, filters, sortable columns) and new prompt / new version editors; prompt detail (Versions, Compare, Preview, Metrics tabs); Settings: Webhooks and LLM connections; Playground (editor + output) and Compare (up to 4 variants); Save as prompt dialog; Open in playground on trace detail.

## Components (all in DESIGN.md "Phase 2 additions")
Label pill, version pill, type pill, tag, code-style editor with variable / include highlights, message card, line diff and config diff, status chip, Run/Stop, usage row, compare column, connection row, secret-once panel, toast, hint under a disabled action.

## Tokens
None added or changed. New CSS lives in the mockup's "Phase 2 additions" block; developers copy it into `traceloom/web/static/app.css` next to the existing tokens. Two small fixes ride along: `.switch[aria-checked=true]::after` uses `--on-accent` (the white thumb vanished on the light switch in dark mode), and `.usage` margins are reset.

## Interactions
- Tabs on prompt detail are HTMX partial swaps; Metrics loads `GET /project/{id}/prompts/{name}/metrics`.
- Set production is one click, no confirm; a toast with Undo appears and the Label history gets a new top row. Members see it disabled with "Admins only. Move staging instead."; the server still returns 403.
- Run streams (SSE or chunked fetch) into an `aria-live=polite` region with a caret; Stop aborts the request; usage cells fill as they arrive. Run is disabled with a countdown message on 429.
- Compare: "Run all" starts every column independently; each has its own status chip. Add variant disables at 4 and explains why.
- Connections: Test connection shows an info row while pending, then an ok or err banner. Secrets: shown once (webhook) or never (connection keys, only `…last4`).
- Editor highlight: mirror the text in a `<pre>` behind a transparent textarea, or render highlights server-side on read-only views. Always escape; never `innerHTML` user content.

## Accessibility
Focus ring 2px offset 2px everywhere; status is icon + word + tint; label and variable chips carry word and outline; disabled controls use `aria-disabled` and a visible reason; dialogs use `role=dialog aria-modal` and return focus; streaming output is `aria-live=polite` with `aria-busy` while running; form errors are `aria-invalid` plus `aria-describedby` and `role=alert`; targets are 36px+ on mobile with 16px inputs; caret and spinners stop under `prefers-reduced-motion`; colour pairs reuse the v3 tokens (>= 4.5:1 text, >= 3:1 field edges).

## Assumptions (no human to ask)
1. Nav: a new "Build" group; mobile bottom bar becomes Traces, Prompts, Playground, Dashboard, More (Sessions, Users, Settings move under More). Needs a "More" sheet in T-6/T-25 build or a simple page; flag if you prefer a different mobile nav.
2. The spec says up to 4 compare variants while the task text says a "5th cannot be added"; designed as max 4 with Add variant disabled at 4 (consistent with both). Layout: 3 across, 4 as 2 by 2, stacked at <=1100px.
3. Prompt list columns: Name (folder prefix muted, type pill beneath), Latest, Labels (with the version each points at), Tags, Updated. Content search matches show one snippet line under the name.
4. Version rows show a checkbox pair for Compare plus a Compare tab with From/To selects (pick any two versions).
5. Production move has no confirmation dialog (spec: one click) and relies on Undo; Admin-only.
6. Webhook secret is shown in a dialog right after creation; there is no rotate flow in this phase beyond a mention.
7. LLM connection provider list: Anthropic, OpenAI, Azure OpenAI, Bedrock, Google, OpenAI-compatible; only Anthropic, Azure, Bedrock and OpenAI-compatible forms are drawn (OpenAI and Google follow the Anthropic form: name + key + custom models).
8. Playground parameters panel shows only supported parameters (example: top_p and stop hidden for claude-sonnet-5-5). Per-model support comes from the backend.
9. Copy and data are synthetic. Model names and prices follow the spec's seed.
10. Webhook "Send test" creates an `prompt.test` delivery with a "test" chip.

## Per task: what to build and where in the mockup
- **T-6 Prompts list, new prompt, version editor** (sections "T-6"): list with folder tree (collapses to a filter row at <=760), search + Tag + Label + Sort, filter chips, no-results, loading skeleton, empty, Viewer view (no New prompt) and 403 on /new. New prompt: name (inline error), type segmented, tags, editor with highlighted variables and a variables list panel, config panel, commit message (required, inline error), labels checkboxes (production locked for Member, enabled for Admin). Chat variant uses message cards. Script tags display as text. Version editor starts from v7 and saves v8.
- **T-7 Prompt detail** (section "T-7"): header (name, type, latest, tags, Open in playground, New version), tabs Versions / Compare / Preview / Metrics. Versions table with label pills, commit, author, Set production; selected-version content on the right; Label history (audit rows). Admin states: hover, moved (toast + new audit row). Member: disabled with reason. Viewer: no actions. Compare: From / To selects, summary chips, escaped line diff per message, config key diff. Preview: variable inputs, resolved messages with Included-from blocks, dependency list, and the missing-reference / cycle error banners.
- **T-9 Metrics partial** (section "T-9"): version select; four stat tiles (generations, median latency, tokens, cost); score chips; linked generations table (50 max) with trace links; loading skeleton; empty state with the SDK snippet; 404 page for another project's prompt.
- **T-17 Webhooks** (section "T-17"): Settings tab (Admin only; Members get a 403 panel and no tab). Table of endpoints (switch, last delivery pill, Send test); deliveries table (status, attempts n of 5, code, last error); add dialog; inline private-URL error; secret-once dialog with signature recipe; test delivery row; empty state; 390px layout.
- **T-24 LLM connections** (section "T-24"): rows with provider tile, key hint, last test pill, Test connection / Edit / Delete; add dialog with a field set per provider (Anthropic, Azure, Bedrock, OpenAI-compatible with required base URL); edit shows "Saved, ends in …a91f" plus Replace key; test states (testing, passed, failed with scrubbed message); empty; Member 403 and no tab.
- **T-25 Playground** (section "T-25"): top bar (Compare variants, mobile Run), left editor (connection, model, message cards, Variables, Parameters, Tools, Structured output as collapsible sections), right Output (Run/Stop, Trace this run switch, status chip, usage row). States: idle, streaming, done, escaped output, tool call, stopped, provider error, 429, no connections (Admin links to settings; Member told to ask an Admin and named Admins), opened from a trace with no connection chosen. 390px stacks editor above output.
- **T-26 Compare** (section "T-26"): breadcrumb, variant count, Add variant (disabled at 4 with reason), Trace this run, Stop all / Run all, shared messages and inputs, columns A to D each with own model, params, output, usage. Stacks at <=1100px (see the 900px frame) and on mobile.
- **T-27 Save as prompt / Open in playground** (section "T-27"): Open in playground button in the generation panel header (Members and Admins; absent for Viewers); Save dialog with target segmented (new version of a chat prompt / new prompt), prompt select showing "saves as v8", name field with exists-error, required commit message, labels (production locked for Member, enabled with audit note for Admin), saved toast.


---
## Revision 2: declutter (human feedback)
The whole app, including Phase 1 pages, was too crowded. The mockup now has a **Before / after** section (jump link "Before / after") with Phase 1 frames untouched ("Before") next to the same markup with the v4 rules ("After"): traces list, trace detail and dashboard at 1280 and 390, plus sessions, users, settings and sessions mobile after-only. Every Phase 2 frame uses the v4 rules.

What changed (tokens and rules are in DESIGN.md "Declutter pass"): base text 15px / 1.55, page titles 32px, rows 60px, controls 40 to 44px, 32px section gaps, 40px page padding; panel, table-header, stat and dialog borders removed; one primary action per view with the rest in "..." menus.

Per area:
- **Playground (T-25/T-26):** equal wide columns; Run is the single primary; Settings panel (Parameters, Tools with a JSON validation state, Schema) opens on demand and overlays the right edge; Compare, Load prompt and Save as prompt moved into the "..." menu. Compare columns have 24px gaps, a series-coloured top bar plus letter badge, status chip, and a one-line usage summary. 4 variants still show as 2 by 2, stacked at <=1100px.
- **Prompt detail (T-7):** title > content > metadata. Version rail on the left, version content on the right with Set production as the one action (disabled with a reason for Members, absent for Viewers), collapsed Label history, More menu (Open in playground, Copy name, Compare with previous, Delete for Admins).
- **Settings (T-17, T-24):** one-line rows with status pill, ghost Test and a "..." menu; fewer columns in the deliveries table; Provider select in the add dialog.
- **Existing pages:** sidebar and shell get roomier spacing; traces list loses the URL bar and the secondary header buttons (saved views, columns, delete matching) into a "..." menu and hides Tags and Scores columns below 1400px; trace detail moves Public share and Delete into a menu and hides Environment, Release and Version from the header; dashboard and stats lose their dividers.

Assumptions added: (a) hiding Tags and Scores columns and the trace-header fields means they need a home in the row/details panel (not drawn); (b) the Settings panel in the playground overlays the response while open (static stack under 1100px); (c) the "..." menus are drawn open in the prompt detail and playground frames only, other menus follow `.pop`.
