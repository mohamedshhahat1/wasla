# FE-3 — App Shell, Workspace Gates And Channels

## Phase status

| Phase | State | Evidence (E:\wasla-fe3-notes\, rescued to E:\wasla-verification-scripts\fe3\) | Made |
|---|---|---|---|
| A0 state, four skills, untracked, infrastructure | done | evidence/untracked-start.sha256, the draft's start table | worktree E:\wasla-fe3-fix (branch fe3-app-shell-20261113 from c57dcb0); wasla-fe3-net, -pg, -redis; venv |
| B1–B3 FEB-06 for FE-3 (A-BE) | done | logs/gb1.log, logs/gb2/*, logs/gb3_mypy.log, logs/e2e_smoke*.log, logs/gb6_helpers.log, mutation/backend_results.txt | 092e263, 167cbad, 4f3664d, 098d7db |
| A1 brief, skills | done | skills/* | — |
| A2–A7 composites, shell, gates, stream, home, channels | done | — | 2b38833, 5360545, ebdec8b |
| Browser walk-through (the owner asked to watch) | done | the walk in the built-in pane | findings → 098d7db, 79b3a3b |
| A8 journeys, visual roles, audits, fixes | done | logs/e2e_full1.log, e2e_full2.log, evidence/measure_fe3*.txt, skills/impeccable/audit-fe3-*.md | c0ba782, f2e3f52, fa7a495, 5f21bf9, d91a6b5, a91041d, 75f4326, d7bd960, 5043fd9 |
| A9 frontend.yml, docs, ADR-196 | done | — | fc8182c, 25b7909 (+ the numbers commit) |
| A10 mutation, diff guard, gates → FE3_HEAD | done | mutation/frontend_results.txt, logs/g3_pass*.log | FE3_HEAD c8c47f6 |
| Part B verify, merge, gates on the merged tree | done | logs/verify/*, logs/gb_verify/*, evidence/untracked-*.sha256 | merge a3cdcd8; this report |
| Part B push, frontend.yml, ci.yml | after this commit: in the addendum (section 22) | | |
| Part C | after CI: in the addendum (section 23) | | |
| Part D | only if CI GREEN: E:\wasla-cleanup\CLEANUP_fe3_<date>.md | | |

## 1. Executive Summary

- **What the owner can open.** A signed-in person now lands in the app: a dark-blue top bar (wordmark, the workspace
  switcher with each workspace's status, the live-updates mark, the account menu), a light-blue side navigation (a
  sheet on a phone) with only the two screens that exist - Home and Channels - and a footer band. Walk it locally
  (frontend/README.md, "The signed-in app"); the owner already watched it live in the built-in browser pane on
  2026-11-13. The previews on *.workers.dev have no API, so they show the signed-out pages only.
- **How the request was read.** As the prompt's reading table: the plan's FE-3 exactly (APP-SHELL, APP-HOME,
  APP-WS-03, APP-CH-01..03), nothing of FE-4..FE-10. **FEQ-14** was answered by the owner ("Both: form now, plan
  Embedded next"): the assisted WhatsApp form with a guided explainer is built; **Embedded Signup is the next backend
  stage**.
- **The gates, as people now see them.** Suspended: a full state for everyone, the page's calls stop, the stream is
  not reopened, "Check again" brings it back. Access withdrawn: a full state; a stranger still gets the same 404 as a
  random id. Payment owed: a red banner for owners and admins, a quieter amber one for members - never an amount.
  Trial ending in 7 days: a blue, dismissible banner for owners and admins. Channel slots full: Connect disabled with
  its reason in words; a reduction: a notice for everyone, the owner chooses what stays. AI allowance used up: an amber
  banner for owners and admins, a line on home for members. Live updates down: "paused - retrying", then back.
- **One stream per browser per workspace**, held by the tab that owns a Web Lock and shared over a BroadcastChannel;
  six tabs open one stream; when the leader closes, another leads with a new ticket.
- **First, for the owner:** FEF-24 (a reload of a suspended workspace's page signs the person out - FE-2's session,
  protected here), FEF-25 (Windows-only Firefox content-process crashes in some axe scans, bisected), FEB-32 (the
  smoke's mailbox race); the explainer's wording and the ar / fr / tr texts are drafts; there is no support address
  in the suspended state; the payment banner has no link until FE-8.

## 2. Source Branches and Exact Heads

| Name | Commit |
|---|---|
| FERD_CI_GREEN_HEAD = ORIGIN_CANONICAL_START_HEAD | c57dcb0096d59e0404bc6cd3fce36d11aba31b79 |
| A-BE commits | 092e263, 167cbad, 4f3664d (puts back two frontend files 092e263 removed by mistake - no amend), 098d7db (found in the walk-through) |
| BE_HEAD (last A-BE commit) | 098d7db |
| FE3_HEAD (fix branch fe3-app-shell-20261113) | c8c47f6bcc3714e771da7b7b53300ac64aae3662 |
| CANONICAL_MERGED_HEAD | a3cdcd828cc048d66b896fdcf258ccb95f7b74f0 (parents c57dcb0, c8c47f6) |
| origin before the push | c57dcb0 (checked before the merge; checked again before the push) |
| main | 8e3a1ba (untouched) |
| Alembic head | 0138 (unchanged) |
| LAST_ADR before / this stage | ADR-195 / ADR-196 |
| OPENAPI_SHA256 | b606e3a79f6cc5454d5e97b546f4f36131c38776ec08f4e3164efe4f47b6f297 (unchanged - the codes are not named in the document) |

## 2a. FEB-06 for FE-3 (A-BE)

EC-01's error table (from the code):

| Route | Status | Code before → after | Sentence | Raise site | FE-3 state |
|---|---|---|---|---|---|
| POST /whatsapp/accounts (a number another workspace holds) | 409 | conflict → **whatsapp_number_already_connected** | That WhatsApp number is already connected. (unchanged, names no workspace) | app/repositories/whatsapp_repository.py (read check + integrity race) | connect: "already connected to Wasla" |
| POST /auth/workspace, POST /auth/login with a suspended workspace (found in the walk-through) | 403 | permission_denied → **workspace_suspended** | This workspace is suspended. (unchanged) | app/services/auth_service.py `_resolve_workspace` | switcher: the suspended sentence |
| POST /whatsapp/accounts, …/enable | 409 | channel_capacity_exceeded, channel_type_not_allowed | - | channel_capacity.py | already specific |
| POST /whatsapp/accounts, …/verify | 422 | whatsapp_ownership_unverified | - | ownership.py | already specific |
| POST /whatsapp/accounts, …/verify | 503 | dependency_unavailable | - | whatsapp_account_service.py | class kept (generic sentence fits) |
| POST …/verify, enable, disable, release; GET …/messaging-limit | 404 | not_found | - | repositories | class kept (the not-found state) |
| POST /billing/channel-capacity/selection | 403 / 422 / 409 / 404 | permission_denied / validation_error ×2, channel_capacity_selection_invalid / conflict ×3 / not_found | - | TenantOwnerDep, capacity_reduction.py | class kept: not drawn for non-owners; the dialog cannot send the two 422s; "changed, reload" is right for the 409s |
| every workspace route | 403 / 402 / 429 | workspace_suspended, membership_revoked, email_verification_required / payment_required, plan_limit_exceeded / rate_limited, stream_limit | - | FEB-07 and before | already specific |

Rows 16; given a code 2 (three raise sites); already specific 10; kept on the class code 7 (each with its reason).

Probes (P-E01..P-E05), before → after: P-E01 the second claim of a number 409 conflict → 409
whatsapp_number_already_connected, same sentence, no workspace named; P-E02 wrong-order lifecycle (enable an active
number: 200 no-op; release twice / verify a released one: 404 not_found) unchanged; P-E03 selection as an admin 403
permission_denied, a wrong count 422 channel_capacity_selection_invalid - unchanged; P-E04 a switch into a suspended
workspace 403 permission_denied → 403 workspace_suspended, same sentence; P-E05 the gate states (below).

| Gate state | Reached by |
|---|---|
| suspended / restored | API: platform suspend / restore (staff) |
| access withdrawn | API: owner withdraws the member |
| capacity full | API: connect the default plan's one slot |
| reduction pending | API: a platform capacity grant, a connection into it, the grant withdrawn (JR-06 proves the full case; the reduction notice and selection are unit- and story-proved) |
| trial ending, past due + renewal owed, AI allowance used up | seed helper tests/e2e_stack/gate_states.py (test-only; no app/ import - unit-tested) |

| Gate | Result |
|---|---|
| GB-1 | 42 passed (conflict code 4, accounts 28, gate-state helper 10); 098d7db: switching 10 incl. the new test |
| GB-2 | by file: conflict_code 4, accounts 28, e2e_gate_states 10, whatsapp_ownership 17, reverification 15, audit_trail 16, channel_capacity 15, plan_enforcement 7, messaging_limit_reads 8 + 1 failed under load (a 10 s timing window in a campaigns test while the frontend install ran) → alone 9 passed, error_envelope_contract 14, route_authorization 8, documentation_truth 27, deployment_configuration 69, openapi_export 16; after 098d7db: switching 10, auth_endpoints 11, workspace_gates 27, authorization 15, lifecycle_concurrency 9, orphan 12, billing_lifecycle 11, entitlements 12, lifecycle 26 |
| GB-3 | ruff clean; black clean (one new test reformatted); mypy 986 files clean; `type: ignore` 263 = 263 |
| GB-4 | export --check 0; drift 0; Alembic 0138 |
| GB-5 | M-Z01..M-Z04 killed, control green, tree clean after each |
| GB-6 | FE-0's profile on the stage's image: J1–J10 as FE-0 expects on runs 3–4; runs 1–2 J6 FAILED at the smoke's own account() verify (two codes in the mailbox, the first superseded) - the start head's image 2/2 green, this stage's 2/2 green on a quiet machine → FEB-32. The seed helpers' states read back through GET /workspace/gates and /billing/entitlements |

## 3. Environment

| Item | Value |
|---|---|
| Node / pnpm / Turborepo | 24.15.0 / 12.18.0 (corepack shim in the notes) / 2.11.10 |
| Browsers | Playwright 1.65.0's Chromium, Firefox, WebKit |
| Docker | 29.8.0 (Docker Desktop restarted once during the stage - every container exited 255; images survived) |
| Packages added | none (lockfile byte-identical to the start head) |
| pnpm audit (prod / all) | 0 / 0 |
| Licences | 174 shipped / 869 build-only, 0 violations, 0 exceptions (= FE-RD) |
| Disk | C: 8.0 GB free at the start (below 10 GB - noted); only the e2e image was built (no backend base images) |
| Machine | frontend install 41m48s; Next build of the three apps 3m41s–7m18s; full e2e 49–51 min with 2 workers |
| PowerShell | stopped starting mid-stage ("You must install .NET") - the shell tool was used instead |

## 4. Design Skills

| Skill | Path | SKILL.md SHA-256 | Used for | Outputs (notes\skills\) |
|---|---|---|---|---|
| ui-ux-pro-max | ~/.claude/skills/ui-ux-pro-max | 43d1c5add9a2…0d7861f7d (= FE-RD) | the UX of the new screen kinds; the pre-delivery checklist | 01_design_system.md … 12_stack_next.md, checklist-fe3.md |
| taste-skill (design-taste-frontend 2.5.1) | ~/.claude/skills/taste-skill | dfd1190acb78…24209721f (= FE-RD) | composition: one system, one primary per view, where the 30 % sits | direction-fe3.md |
| emil-design-eng | ~/.claude/skills/emil-design-eng | f66556037b4c…fa856c7a9 (= FE-RD; FE_BLUE_REDESIGN.md's abbreviation "…0f43c7a9" was a typo) | motion of the sheet, banners, live mark, switcher, checklist | motion-review-fe3.md |
| impeccable 4.8.2 | ~/.claude/skills/impeccable | e0565c6cc6e4…e56f4b48 (= FE-RD) | audit before / after the fixes (Operate mode) | audit-fe3-1.md (17/20), audit-fe3-2.md (19/20) |

Guidance not taken (SK3-04): impeccable's launcher and detector (downloads a binary - network); ui-ux-pro-max's
glassmorphism style, orange accent and Plus Jakarta Sans (DESIGN.md's palette and fonts win); taste-skill's
marketing machinery (photographs, dealt heroes, moments, effects, its scripts and gate - not an Operate surface);
emil's "gentler, not zero" reduced motion (CM-07 stays 0 s).

impeccable's first audit (by severity): P1 2 (icons on the dark band; the checklist's "could not check" while loading),
P2 4 (bare loading text, a landmark per card, the menu under the dialog, a repeated label), P3 2. All P1 / P2 fixed
(79b3a3b, fa7a495, d7bd960); the two P3 recorded as not taken with their reasons. Second audit: P0 0, P1 0, P2 0.

## 5. Decisions Applied

| ID | Applied (source) |
|---|---|
| EC-01..05 | section 2a (prompt); the second code (workspace_suspended on switch / log-in) found in the walk-through, same rules |
| SK3-01..04 | section 4 |
| SH-01..07 | the (app) route group; frame per width (desktop side navigation from 1024, phone sheet below - ui-ux-pro-max / taste-skill); the bar scrolls with the page (not sticky: SH-02's proof burden avoided); the registry (Home, Channels); the account menu; the switcher with the Suspended badge; FE-2's facts on home |
| GT-01..09 | lib/gates.ts (one reducer, table-tested), GATE_QUERY_POLICY, gateSignalOf; the full states and banners per role; the trial uses the viewer's time zone for the date (the workspace's is not in /auth/me or the gates - recorded) |
| ST-01..06 | lib/stream-coordinator.ts (framework-free, unit-tested); FE-2's helper unchanged; lastEventId by the query alias through the helper's EventSource factory |
| HM-01..03 | home cards per role; the checklist from five reads (knowledge = "at least one base": the list carries no document count) |
| CN-01..08 | the adapter registry; the explainer checked against Meta's Cloud API guide (read 2026-11-13); the token as a secret (no mutation cache); the detail with typed release; the owner's selection |
| UI3-01..05 | section 11 |
| JR-01..12, VQ3-01..04 | section 12 |
| PV3-01..02 | section 23 |

Decisions NOT taken, each to its owner: Embedded Signup (next backend stage); Instagram / Messenger adapters
(LP-FE-02); the inbox and its invalidation map (FE-4); customers, AI and knowledge, team, settings, billing screens and
the payment link (FE-5..FE-8); console screens (FE-9); marketing (FE-10); a support address (owner); cross-device
checklist skips (FEB-18, P3); FEB-09; a dark theme, a visual baseline, a logo (owner); ar / fr / tr and the
explainer's review (owner); FEF-12/13/14/18/20/21 (residuals); FEB-30/31/14/16/17/10/08/11/12/13 (their stages);
staging, DNS, Access, production Workers; real devices, real Meta; paths-ignore; nightly / real-clock / Linux-local
lanes; deleting earlier stages' folders (the user).

## 6. The Shell

| Width | Frame |
|---|---|
| ≥ 1024 | dark-blue top bar (wordmark; switcher, live mark, account at the inline end); light-blue side navigation at the inline start (256 px, `--spacing-nav`); banners under the bar; the page; the action-blue footer |
| < 1024 | the same bar with a menu button; navigation in a sheet from the inline start (mirrored in RTL); every target ≥ 40 × 40 |

Registry (apps/web/lib/navigation.ts): Home (/), Channels (/channels) for every role; a unit test holds each route to
its page file and the served route. FE-2's test lines that read the placeholder (SH-06), each moved, the fact
unchanged:

| File | Old | New |
|---|---|---|
| fe2-auth.spec.ts | heading `auth.home.title` | heading level 1 `app.home.title` |
| fe2-auth.spec.ts | click "Sign out" | open the account menu, then click "Sign out" |
| fe2-axe.spec.ts | click "Sign out everywhere"; `getByRole("dialog")` | open the account menu first; the dialog named by its title (the menu is itself a role="dialog" popover) |
| i18n.test.ts | the namespace list and review list without `app` | with `app` (an added namespace) |

## 7. The Gates

The reducer's table test walks role (3) × subscription status (7) × payment (2) × trial (3) for billing, role × capacity
(4), role × allowance (4), role × live (4), the precedence and dismissal rules. Each state per role and its journey:

| Gate | Owner / admin | Member | Proved by |
|---|---|---|---|
| suspended | full state, Check again | full state | JR-03 (staff suspends with both signed in; no new ticket; restore) |
| access withdrawn | - | full state; switcher drops it; stranger = random id | JR-04 |
| payment owed / past due | red banner, no amount, no link | amber "ask the owner", no /billing/summary call, no amount | JR-05 |
| trial ending (≤ 7 days) | blue banner, dismissible for the session | nothing | JR-05 |
| capacity full / reduction | Connect disabled with its reason / notice + owner's selection | the list's notice | JR-06, unit, stories |
| AI allowance used up | amber banner | a line on home | unit, seed helper read-back |
| live updates down / back | "paused - retrying", once announced | same | JR-11 |
| unverified | FE-2's /verify (unchanged) | same | FE-2's J1 |

Reads (JR-10, chromium-desktop): Home → Channels → a connection → Home → Channels: one GET /workspace/gates and one
GET /billing/entitlements; a tab hidden and shown within five minutes: none; after five minutes (the injected clock):
one more.

## 8. The Stream

Coordinator: the Web Lock `wasla-stream-<workspace id>`; the leader opens FE-2's openEventStream and posts frames and
state on the BroadcastChannel of the same name; followers apply them; a newcomer asks the leader's state; the next
tab leads with a new ticket and `lastEventId` when the leader leaves; a switch leaves before the new token (lib/switch.ts,
tested order); a ticket refused after a revocation re-reads the gates. Unit tests: six coordinators → one stream, frames
once per tab, hand-over with the last id, a waiting tab that leaves never opens a stream, "Reconnect here" from a
follower, separate workspaces separate streams, nothing posted carries a token or ticket.
JR-09 (chromium-desktop): six tabs → one ticket, one leader; the leader closes → another leads within 10 s, two tickets
in total; the control with the lock bypassed → a ticket per tab. FE-2's stream-restart project passes through the shell.
FEF-22: EventSource cannot read 429 `stream_limit`; it shows as "paused - retrying" under the helper's capped backoff.

## 9. Home and the Checklist

Everyone: open conversations (one page of 20, "20+" when more), active channels. Owners and admins: leads (open,
unassigned, all), this period (messages in / out, the AI allowance as a meter), and the onboarding checklist while a
step is undone - connect a channel (linked), set up the AI agent, add knowledge, invite a teammate, choose a plan
(each "available in a later version"). The checklist shows no state until all five reads have answered; a failed read
says "could not check". Nothing is stored.

## 10. The Channels

List with the capacity meter, scheduled change, reduction notice, released numbers (collapsed); Connect by one rule
(role × remaining × allowed types × adapter). The connect screen renders the "whatsapp" adapter of the registry; the
explainer follows Meta's WhatsApp Cloud API guide, read 2026-11-13
(https://developers.facebook.com/docs/whatsapp/cloud-api/get-started - Step 5, system user, Assign assets, Generate
token with business_management, whatsapp_business_messaging, whatsapp_business_management); the Business Help page on
system users (https://www.facebook.com/business/help/503306463479099) answered with its title only from this machine -
EXTERNAL VERIFICATION. The token: password field, show / hide, no autocomplete / spell check, a plain request, reset after
success, kept in the form only after a refusal - unit-tested (no mutation, no query cache entry, no URL / history) and
searched for in JR-07 in storage, cookies, IndexedDB, the URL, history, the console, every BroadcastChannel message,
the DOM and the mutation cache, after a refusal and after a success: 0 hits. The detail: facts, messaging limit (or
"not told yet"), verify, disable (confirm), enable, release (type the last four digits) - JR-07.

## 11. Composites and Look

New in @wasla/ui: AppShell, NavList, Banner, BannerStack, Badge, StatCard, Meter (role="meter"), Checklist,
KeyValueList, LiveIndicator, StatusState, SwitcherMenu; IconButton `tone="inverse"`; Popover optional `open` /
`onOpenChange`; token `--spacing-nav`. Stories 47 → 56; axe 94 → 112 story × locale runs, 0 serious / critical.
Contrast pairs 49 → 58, all at threshold. Motion: the sheet (slow in, fast out, mirrored), nothing else new; reduced
motion 0 s with a control. RTL: side navigation and sheet at the inline start, arrows mirror, numbers in `<bdi>`.

## 12. Proofs

| Proof | Result |
|---|---|
| Unit | web 127 → 376 (+249: rules 238, coordinator 10, connect secret 3 … see G-3 logs), ui 77 → 86, i18n 20, config 53, marketing 3 |
| Storybook | 263 passed (axe 112 runs + FE-RD's) ; RTL keyboard + 24 new (4 × 2 directions × 3 browsers); motion 13 (sheet added) |
| e2e full run 2 | 479 passed / 12 failed / 35 skipped; chromium-desktop 78/78, webkit-desktop 71/71, visual-roles 29 + 29; chromium-phone J1 (FE-2 time-zone timing, passed alone), webkit-phone JR-07b (fixed d91a6b5, passed alone), firefox-desktop 4 + firefox-phone 6 "Page crashed" (FEF-25) |
| 60 / 30 / 10 | 1280 62.3 / 29.9 / 7.6, 390 64.0 / 27.1 / 8.3 (39 screens each, bands 55–68 / 24–36 / 6–12), every screen in its hard limits; gate states apart: payment banner status 4.1–6.3 %, suspended 0.3–0.5 % (≤ 8 %) |
| Targets | VQ3-03 in the visual-roles projects: 0 failing |
| impeccable | 17/20 → 19/20; P0 / P1 0 after the fixes |

## 13. The Contact Sheet

Not committed. The captures are in E:\wasla-fe3-notes\screens\ (to be rescued to E:\wasla-verification-scripts\fe3\screens\):
- `visual\`: 82 captures from the visual-roles projects (every FE-3 screen × en / ar × 1280 / 390, plus FE-2's and FE-RD's).
- `gates\`: 8 captures of the gate states apart (payment banner and suspended, at both widths).
- `verify-visual\` (82) and `verify-gates\` (8): the same captures from the verify worktree.

No separate composite image was made; the folder is the sheet. The owner can judge the screens through the walk-through in
frontend/README.md ("The signed-in app"), and already walked them live on 2026-11-13.

## 14. frontend.yml

The e2e job passes WASLA_VISUAL_GATES_DIR and uploads the gate captures on a failure; e2e-local builds with
WASLA_E2E_ROUTES (build only) - the journeys' hooks; turbo.json passes it. Duration: see section 22.

## 15. Mutation Check

Backend M-Z01..M-Z04: killed. Frontend: M-Z11, 12, 13, 14, 15, 17, 18, 19, 21, 22, 23 killed by unit tests; M-Z20 by
JR-07; M-Z24 by the Storybook layout test (RTL cases); M-Z16 survived first (one observer - no journey can see a
refetch-on-mount) → the read policy became GATE_QUERY_POLICY with a unit test (5043fd9) → killed. Controls green; every
file restored and the tree clean; none hung.

## 16. Documentation and ADR Changes

| File | Change |
|---|---|
| frontend/packages/ui/DESIGN.md | the FE-3 section: the frame per width, the new composites and their roles, the banner order and tones, "cards are not landmarks", the sheet's motion, the measured shares, the skills' parts |
| DECISIONS.md | ADR-196: the shell; the gates and their one reducer; the read policy; the stream coordinator; home; the channel adapter registry; the secret rule; FEQ-14; EC's two codes; Rejected; Consequences, including FEF-22 / FEF-24 |
| frontend/README.md | "The signed-in app": how to walk the shell locally against FE-0's e2e profile |
| docs/API.md | the 409 row: whatsapp_number_already_connected; the 403 row: switching into or logging into a suspended workspace answers workspace_suspended |

## 17. Part A Commits and the Diff Guard

21 commits c57dcb0..c8c47f6 on the fix branch fe3-app-shell-20261113, in order:
092e263 feat(api), 167cbad test(e2e), 4f3664d chore (puts back FE-2's placeholder files that 092e263 removed; no amend),
2b38833 feat(ui), 5360545 feat(web), ebdec8b feat(web), 098d7db fix(api), 79b3a3b fix(web), c0ba782 fix(web),
f2e3f52 test(frontend), fc8182c ci(frontend), 25b7909 docs, fa7a495 fix(web), 5f21bf9 test(frontend), d91a6b5 test(frontend),
a91041d test(ui), 75f4326 refactor(web), d7bd960 fix(web), 5043fd9 test(web), bed3836 docs, c8c47f6 docs.

Size of the change:
- `git diff --stat c57dcb0 c8c47f6`: 73 files, +8143 / -170.
- frontend/ alone: 63 files, +7669 / -164.
- Tracked frontend/ files at FE3_HEAD: 361 (3,435,593 bytes).

Paths outside frontend/ (the diff guard): .github/workflows/frontend.yml, DECISIONS.md, docs/API.md,
app/repositories/whatsapp_repository.py, app/services/auth_service.py, tests/e2e_stack/gate_states.py,
tests/integration/test_whatsapp_accounts.py, tests/integration/test_whatsapp_number_conflict_code.py,
tests/integration/test_workspace_switching.py, tests/unit/test_e2e_gate_states.py. Each one is an A-BE path, frontend.yml or
DECISIONS.md.

What did not change:
- No migration, route, schema field, lock, pyproject, Dockerfile, compose file, ci.yml or security.yml.
- The session, the session routes, packages/session and the stream helper: empty diff.
- openapi.json and schema.d.ts are not in the diff (export --check 0, drift 0).
- The lockfile is byte-identical.

## 18. Gates at FE3_HEAD and in the Verify Worktree

| Gate | Fix worktree (FE3_HEAD) | Verify worktree E:\wasla-fe3-verify (never edited) |
|---|---|---|
| GB-1..GB-4 | section 2a | app.__file__ = E:\wasla-fe3-verify\app\__init__.py. Passed: conflict_code 4, accounts 28, e2e_gate_states 10, switching 10, auth_endpoints 11, workspace_gates 27, whatsapp_ownership 17, error_envelope_contract 14, route_authorization 8, documentation_truth 27, deployment_configuration 69, openapi_export 16. Ruff, black and mypy clean |
| G-1 | lockfile unchanged, audit 0 / 0, licences 174 / 869 with 0 violations | the same (fresh install) |
| G-2 | drift 0, format clean | drift 0, format clean |
| G-3 ×3 | 30 / 30 and 0 cached on each pass | 30 / 30 and 0 cached on each pass |
| G-4 Storybook | 263 passed | 289 passed / 224 skipped (includes a91041d's keyboard and motion tests) |
| G-5 e2e | run 2: 479 passed / 12 failed / 35 skipped | 478 passed / 13 failed / 35 skipped / 1 did not run (50.8 min). Failures: firefox-desktop 4 and firefox-phone 6, "Page crashed" (FEF-25, the same set as run 2); chromium-desktop ar JR-04; chromium-phone en J1 (FE-2's time-zone field); chromium-phone ar J6 control. The last three re-ran alone and passed (rerun_a 4, rerun_b 2, rerun_c 1) |
| G-6 60 / 30 / 10 | 1280: 62.3 / 29.9 / 7.6; 390: 64.0 / 27.1 / 8.3 | 1280: 62.2 / 29.9 / 7.8; 390: 64.5 / 26.7 / 8.3 (within ±1 point) |
| G-9 Workers | built on Linux; smoke green | web 3847 files / 82,325,861 B; console 3687 / 78,723,127 B; marketing 29 / 569,159 B. Every page 200 on workerd; session routes 405 / 503 |
| Backend image | e2e image wasla-fe3:e2e-be-head (FEF-21 workaround), 1.17 GB = the start head's wasla-fe3:e2e-start, 1.17 GB (A-BE's delta is source only) | - |

All three Workers' outputs changed against FE-RD's. Web and console carry the shell, and marketing's entry hash changed with
@wasla/ui's tokens and styles. Part C therefore redeploys all three.

The merged tree (the verify worktree re-pointed to a3cdcd8 after `git status --short` showed 0):
- G-2: drift 0.
- G-3 first run: stopped at 6 / 19. Vitest reported "Failed to start threads worker … Timeout waiting for worker to respond" for
  two web test files: a worker-start timeout on a loaded machine, with no assertion failed.
- G-3 re-run three times: 30 / 30 and 0 cached on each pass.

Both runs are recorded (logs/verify/merged_g3.log, merged_g3_pass1..3.log).

## 19. Untracked Files

The user's 14 untracked paths in E:\wasla (11 *.md files and docs/ui/'s files) have the same SHA-256 list at every check:
- at the start (evidence/untracked-start.sha256);
- before the merge and after it (untracked-premerge / untracked-postmerge);
- before this report commit.

None was modified, staged, committed or deleted.

## 20. Merge and Tree Equality

1. `git fetch`: local and origin/worktree-billing-google-auth were both c57dcb0.
2. `git merge --no-ff c8c47f6 -m "merge: FE-3 app shell, workspace gates and channels (ADR-196)"` made a3cdcd8 (parents c57dcb0
   and c8c47f6), with no conflict.
3. `git diff --exit-code c8c47f6 a3cdcd8` exited 0: the trees are equal.
4. Alembic heads: 0138.
5. The untracked list and its hashes are unchanged.

## 21. Push

The push follows this report commit, without force, once `git fetch` shows origin still at c57dcb0 and
`merge-base --is-ancestor` holds. Origin equal to local afterwards is recorded in the addendum (section 22).

## 22. GitHub Actions

The push's frontend.yml and ci.yml runs, any section-27 fix and the addendum's own run are recorded in the addendum below.
Expected ci.yml tests line: the start head's count + 15 for A-BE's new tests:
- test_whatsapp_number_conflict_code: 4
- test_e2e_gate_states: 10
- test_workspace_switching: +1

test_whatsapp_accounts gained an assertion, not a test.

## 23. Part C — the Previews

Runs after CI is green, if `wrangler whoami` names FE-RD's account. All three Workers' outputs changed (web, console and
marketing), so all three are redeployed. The results are in the addendum.

## 24. Findings

| ID | Finding | Status |
|---|---|---|
| FEF-19 | the switcher did not show a suspended workspace's status | CLOSED (Suspended badge; switching answers its own sentence) |
| FEF-22 | EventSource cannot read 429 stream_limit; the helper's capped backoff shows it as "paused - retrying" | ACCEPTED RESIDUAL (a dedicated state needs the helper to report the status - FE-4 / a helper change) |
| FEF-23 | a switch into a suspended workspace is refused by the API (403) - SH-05 had assumed it goes through | CLOSED BY DESIGN (the refusal now says "suspended"; the full state shows when a workspace is suspended while open) |
| FEF-24 | reloading a page of a suspended workspace signs the person out (refresh into it is 403; FE-2's session reads a 403 refresh as signed out) | DEFERRED (session code is protected in FE-3) |
| FEF-25 | Playwright 1.65's Firefox on Windows crashes its content process in some axe scans of the shell (bisected: aria-allowed-attr on named sections - removed; aria-dialog-name on the confirmation inside the shell; after scans of the open switcher / sheet); stderr: "RenderCompositorSWGL failed mapping default framebuffer"; same DOM passes in Chromium and WebKit; FE-3's journeys skip aria-allowed-attr in Firefox, FE-2's helper unchanged | ACCEPTED RESIDUAL locally; Linux CI decides (section 22) |
| FEB-32 | the e2e smoke's account() reads the first code in the mailbox; when the register mail arrives after the send, the verify fails (J6 under load) | NEW (tests/e2e_stack - not changed here) |
| FEF-12/13/14/18/20/21 | re-observed: FEF-12's skip unchanged; Firefox crashes (FEF-25); J6 Firefox round 26 once (FEF-18) | ACCEPTED RESIDUAL |

## 25. Decisions Ledger

| Item | Status |
|---|---|
| APP-SHELL | CLOSED (JR-02, JR-12, every project) |
| APP-HOME | CLOSED (JR-08) |
| APP-WS-03 checklist | CLOSED (JR-08; unit tests) |
| APP-CH-01 list | CLOSED (JR-06, JR-07) |
| APP-CH-02 assisted connect and explainer | CLOSED (JR-07); the explainer's wording: EXTERNAL VERIFICATION (the owner's review) |
| APP-CH-03 detail and actions | CLOSED (JR-07) |
| Gate: suspended | CLOSED (JR-03) |
| Gate: access withdrawn | CLOSED (JR-04) |
| Gate: payment owed / past due | CLOSED (JR-05; seed helper) |
| Gate: trial ending | CLOSED (JR-05; seed helper) |
| Gate: capacity full / reduction | CLOSED (JR-06; the reduction by unit tests and stories) |
| Gate: AI allowance used up | CLOSED (unit tests; the seed helper's state read back) |
| Gate: live updates down | CLOSED (JR-11) |
| FEQ-14 | CLOSED: answered "form now, Embedded next"; the Embedded Signup backend stage is scheduled |
| FEB-06 (the FE-3 part), EC-01..05 | CLOSED: two codes, each tested, no leak |
| FEF-19 | CLOSED |
| ui-ux-pro-max, taste-skill, emil-design-eng, impeccable | USED (section 4; outputs in skills\) |
| SK3-01..04, SH-01..07, GT-01..09, ST-01..06, HM-01..03, CN-01..08, UI3-01..05, JR-01..12, VQ3-01..04 | CLOSED (sections 4–12) |
| PV3-01..02 | in the addendum |
| FEF-12, -13, -14, -18, -20, -21 | ACCEPTED RESIDUAL (re-observed) |
| FEF-22 | ACCEPTED RESIDUAL |
| FEF-23 | CLOSED BY DESIGN |
| FEF-24 | DEFERRED PRODUCT DECISION (the session code is protected in FE-3) |
| FEF-25 | ACCEPTED RESIDUAL locally; Linux CI decides |
| FEB-32 | NOT CLOSED (the smoke in tests/e2e_stack; a later stage) |

Every item of "Decisions NOT taken" (section 5) is DEFERRED PRODUCT DECISION, each with its owner:
- Embedded Signup; the Instagram and Messenger adapters; the FE-4..FE-10 screens.
- A support address; FEB-18; FEB-09.
- A dark theme, a visual baseline, a logo; the ar / fr / tr review.
- Staging, DNS, Access and production; real devices and real Meta.
- paths-ignore; the extra lanes; the earlier stages' folders.

## 26. Infrastructure Created by This Stage

| Kind | Name |
|---|---|
| worktrees | E:\wasla-fe3-fix (branch fe3-app-shell-20261113), E:\wasla-fe3-verify (detached), E:\wasla-fe3-base (detached; the start head's image), E:\wasla-fe3-preview (Part C, if made) |
| containers | wasla-fe3-pg, wasla-fe3-redis; FE-0's e2e profile wasla-fe-e2e-* (up during the stage) |
| network | wasla-fe3-net |
| images | wasla-fe3:e2e-be-head, wasla-fe3:e2e-start, wasla-fe-e2e-app:local |
| folder | E:\wasla-fe3-notes (venv, logs, evidence, the skills' outputs, screens, mutation) |
| Cloudflare | none created; only the three existing preview Workers are redeployed (Part C) |

## 27. Part D — Cleanup

Part D runs only on "FE-3 MERGED AND PUSHED, CI GREEN", with the addendum's run green.

Planned:
1. Rescue to E:\wasla-verification-scripts\fe3\, verified by SHA-256.
2. Bring the e2e profile down with -v.
3. Remove by exact name: the two containers, the network and the three images.
4. Remove the worktrees verify, preview, base and fix (the branch stays), then `git worktree prune`.
5. Remove E:\wasla-fe3-notes.

Kept: the preview Workers, E:\wasla, the earlier stages' folders, the pnpm store, the Playwright cache, every branch and the
dev stack. The result goes to E:\wasla-cleanup\CLEANUP_fe3_<date>.md (not committed).

## 28. Remaining External Actions

- Walk the shell locally (frontend/README.md) and say what to change.
- Review the connect explainer's steps and wording and the ar / fr / tr texts (FEQ-08); decide FEQ-07.
- Give a support address for the suspended state, if wanted.
- The WhatsApp Embedded Signup backend stage: a Meta app with Facebook Login for Business, its configuration id and app
  review, then the code exchange and registration.
- The FEB-03 / 04 / 05 backend stage before FE-4.
- Delete E:\wasla-fe1-*, E:\wasla-fe2-*, E:\wasla-ferd-* and whatever Part D could not delete.
- Free space on C:.
- FEB-30 / 31 in a later stage; decide FEB-14 / 16 / 17.
- The iwasla.tech nameservers.
- The first staging deploy, real Google, real Meta, real devices.
- Audit 24's paths-ignore; security.yml's RGM-23 / 24 / 27.
- The local branch fe3-app-shell-20261113: keep or delete.
- Merging the canonical branch into main.

## 29. What the Next Stages Can Rely On

- **Embedded Signup backend stage:** apps/web/lib/channels.ts's CONNECT_ADAPTERS takes the Embedded adapter as one more entry
  beside "whatsapp". components/channels/connect-adapter.tsx draws the chosen adapter. The detail and the lifecycle actions
  do not depend on the channel type.
- **FE-4 (inbox):**
  - lib/stream-coordinator.ts: one stream per browser per workspace, hand-over with lastEventId.
  - Replace lib/stream-handlers.ts's FRAME_HANDLERS table with the inbox's invalidation map.
  - lib/navigation.ts's registry takes the inbox entry.
  - The gates apply unchanged (lib/gates.ts, GATE_QUERY_POLICY, gateSignalOf).
  - Waits for FEB-03 / 04 / 05.
- **FE-5..FE-8:** the registry, BannerStack and the gate hooks (GT-05's payment banner gets FE-8's link), DESIGN.md's FE-3
  additions and the composites. Waiting for: FEB-19 (FE-5), FEB-09 (FE-7), FEB-12 / EXT-FE-04 (FE-8).

## Runtime Matrix (section 31)

| Scenario | Before (c57dcb0) | After (FE3_HEAD / merged) | Verdict |
|---|---|---|---|
| "/" for a signed-in member | FE-2's placeholder | the shell and home (JR-02, JR-08) | CLOSED |
| Switcher shows a suspended workspace | like an active one (FEF-19) | "Suspended" badge; switching answers workspace_suspended | CLOSED |
| Navigation | none | Home and Channels only; a unit test holds each to a page | CLOSED |
| Suspended workspace (owner and member) | not drawn | full state; no new ticket; restore returns it (JR-03) | CLOSED |
| Revoked member | not drawn | full state; gone from the switcher; a stranger still gets 404 (JR-04) | CLOSED |
| Payment required / past due | not drawn | owner / admin red banner, member amber notice, no amount (JR-05) | CLOSED |
| Trial ending within 7 days | not drawn | owner / admin blue banner (JR-05) | CLOSED |
| Capacity full / reduction pending | not drawn | Connect disabled with the reason; notice; the owner selects (JR-06) | CLOSED |
| Allowance exhausted | not drawn | owner / admin banner; a line on home for members (unit, seed) | CLOSED |
| Live updates down / back | no stream in the app | indicator, announced once; a new ticket on recovery (JR-11) | CLOSED |
| /workspace/gates calls on Home → Channels → detail → Home | — | one (JR-10) | CLOSED |
| Streams for six tabs | — | one; the control opens one per tab (JR-09) | CLOSED |
| Leader tab closed | — | hand-over within 10 s; two tickets in total (JR-09) | CLOSED |
| Connect with the fake verifier; disable; enable; release | API only | through the screens; release needs the typed last four digits (JR-07) | CLOSED |
| Same number from another workspace | 409 conflict | 409 whatsapp_number_already_connected, no workspace named | CLOSED |
| Typed access token after submit | — | 0 hits in every place searched (JR-07) | CLOSED |
| Member on connect / owner-only controls | — | not drawn; the reason given | CLOSED |
| Home for owner / member | — | owner: checklist, statistics, usage; member: conversations, channels | CLOSED |
| Checklist | — | derived from five reads; nothing stored | CLOSED |
| FE-2 journeys and FE-RD suites | 312 + 50 visual | the same plus FE-3's; SH-06's moved lines in section 6 | CLOSED (local residual: Firefox FEF-25) |
| axe (stories and screen states) | 0 serious / critical | 0 serious / critical (112 story × locale runs; the journeys' scans) | CLOSED |
| RTL keyboard | DS-07 green | DS-07 plus the new composites, green | CLOSED |
| 60 / 30 / 10 averages (1280 / 390) | 65.0 / 25.7 / 9.2 · 63.2 / 26.7 / 9.6 | 62.3 / 29.9 / 7.6 · 64.0 / 27.1 / 8.3; gate states apart | CLOSED |
| Packages / licences | 1,043 (174 / 869), 0 / 0 | unchanged | CLOSED |
| Session, session routes, stream helper | FE-2's | unchanged byte for byte | CLOSED |
| Fonts, scripts and assets from other origins | none | none (no Meta SDK) | CLOSED |
| Workers outputs | FE-RD's | all three changed; sizes in section 18 | CLOSED |
| Previews | FE-RD's | addendum | — |
| The backend | 259 operations | 259 operations; only EC's two codes | CLOSED |
| Alembic | 0138 | 0138 | CLOSED |
| GitHub: frontend.yml / ci.yml | green / green | addendum | — |
| iwasla.tech DNS and Cloudflare account settings | the owner's | untouched | CLOSED |

## 30. Final Verdict

The verdict is written in the addendum, once the push's frontend.yml and ci.yml runs are read (section 22).
