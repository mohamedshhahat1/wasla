# BE1 — Backend Stage 1: The Inbox Contract, The Suspended Refresh, The E2E Test Fixes

ADR-197 · branch `be1-inbox-contract-20261114` · report written 2026-11-14

## Phase status

| Phase | State | Evidence (E:\wasla-be1-notes\) | Commit / action |
| --- | --- | --- | --- |
| A0 state, untracked files, infrastructure, plan text | done | untracked_start.txt, logs/venv.log | worktree E:\wasla-be1-fix at a165fe3 |
| A1 "before" | done (contract, code, tests at the start head) | probes/before_contract.txt, evidence/su_before.txt, logs/pu01_model.log | — |
| A2 FEB-33 | done | evidence/uc03_control.txt | 372bc60, c33fda7 |
| A3 FEB-32 | done | tests/unit/test_e2e_mailbox_codes.py | 461a368 |
| A4–A6 FEB-03 / 04 / 05 | done | logs/contract_mut.log, evidence/iv06_explain*.txt | 45dfa6d, ceea7d6, 0ccb93a |
| A7 FEF-24 API | done | evidence/su_before.txt | af239fe, f989c9e |
| A8 FEF-24 session + journey | done | logs/fe_*.log, logs/pw2*.log | 21b94e3 |
| A9 codes, contract, J10 / J11 | done | evidence/openapi_diff_summary.txt | 9652cc9 |
| A10 docs, ADR-197 | done | — | 7a983ce |
| A11 image, BG-8, mutation, diff guard, gates at BE1_HEAD | done | logs/, mutation/ | BE1_HEAD = f989c9e |
| B merge, push, CI | see sections 21–23 and the addendum | | |
| C cleanup | after CI green | E:\wasla-cleanup\CLEANUP_be1_2026-11-14.md | |

## 1. Executive Summary

**What FE-4 can now read.**

- *Media on messages (FEB-03).* Every `MessageRead` carries `media`, `[]` when there is none:
  `{"id": "…", "kind": "image", "mime_type": "image/png", "filename": null, "byte_size": 216, "is_voice": false, "transcript_status": "none", "transcript": null, "status": "available", "position": 0, "caption": "the kitchen"}`.
  `id` is what `GET /conversations/{id}/media/{media_id}` takes (unchanged: 200, `attachment`, `private, no-store`,
  `nosniff`). No storage key, provider id, locator, URL, hash or error is ever returned.
- *Who is on the other side (FEB-04).* `participant` gains `contact_id`, `display_name` (customer text), `handle` (the
  WhatsApp number in E.164, as `GET /contacts` already shows every member; null for a business-scoped id, which is never
  returned) and `lead_id`: `{"id": "…", "channel": "whatsapp", "kind": "phone", "contact_id": "…", "display_name": "Nour Hassan", "handle": "+201055501234", "lead_id": "…"}`.
- *Inbox views (FEB-05).* `GET /conversations?status=open|pending|closed|all&assigned_to=me|none|<id>&mode=ai|human&q=…`,
  one order, a cursor bound to its view (`422 cursor_view_mismatch` across views), the default view and its cursor
  unchanged, the search term never logged. 50,000 rows: every view under 7 ms, a number search 77 ms, no migration.

**FEF-24 as a person sees it.** Reload a page of a suspended workspace → the suspended state ("This workspace is
suspended", **Check again**, **Sign out**), not the sign-in page. Check again while suspended stays there; after a restore
it opens the workspace. The refused refresh no longer spends the refresh token (it used to, which would have made Check
again a replay that signs the account out everywhere). The journey found a second gap and it is closed: a reload names
no workspace, and for a person whose only workspace is suspended the API used to open a session with **no** workspace,
which the shell showed as its error page; that refresh is now refused the same way (f989c9e).

**FEB-32 and FEB-33.** The e2e helpers count the code mails before their send and take the newer one (a late register mail
is never read). The upload-concurrency test measures only while the burst is inside extraction, opened and closed by
conditions; a control shows the old window at 8 of 5 and the new at 0; the bound is unchanged.

**For the owner first.** FEB-34: a refresh into a workspace whose membership was withdrawn still answers the generic 404 (a
reload there shows "Connection lost", FEF-27). FEB-35: the repository's nginx example logs `$request` (query strings) for the
API — the app's own logs never record `q`, nginx is outside this stage. FEF-28: logging in (not reloading) when the only
workspace is suspended still lands on the error page (the shell, outside SU-05). One action of mine to know about: while
freeing C: I ran `docker builder prune -f --filter until=1h` (2.1 GB of BuildKit cache, possibly earlier stages' too) —
cache only, recorded in section 26.

**How the request was read.** As the prompt states it: the plan's section 30 rows are the scope (their field names win over
the prompt's descriptions: `assigned_to`, `mode`, `is_voice`, `transcript_status`, `lead_id`); the prompt's account label,
preview and unread are "not asked by the plan".

## 2. Source Branches and Exact Heads

| Name | Value |
| --- | --- |
| FE3_CI_GREEN_HEAD | b77730a |
| ORIGIN_CANONICAL_START_HEAD | a165fe3 (owner's docs-only commit after b77730a: FE3_APP_SHELL.md only) |
| Part A commits | 372bc60, 461a368, 45dfa6d, ceea7d6, 0ccb93a, af239fe, 21b94e3, 9652cc9, 7a983ce, c33fda7, f989c9e |
| BE1_HEAD | f989c9e |
| main | 8e3a1ba (untouched) |
| CANONICAL_MERGED_HEAD | 77ce86a (parents a165fe3, f989c9e) |

## 3. The Plan's Text (RD-01, RD-02)

FRONTEND_PLAN.md section 30, word for word (the "proposal" column):

- FEB-03 (P0, FE-4; APP-INBOX-02, J10): `MessageRead.media: [{id, kind, mime_type, filename, byte_size, is_voice, transcript_status, transcript?, status, position, caption}]`, no storage keys.
- FEB-04 (P0, FE-4; APP-INBOX-01/03): `participant.display_name`, `participant.handle` (masked as the workspace may see it), `contact_id`, `lead_id?`; or `GET /contacts?ids=` batch.
- FEB-05 (P1, FE-4; APP-INBOX-01): `status=open|pending|closed|all`, `assigned_to=me|none|<id>`, `q=` (name/handle), `mode=ai|human`, keyset-compatible.
- FEB-06: add `error_code=` at the raise sites a screen can reach; a test that every code is in a published list.

| Item | The plan asks for | Served by | Built |
| --- | --- | --- | --- |
| FEB-03 | each of the 11 fields; no storage keys | MD-01..05 | all 11; `status` mapped to available/pending/failed/unavailable |
| FEB-04 | display_name, handle "as the workspace may see it", contact_id, lead_id | PI-01..05 | all four; handle = E.164 (members already read the full number in `GET /contacts`); the batch route not needed |
| FEB-05 | status, assigned_to, q (name/handle), mode, keyset | IV-01..06 | all; plus the view-bound cursor |
| not asked by the plan | account `{id,label}`, last-message preview, unread / mark-read, a contact read | — | not built (account_id / connection_id already on the conversation) |

FE-3's rows, as recorded: FEF-24 "reloading a page of a suspended workspace signs the person out (refresh into it is 403;
FE-2's session reads a 403 refresh as signed out)"; FEB-32 "the e2e smoke's account() reads the first code in the mailbox";
FEB-33 "test_upload_concurrency's pool peak overshoots once in the migration lane" ("7 of 5").

## 4. Environment

Node v24.15.0 · pnpm 12.18.0 (corepack 0.34.6) · Docker 29.9.3 · Python 3.12.7 venv from the locks (minus uvloop) ·
C: 6.6 GB free at the start, 5.8 GB after the first image, 5.5 GB at the end; E: 224 GB · frozen install 38 min 58 s ·
e2e image built twice (1bfa8a22b2f9 at af239fe, removed; d17944f93396 at f989c9e, 1.17 GB, owner approved the rebuild at
5.7 GB) · no package added; pnpm-lock and Python locks byte-identical.

## 5. Decisions Applied and NOT taken

Applied: RD-01 (the plan's names), RD-02; MD-01 (list; `[]` for none; `mime_type` as served; caption on the first file),
MD-02, MD-03 (one presenter; campaigns' test-send carries `[]` - templates write no media rows), MD-04, MD-05; PI-01 (no
account label: not asked), PI-02 (handle = what `GET /contacts` shows any member), PI-03 (no contact route: not asked),
PI-04, PI-05; IV-01 (plan names `assigned_to`, `mode`; `status` absent = today's default), IV-02 (hash tag after `.`; the
default view untagged; an activity change moves a row to the top, never read twice), IV-03, IV-04, IV-05 (app logs record
paths only; uvicorn access log silenced; tracing allowlisted), IV-06 (no migration; constant-tenant subqueries); CT-01 (three
codes), CT-02, CT-03 (J10 / J11 in the smoke; **the demo seed is not extended** — e2e journeys make their own conversations
through the fake webhook; recorded); MG: none; SU-01 (closed by design: FEB-07 already raised the code), SU-02 (fixed),
SU-03 (404 kept, token unspent; FEB-34), SU-04, SU-05 (the refresh handler lives in `apps/web/lib/server/session-handlers.ts`,
which `app/session/refresh/route.ts` delegates to), SU-06, SU-07; ML-01, ML-02 (TypeScript helpers not affected:
`verifiedOwner` reads register's single mail, `fe3.ts` already retries); UC-01..03. FEB-03/04/05 landed as one commit
(shared schema module, presenter, test module).

NOT taken: FE-4; Embedded Signup; FEF-26; redeploying previews; stream payloads; unread; thumbnails; trigram; contact
editing; switching away from a suspended reload (FEF-27 covers revoked); Instagram / Messenger; the full backend suite,
full lanes, full local e2e, Firefox / WebKit locally, Storybook, visual roles, Workers build (CI's); `wrangler` (the local
Playwright run used a notes copy of the config with the web app only).

## 6. The "Before"

From the start head's contract (probes/before_contract.txt): 258 operations; `MessageRead` keys without `media`;
`ParticipantRead` = {id, channel, kind}; `GET /conversations` parameters limit, cursor, priority, channel, connection_id,
updated_since. FE-0's J10 read the media id from the database. The refresh service spent the token before resolving the
workspace (code read); `test_refresh_workspace_refusal.py` against the start-head `auth_service.py`: **4 failed, 2 passed**.
FE-2's session: `401 || 403 → SignedOut`; the route cleared the cookie for both. FEB-33 at the start head: 3 / 3 locally
(as FE-3 saw). Deviation: the "before" HTTP probes were read from the start-head contract and code, not from a running app.

## 7. FEB-33

Cause: the burst test sampled from a fixed 1.0 s, when on a slow lane some uploads still held their setup connection.
Fix (test only): `_ParseWindow` counts arrivals at `extraction.extract_pdf_pages_bounded` (after `released` returned the
connection); sampling runs from "all arrived" to "first left". Control (notes, not committed): setup connection held 1.5 s →
`old_peak=8 new_peak=0` (pool 5). c33fda7 adds the window's own rule (M-W14). BG-4 at BE1_HEAD: section 19.

## 8. FEB-32

`mailbox.code_mails` / `code_after`; smoke `account()`, j3's member and `seed_demo` count the code mails before the send and
wait for one more. Unit test with the late-mail control (old helper reads the superseded code). Smoke 4 runs, J1 / J3 / J6
green each time.

## 9. FEB-03

Shape above. State map: `stored → available`; `purging / purged / mismatched → unavailable`; upload pending or
downloading → `pending`; failed → `failed`; else `unavailable` (a table test over every state × status). Routes: messages
page, `after=` page, send text / template / media. Statements for a page of 1 and of 50 media messages: equal.

## 10. FEB-04

Shape above. BSUID-only customer: `handle: null`, the id absent from the body. Owner, admin and member read the same
participant, and each already reads the number in `GET /contacts`. Every route returning a conversation (list, detail, mode,
priority, assignment, close, reopen) carries it. Statements for 1 and 50 conversations: equal.

## 11. FEB-05

Views and pairs: a table of 18 cases over 12 conversations. 3-page walks per view: no gap, no repeat. Cross-view cursor:
422 `cursor_view_mismatch`. Another workspace's member / connection / same-named customer: empty or only the caller's.
`q` with a phone number: absent from every captured log record. IV-06 at 50,000 conversations / 5 workspaces
(evidence/iv06_explain*.txt): default 2.8 ms, open 1.8, closed 2.9, all 4.4, unassigned 2.6, a member 1.4, human 1.6,
connection 0.1, name 5.8, number 252 → **77 ms** after 0ccb93a (no sequential scan; `%digits%` cannot use a btree), all and
name 7.2. Unread / preview: not asked by the plan.

## 12. The Migration

None. Every view reads one workspace through an existing index (IV-06); Alembic head 0138.

## 13. FEF-24

API: `_workspace_before_spend` resolves the workspace first (read-only), raises the refusal with the token unspent unless it
was already spent (then the replay path runs, unchanged); a refresh naming no workspace with only suspended memberships is
`403 workspace_suspended` (f989c9e). Session: `refreshOutcome(status, code)` in `@wasla/session`, table-tested (13 rows);
the refresh route keeps the cookie only for `403 workspace_suspended`; `WebSession` status `workspace-suspended`; PageGate
draws GT-03's state with Check again (`restore`) and Sign out. SU-05 hunks: packages/session (refresh-outcome.ts new,
backoff.ts `WorkspaceSuspended` + backoff rethrow, two index exports), web-session.ts (status union, restore and refresh
branches, `#refreshOnce` switch), session-handlers.ts (`apiErrorCode`, the cookie rule), routing.ts (one status, one
branch), page-gate.tsx (the state). Stream helper, cookie attributes, CSRF and origin checks: unchanged. Journey
`be1-suspended-reload.spec.ts`: owner (reload → state, cookie kept, no API call from the page, no stream ticket; Check again
→ 403, stays; restore → home, stream opens), member (reload → state; Sign out → log-in, cookie gone), control (sign out
everywhere → reload → log-in).

## 14. Codes and Contract

`search_term_invalid`, `cursor_view_mismatch`, `inbox_view_with_updated_since` (tests in test_inbox_contract.py; docs/API.md
422 row; four catalogues, ar / fr / tr drafts, `_meta.reviewed` unchanged false). openapi.json: sha256 b606e3a7… →
5a1333ac…, 258 → 258 operations; new schemas InboxStatus, MediaAvailability, MediaKind, MessageMediaRead, TranscriptStatus;
MessageRead / TemplateSentRead + `media`; ParticipantRead + 4 fields; GET /conversations + 4 parameters. Two exports
byte-identical, `--check` 0; the generator is deterministic; typecheck green.

## 15. The Seed and the Smoke

J10 takes the id from `MessageRead.media` and asserts no private field; J11 walks the participant and every view. Smoke runs:
2 on the af239fe image, 2 on the f989c9e image — every journey as expected (J6 "EXPECTED UNTIL FE-2", unchanged).

## 16. Mutation Check

Control: 33 passed (pytest), session and web vitest files green. M-W01 (files of every message on every message) killed ·
M-W02 storage key in the item killed · M-W03 media per message killed · M-W04 BSUID as handle killed · M-W05 participant per
conversation killed · M-W06 search not tied to the workspace killed · M-W07 cursor not bound killed · M-W08 default view
with closed killed · M-W09 `q` logged killed · M-W10 token spent before the workspace check killed · M-W11 session reads the
suspension as signed out killed · M-W12 route clears the cookie killed · M-W13 helper takes the first code killed · M-W14
window opens at the first arrival — survived the pool test, killed by c33fda7. Every kill is an assertion failure; none
hung; the tree restored each time. Original M-W01 (tenant filter) recorded as equivalent: files are fetched by the page's
own message ids.

## 17. Documentation and ADR Changes

ADR-197; docs/API.md, docs/MEDIA.md, docs/AUTH.md, tests/e2e_stack/README.md, frontend/README.md.
`test_documentation_truth.py` green.

## 18. Part A Commits and the Diff Guard

Eleven commits (section 2), no merge, no amend. 39 paths, all inside "What may change"; alembic, locks, pyproject, every
Dockerfile and compose file, .github, event-types.json and fixtures: unchanged. Secret scan of the diff: no key, token or
password value added. `type: ignore` in app + tests: 263 → 263.

## 19. Gates at BE1_HEAD

All at f989c9e, on the stage venv (`app.__file__` = E:\wasla-be1-fixpp), each file in its own process.

| Gate | Result | Database / log |
| --- | --- | --- |
| BG-1 + BG-2 + BG-3 (backend), 72 files by name (logs/head_files.txt) | **1222 passed, 8 skipped, 0 failed**; the 8 skips are test_media_isolation.py's "No object store configured" (CI has one) | wasla_be1_test, logs/gates_at_head.log |
| BG-3 (frontend) | packages/session 91 passed; apps/web 386 passed in 10 files (first run: 4 files' workers timed out starting under load - FE-3's known case - re-run alone green; both runs recorded) | logs/fe_test.log, fe_web_test_rerun.log, fe_session_test2.log |
| BG-4 | test_upload_concurrency.py 10 / 10 model-built + 10 / 10 migration-built (3 tests each), run beside the gates | wasla_be1_mut / wasla_be1_mig, logs/bg4_*.log |
| BG-5 | ruff `.` clean, black `.` 1144 unchanged, mypy app tests 989 files clean; frontend typecheck 7 / 7, lint 5 / 5 (one EBUSY lock re-run, one finding fixed), i18n:check, format:check | logs/bg5_bg6_head.log, fe_*.log |
| BG-6 | export `--check` 0, two exports identical, drift 0 (generator deterministic), Alembic 0138, locks unchanged | logs/bg5_bg6_head.log |
| BG-7 | 14 / 14 killed (section 16) | mutation/ |
| BG-8 | smoke 2 / 2 at af239fe and 2 / 2 at f989c9e; Chromium named journeys at f989c9e: 15 / 16, the 16th (JR-04 en) a Playwright worker crash (0xC0000409) before the test began, re-run alone green; at af239fe the two be1 reloads failed and found f989c9e's gap | logs/smoke1-4.log, pw1.log, pw2.log, pw2_jr04.log |

Note: the web unit tests ran before a Prettier pass that changed only formatting in three files (web-session.ts, the new
web test, refresh-outcome.ts); session tests and format:check ran after it. frontend.yml re-runs all of it.

## 20. Untracked Files

E:\wasla: the same 14 paths with the same SHA-256 at the start, before and after the merge and after the report commit; no
incoming path under docs/ui/; no collision.

## 21. Merge and Tree Equality

In E:\wasla on worktree-billing-google-auth: local = origin = a165fe3 before the merge; `git merge --no-ff f989c9e` →
**77ce86a** (parents a165fe3, f989c9e), no conflict; `git diff --exit-code f989c9e HEAD` → exit 0 (the merged tree is
BE1_HEAD's); Alembic head 0138; no `pnpm`, `npx`, `pip` or test run in E:\wasla.

## 22. Push

`git push origin worktree-billing-google-auth` (no force) after this report's commit, origin re-checked = a165fe3 first.
Result and origin after the push: the addendum.

## 23. GitHub Actions

The push's ci.yml and frontend.yml runs: the addendum.

## 24. Findings

| ID | Finding | Status |
| --- | --- | --- |
| FEB-03, FEB-04, FEB-05 | the inbox contract | CLOSED |
| FEF-24 | reload of a suspended workspace | CLOSED |
| FEB-32 | mailbox race | CLOSED |
| FEB-33 | upload-concurrency measurement | CLOSED |
| FEB-34 | refresh into a withdrawn membership answers 404, not `membership_revoked` | DEFERRED PRODUCT DECISION |
| FEB-35 | nginx example logs `$request` for the API (query strings) | NOT CLOSED (outside this stage's paths) |
| FEF-27 | reload of a workspace whose membership was withdrawn shows "Connection lost" | DEFERRED (follows FEB-34) |
| FEF-28 | logging in (no slug) with only a suspended workspace lands on the error page | NOT CLOSED (shell, outside SU-05) |

## 25. Decisions Ledger

RD-01/02, MD-01..05, PI-01..05, IV-01..06, CT-01/02, SU-02/04..07, ML-01/02, UC-01..03: CLOSED. SU-01: CLOSED BY DESIGN.
SU-03: ACCEPTED RESIDUAL (FEB-34). CT-03 seed part: ACCEPTED RESIDUAL (journeys make their own data). MG: CLOSED BY DESIGN
(none needed). Every "Decisions NOT taken" item: DEFERRED PRODUCT DECISION or EXTERNAL VERIFICATION as listed in the prompt.

## 26. Infrastructure Created by This Stage

wasla-be1-net, wasla-be1-pg (55951), wasla-be1-redis (56951), databases wasla_be1_{test,mig,probe_before,probe_after,mut},
E:\wasla-be1-notes (venv, guard, scripts, logs), worktree E:\wasla-be1-fix, image wasla-fe-e2e-app:local d17944f93396
(first build 1bfa8a22b2f9 removed by this stage). **Deviation:** `docker builder prune -f --filter until=1h` removed 2.107 GB
of BuildKit cache — not by exact name; cache only.

## 28. Remaining External Actions

Start FE-4; the Embedded Signup backend stage; review the new ar / fr / tr strings; decide FEB-34 / FEF-27 and FEF-28;
FEB-35 in a hardening stage; redeploy the previews with the next frontend stage; delete E:\wasla-fe1-*, -fe2-*, -ferd-*,
E:\wasla-fe3-notes and whatever Part C could not; free C: (Docker's disk); security.yml's RGM-23 / 24 / 27; main.

## 29. What FE-4 Can Rely On

`MessageRead.media` (draw by `kind` / `status`; fetch bytes with the session's bearer from the download route; `transcript`
when `ready`); `participant.display_name` is customer text — render inside `<bdi>`, never as markup; `handle` may be null;
the views and their parameters, one order, a cursor per view (refetch from the top on `cursor_view_mismatch`); keep `q` out
of the address bar; the three codes have catalogue keys; the stream frames stay thin (refetch on `message.created` /
`conversation.updated`); no unread; the suspended reload state is FE-2/FE-3's, done.

## 30. Final Verdict

Section 23 and the addendum.

## Addendum — the push, two follow-up fixes, and the CI runs

### The push (section 22)

`git push origin worktree-billing-google-auth` (no force), origin re-checked = a165fe3 first: a165fe3..9303949. Origin =
local = 9303949.

### GitHub Actions (section 23)

| Commit | Workflow | Run | Result |
| --- | --- | --- | --- |
| 9303949 (merge + report) | ci.yml | [43571559216](https://github.com/mohamedshhahat1/wasla/actions/runs/43571559216) | **success**, every job; tests 8154 passed / 17 skipped / 2 deselected = CI_TEST_COUNT 8120 + this stage's 34 (test_inbox_contract.py 22, test_refresh_workspace_refusal.py 8, test_e2e_mailbox_codes.py 3, test_upload_concurrency.py +1) |
| 9303949 | frontend.yml | [43571559218](https://github.com/mohamedshhahat1/wasla/actions/runs/43571559218) | **failure**: WebKit job, FE-3's JR-02 (ar, webkit-desktop) - axe `color-contrast` on the account menu; 147 passed, 1 failed; every other job green |
| 9017ad4 (fix 1) | ci.yml | [43576090917](https://github.com/mohamedshhahat1/wasla/actions/runs/43576090917) | **success**, every job |
| 9017ad4 | frontend.yml | [43576090907](https://github.com/mohamedshhahat1/wasla/actions/runs/43576090907) | **failure**: WebKit job, FE-2's J3 (ar) - the invitee verified with a "code" the API refused; 147 passed, 1 failed; JR-02 green |
| 6ff1c81 (fix 2) | frontend.yml | [43581964826](https://github.com/mohamedshhahat1/wasla/actions/runs/43581964826) | **success**, all 8 jobs (Chromium 154, Firefox 148, WebKit 150 passed; visual roles; Storybook; drift; install) |
| 6ff1c81 | ci.yml | [43581964818](https://github.com/mohamedshhahat1/wasla/actions/runs/43581964818) | **success**, every job; tests 8155 passed / 17 skipped / 2 deselected = 8120 + this stage's 35 (the 34 above + the invitation-mail unit test) |

### The two fixes (section 25), made at the owner's request ("is you can fix the failure while the ci runs?")

1. **9017ad4 fix(e2e): FE-3's axe scans wait for finite animations to end.** The account menu is a popover that fades in
   (`wasla-pop-in`); FE-3's `axe()` helper waited for the stream and the network, not for the animation, and on the
   runner WebKit measured the text half transparent. The helper now waits until no finite animation runs (a spinner,
   infinite, is not waited for). Not this stage's code path: the shell components, `packages/ui` and the app routes are
   unchanged since a165fe3; JR-02 ar passed 3 / 3 in local WebKit before the fix. After: JR-02 / 03 / 05 / 06 / 07 / 07b in
   Chromium and WebKit, en and ar, 24 / 24 locally (one local Playwright worker crash, 0xC0000409, re-run alone green).
   `fe3-shell.spec.ts` is outside the stage's list; changed at the owner's request, under section 25's rule for runner
   timing in a spec.
2. **6ff1c81 fix(e2e): verification codes are read from verification mails only (FEB-32).** The invitee's trace shows no
   verification send before the refused verify: the "code" came from the invitation mail, whose random token or slug held
   six digits - the mailbox lists every six-digit run of any mail. `waitForCode` / `newestCode` and `code_mails` /
   `code_after` now read only mails with the verification subject (Python takes it from `subject_for`); a unit test
   holds an invitation with six digits out. This corrects section 5's ML-02 judgement that the TypeScript helpers were not
   affected. Locally: smoke 11 / 11; FE-2 J3 and J1 in Chromium and WebKit, en and ar, green (J1 en once more on a fresh
   stack after the stack's own workspace-creation limit, 429, on the first run). The e2e image was not rebuilt: its seed
   reads only a registration mail, which the subject filter does not change.

Each push waited for the running ci.yml to finish (both workflows cancel an in-progress run on a new push), so every
commit's ci.yml ran to the end. No run was re-run or cancelled by hand.

### Verdict

**BACKEND STAGE 1 MERGED AND PUSHED, CI GREEN** - ci.yml and frontend.yml green in every job on 6ff1c81, the canonical head after the two section-25 fixes. FEB-03, FEB-04, FEB-05, FEF-24, FEB-32, FEB-33: CLOSED.

NEXT: FE-4 (THE REALTIME INBOX) CAN START; the WhatsApp Embedded Signup backend stage remains scheduled (FEQ-14).
