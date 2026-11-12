# FE-RD — Workspace Gates For Every Member (FEB-07), The Transitive Licence Policy (FEF-06), And The Blue Redesign

## Phase status

| Phase | State | Evidence (E:\wasla-ferd-notes\) | Made |
|---|---|---|---|
| A0 state, four skills, untracked, infrastructure | done | evidence/untracked-start.sha256 | worktrees wasla-ferd-fix (branch fe-rd-blue-redesign-20261112), wasla-ferd-base; wasla-ferd-pg, -redis, -net; venv |
| B1–B3 FEB-07 (A-BE) | done | evidence/probes_*.clean.json, logs/gb2, logs/e2e_smoke.log, mutation/backend_results.txt | fe5b063, 7fefed0, 316eb85 = BE_HEAD |
| A1 licence policy | done | logs/licences_after6.txt | 05b159b |
| A2 "before" captures, impeccable before | done | screens/before (58), skills/impeccable/audit-before.md | — |
| A3 design brief | done | DESIGN_BRIEF.md, skills/* | — |
| A4–A6 tokens, primitives, composites, screens | done | — | e90aa9d … f5a6784 |
| A7 proofs, composition passes, impeccable after | done | evidence/measure_final.txt, contact-sheet/index.html, skills/impeccable/audit-after.md | fc1be17, d70f1e9, 5130289, 267968c, a344877, 6964a49, dc53ec7, 05e0ee9, a984936 |
| A8 frontend.yml, docs, ADR-195 | done | — | 322cca5, bc9d293, 5c11d75 |
| A9 mutation, diff guard, gates → FERD_HEAD | done | mutation/*.txt | FERD_HEAD = a984936 |
| B verify worktree | done | logs/gb2_verify, logs/v_* | E:\wasla-ferd-verify |
| B merge | done | — | 3199d07 (parents 80b0302, a984936) |
| B report, push, workflows | see sections 20–21 | | |
| C previews | see section 22 | | |
| D cleanup | see section 26 | | |

## 1. Executive Summary

- **What a member now learns (FEB-07).** `/auth/me` gives every workspace its `status` and keeps a suspended one in the
  list. A suspended workspace answers 403 `workspace_suspended`; a member whose membership was withdrawn gets 403
  `membership_revoked`; somebody who never belonged - and everyone once a workspace is deleted - still gets the same
  404 a random id gets. `GET /api/v1/workspace/gates` tells any member, even of a suspended workspace, the status,
  whether a renewal is owed, the subscription status, the trial's end and two channel-capacity flags - never an
  amount, price, invoice, card or limit.
- **Licences (FEF-06).** The owner's accepted policy now fails `frontend.yml` on any of the 1,043 packages: 174 shipped
  (the three apps and the workspace packages they depend on), 869 build-only, **0 violations, 0 exceptions**.
  Enforcing it found eight shipped violations: `astro` and `@wasla/config` were runtime dependencies of build tooling
  and moved to `devDependencies`; `caniuse-lite` (CC-BY-4.0 browser-support data Next.js reads at build time) is
  allowed as named data by **the owner's decision on 2026-11-12**.
- **The look.** One blue family (blue-50..950, hue ≈ 214°) plus white; red, amber and green only for statuses; the
  console's own blue band. Measured over 27 screens × en/ar: **1280 → 65.0 % dominant / 25.7 % secondary / 9.2 %
  accent; 390 → 63.2 / 26.7 / 9.6** (bands 55–68 / 24–36 / 6–12), every screen inside its hard limits. The first
  measurement of the restyled screens was 77.5 / 21.5 / 0.9 - the frame was redesigned (a dark header band, the
  action-blue footer as the one brand band, the auth brand panel, light-blue state bands) until it held.
- **The four skills** (impeccable, taste-skill, ui-ux-pro-max, emil-design-eng) were found, read in full, hashed and
  used for their parts; outputs kept (section 4). impeccable's audit: 15/20 before → 20/20 after, no P0/P1.
- **Nothing a person can do changed**: FE-2's journeys 312 / 25 (section 11), the session, routing and API code
  byte-identical.
- **For the owner first:** the contact sheet `E:\wasla-ferd-notes\contact-sheet\index.html` (58 rows, before | after);
  the previews (section 22); FEF-18 (J6's first round under heavy local load, same on both heads); the Arabic, French
  and Turkish texts - including the new brand line - are unreviewed drafts (FEQ-08); the wordmark is text.

How the request was read: as the prompt's reading table, applied without change. FE-3 is not built here.

## 2. Source Branches and Exact Heads

| Name | Commit |
|---|---|
| FE2_CI_GREEN_HEAD = ORIGIN_CANONICAL_START_HEAD | 80b03024c74fc7683d953db94f2a0eb0ebe07baa |
| BE_HEAD | 316eb85 |
| FERD_HEAD (fix branch fe-rd-blue-redesign-20261112) | a9849368444f65d70d3e31d87bc952a1e04cb40c |
| CANONICAL_MERGED_HEAD | 3199d07674ae5b8c61a4459c1bc4120af15d2eea (parents 80b0302, a984936) |
| main | 8e3a1ba (untouched) |
| Alembic head | 0138 (unchanged) |
| LAST_ADR before / this stage | ADR-193 / ADR-194 (FEB-07), ADR-195 (licences, redesign) |
| OPENAPI_SHA256 | 57bed53c… → b606e3a79f6cc5454d5e97b546f4f36131c38776ec08f4e3164efe4f47b6f297 |

## 2a. FEB-07 — Workspace Gates

| Probe | Before (80b0302) | After (BE_HEAD) |
|---|---|---|
| P-G01 /auth/me, owner of Running + suspended Paused | 200, only Running listed, no status | 200, both listed, `status` active / suspended |
| P-G02 member, suspended workspace, GET /conversations | 403 permission_denied "This workspace is not available." | 403 **workspace_suspended**, same sentence |
| P-G03 withdrawn member, GET /conversations | 404 not_found | 403 **membership_revoked** "You no longer have access to this workspace." |
| P-G04 stranger: real / random / suspended workspace | 404 not_found ×3 | 404 not_found ×3, identical message (no leak) |
| P-G05 member: /billing/summary · /workspace/gates · gates while suspended | 403 · 404 · 404 | 403 · **200** · **200** (`status: suspended`) |

As built: `get_workspace_membership` is the one place membership is decided (revoked → `membership_revoked` only while
the workspace exists; a deleted workspace keeps the 404); `get_active_workspace` adds `workspace_suspended`;
`GET /workspace/gates` resolves the membership alone - the one route that answers a suspended workspace, held by
`test_route_authorization.py`. Body keys exactly: `status`, `payment_required`, `subscription_status`,
`trial_ends_at`, `channel_capacity{over_limit, reduction_pending}`; `Cache-Control: no-store`; the workspace bucket.
Status values: `active`, `suspended`. Routes that answer for a suspended workspace: `/auth/me`, the switch, sign-out,
platform routes (unchanged), and now `/workspace/gates`. No migration; the stream's reasons unchanged.

Contract: `openapi.json` regenerated by `scripts/export_openapi.py` (two exports byte-identical, `--check` 0), 259
operations (+1); diff = the path, `WorkspaceGates`, `ChannelCapacityGates`, `WorkspaceSummary.status`; `schema.d.ts`
by the generator (drift 0). Catalogues: both codes in en / ar / fr / tr.

| Gate | Result |
|---|---|
| GB-1 | `test_workspace_gates.py` 27 passed (+1 route-authorization test) |
| GB-2 | 22 files green in the fix and the verify worktree (e.g. gates 27, auth_endpoints 11, switching 9, authorization 15, lifecycle 26, revocation 26, invitations 8, billing_authorization 23, entitlements_api 5, channel_capacity 15, envelope 14, route_authorization 8, tenant_isolation 10, events_stream 14, orphan 12, platform reads 13, platform roles 11, grant withdrawal 14, hierarchy 22, documentation_truth 27, deployment 69, openapi_export 16). Existing tests changed because their expected answers are exactly what FEB-07 changes: switching (revoked → 403 membership_revoked), revocation sweep (also walks the gates route), auth_endpoints stub (new keyword + status assertion), route authorization (membership dependency), openapi count 258 → 259 |
| GB-3 | ruff, black, mypy (983 files) clean; `type: ignore` 263 = 263 |
| GB-4 | export `--check` 0; drift 0; Alembic 0138 |
| GB-5 | M-Y01..M-Y05 killed, control green |
| GB-6 | J1–J10 as FE-0 expects; J3: 403 membership_revoked (gates too), stranger 404 = random id; J8: 403 workspace_suspended, /auth/me suspended, gates 200 suspended → active after restore; the signed-forward lines unchanged |

FE-3 reads `/auth/me` `workspaces[].status` for the switcher and `GET /workspace/gates` once per workspace switch for
the banners; it maps `workspace_suspended`, `membership_revoked` and the stream's `workspace_unavailable` /
`membership_revoked` to its states. Until FE-3, the workspace list shows a suspended workspace without its status
(FEF-19).

## 3. Environment

| Item | Value |
|---|---|
| Node / pnpm / Turborepo | 24.15.0 / 12.18.0 (corepack shim in the notes) / 2.11.10 |
| Browsers | Playwright 1.65.0's Chromium, Firefox, WebKit |
| Docker / compose | 29.8.0 / v5.5.1 |
| wrangler | 4.158.0; account "Mohamedshhahat1@gmail.com's Account" (1cc89863…) = FE-2's |
| Added | pngjs 7.0.0 (MIT, published 2023-02-20, dev-only, for the measurement) |
| pnpm audit (prod / all) | 0 / 0 at every severity, before and after |
| Long paths | Windows LongPathsEnabled = 1; git core.longpaths unset |
| Disk | C: 5.4 GB free at the start (below 20 GB - noted in the draft), as low as 2.5 GB during image builds; E: 225 GB |
| Machine | heavily I/O-bound today: each frontend install 20–100 min, a cold app import 143 s |

Operator workarounds, recorded: BuildKit's external `docker/dockerfile:1` frontend crashed ("grpc server closed"), and
the classic builder ignores `Dockerfile.dockerignore` - images were built with BuildKit's built-in frontend from a
notes copy of the same Dockerfiles minus the `# syntax=` line; `e2e:local` always rebuilds with `--build`, so its steps
were run by hand (`compose up --no-build`), unchanged otherwise; 2 Playwright workers instead of 4 locally (browser
crashes under load).

## 4. Design Skills

| Skill | Path | SKILL.md SHA-256 | Phases | Outputs (notes\skills\) | Decided |
|---|---|---|---|---|---|
| ui-ux-pro-max | ~/.claude/skills/ui-ux-pro-max | 43d1c5ad…0d7861f7d | A3, A7 | 01_design_system.md … 10_stack_next.md, checklist-after.md | role strategy (navy structure, one CTA blue), UX rules (labels, errors at field, focus not obscured, targets), the checklist (PA-03, CM-02) |
| taste-skill (design-taste-frontend 2.5.1) | ~/.claude/skills/taste-skill | dfd1190a…24209721f | A3, A6, A7 | direction.md | one system, one primary per view, 13 px floor, contrast-led hierarchy, no violet, the accent as the one brand band (RS-02, a344877) |
| emil-design-eng (Emil Kowalski) | ~/.claude/skills/emil-design-eng | f6655603…0f43c7a9 | A3, A5 | motion-review.md | curves, durations, press scale, exits faster, instant later tooltips, nothing animates on keyboard (CM-05..07) |
| impeccable 4.8.2 | ~/.claude/skills/impeccable | e0565c6c…7460e56f4b48 | A2, A7 | audit-before.md, audit-after.md | the audit both times, craft floor (browser surfaces, no eyebrows, no coloured side borders) |

Guidance not taken (SK-04 / SK-05): impeccable's engine (would download itself - network); ui-ux-pro-max's Plus
Jakarta Sans from Google Fonts and GSAP (PA-07, CSP, no runtime dependency); taste-skill's 90–95 % neutral (the owner's
60/30/10 wins), photographs, dealt heroes, moments, effects, its scripts and kit, a fixed header (could intercept FE-2's
clicks), one-section-per-screen marketing (FE-10); emil-design-eng's origin-aware popover scale (DS-07 measures the box
as it opens), colour transitions on hover (axe scans state changes), "gentler, not zero" reduced motion (CM-07);
the hover-transition item of ui-ux-pro-max's checklist (same reason).

## 5. Decisions Applied

| ID | Applied (source) |
|---|---|
| WG-01..08 | as section 2a (prompt) |
| LC-01..07 | `licence-policy.json`; shipped = apps + workspace packages through `dependencies`; SPDX reader; exceptions build-only and exact; `shippedDataPackages` = the owner's caniuse-lite decision (owner, 2026-11-12) |
| SK-01..06 | section 4 |
| PA-01..08 | the PA-02 palette unchanged (ui-ux-pro-max agreed on roles, proposed hue 201° - not taken, one family); `canvas`, `secondary-subtle`, `brand`, `primary-pressed`, `on-brand(-muted)`, `selection`, `focus-inverse` (blue-300, 3.2–7.0:1 on the bands - found by the contrast test: the focus blue on blue-800 was 2.83) added; status colours unchanged |
| CM-01..07 | Button variants kept + `busy`; drawn disabled; Link `tone="inverse"`; composites AuthLayout, BrandPanel, Card, PageHeader, Wordmark; motion per section 8 (emil-design-eng) |
| RS-01..05 | screens through tokens and composites; one new line (`common.brandPromise`, four locales, drafts); wordmark as text |
| VQ-01..06 | sections 10–12 |
| PV2-01..02 | section 22 |

Decisions NOT taken - each to its owner: FE-3 (shell, gates, home, channels), FE-4..8, FE-9 console screens, FE-10
marketing pages and copy, a dark theme (FEQ-09), a committed visual baseline (FEQ-11), a logo / brand mark / imagery,
a font change, review of ar/fr/tr (FEQ-08), FEF-12/13/14 (residuals), FEB-30/31 and FEB-14/16/17/03/04/05/10/08
(their stages), drawing the new status/codes/gates (FE-3), staging/custom domains/DNS/Access/production Workers,
real devices (FE-11), `paths-ignore` (audit 24), nightly/real-clock/Linux-local lanes (light gates), deleting
E:\wasla-fe1-* and E:\wasla-fe2-* (the user).

## 6. The Licence Policy

| | Before (FE-2) | After |
|---|---|---|
| Direct rule | 108 direct, 0 outside | 109 direct (pngjs), 0 outside |
| Transitive | 15 kinds printed, not enforced | enforced; shipped 174: MIT 151, Apache-2.0 9, ISC 6, BSD-3-Clause 3, OFL-1.1 2, 0BSD 1, CC-BY-4.0 1 (caniuse-lite, owner) · build-only 869: MIT 677, Apache-2.0 109, ISC 30, BSD-2-Clause 20, BSD-3-Clause 11, BlueOak-1.0.0 8, CC0-1.0 4, MPL-2.0 4, Python-2.0 1 |
| Violations found | — | 8 shipped (astro chain: lightningcss MPL-2.0 ×2, sax / lru-cache / common-ancestor-path BlueOak-1.0.0, mdn-data CC0-1.0 ×2; caniuse-lite CC-BY-4.0) + 5 more through `@wasla/config`'s ESLint chain once workspace packages were followed |
| Done | — | astro → devDependencies (marketing serves only `dist/`); @wasla/config → devDependencies in the three apps (imported only by build and lint configs); caniuse-lite allowed by the owner as named data |
| Exceptions | — | 0 |

Tests: the policy as accepted; GPL shipped fails; LGPL build-only fails; OR / AND / parentheses / WITH; exact exception
passes and a moved version is stale; a shipped exception is refused; MPL-2.0 builds but never ships; CC-BY-4.0 only on
named data; no licence / unknown / custom text fails; caniuse-lite ships only under the owner's decision; workspace
links followed through `dependencies` only; the real lockfile passes (120 s timeout - once exceeded locally under the
image builds' load; seconds on Linux).

## 7. The Palette and the Roles

| Token | Old (FE-1) | New | Role |
|---|---|---|---|
| surface / surface-raised | #ffffff | #ffffff | dominant |
| canvas (new) | — | blue-50 #F4F8FE | dominant |
| surface-sunken | #f5f6f8 | blue-50 | dominant |
| secondary-subtle (new) / info-subtle | — / #eff6ff | blue-100 #E4EEFC | secondary |
| border / border-strong | #e3e6eb / #c4c9d2 | #D3E0F2 / #7088AE | secondary |
| brand (new) / console | — / #6d28d9 violet | blue-900 #122F5E / blue-800 #153E7D | secondary |
| text / text-muted | #14171f / #5b6270 | blue-950 #0A1A35 / #4B5C7A | text |
| primary / hover / pressed (new) | #0f766e teal / #0b5e58 / — | blue-600 #1C5FC2 / 700 / 800 | accent |
| focus / focus-inverse (new) | #2563eb / — | #3B82F6 / blue-300 | accent |
| info | #1d4ed8 | blue-700 | accent |
| danger / warning / success (+subtle) | unchanged | unchanged | status |

Contrast (computed by `test/tokens.test.ts` from tokens.css, 49 pairs: FE-1's 23 + 26 role pairs): every pair at its
threshold. Lowest by threshold - text: primary on secondary-subtle 5.18, text-muted on secondary-subtle 5.76; non-text:
focus-inverse on primary 3.22, border-strong on canvas 3.38, focus on canvas 3.45. Highest: text on surface 17.33.

## 8. Type, Space, Radius, Elevation and Motion

| Token | Old | New |
|---|---|---|
| text-xs / 3xl / 4xl (new) | 12 / 30 / — | 13 / 32 / 40 px |
| spacing-11 / 12 / field / form / hero (new) | — | 64 / 96 / 44 / 440 / 560 px |
| radius sm / md / lg / xl (new) | 4 / 8 / 12 / — | 6 / 8 / 12 / 16 |
| shadows | rgb(16 24 40) | rgb(10 26 53) (blue-950), offset + blur |
| motion | fast 120, base 200, cubic-bezier(0.2,0,0,1) | press 120, fast 150, base 200, slow 240; ease-out (0.23,1,0.32,1), ease-in-out (0.77,0,0.175,1) |

Motion as built: press `scale(0.97)` (never disabled or busy); colours change at once on buttons, fields and tabs;
dialogs enter by scale alone and fade out; popovers, menus, selects, tooltips fade in place; a later tooltip opens at
once; toasts slide from the inline end; focus never animates; under reduced motion every transition 0 s and no
animation (CM-07).

## 9. Buttons, Primitives and Composites

Button (4 variants, rest / hover / pressed / focus-visible / disabled / busy, en and ar - the Foundations "Buttons
matrix"); IconButton, Link (+inverse), Input / Textarea / Select / Combobox (44 px, 3:1 boundary), Checkbox / Radio /
Switch (2 px boundaries, accent on), Dialog / ConfirmDialog (blue-950 scrim), Popover / DropdownMenu / Tooltip, Tabs
(accent indicator), Toast (no side border); EmptyState / ErrorState (cards with a light-blue icon band), FormField.
New: AuthLayout, BrandPanel, Card, PageHeader, Wordmark. Storybook: 38 → 47 stories (Foundations 5, Layout 4); axe 76 →
94 story × locale runs.

## 10. The Screens

| Screen (en/ar) | 1280 dominant / secondary / accent | 390 |
|---|---|---|
| web login, forgot, reset, invitation | ≈ 61 / 29 / 10 | ≈ 61 / 27 / 12 |
| web signup | ≈ 60 / 31 / 9 | ≈ 70 / 21 / 9 |
| web workspaces new / list | ≈ 59–61 / 30 / 9–11 | ≈ 62–64 / 27–30 / 8–11 |
| web signed-in placeholder | ≈ 70 / 19 / 10 | ≈ 68 / 21 / 8 |
| web 404, session-unavailable | ≈ 67 / 24 / 9 | ≈ 64 / 26 / 9 |
| console home, 404 | ≈ 70 / 19 / 11 | ≈ 66 / 24 / 10 |
| marketing home ×4, 404 | ≈ 52–74 / 18–37 / 9–11 | ≈ 47–70 / 20–42 / 9–12 |
| **average (27 screens)** | **65.0 / 25.7 / 9.2** | **63.2 / 26.7 / 9.6** |

Per-screen numbers: `evidence\measure_final.txt` (and `measure_verify.txt`: every share within 0.1 point). Before
(FE-1's palette, `before-tokens\`): 1280 98.2 / 0.4 / 1.2, 390 96.6 / 0.7 / 2.2.

## 11. Proofs

| Proof | Result |
|---|---|
| FE-2 journeys (six projects + stream-restart) | 312 passed / 25 skipped equivalent: 310 + J6 + stream-restart; per project 56 entries each, as FE-2. Final fix run (2 workers): 360 passed (incl. 50 visual), 1 failed (J6 Firefox desktop round 1: two refreshes), stream-restart not run (depends on all) → J6 ×5 10/10, stream-restart passed. Verify run: 359 passed, 2 failed (the same J6; a Firefox protocol error closing a context in an axe test), → each alone: passed |
| J6 A/B (Firefox desktop ×5, en+ar) | start head's builds 10/10; this head's builds 10/10 (FEF-18) |
| Unit tests | web 127 (= FE-2), session unchanged, ui 77, config 53, marketing 3, i18n / api-client unchanged |
| Changed test lines | none in FE-2's or FE-1's tests (frontend); the token-test helper now follows `var()` (no assertion changed) |
| axe | Storybook 94 runs 0 serious/critical; e2e every FE-2 state × en/ar × 6 projects 0 serious/critical (FEF-12's Firefox skip unchanged). Found and fixed on the way: a dialog fading in and a button mid-colour-transition were scanned (267968c) |
| DS-07 RTL keyboard | green in three browsers (found and fixed: the popup scale moved the measured box, d70f1e9) |
| VQ-04 targets | every interactive element ≥ 40×40 at 390 / 24×24 at 1280, or 2.5.8 spacing - 0 failing in 58 captures |
| CM-07 | 5 parts still under reduce, moving otherwise; focus ring never animates |
| Layout | brand panel right in en, left in ar |
| impeccable | before 15/20, after 20/20; P0 0, P1 0; IA-01 (marketing sparse, FE-10) P2 not taken |
| ui-ux-pro-max checklist | 5 pass, 1 not taken (hover transitions), 1 partial (768/1024 not captured) |
| Marketing | 0 `<script>` in all 6 pages |
| VQ-06 protected paths | 0 diff lines (apps/web/lib, app/session, packages/session, api-client, i18n/src) |

## 12. The Contact Sheet

`E:\wasla-ferd-notes\contact-sheet\index.html`: 58 rows (29 states × 1280/390, en and ar, marketing in four
languages), before | after with each screen's shares. Not committed; not a baseline (FEQ-11). Rescued to
`E:\wasla-verification-scripts\ferd\` in Part D.

## 13. frontend.yml

checks: the licence step enforces the policy. e2e: `WASLA_VISUAL_DIR` for the visual-roles projects, then
`measure-colour-roles.mjs --check` on the averages; the PNGs uploaded only on failure (whole folder, 7 days).
Storybook: the motion and layout specs run in its Chromium project. Time limits unchanged; nothing deployed.

## 14. Mutation Check

Backend M-Y01..M-Y05 (revoked → 404; stranger → revoked; amount in gates; gates refuse suspended; /auth/me drops
suspended): all killed. Frontend M-Y11..M-Y20 (OR as AND; no shipped set; exception version ignored; text threshold
3.0; text-muted blue-400; a token without a role; accent band 6–100; reduced-motion rule off; button ring removed;
AuthLayout forced ltr): all killed; controls green (the first UI control was red on a wrong pair, fixed in 05e0ee9, and
M-Y14..16 re-run). Every file restored and proved clean; none hung.

## 15. Documentation and ADR Changes

ADR-194 (FEB-07), ADR-195 (licences, palette and roles, composition, motion, the skills); docs/API.md (gates, codes,
259 operations), docs/AUTHORIZATION.md (rule, matrix row), docs/SECURITY.md (licence policy); frontend/README.md;
`frontend/packages/ui/DESIGN.md` (the design language FE-3 builds to).

## 16. Part A Commits and the Diff Guard

21 commits 80b0302..a984936 (3 A-BE, 18 A-FE: 10 planned + 8 fixes and follow-ups each with its reason), 85 files,
+3969 / −331. A-BE paths only in A-BE; A-FE only under frontend/, frontend.yml, DECISIONS.md, docs/SECURITY.md;
event-types.json and fixtures unchanged; no artefacts, no raw colours in apps, no merge, no secret.

## 17. Gates at FERD_HEAD and in the Verify Worktree

| Gate | Fix worktree | Verify worktree (a984936, fresh install) |
|---|---|---|
| G-1 install / audit / licences | 0 / 0,0 / 0 violations | install 0; licences 0 |
| G-2 drift | 0 | 0 (and 0 on the merged tree) |
| G-3 pipeline ×3 forced | every task green (config lint fixed in a984936) | lint, typecheck, i18n, build green ×3; unit-test workers timed out starting under load → alone: config 53, ui 77, web 127 |
| G-4 Storybook | 245 passed / 188 skipped | 245 passed / 188 skipped |
| G-5 journeys | as section 11 | as section 11 |
| G-6 visual | averages in band, every screen in limits | identical within 0.1 point |
| G-7 mutation | section 14 | not repeated (same tree) |
| G-8 backend image | base 623,431,929 · base-b 623,434,367 (noise 2,438) · head 623,477,579 (+43–46 KB for +9,416 B of app source in wheel + /app + bytecode; locks unchanged) | — |
| G-9 Workers (Linux) | web 3811 files / 81,526,603 B, console 3686 / 78,428,908 B, marketing 29 / 566,366 B; entry d05223bf… = FE-2; workers:smoke every page 200, session 405 / 503 | — |
| GB-1..4 | green | green (22 files; ruff, black, mypy, export, Alembic) |

## 18. Untracked Files

14 paths in E:\wasla (11 *.md, docs/ui/×3), SHA-256 identical at the start, before and after the merge (evidence/
untracked-*.sha256); no incoming path under docs/ui/ or equal to an untracked path. One operator slip: `npx wrangler
whoami` from E:\wasla wrote `.wrangler/cache/wrangler-account.json` (an account-id cache, no token) - removed that path
only; wrangler then ran from the notes folder.

## 19. Merge and Tree Equality

`git merge --no-ff a984936` in E:\wasla → 3199d07, no conflict; `git diff --exit-code a984936 HEAD` → 0; Alembic 0138.
On the merged tree (verify worktree re-pointed): G-2 0; G-3 one forced pass, 30/30 tasks green (the tree is
byte-identical to FERD_HEAD's three passes).

## 20. Push

See the addendum.

## 21. GitHub Actions

See the addendum.

## 22. Part C — the Previews Redeployed

See the addendum.

## 23. Findings

| ID | Finding | Status |
|---|---|---|
| FEF-18 | J6 (Firefox desktop, round 1) sometimes saw two refreshes on this overloaded machine - the new tab's 300 ms token hand-off window; identical on the start head's builds (10/10) and this head's (10/10) | ACCEPTED RESIDUAL (load; CI on Linux is the cross-check) |
| FEF-19 | Until FE-3 draws status, the workspace list and switcher show a suspended workspace like an active one (FEB-07 now lists it); switching to it is refused by code | DEFERRED to FE-3 |
| FEF-20 | `frontend.yml`'s licence test reads the real lockfile through `pnpm licenses` within 120 s; under heavy local load it exceeded it once | ACCEPTED RESIDUAL (seconds on Linux) |
| FEF-21 | BuildKit's external `docker/dockerfile:1` frontend crashes on this Docker Desktop; the classic builder ignores `Dockerfile.dockerignore` | EXTERNAL (machine); images built with BuildKit's built-in frontend from identical Dockerfile copies |
| FEF-12 / 13 / 14 | re-observed: Firefox label rules skipped; Playwright worker native crashes 0xC0000409 (twice, 4-worker runs); Firefox context-close protocol error once | ACCEPTED RESIDUAL |
| FEB-32.. | none | — |

## 24. Decisions Ledger

| Item | Status |
|---|---|
| FEB-07, WG-01..08 | CLOSED |
| J3, J8 | PASSED (no longer "with FEB-07") |
| FEF-06 policy | CLOSED; caniuse-lite in the shipped set by the owner's decision (recorded); exceptions: none |
| the four skills | USED (section 4) |
| LC-*, SK-*, PA-*, CM-*, RS-*, VQ-*, PV2-* | CLOSED (PV2: see the addendum) |
| every RS-02 screen and the Buttons matrix | CLOSED |
| FEF-18, FEF-20 | ACCEPTED RESIDUAL; FEF-19 DEFERRED (FE-3); FEF-21 EXTERNAL; FEF-12/13/14 ACCEPTED RESIDUAL |
| Decisions NOT taken (section 5) | each DEFERRED to its owner |

## 25. Infrastructure Created by This Stage

Worktrees E:\wasla-ferd-fix (branch fe-rd-blue-redesign-20261112), E:\wasla-ferd-base, E:\wasla-ferd-verify (and
E:\wasla-ferd-preview in Part C); E:\wasla-ferd-notes; containers wasla-ferd-pg, wasla-ferd-redis; network
wasla-ferd-net; images wasla-ferd:base, :base-b, :head, wasla-fe-e2e-app:local; volume wasla-ferd-workerbuild-store;
databases wasla_ferd_* (in wasla-ferd-pg).

## 26. Part D — Cleanup (planned)

Only after the addendum's run is green: rescue notes (scripts, logs, evidence, screens, contact sheet, skills' outputs,
mutation, draft; never node_modules) to E:\wasla-verification-scripts\ferd\ with SHA-256; remove the containers,
network, images, volume and the four worktrees by exact name (`git worktree remove`), `git worktree prune`, then
E:\wasla-ferd-notes. Kept: the previews, every branch, the developer stack, shared caches. Record:
E:\wasla-cleanup\CLEANUP_ferd_<date>.md.

## 27. Remaining External Actions

Open the previews and the contact sheet and say what to change; review ar / fr / tr (FEQ-08), the brand line
included; a logo, a dark theme (FEQ-09), a visual baseline (FEQ-11); delete E:\wasla-fe1-*, E:\wasla-fe2-* and what
Part D cannot; free space on C:; FE-3 next (FEQ-14 first); FEB-30, FEB-31; iwasla.tech nameservers and the first
staging deploy; real Google and devices (FE-11); audit 24; security.yml's RGM-23 / 24 / 27; the local branch
fe-rd-blue-redesign-20261112; merging into main.

## 28. What FE-3 Can Rely On

| Piece | What it gives |
|---|---|
| `/auth/me` `workspaces[].status` | the switcher can show a suspended workspace |
| `GET /api/v1/workspace/gates` | the banners' facts, any member, suspended too, no amounts |
| `workspace_suspended`, `membership_revoked` | own codes, catalogued in four locales; strangers still 404 |
| `packages/ui/DESIGN.md`, role tokens, composites | how every new screen looks; AuthLayout, Card, PageHeader, BrandPanel, Wordmark |
| `measure-colour-roles.mjs` + frontend.yml | new screens join the 60/30/10 gate by adding them to the visual-roles spec |
| licence policy | new packages are checked on every push |
| still waits for | nothing in the backend; FEQ-14 is the owner's |

## 29. Final Verdict

Set in the addendum from the push's frontend.yml and ci.yml runs.

```text
Provisional: every local gate green at FERD_HEAD a984936 and in the verify worktree (the J6 load flake and one Firefox
protocol error, each green alone), merged as one normal merge commit 3199d07 with the merged tree byte-identical to
FERD_HEAD.
```
