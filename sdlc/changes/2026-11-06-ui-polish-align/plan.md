# Plan: UI polish and alignment pass

From: spec.md (db24658) · Status: accepted · Accepted by: Kunal Mugalkhod

<!-- Status: draft -> accepted (engineer; tech lead for higher-risk) -> implemented.
     Keep this file in sync with the code: a deviation updates plan.md in the same commit. -->

This is presentation only. FR/NFR/AC ids refer to spec.md. The work runs as a team (2 developers, 1 QA, a designer for D-1), so it is cut into stories that own **separate files**. Parallel stories never edit the same file.

## CSS layout decision
`app.css` (1155 lines) stacks v1/v2/v3.1 overrides. Rather than edit those layers in place, which would mean many conflicts and a high regression risk, the new rules go in a **v3.2 layer** loaded after `app.css`:
- `traceloom/web/static/ui/base.css`: tokens (`--row-h`, `--ctl-h`, `--ctl-h-sm`, `--panel-head-h`) plus the shared rules FR-S1..S8 and FR-M1. Owned by S1.
- `ui/traces.css` (S2), `ui/dashboard.css` (S3), `ui/build.css` (S4), `ui/settings.css` (S5): screen rules, each owned by one story.

S1 creates all five files, with the four screen files empty, and links them from `base.html` and `base_auth.html`. After that, the stories never touch each other's CSS.

`app.css` is edited only in S1, and only to switch `.num` and `.stat b` from mono to Inter tabular. That is the one in-place change the spec requires (FR-S3). NFR-4's "app.css grows ≤15%" is read as **total CSS** growing ≤15% (about 180 lines across `ui/*`).

## Files that change
**S1: Foundation (shared rules, context bar)**
- `traceloom/web/static/ui/base.css` (new), plus empty `ui/traces.css`, `ui/dashboard.css`, `ui/build.css`, `ui/settings.css` (new).
- `traceloom/web/static/app.css` (modified): `.num` and `.stat b` change to `font-family: var(--font-ui)` + tabular-nums. Nothing else.
- `traceloom/web/templates/base.html`, `base_auth.html` (modified):
  - link the `ui/*.css` files;
  - `.ctxbar` gains a left `{% block crumbs %}{% endblock %}` slot (FR-S7);
  - a page with neither env nor range renders the crumbs there.
- `traceloom/web/templates/_macros.html` (modified): a `crumbs(items)` macro and an `empty_state(icon, title, body)` caller macro (FR-S6), both escaping everything.
- `traceloom/web/static/charts.js` (modified): axis font = `getComputedStyle(...).getPropertyValue('--font-ui')`, 11px, `--ink-3` (FR-D3).
- `DESIGN.md` (modified): the "v3.2 alignment pass" section.
- `tests/e2e/test_polish_shared.py`, `tests/unit/test_css_tokens.py` (new).

**S2: Traces list + trace detail** (after S1)
- `traceloom/web/templates/traces/list.html`, `_filters.html`, `_rows.html`: tray, toolbar, columns (FR-TL1..3).
- `traceloom/web/templates/traces/detail.html`, `_tree.html`, `_level.html`, `_io.html`, `_io_header.html`, `_panels.html`, `_scores_panel.html`, `_comments.html`:
  - the kv grid (FR-TD1);
  - crumbs moved into `{% block crumbs %}`;
  - split pane, tree toolbar and tree rows (FR-TD3/4);
  - IO (FR-TD5);
  - collaboration grid (FR-TD6).
- `traceloom/web/templates/traces/_tags_body.html`: chips with ×, plus an inline "+ Add tag". The "Edit tags:" row goes. The same form posts, field names and hx-targets stay (FR-TD2).
- `traceloom/web/templates/sessions/detail.html`, `users/detail.html`: crumbs block only.
- `traceloom/web/static/ui/traces.css`, `traceloom/web/static/trace.js` (only if the tag input toggle needs it; otherwise `<details>`).
- `tests/e2e/test_polish_trace.py`, `tests/api/test_trace_tags_markup.py` (new).

**S3: Dashboard + sessions/users lists** (after S1)
- `traceloom/web/templates/dashboard.html`: stat icon chips, the 8+4 grid, error-by-level fill, breakdown rows, top-users table (FR-D1..5).
- `traceloom/web/routers/dashboard.py`: read-only. The top-users traces count is shown only if it is already in the query result (FR-D5); no query change.
- `traceloom/web/templates/sessions/list.html`, `users/list.html`: table and empty-state alignment.
- `traceloom/web/static/ui/dashboard.css`.
- `tests/e2e/test_polish_dashboard.py` (new).

**S4: Prompts + playground** (after S1)
- `traceloom/web/templates/prompts/list.html`, `_empty.html`, `_filters.html`, `new.html`, `detail.html`, `_versions.html`: crumbs block, empty state (FR-PP1), form alignment (FR-PP2).
- `traceloom/web/templates/playground/index.html`, `_variant.html`: empty state and alignment (FR-PP3).
- `traceloom/web/static/ui/build.css`.
- `tests/e2e/test_polish_build.py` (new).

**S5: Settings, auth, mobile sweep** (settings/auth after S1; the mobile sweep after S2–S4)
- `traceloom/web/templates/settings/_tabs.html`, `settings/base.html`, `keys.html`, `members.html`, `models.html`, `connections.html`, `webhooks.html` (FR-SA1).
- `traceloom/web/templates/auth/*.html`, `base_auth.html`: only the auth panel classes; S1 already did the link tags (FR-SA2).
- `traceloom/web/static/ui/settings.css`.
- `tests/e2e/test_polish_settings_auth.py`, `tests/e2e/test_polish_mobile.py` (new, AC-10 over all pages).

## Dependencies
None. Playwright and pytest are already dev dependencies, and the fonts are already self-hosted.

## Order of work
1. **D-1 (designer):**
   - `design/mockup.html` and `design/design-notes.md`: trace detail, traces list and dashboard at 1440 and 390, built with the real tokens and following the spec.
   - The owner approves it on the board.
   - S2/S3 UI tasks wait for approval. S1's token and CSS skeleton may start before it.
2. **S1 (Foundation):**
   - T1: `ui/*.css` skeleton + links + tokens + `.num`/`.stat b` switch + `test_css_tokens.py`.
   - T2: shared control, table, error-row, empty-state and page-header rules (FR-S1..S6, S8).
   - T3: crumbs slot + macro + mobile context row (FR-S7, FR-M1).
   - T4: charts.js axis font + DESIGN.md v3.2.
   - T5: `test_polish_shared.py` (AC-1 context bar half, AC-2 header/num computed style, AC-3, AC-5 on one detail page, AC-11).
3. **In parallel after S1**, with two developers:
   - S2 (dev-1). Its order:
     - T6: traces list;
     - T7: trace header and tags (with `test_trace_tags_markup.py` first);
     - T8: split pane and tree;
     - T9: IO and collaboration;
     - T10: e2e.
   - S3 (dev-2): T11 dashboard; T12 sessions/users lists; T13 e2e.
4. **S4** (whichever developer frees first): T14 prompts; T15 playground; T16 e2e.
5. **S5:** T17 settings; T18 auth; T19 the mobile sweep (fix any 390px overflow left in the screen CSS files, with that screen's story owner already merged), plus `test_polish_mobile.py`.
6. Before/after screenshots of the 10 screens go into `design/after/` (scratch script; images only) for the review gate.

Each task leaves `sdlc.py verify` green.

## Tests that prove it
Every e2e test measures with `page.evaluate(getBoundingClientRect / getComputedStyle)` at fixed viewports, after `document.fonts.ready`, with the spec's tolerances. It seeds through the existing `live` / `trace_events` fixtures.

| AC | Test |
|---|---|
| AC-1 | `test_polish_shared.py::test_context_bar_controls_share_height`, `test_polish_trace.py::test_traces_toolbar_controls_share_height` |
| AC-2 | `test_polish_trace.py::test_numeric_columns_right_aligned_tabular_ui_font` (+ row height 44±1) |
| AC-3 | `test_polish_trace.py::test_error_row_not_tinted` |
| AC-4 | `test_polish_trace.py::test_header_kv_baseline`. `test_trace_tags_markup.py`: no "Edit tags:"; tags appear once; Member sees ×/add; Viewer sees neither. The existing tag add/remove flow tests stay unchanged and pass. |
| AC-5 | `test_polish_trace.py::test_breadcrumb_in_context_bar` |
| AC-6 | `test_polish_trace.py::test_split_pane_alignment` (tops equal, 40%±2%, duration right edges equal) |
| AC-7 | `test_polish_dashboard.py::test_stat_cards_and_chart_row` |
| AC-8 | `test_polish_dashboard.py::test_top_users_rank_alignment` |
| AC-9 | `test_polish_build.py::test_empty_states_centred` (prompts, playground) |
| AC-10 | `test_polish_mobile.py::test_no_horizontal_scroll[page]` over 6 pages; `test_ctx_row_single_line` |
| AC-11 | `tests/unit/test_css_tokens.py`: no `#hex`, `rgb(` or `hsl(` literal in `ui/*.css`. The existing dark-mode/contrast tests stay. |
| AC-12 | `python .sdlc/bin/sdlc.py verify`; the existing e2e journeys still record zero `problems` |

Existing class-name assertions (spec "Components and changes") are kept by construction. If one must change, the developer stops and escalates rather than editing the test.

## Risks
- **Riskiest step:** T7, the trace header and tags. It touches an HTMX fragment, role gating and a no-JS fallback. Mitigation:
  - write `test_trace_tags_markup.py` first (Viewer and Member);
  - keep the route, form fields and targets byte-identical;
  - run the existing tag flow tests.
- **Cascade conflicts:** the v3.2 layer overrides older `app.css` rules, and specificity fights can leave a rule that silently doesn't apply. Mitigation:
  - every AC is asserted on computed style, not class presence;
  - `ui/base.css` uses the same selectors as `app.css`, not `!important`. One departure: `app.css` marked `.err-text` as `!important`, which no later rule can beat, so T-2 dropped that `!important` there (the same rule already exists without it) and `ui/base.css` sets `.tbl td.err-text` to `--ink`.
- **Empty-state tile:** the `empty_state` macro now wraps its icon in `<span class="tile">` so `.empty .tile` can draw the 40px tinted tile.
- **Breadcrumb move:** this could break `test_mobile_context.py` ("no context row on detail pages"). Mitigation: the crumbs live in the desktop `.ctxbar` only. On mobile, the crumbs stay at the top of the page body (CSS shows and hides the two copies; the hidden copy is `hidden` / `display:none`, so it is not doubled for screen readers).
- **Flaky pixel tests:** use fixed viewports, `fonts.ready`, and ±1–2px tolerances.
- **Phase 2 still in review:** if its branch changes, rebase this one onto it before the merge.

## Alternatives considered
- **Rewrite `app.css` as one clean v3.2 file:** cleaner, but large and risky, and it would conflict with every parallel story. Rejected for this pass; it is a good follow-up once this lands.
- **Tailwind or another CSS framework:** a new dependency and a build step. Rejected per NFR-1.
- **A compact-density toggle, the histogram above traces, resizable panes:** out of scope per intent; follow-ups.
- **Keep the full-row error tint and only fix alignment:** rejected. The owner's reference and the competitor tables mark errors with a pill and marker only, which reads calmer.

## Verification target
- `python .sdlc/bin/sdlc.py verify` is green (setup → build → test → lint), with all new `test_polish_*` and `test_css_tokens` tests passing and no existing test edited.
- Before/after screenshots of the 10 screens at 1440 and 390 are in `design/after/`, and the owner judges them "aligned and professional" at the review gate.
- `sdlc:verifier` passes on the team branch.

## Deviations
- **2026-11-07, D-1 review (owner):** trace detail becomes a full-height workspace. It has a compact header, two panes that scroll independently, and right-pane tabs (Preview, Metadata, Scores, Comments) in place of the stacked panels below (spec FR-TD1..TD6, AC-4, AC-6 revised).
  - T-6 (header + tags) builds the compact header.
  - T-7 (panes + tree) builds the full-height panes.
  - T-8 (IO + comments/scores) builds the detail-pane tabs and moves the existing Scores/Comments fragments into them. Their routes, forms and permissions are unchanged.
  - The tabs reuse `[data-tabs]` from app.js, so no new JS framework is involved.
- **2026-11-07, team breakdown:** the planner split the screen CSS more finely than planned: `ui/trace-detail.css`, `ui/trace-tree.css`, `ui/trace-io.css`, `ui/prompts.css` and `ui/playground.css` in addition to the files listed. T-7 depends on T-6 and T-8 on T-7, so the trace detail work is serialized.
- **2026-11-07, QA on T-2..T-4:** AC-2 now matches FR-S4. Single-line rows are 44px; rows with a muted second line (the traces list) are ≤56px.
- **2026-11-07, QA on T-5:** the existing `test_mobile_status_tabs_and_env_switcher_fit_390px` stays as is. At 390px the status tray fits on one row without scrolling, achieved with tighter segments. Scroll is only a fallback below 360px.
- **2026-11-07, T-8, owner ruling (Kunal Mugalkhod):** `tests/e2e/test_journey_viewer.py::test_controls_load_on_the_real_detail_page` waited for `#comments` to be visible on load. In the approved workspace (FR-TD5), Comments is a tab and stays hidden until selected. The test now opens the Comments tab (and the Scores tab where it checks `#scores`) before its checks. Every assertion is kept: the Viewer has no write controls, and the Member can bookmark and comment. No assertion is removed or weakened.
