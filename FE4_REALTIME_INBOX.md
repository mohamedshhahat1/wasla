# FE-4 — The Realtime Inbox (ADR-198)

## Phase status

| Phase | State | Evidence (E:\wasla-fe4-notes\) | Commit / merge / push / deploy |
|---|---|---|---|
| A0 state, skills, untracked, infrastructure | done | draft §2 | worktree E:\wasla-fe4-fix, branch fe4-realtime-inbox-20261114 from b98507d |
| B1 error inventory (EC4-01) | done | §2a below | — |
| B2 EC4 codes | done | logs/gb4-1_*.log | 94d7e93 |
| B3 UR (0139, read state, preview, filter, summary, first reply) | done | logs/gb4-1_read_state.log, gb4-6_*.log | 450abf8 |
| B4 seed helpers, contract, A-BE gates | done | logs/gb4-2_neighbours.log, gb4-3_mypy.log | e6a6b72 = BE_HEAD |
| A1 install, brief | done | logs/fg1_install.log, DESIGN_BRIEF_FE4.md | — |
| A2–A6 composites, screens, realtime map | done | logs/fg3_run2.log | 66f4cd4, cdf3d04, 8ce4794 |
| A7 e2e image, smoke, journeys, audit | done | logs/smoke.log, fg4_*.log, skills/ | f8f2bb1, d68961a |
| A8 frontend.yml, docs, ADR-198 | done | — | f8f2bb1, 31c43a9 |
| A9 mutation, diff guard, gates at FE4_HEAD | done | mutation/, logs/ | FE4_HEAD (§21) |
| B merge, report, push | see §23–§24 | | |
| CI, C previews, D cleanup | done (addendum) | | d571765: frontend.yml 43735244892, ci.yml 43735244716 green; previews f3c3842b / d20591cf / 6f2e2b78 |

## 1. Executive Summary

- **What the owner can open.** The inbox runs locally on FE-0's e2e profile (frontend/README.md, "The inbox locally":
  `pnpm e2e:local --skip-build` brings the profile up, a customer message is sent through the fake webhook with the
  profile's test values). The previews on `*.workers.dev` have no API, so they cannot show the inbox; `/inbox` there
  goes to sign-in (FEF-26's "Connection lost" on `/` is unchanged).
- **How the request was read.** "FE-4 on the testing basis" = the plan's FE-4 (APP-INBOX-01..03) on BE1's contract and
  FE-3's shell, with the owner's BE1 rule: focused local tests on what changed, Chromium only, one push, GitHub as the
  one full check. Plus the owner's 2026-11-14 additions (14:03, 14:16, 14:17 UTC): shared unread, "seen by", the
  last-message preview, assign on first reply, an unread-only filter, a total next to Inbox, mark as unread — built as
  UR in A-BE and shown by the screens (IL-07).
- **What a member can do now.** See the workspace's conversations live (server views: status, assignee, mode, unread;
  search by name or number, never in the URL), open one with its text, images, files, voice notes and transcripts
  (bytes as revoked `blob:` URLs fetched with the session's bearer), reply with text, a file or — when the 24-hour window
  is closed — an approved template; retry a failed reply without duplicating it; take over / hand back, assign without
  overwriting a teammate, re-prioritise, close / reopen, mark unread; see the customer and their lead beside it. Phone:
  three steps tracked by the URL; desktop: three panes; 768–1023: a drawer.
- **Locally vs GitHub.** Locally: A-BE's focused and neighbour tests, static checks, 0139 up/down/up; the unit tests of
  web / ui / i18n / api-client; FE-0's smoke J1–J11; the FE-4 journeys and FE-2/FE-3/BE1's named journeys in Chromium
  desktop + phone. Only GitHub: Firefox, WebKit, the whole matrix, Storybook (axe, keyboard, motion, layout over the new
  stories), the full visual-roles averages on Linux, the full backend suite.
- **Findings first — bugs this stage's own journeys caught and fixed before the push.**
  FEF-29 (d68961a): on desktop "Load more" never showed the next page — IL-06 parked the appended page because the list
  pane was scrolled — and the cursor-mismatch restart raced TanStack's append (found by IJ-03).
  FEF-30 (fcd84ed): the polling fallback never polled during an outage — each reconnect attempt reset its timer (found
  by IJ-08). FEF-31 (fcd84ed): the header said "Assigned to Unassigned" (seen in IJ-08's capture).
  VQ4 (f1abb5b): the empty inbox on a phone was 44.1 % white (limit 45) — the list's blue band now shows only beside a
  conversation pane. Two mutants first survived; their journeys were strengthened (M-V23 → IJ-07, M-V32 → IJ-13b). Deviations to review: `last_read_message_id` has no foreign key (a key from
  conversations into messages would make every message delete scan conversations); the preview's `kind` is the message
  type (photo/audio/video/document…) so the screen can name it; the CSP already allowed `blob:` for img/media; one more
  file may use sessionStorage (the composer draft, text only); README.md and docs/DEPLOYMENT.md changed because the
  documentation-truth test checks the migration range and route count. C: started at 5.0 GB (below the 6 GB stop) — the
  owner said to continue; no prune was run.

## 2. Source Branches and Exact Heads

| Name | Commit |
|---|---|
| BE1_CI_GREEN_HEAD = ORIGIN_CANONICAL_START_HEAD | b98507d |
| main | 8e3a1ba (untouched) |
| BE_HEAD | e6a6b72 |
| FE4_HEAD | 652316a |
| CANONICAL_MERGED_HEAD | 88d913e (parents b98507d, 652316a) |
| report commit, push, addendum | §24–§25 and the addendum |

## 2a. FEB-06 for FE-4 (A-BE)

EC4-01's table — every error the inbox screens receive that needed telling apart:

| Route | Error | Before | After | Raise site | Screen state |
|---|---|---|---|---|---|
| POST …/messages, …/messages/media | reply window closed | 422 validation_error | 422 **reply_window_closed** | app/channels/policy.py → messaging_service | composer → templates |
| POST …/messages* | key reused, other body | 409 conflict | 409 **idempotency_key_reused** | messaging_service `_require_same` | new key, nothing shown |
| POST …/messages/media | type refused | 422 validation_error | 422 **media_type_not_allowed** | app/core/media_types.resolve; policy.require_sendable_media | "Not sent" + reason |
| POST …/assignment | stale expected | 409 stale_assignment | unchanged (already specific, names nobody) | inbox_service.assign | refetch + "someone changed the assignee" |
| POST …/close, …/reopen | already so | 200 idempotent | unchanged | inbox_service | CLOSED BY DESIGN |
| send on a closed conversation | — | 201 (allowed) | unchanged | — | composer note only; CLOSED BY DESIGN |
| template sends | refused / marketing / opted out / limit | specific codes | unchanged | messaging_service | their sentences |
| GET /conversations | q / cursor / updated_since | BE1's three codes | unchanged | inbox_service | list states |
| any id route | unknown / other workspace | 404 not_found | unchanged (no oracle) | — | not-found |
| …/priority, …/mode | bad enum | 422 validation_error | unchanged — the UI cannot produce it | pydantic | — |

Status, sentence and every other key are byte-identical for each new code (tests in
tests/integration/test_inbox_error_codes.py). Seed helpers (tests/e2e_stack/inbox_states.py, test-only, import guard green):
a closed reply window (last inbound 25 h old), approved UTILITY and MARKETING templates, a media item in each status, a
voice note with a ready transcript, a conversation with a lead.

**UR.** Alembic 0139: three nullable columns on conversations (`last_read_message_id` — no foreign key, reason in the
migration; `last_read_by_user_id` → users ON DELETE SET NULL, as `assigned_to_id`; `last_read_at`). ConversationRead
gains `unread_count` (inbound after the marker, capped 100), `last_message` {id, direction, kind, excerpt ≤ 120 code
points, created_at} and `read` {message_id, by {user_id, display_name}, at}, filled by one batched read per page
(statement counts equal for 1 and 50 rows). Routes: `POST /conversations/{id}/read` (monotonic), `POST …/unread`,
`GET /conversations/unread-summary` (closed excluded, capped 1000), `GET /conversations?unread=true` (bound into BE1's
cursor). A member's successful send marks read and assigns an unassigned conversation through `InboxService.assign`
(expected none; a replay does nothing; the AI's send does neither). Stream: `conversation.updated` with field `read`;
event-types.json unchanged (fields are free strings). OpenAPI 258 → 261 operations, sha256 5a1333ac… → 5e94a3ae….

A-BE gates: GB4-1 focused tests green (25 + 4 + 1 read-state tests, the code tests, the seed helpers); GB4-2 neighbours
green per file (logs/gb4-2_neighbours.log); GB4-3 ruff, black, mypy (995 files) clean; GB4-4 export --check 0, alembic
head 0139, locks unchanged; GB4-5 12 mutants killed (control 35 passed); GB4-6 0139 up / down / up on empty and seeded,
model-built = migration-built (29 passed). 28 test functions added (2 parametrised).

## 3. Environment

Node v24.15.0, corepack 0.34.6, pnpm (pinned via corepack), Docker 29.9.3, Playwright Chromium 1.65.0, wrangler 4.158.0
signed in as the account FE-3 recorded (1cc89863…). No package added; pnpm lockfile 6c4beb1a… unchanged. Frozen install
30 m 11 s. e2e image ba02b57fc11e (1.17 GB, built once at BE_HEAD, 148 s, FEF-21's Dockerfile copy). Free space on C:
5.0 GB at the start, 6.0 GB at FE4_HEAD.

## 4. Design Skills

| Skill | SKILL.md SHA-256 (= FE-3's) | Outputs (skills\) | Decided | Not taken |
|---|---|---|---|---|
| ui-ux-pro-max | 43d1c5ad… same | 11 topic notes; pre-delivery-checklist-fe4.md (22 pass, 0 fail) | IL-03 rows, TH-01 pill, CP-01 Enter rules, LY-01 back behaviour, IL-04 load more | virtualised lists (no package) |
| taste-skill 2.5.1 | dfd1190a… same | direction-fe4.md | the panes' 30 % (list band, outbound bubbles, panel), one primary per view | marketing-page rules (Operate screen) |
| emil-design-eng | f6655603… same | motion-review-fe4.md | bubble 200 ms opacity, pill 150 ms, drawer = FE-3's sheet, no step transitions | — |
| impeccable 4.8.2 | e0565c6c… same | audit-fe4-1.md (14/20, P0 1), audit-fe4-2.md (15/20, 0 P0/P1) | the P0 fix d68961a | the launcher and detector (download an engine — SK4-04); two P3s |

The owner's mock-up docs/ui/inbox-page-mockup.html was read: three panes, filter row, header actions, Reopen in place of
the composer, template as primary when closed, Back on the phone step, Retry and Download were taken; notes, AI
suggestions, tags, follow-ups, consent switch, "Create lead", emoji icons, a violet button and a Take-over confirmation
were not (later stages / CX-01).

## 5. Decisions Applied

RP-01/02 (plan rows and BE1 §29 as scope); EC4-01..05; UR-01..12 (the owner's quotes and card above); SK4-01..04;
IL-01..07; TH-01..07; CP-01..06; CX-01..05; PN-01; RT-01..05; LY-01..03; UI4-01..05; IJ-01..13; VQ4-01..04; PV4-01..02 —
applied as written, with these recorded variations:

| Decision | Variation | Why |
|---|---|---|
| UR-02 foreign keys | no FK on `last_read_message_id` | a key from conversations into messages makes every message delete scan conversations |
| UR-04 kind | MessageKind values (text, image, audio, video, document, template, …) | the row names "Photo", "Voice message" |
| UR-05 "in the list's one query" | one batched statement per page beside BE1's participants read | the presenter's existing pattern; equal counts for 1 and 50 rows |
| TH-05 | CSP unchanged | img-src / media-src already allowed `blob:` and nothing else needed it |
| CP-01 draft | one more file on the browser-storage allow-list | the draft holds only its text, in sessionStorage |
| A-BE paths | README.md, docs/DEPLOYMENT.md | test_documentation_truth checks the migration range and the route count |

Decisions NOT taken: as the prompt's list (per-member unread, assign on open, FE-5..FE-14 screens, typing indicator,
virtual list / player / PDF / markdown packages, upload percentages, push, service worker, Embedded Signup, the full
local matrix — frontend.yml / ci.yml ran it).

## 6. The Plan's FE-4 Rows

| Plan demand (section 29 / 27.1) | Served by | Built |
|---|---|---|
| APP-INBOX-01 list, filters, cursor | IL-01..07 | yes |
| APP-INBOX-02 conversation, media, composer | TH, CP, CX | yes |
| APP-INBOX-03 customer panel (GET contacts?q= workaround) | PN-01 | yes, from participant + lead; the workaround is superseded by FEB-04 |
| stream leader election, reconnect rules | FE-3's coordinator, unchanged | reused |
| event → invalidation map | RT-01..02 | yes |
| optimistic send with idempotency | CP-02 | yes |
| media viewer (blob) | TH-04, TH-06 | yes |
| composer window / template rules | CP-04, CP-05 | yes |
| phone three-step layout | LY-01 | yes |
| gap refetch with `updated_since` | RT-03 | superseded by BE1's views (422 inbox_view_with_updated_since) — refetch from the top |
| J2, J10 green; 6 tabs → 1 stream; API killed → banner → resync; switch closes stream; no duplicate after 10 retries | IJ-02, IJ-05, IJ-11, IJ-08, IJ-11, IJ-06 | yes |

RP-02: every sentence of BE1 §29 maps to a decision and a test (refetch on cursor_view_mismatch → IL-04 / IJ-03; q out
of the address bar → IL-05 / IJ-03 / IJ-10; thin frames → RT-01; media by kind and status → TH-03 / IJ-05).

## 7–12. The screens

- **List** (`/inbox`): server views in the URL (`status`, `assigned_to`, `mode`, `unread=1`), search in memory only
  (debounced 300 ms, bounds of search_term_invalid), one infinite query per view, stop on `next_cursor: null`, a stale
  cursor → one silent restart from the top, a second → error state with Reload. Rows: name or number in `<bdi>`,
  channel, connected number (when more than one), state and mode badges with text, assignee ("You"), priority, relative
  time on one shared minute timer, unread pill (99+), preview (Reply:/Template:/the media kind). Live updates never move
  rows under the pointer: "Updated — show latest" when scrolled or focused; a page the person loaded applies at once
  (FEF-29).
- **Conversation** (`/inbox/[id]`): history paged backwards with the anchor kept; `after=` pages for new messages;
  bubbles by side with author (AI / member name), day separators in the viewer's time zone, `dir="auto"` text, links as
  buttons behind a confirmation showing the full URL; delivery status with words. Media: blob fetched with the bearer
  (images ≤ 2 MiB in view, larger on request, audio/video on play, documents on Download), ≤ 3 in flight, type checked
  against the tile, object URLs revoked on unmount, conversation change, sign-out, switch and page hide, 50 MB in-memory
  cap. Viewer dialog; transcripts behind "Show transcript (automatic)". Mark read once per change while visible and at
  the newest; "Seen by <name> · <time>".
- **Composer**: Enter sends on pointer devices, never during IME composition; draft per (workspace, conversation) in
  sessionStorage, cleared on send, sign-out, switch and close; one Idempotency-Key per bubble kept for every retry,
  deduped on message id; media pre-checks (canonical list, 25 MiB); closed window → templates (react-hook-form + zod
  from the template's parameters); opted-out and closed states; "Replying will assign this conversation to you."
- **Actions and panel**: not optimistic; assignment sends `expected_assigned_to_id`, a stale answer refetches and says
  who has it now. Panel: participant, number with Copy, lead (GET /leads/{id}) or "No lead"; nothing of FE-5.
- **Realtime**: FRAME_HANDLERS replaced by the typed map (message.created → after= page, detail, first page of each
  loaded view, home's card, the summary; message.status → patched in cache; conversation.updated → detail, lists, the
  summary when `read`/`status`; resync → from the top; follow_up / campaign → no-ops), coalesced per key in 250 ms; the
  polling fallback after FE-3's 10 s quiet period at `poll_after_seconds` (30 s default), never hidden, stops when the
  stream opens.
- **Layout**: ≥ 1024 three panes, 768–1023 the panel as a drawer from the inline end, < 768 three steps
  (`/inbox`, `/inbox/<id>`, `?panel=customer`), `interactive-widget=resizes-content`.

## 13. Composites and Look

New in @wasla/ui (Inbox story group): ConversationRow, ConversationList, InboxFilters (SegmentedControl), MessageBubble,
DaySeparator, MediaTile, TranscriptBlock, Composer, LinkConfirm, SidePanel, NewMessagesPill, and NavList's count slot.
Role tokens only; contrast pairs added for the bubbles, pill, failed strip and muted transcript. DESIGN.md gains an
inbox section (+35 lines).

## 14. Proofs (local)

| Gate | Result |
|---|---|
| FG-1 install / audit / licences | frozen install exit 0, lockfile unchanged, audit 0 high/critical, licences 174 / 869 unchanged |
| FG-2 drift | 0 |
| FG-3 unit, typecheck, lint, i18n, format | web 433 (start 386), ui 97, i18n 20, api-client 51 — green; format clean; 0 new lint disables |
| smoke | J1–J11 PASSED (J6 expected as before) |
| FE-4 journeys, FE-2 / FE-3 / BE1 named journeys, inbox visual roles | §21 |
| axe | inside the journeys: 0 serious / critical on every new state reached |
| impeccable | 0 P0 / P1 after d68961a |
| 60 / 30 / 10, new screens | all 12 captures inside the hard limits (§21) |
| ui-ux-pro-max checklist | 22 pass, 0 fail |

## 15. What Was NOT Run Locally

Firefox and WebKit projects; the full six-project matrix; Storybook build with axe / keyboard / motion / layout; the full
visual-roles averages; the Workers build before the push; the full backend suite and its lanes — frontend.yml / ci.yml.

## 16. The Contact Sheet

Not produced as a separate sheet: the visual-roles captures of the inbox (empty, list + conversation, phone list step,
phone conversation step, templates dialog; en / ar; 1280 / 390) are written by the visual-roles project and kept as CI's
artefacts and in E:\wasla-fe4-notes\screens\ when run locally.

## 17. frontend.yml

The FE-4 specs join every browser project automatically; IJ-08 (fe4-api-down.spec.ts) joins the api-down project after
stream-restart (`API_DOWN = /fe[34]-api-down\.spec\.ts$/`); the job titles say so. No new job; nothing deployed.

## 18. Mutation Check

Backend M-V01..M-V10, M-V35, M-V36: 12 killed (control 35 passed).

Frontend (controls: the web test file 45 → 46 passed, ui 11 passed): M-V11, M-V11b (onlyAppends), M-V12..M-V22, M-V24..M-V27,
M-V26b (downSince), M-V29, M-V30, M-V31, M-V33, M-V34, M-V37, M-V38, M-V17 — killed by unit tests. M-V28 killed by
test/inbox.test.tsx (its first run was invalid — the edit left an unbalanced tag, "no tests" — re-run with both tags).
M-V23 (expected assignee dropped) SURVIVED IJ-07 at first: its stale case started unassigned, where null is right; IJ-07
gained the case null cannot pass → KILLED (409 expected, 200 received). M-V32 as first written (count 0 from the answer)
was equivalent; re-written as an optimistic zero of the row before the answer → KILLED by the new IJ-13b. None hung; the
tree was restored after each (git status clean).

## 19. Documentation and ADR

ADR-198 in DECISIONS.md; DESIGN.md inbox section; frontend/README.md inbox routes and walk-through; docs/API.md codes and
the three routes; README.md / docs/DEPLOYMENT.md migration range and route count.

## 20. Part A Commits and the Diff Guard

| Commit | Subject |
|---|---|
| 450abf8 | feat(api): shared read state, seen by, preview, unread filter and summary, mark read / unread, assign on first reply |
| 94d7e93 | feat(api): specific error codes for the inbox errors FE-4 shows (FEB-06) |
| e6a6b72 | test(e2e): seed helpers for FE-4's inbox states (EC4-04) |
| 66f4cd4 | feat(ui): inbox composites |
| cdf3d04 | feat(web): the stream's invalidation map, coalescing and the gap hook |
| 8ce4794 | feat(web): the realtime inbox |
| f8f2bb1 | test(frontend): FE-4 inbox journeys, IJ-08 in api-down, inbox screens in visual roles |
| 31c43a9 | docs: the realtime inbox; DESIGN.md additions (ADR-198) |
| d68961a | fix(web): a loaded page applies at once and a stale cursor restarts cleanly (IL-04, IL-06) |
| f1abb5b | fix(web): the inbox list's blue band only beside a conversation pane (VQ4-02) |
| 86fca61 | test(e2e): JR-02 counts the Inbox entry; a modal is measured as itself |
| 41dc0c4 | test(e2e): a stale unassignment is refused; a failed mark-read leaves the count (CX-02, IL-07) |
| fcd84ed | fix(web): the polling fallback runs through the stream's reconnect attempts (RT-04) |
| d4b17e1 | test(e2e): IJ-07's stale unassignment holds the screen's view while it happens |
| 652316a | test(e2e): IJ-07's held detail read tolerates a request the screen aborts |

Diff guard: A-BE touched only its paths (list in the draft); A-FE only frontend/, .github/workflows/frontend.yml,
DECISIONS.md; protected paths (session routes, packages/session, the api-client's client and stream helper, the stream
coordinator, event-types.json, fixtures, the lockfile) — empty diff; 0 raw colours outside tokens.css, 0 physical sides,
0 innerHTML.

## 21. Gates at FE4_HEAD (652316a)

| Gate | Result |
|---|---|
| FG-1 | install / audit / licences as §14; lockfile 6c4beb1a… unchanged |
| FG-2 | drift 0 |
| FG-3 | at d4b17e1 (the code of FE4_HEAD): web 433, ui 97, i18n 20, api-client 51; typecheck, lint, i18n:check, format clean |
| FG-4 smoke | J1–J11 PASSED (J6 as expected before FE-2), image ba02b57fc11e |
| FG-4 named set at d4b17e1, fresh profile (down -v, up --no-build) | 62 passed, 13 skipped by design, 1 failed (IJ-07 chromium-phone ar: the test's own route raced an aborted read) |
| FG-4 at 652316a | IJ-07 8 / 8 (desktop and phone, en and ar, twice); the only change after d4b17e1 is that test's own step |
| FG-4 IJ-08 alone (api-down project) | passed at d4b17e1 (1.0 m) |
| FG-5 axe | inside the journeys: 0 serious / critical |
| FG-6 visual roles, inbox (Chromium) | table below; targets: 0 failing on every capture |
| FG-7 mutation | §18 |
| FG-8 backend path check | only A-BE's paths in b98507d..e6a6b72; none after it |

Named set: fe4-inbox.spec.ts (IJ-02..IJ-07, IJ-09..IJ-13, IJ-13b), fe3-shell.spec.ts (JR-02, JR-03, JR-08, JR-09, JR-10),
be1-suspended-reload.spec.ts, fe2-session.spec.ts (J6, J6 control, J7), visual-roles.spec.ts (web-fe4 inbox); Chromium
desktop 1280 and phone 390, en and ar, 2 workers. J6 phone ar crashed once (0xC0000409, before the test began) and passed
when re-run alone.

| Capture | dominant | secondary | accent | status | text/other |
|---|---|---|---|---|---|
| web-inbox-empty en / ar 1280 | 57.3 / 56.6 | 37.0 / 37.4 | 5.5 / 5.9 | 0.1 / 0.1 | 1.0 / 0.8 |
| web-inbox-conversation en / ar 1280 | 49.7 / 49.6 | 44.1 / 44.0 | 5.9 / 6.2 | 0.3 / 0.2 | 3.0 / 2.7 |
| web-inbox-templates (dialog) en / ar 1280 | 97.2 / 97.6 | 2.8 / 2.4 | 0 / 0 | 0 / 0 | 2.9 / 2.5 |
| web-inbox-empty en / ar 390 | 67.8 / 67.6 | 25.0 / 24.8 | 7.0 / 7.4 | 0.3 / 0.2 | 2.2 / 2.0 |
| web-inbox-list-step en / ar 390 | 78.3 / 76.8 | 13.9 / 15.1 | 7.2 / 7.6 | 0.6 / 0.5 | 3.7 / 3.5 |
| web-inbox-conversation-step en / ar 390 | 77.2 / 76.3 | 15.8 / 16.5 | 6.6 / 6.9 | 0.5 / 0.3 | 2.8 / 2.6 |

Averages estimated with FE-3's Linux averages for the earlier screens (1280: 62.3 / 29.9 / 7.6; 390: 64.0 / 27.1 / 8.3) and
about 38 earlier captures per width: 1280 ≈ 63.2 / 29.6 / 7.3, 390 ≈ 65.6 / 25.9 / 8.0 — inside the bands. The real averages
are frontend.yml's visual-roles projects on Linux (§25). The dialog is measured as itself (86fca61): the scrim over the page
read as 49 % text/other; no band or limit changed.

## 22. Untracked Files

E:\wasla: 14 paths, SHA-256 identical to BE1's untracked_start.txt at the start and before the merge (§23 records after).
Nothing incoming lies under docs/ui/; docs/ui/inbox-page-mockup.html was read only.

## 27. Findings

| ID | Finding | Status |
|---|---|---|
| FEF-29 | desktop "Load more" parked behind "Updated — show latest"; the cursor restart raced the append | CLOSED (d68961a) |
| FEF-30 | the polling fallback was reset by every reconnect attempt — it never polled during an outage | CLOSED (fcd84ed) |
| FEF-31 | the header said "Assigned to Unassigned" | CLOSED (fcd84ed) |
| FEF-32 | Playwright on Windows can leave its `next start` servers running after a run (port 3000 held) | ACCEPTED RESIDUAL (local only; stopped by port, PIDs and start times checked) |
| FEF-33 | "Assigned to <name>" is two strings, not one ICU message — word order in ar / tr | DEFERRED (with FEQ-08's review) |
| FEB-36 | none: every inbox state was reachable through the API, the fake webhook or a seed helper | — |
| FEF-27 (BE1), FEB-34, FEB-35, FEF-28, FEF-26, FEF-22 | carried, not touched | as recorded |
| FEF-27 double use | FE-3's addendum and BE1 both used FEF-27; this report writes "FEF-27 (BE1)" for BE1's | noted |

## 29. Infrastructure Created by This Stage

Worktree E:\wasla-fe4-fix (branch fe4-realtime-inbox-20261114); notes E:\wasla-fe4-notes; A-BE's wasla-fe4-net / -pg /
-redis (removed by name after A-BE); the venv in the notes folder; the e2e image wasla-fe-e2e-app:local (ba02b57fc11e,
1.17 GB); the e2e profile's containers and volumes (down -v after the last run). C: 5.0 GB free at the start, 5.8 GB after the
last run. No prune of any kind.

## 31. Remaining External Actions

Walk the inbox locally (frontend/README.md) and say what to change; say whether unread and the preview read the way you
want; review the new ar / fr / tr texts (FEQ-08, with FEF-33); pick the next stage (FE-6, FE-8, FE-7, FE-5 or the WhatsApp
Embedded Signup backend stage); FEB-34 / FEF-27 (BE1), FEB-35, FEF-28, FEF-26 later; delete earlier stages' folders and
decide the local branches fe3-app-shell-20261113, be1-inbox-contract-20261114, fe4-realtime-inbox-20261114; free space on
C:; security.yml's RGM-23 / 24 / 27; merging the canonical branch into main.

## 32. What the Next Stages Can Rely On

FE-5: the panel (components/inbox/customer-panel.tsx) is where the contact detail and lead editing plug in, after FEB-19;
the inbox composites in @wasla/ui. FE-6: lib/stream-handlers.ts's typed map is where document events (FEB-11) go. FE-7 / FE-8:
the shell's registry, NavList's count slot and the banners unchanged. Embedded Signup: rows and panel are channel-neutral.
Later unread work: UR's three routes and fields.

## 23. Merge and Tree Equality

In E:\wasla on worktree-billing-google-auth: origin = local = b98507d before the merge; no untracked path collides with an
incoming one; none lies under docs/ui/. `git merge --no-ff 652316a` → 88d913e (parents b98507d, 652316a), no conflict;
`git diff --exit-code 652316a HEAD` → 0; alembic/versions ends at 0139; the 14 untracked paths' SHA-256 unchanged.

## 24. Push

Pushed once without force after checking that no ci.yml run was in progress and origin was still b98507d. The runs are in
the addendum.

## 33. Final Verdict (at the report commit)

Every focused local gate is green at FE4_HEAD 652316a and the merge is made; the push follows this commit. The verdict line
(FE-4 MERGED AND PUSHED, CI GREEN / RED / NOT OBSERVED) is written in the addendum once frontend.yml and ci.yml have run.

---

# Addendum — GitHub Actions, the fixes after the push, Part C and the verdict

## A1. Runs

| Push | Head | frontend.yml | ci.yml |
|---|---|---|---|
| 1 | 7ba4935 (merge 88d913e + report) | [43639725952](https://github.com/mohamedshhahat1/wasla/actions/runs/43639725952): FAILURE — Storybook (drawer without a name), Chromium IJ-07, Firefox IJ-03, WebKit IJ-07; api-down did not run | [43639726001](https://github.com/mohamedshhahat1/wasla/actions/runs/43639726001): FAILURE — 8 / 8189 (an old-revision seed, a stub, people left behind) |
| 2 | 63c57d7 | [43645987272](https://github.com/mohamedshhahat1/wasla/actions/runs/43645987272): FAILURE — Chromium JR-04 ar, Firefox BE1 suspended reload, WebKit IJ-08 (api-down ran JR-11 beside IJ-08) | [43645987305](https://github.com/mohamedshhahat1/wasla/actions/runs/43645987305): FAILURE — 5 / 8192 (six users left by test_e2e_inbox_states) |
| 3 | 2fbc9ba | [43652731659](https://github.com/mohamedshhahat1/wasla/actions/runs/43652731659): FAILURE — Firefox JR-04 ar + IJ-02 en, WebKit JR-04 en | [43652731732](https://github.com/mohamedshhahat1/wasla/actions/runs/43652731732): SUCCESS |
| 4 | 229814c | [43688877493](https://github.com/mohamedshhahat1/wasla/actions/runs/43688877493): FAILURE — Firefox JR-04 en phone (FEF-34's second path) | [43688877502](https://github.com/mohamedshhahat1/wasla/actions/runs/43688877502): SUCCESS |
| 5 | 1c923fb | [43695920903](https://github.com/mohamedshhahat1/wasla/actions/runs/43695920903): FAILURE — Firefox BE1 suspended reload, Firefox J1 ar phone, WebKit IJ-13 en | [43695920869](https://github.com/mohamedshhahat1/wasla/actions/runs/43695920869): SUCCESS |
| 6 | 4550675 | [43716094045](https://github.com/mohamedshhahat1/wasla/actions/runs/43716094045): FAILURE — WebKit IJ-13, IJ-06, IJ-04/05 | [43716094062](https://github.com/mohamedshhahat1/wasla/actions/runs/43716094062): SUCCESS |
| 7 | e9b8d65 | [43722323737](https://github.com/mohamedshhahat1/wasla/actions/runs/43722323737): FAILURE — Chromium JR-10 only | [43722323639](https://github.com/mohamedshhahat1/wasla/actions/runs/43722323639): SUCCESS |
| 8 | d571765 | [43735244892](https://github.com/mohamedshhahat1/wasla/actions/runs/43735244892): **SUCCESS, every job** | [43735244716](https://github.com/mohamedshhahat1/wasla/actions/runs/43735244716): **SUCCESS, every job** |

Green run 43735244892 (frontend.yml, d571765), job by job: install, drift, checks (typecheck, lint, unit, i18n,
build, audit), Storybook (axe per story, RTL keyboard): success. E2E:

| Part | Result | Duration |
|---|---|---|
| Chromium, desktop and phone | 178 passed / 18 skipped | 15m |
| Firefox, desktop and phone | 170 passed / 26 skipped | 17m45 |
| WebKit, desktop and phone; then the API restart, JR-11 and IJ-08 alone | 173 passed / 26 skipped | 19m41 |
| Visual roles and the 60 / 30 / 10 check | 62 passed | 4m35 |

Green run 43735244716 (ci.yml, d571765): lint / format / types, tests and migrations, migration-built schema, alert
rules, Docker build: success.

## A2. Fixes after the push (no band, bound, retry count or journey weakened; nothing skipped)

| Commit | What | Found by |
|---|---|---|
| a91e453 | fix(ui): the customer panel's drawer is named by its title | push 1, Storybook axe |
| 5e530f1 | test(e2e): IJ-07's stale assignments and IJ-03's paging wait on facts | push 1 |
| 8d25978 | test: an old-revision seed, a stub, people left behind | push 1, ci.yml |
| 9a0b91d | test(e2e): api-down runs one journey at a time | push 2 |
| 68ddfe7 | test: the e2e inbox-state test leaves no people behind | push 2, ci.yml |
| 627f2bb, 1b098ec | fix(web): FEF-34 — the gate middleware is in place before the first reads (layout effect); a recheck cancels the still-running first reads before reading the gates again | pushes 3 and 4, JR-04 |
| 8f4f00f | fix(web): FEF-35 — a mark-as-unread is not undone by the screen showing it | push 5, IJ-13 |
| a87b5e7 | fix(web,ui): FEF-36 — time zone forms submit once the zones are in; the Combobox shows a value set from outside | push 5, J1 |
| e8437c1, 60764cc | fix(web): FEF-37 — the first stream open after the inbox loaded is a gap; a gap and the polling fallback re-read the unread total | local sweep, push 6 |
| 9a72ea2 | test(e2e): BE1 records only the reloaded page's requests | push 5 |
| 8fb0ea7 | test(e2e): IJ-06 counts the requests sent, not presses; IJ-04 scans after the dialog is gone | push 6 |
| c2b9a71, 7e37ebc | fix(web): FEF-38 — sign-in and sign-up leave navigation to PageGate; the e2e retry helper added at push 4 removed | push 7, JR-10 |

Each app fix has a unit test whose mutant (the fix removed) fails. Before pushes 6–8 the whole e2e suite ran locally
at CI's 3 workers (Chromium, Firefox, WebKit, then API restart / api-down alone). Local-only noise, not counted as
results: WebKit and Playwright workers crashing on Windows (0xC0000409), login / workspace-create 429s at 5+ workers,
and one IJ-02 ar phone miss in a loaded Chromium run (12 / 12 alone, then the full Chromium suite 178 / 178).

## A3. Part C — the previews

- **Account.** `wrangler whoami` (4.158.0, run from the notes folder): "Mohamedshhahat1@gmail.com's Account",
  1cc898637894546669e72610c46e7d0e.
- **Build.** `build-workers-linux.mjs --env preview` in E:\wasla-fe4-preview at d571765, with PUBLIC_APP_ORIGIN,
  PUBLIC_CONSOLE_ORIGIN and PUBLIC_MARKETING_ORIGIN set to the three workers.dev origins.
- **Which outputs changed.** Console and marketing were also built at b77730a (FE-3's deployed sources) in
  E:\wasla-fe4-preview-base. Marketing at b77730a is 29 files / 570,342 B (= FE-3's deploy); at d571765 29 files /
  572,550 B — the shared stylesheet and the four locale pages differ. Console's CSS and JS chunks differ. Web changed
  with FE-4 itself. All three redeployed.
- **Smoke.** `workers:smoke` green: every page 200, session routes GET 405 / same-origin POST 503 session_unavailable
  (= FE-3). Every wrangler.jsonc unchanged; no secret set.

| Worker | URL | Previous version (FE-3) | New version | Upload (gzip) |
|---|---|---|---|---|
| wasla-preview-web | https://wasla-preview-web.wasla-marketing.workers.dev | 0ef04cfb-8e99-4303-ba43-2b9e937972c7 | f3c3842b-779c-4bb6-afe6-8281512a8300 | 8070.36 KiB (1635.05 KiB) |
| wasla-preview-console | https://wasla-preview-console.wasla-marketing.workers.dev | 07d1bba6-0f7b-4f8a-932b-06c94383b4c0 | d20591cf-d1a1-48a8-88e8-81aedf636c51 | 6235.23 KiB (1319.30 KiB) |
| wasla-preview-marketing | https://wasla-preview-marketing.wasla-marketing.workers.dev | 0b732c17-1e18-4c8c-ab71-0dde38bb9855 | 6f2e2b78-3240-4f75-9381-27846bad0490 | 0.35 KiB (0.24 KiB) |

Live: web /login and /, console /, marketing /en/ and /ar/ answer 200; the live session route answers a same-origin
POST with 503 session_unavailable and a cross-origin one with 403 session_request_refused. The previews still have no
API: the inbox is tried locally (frontend/README.md); FEF-26 unchanged.

## A4. Findings updated

| ID | Finding | Status |
|---|---|---|
| FEF-34 | FE-3's gate race: a revoked member sent to /workspaces/new — the 403 met by reads started before the gate middleware, and a recheck reusing the still-running first reads | CLOSED (627f2bb, 1b098ec) |
| FEF-35 | an open conversation re-read a conversation another member had just marked unread | CLOSED (8f4f00f) |
| FEF-36 | FE-2's time zone forms: a press before the zones answered failed silently; the Combobox stayed blank | CLOSED (a87b5e7) |
| FEF-37 | a message between the list's first read and the stream's first open was lost; the gap re-read missed the unread total | CLOSED (e8437c1, 60764cc) |
| FEF-38 | FE-2's sign-in / sign-up navigated twice; the late second navigation pulled the person back home | CLOSED (c2b9a71, 7e37ebc) |
| FEF-29 .. FEF-33 | as in §27 | unchanged |

## A5. Final Verdict

**FE-4 MERGED AND PUSHED, CI GREEN**: frontend.yml 43735244892 and ci.yml 43735244716 for d571765 are green in every
job. This addendum's own runs are recorded in E:\wasla-cleanup\CLEANUP_fe4_2026-11-15.md.

PREVIEWS UPDATED: wasla-preview-web, wasla-preview-console and wasla-preview-marketing, all three redeployed.

CLEANUP: see E:\wasla-cleanup\CLEANUP_fe4_2026-11-15.md.
