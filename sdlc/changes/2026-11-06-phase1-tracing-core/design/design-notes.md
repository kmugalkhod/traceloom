# Design notes: Traceloom Phase 1 UI

Mockup: `design/mockup.html` (one file, open it in a browser; the toolbar jumps to each task, and the theme toggle switches light/dark). Tokens: `DESIGN.md` and the `:root` block in the mockup. The impeccable skill was used (init for PRODUCT.md, new-work direction roll, detector). Its subagent finish reviewer was not run; I did two screenshot checks (traces, detail, share in light and dark, 1280 and 390) and fixed what they showed.

## Direction (two sentences)
Revised (v2) to follow the reference image the human supplied: a floating sidebar and one large rounded card on a quiet desk, white rounded panels, status pills, pastel initial avatars and a near-black primary button, with Inter for text and JetBrains Mono for ids. Light and dark are both fully designed from the same tokens, and red and amber appear only for Error and Warning.

**Changes in v2:** replaced the strip-rack look (condensed caps labels, 6px tabs, cobalt button, top bar) with the reference style; new shell (sidebar with user card, context bar in the card); pills for levels, sources and scores; tinted-header tables with 48px rows; stat cards with icon chips; larger radii; Inter replaces Barlow; Session and Environment columns are off by default so the table fits 1280px. Contrast was kept: field edges use a darker `--field-line` (>= 3:1), text is >= 4.5:1. Only the reference's look was used, not its brand, name or content.

## Screens in the mockup
Foundations; shell + 403/404 + shortcut help; auth (6 screens, error/loading/429); org picker, create org, members (Owner and Admin views); onboarding, API keys (created/secret-once/revoke/403), models (list/edit/403); traces list (filled, saved views, columns, loading, loading more, empty, error, Viewer); trace detail (tree, timeline, graph, three IO modes, XSS case, DEBUG toggle); comments, mention, scores, Viewer view; share toggle, delete dialogs, public page, 404, audit log; sessions, replay, users, user page; dashboard (filled, loading, empty, error). Each is shown at 1280px (or 560-900px for small forms) and most inside a 390px frame. The 390px frames use the same CSS through container queries, so they are the real mobile layout.

## Interactions
- Live frame in T-17: click Tree / Timeline / Graph, press `t` to toggle tree and timeline, `e` to select the first ERROR (hover the frame first), click rows to select. `?` opens help anywhere. Switches, segmented controls and tabs toggle in every frame.
- Filters live in the URL (shown in a URL bar in the mockup, which is an annotation, not UI to build). The environment and column choice persist per user and project.
- `j`/`k` move the cursor row (focus-coloured outline); `enter` opens; `/` focuses search.

## Accessibility
Text pairs are >= 4.5:1 on their surface in both themes (ink-3 on surface is about 5.5:1 light, 5.9:1 dark); focus ring 2px with offset on every control; level and type never use colour alone (icon + word); tabs, switches, tree (`role=tree`, `aria-selected`), dialogs (`role=dialog aria-modal`) and inline errors (`role=alert`, `aria-describedby`) are marked up; targets grow to 36px on mobile; reduced motion stops the shimmer, spinner and progress bar. Charts have text alternatives and every bar prints its number.

## Assumptions (no human could be asked)
- Product context was inferred from intent.md and spec.md (PRODUCT.md is labelled so). Platform: web.
- Brand: no logo exists. The woven-threads mark and "Traceloom" wordmark are placeholders to replace. All data is synthetic (support-agent demo, claude-sonnet-5-5 prices of $3 / $15 per 1M are illustrative, not real price claims).
- Fonts load from Google Fonts here; the app must self-host them. Chart bars are static SVG in the mockup; the app uses vendored uPlot with the same series colours. The indefinite progress bar is the HTMX loading indicator (the detector flags it as a loop; it stops under reduced motion).
- Release, Version and Session are in the column chooser; Release is off by default to keep 1280px readable.
- Delete by filter asks the user to type the count (extra safeguard beyond the spec).
- "Delete matching..." appears only for Admin+; "Request access" on 403 is optional and may be dropped.
- Log out is an account-menu item (not drawn open). The public share page uses a dark read-only bar and no nav.
- Pending-invite expiry: 7 days; reset link: 30 minutes (from `TRACELOOM_RESET_TOKEN_MINUTES`, shown as a sample value).

## Per task: what to build, and where
- **T-5 Shell, theme, error pages**: section "T-5". Build the shell (top bar, 200px nav, bottom tabs <760px), theme tokens with system default + toggle persisted, `?` help dialog, 403/404 inside the shell. Static CSS/JS only (`script-src 'self'`); no inline scripts (the mockup's inline JS must move to a static file). Focus ring and contrast per DESIGN.md.
- **T-6 Auth**: section "T-6". Sign-up (inline "Use at least 10 characters" error under the field), log in (429 banner with wait), forgot (identical reply for any address), reset (expired/used state), accept invite (role chip), invite-only sign-up notice, log out in the account menu.
- **T-7 Orgs/members**: section "T-7". Picker with per-org role chips, create-org dialog, members table with role select, last-Owner lock note, pending invites, Admin view without the Owner option and without Delete organization. Hide controls by role; never render them disabled.
- **T-8 Keys/models**: section "T-8". Onboarding with snippet when the project has no data, keys list, create dialog, one-time secret panel, revoke dialog with 401 note, 403 panels for Member/Viewer, models table with Built-in and Project badges ("overrides the built-in price"), edit form with tiers.
- **T-16 Traces list**: section "T-16". Search, filter chips, saved views menu (edit only for creator/Admin), column chooser, strip table with sortable headers, cursor row, Load more, loading/empty/error, Viewer variant, two-line strips at 390px. State in the URL.
- **T-17 Trace detail**: section "T-17" (first frame is live). Header, tabs Tree/Timeline/Graph (graph tab only with agent/tool/chain steps), DEBUG switch, Jump to first error (`e`), `t` toggle, IO viewer with three modes, generation grid (model, usage, cost + source badge, TTFT), XSS frames for all modes.
- **T-19 Comments/scores/bookmark/tags**: section "T-19". Bookmark, removable tags, comment composer with @mention list, delete rights (own or Admin), score chips + add-score form with validation, Viewer sees none of the controls.
- **T-20 Share/delete/audit**: section "T-20". Share switch + link in the header (Admin+), delete-by-filter dialog showing the match count, single delete dialog, public page (read-only bar, no nav), 404 when off, audit log table.
- **T-22 Sessions/users**: section "T-22". Sessions table with sort + pagination, replay with totals row and per-turn cost/latency/link, empty state with snippet, users table, user page with chart and recent traces.
- **T-23 Dashboard**: section "T-23". Totals row, traces over time, error rate by level, cost and tokens by model, latency percentile tables, top users, with loading, empty and error states; panels stack under 1100px.
