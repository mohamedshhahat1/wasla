# FE-7 — Team and settings (ADR-201)

Verdict at the time of this commit: **merged locally; pushed at the owner's instruction before the full local runs
finished** (see "The owner's push-now decision"). GitHub's `frontend.yml` and `ci.yml` on the pushed head decide the
final verdict; the addendum records them.

## Phase status

| Phase | State | Evidence / commit |
| --- | --- | --- |
| A0 state, skills, untracked, infrastructure | done | start c30d35f = origin; skills' hashes equal FE-6's |
| A-BE DOCKER (DK-01..04) | done; DK-05 measured rebuild NOT done | 6616000 |
| A-BE migration 0141 | done | 8816899 |
| A-BE LANGUAGE (FEB-09) | done | d34b480 |
| A-BE LAST-USED (FEB-36) | done | 1fd1298 |
| A-BE LOGS (FEB-41) | done | 3ec2e44 |
| A-BE CODES (FEB-06) | done (9 codes) | e9eafee |
| A-BE seed helpers, contract, docs | done → **BE_HEAD 06822d1** | 06822d1 |
| A-FE composites, relays, six screens, i18n, checklist | done | 4ea7147 .. 84a403b |
| FEF-45 | fixed | 9932aff |
| FEF-43 | cause found by reading, fixed | 10bf99d |
| Journeys, keyboard, sweep, visual roles | done in Chromium | e2e commits, 129828e = **FE7_HEAD** |
| FULL-B (all of ci.yml locally) | partial: lint/types, migrations job, alert rules green; the 12 test shards still running at push time | notes |
| FULL-F (all of frontend.yml locally) | not run (owner: push now) | — |
| Part B merge | done, 54937c8, tree = FE7_HEAD | this commit's parent |
| Push, CI | at the owner's instruction (2026-11-19 23:29Z) | addendum |
| Part C previews, Part D cleanup | after CI | addendum |

## Source heads

| Name | Commit |
| --- | --- |
| ORIGIN_CANONICAL_START_HEAD | c30d35f |
| BE_HEAD | 06822d1 |
| FE7_HEAD | 129828e |
| CANONICAL_MERGED_HEAD | 54937c8 (parents c30d35f, 129828e) |
| main | 8e3a1ba (untouched) |
| Alembic | 0140 → 0141 |
| OpenAPI | 266 → 267 operations (+ PATCH /auth/me) |

## What the owner can now do (en, with ar / fr / tr drafts)

- **Team (`/team`)**: every member reads who is in the workspace. Owners and administrators remove people exactly as
  the API allows (an administrator never sees Remove on an owner; nobody on the last owner) and reinstate people who
  left (seat-limited, said plainly). Anyone can leave; the last owner is told to hand over first. The owner hands over
  ownership and becomes an administrator at once. Controls a role may not use are absent, not disabled.
- **Invitations (`/team/invitations`)**: invite with a role and the e-mail's language; revoke; expired ones in words.
- **Workspace settings, activity, close**: name, address (owner), time zone with the local time now, the automation
  disclosure (only changed fields are sent); the activity log in words (an unknown action as its code, as text); closing
  only with the address typed exactly, the page saying what the code does (records kept, the address never released,
  nobody's account closed) — not the plan's "30 days", which the code does not do.
- **Account (`/settings/account`)**: language (written to the account and the device), change or set a password,
  connect / disconnect Google, sign out everywhere, close the account with a password or with Google's
  re-authentication — and the owned workspaces to hand over first, each linked.
- **FEB-09**: every e-mail to a user is in their language; an invitation in the inviter's choice; English unchanged byte
  for byte (16 templates); ar / fr / tr marked as unreviewed drafts (FEQ-08).
- **FEB-36**: a sign-in without a workspace opens the last-used active one, else the oldest active, never a suspended one.
- **FEB-41**: every service of `docker-compose.prod.yml` keeps its logs bounded (json-file 10m × 5), with a test.
- **Dockerfile**: the locked dependencies install before the code is copied, so a code change reuses that layer; the
  packages, flags and hash pinning are unchanged (DK-T tests pin the order). The measured before/after rebuild (DK-05)
  is not done yet (C: had 5.6 GB free).

## Proofs run on this machine

| Gate | Result |
| --- | --- |
| GB7-3 ruff / black / mypy (ci.yml's commands) | clean at BE_HEAD (1174 files / 1015 files) |
| GB7-1/2 focused backend tests | 298 passed (8 files) at BE_HEAD; earlier neighbour run 679 passed |
| GB7-5 migrations | upgrade → downgrade base → upgrade, db_preflight verify ok, `alembic check` no drift, head 0141 |
| alert rules job | config ok, 91 rules, rule tests and amtool pass |
| FG7-3 unit tests | web 634, ui 145, session 91, i18n 20, api-client 51, config 53 — all pass; typecheck, lint, format, i18n:check (1319 keys × 4) clean |
| e2e image at BE_HEAD | f66c2917c804, 1.17 GB, built in 4 min |
| Smoke J1–J11 | passed (J6 expected as before) |
| FE-7 journeys, Chromium desktop + phone | 12 / 12: J3 through the screens (revoked member told at once, never /verify), ownership transfer, account closed with the fake Google's re-authentication, owner refused with `account_owns_workspaces`, settings / activity / close, last-used workspace, Arabic account → Arabic e-mail |
| Keyboard-only journey (extended) | 6 / 6 Chromium |
| Focus sweep (+ six FE-7 screens) | 6 passed, 0 hidden stops |
| FE-3 shell, FE-2 session + auth, FE-5 customers | 41, 40, 18 — all pass (one local "offline" web-server hiccup on J1, rerun green) |
| Visual roles, six FE-7 screens, en / ar × 1280 / 390 | inside the hard limits |
| FEF-43 loop | CJ-10 × 30 in WebKit: the withdrawn member never landed on /verify; 1 red in the suspended half from the same local "offline" hiccup |

Not run locally: the 12 backend test shards (running when the owner chose to push), the whole frontend.yml in
Firefox and WebKit, Storybook's runs, the Workers build, the mutation check.

## The owner's push-now decision

2026-11-19 23:29Z, after the explanation that GitHub runs the 12 shards and 4 browser jobs in parallel (about
20–25 minutes) while this machine runs them three lanes deep (3–4 hours): **"Push now."** This sets aside, for this
push, the stage's rule of running every test locally first. The local backend lanes keep running as a cross-check.

## Fixes found while testing

- FEF-45: a wrong current password (401 `current_password_invalid`) signed the person out of every tab — now answered on
  the field.
- FEF-43: a stale `/auth/me` answer was filed under the newer session state and read as "unverified" → /verify; now an
  answer is kept only for the state it was asked for.
- FE-2's JR-07 privacy proof caught the language check writing to sessionStorage — moved to memory.
- The close-workspace button stays secondary until the address matches (the status share was 3.3 % > 3 % at 390).
- First local FULL-B start was void (my lane script left `TEST_REDIS_URL` unset); stopped, fixed, restarted.

## Findings

| ID | Finding | Status |
| --- | --- | --- |
| FEB-42 | No route changes a member's role (member ↔ administrator) | NOT BUILT (OQ-3 default) |
| FEB-43 | ci.yml and the e2e image install the dev tools without hashes | NOT CLOSED (recorded) |
| FEB-44 | Invitations do not say who sent them or when | NOT CLOSED |
| FEB-45 | `/auth/me` does not say whether the account has a password or Google | NOT CLOSED (the page offers both modes) |
| FEF-43 | Withdrawn member sent to /verify | fixed; pre-fix loop not run |
| FEF-45 | Wrong current password signed the person out | CLOSED |

Plan-vs-code notes: "retention 30 days" superseded by the code's tombstone; `GET workspace/local-time` resolves a wall
time in the saved zone, so "now" in a chosen zone is computed with Intl; a workspace rename is not audited by design.

## Decisions applied

OQ-1 .. OQ-7 by their defaults (e-mails in four languages; the language written once from the one in use; no role
change; no FEB-17; one shared log bound; the load-sensitive test left alone unless red; the Dockerfile's order only).
FEB-11 not built. Nothing of the Tend restyle. ADR-201 in DECISIONS.md.

## Remaining external actions

- Free Docker's disk by hand: `docker builder prune -f --filter until=168h` (or `-a -f`), and look at the unused volumes.
- Review the ar / fr / tr screen texts and e-mails (FEQ-08).
- Decide FEB-42 .. FEB-45; the next stage (FE-8, the Embedded Signup backend stage, or FEB-11).

## Addendum 1 — GitHub runs, the two CI fixes, and the previews

**Verdict: FE-7 MERGED AND PUSHED, CI GREEN. PREVIEWS UPDATED.**

| Push | Head | ci.yml | frontend.yml |
| --- | --- | --- | --- |
| 1 (merge 54937c8 + report) | 648b996 | 44638302896 — green, all 19 jobs, 13.4 min | 44638302967 — red: Firefox phone 3 (FE-2 J6, FE-5 CJ-05..08, FE-6 AJ-07; all green locally in Firefox phone), WebKit 6 (TJ-03, TJ-06 ×2, KY-02 en sent to /verify mid-navigation; KY-03 phone ×2, the save bar) |
| 2 (merge of 3ac448d, 8c02bfb) | 7120744 | 44648984526 — green | 44648984555 — red: Chromium phone JR-04 (network never idle), Firefox desktop AJ-02/03 (ar) and AJ-07 (green 6/6 locally), WebKit phone KY-03 en (the save bar) |
| 3 (merge of f1ce4bb) | 8f2996b | 44654262285 — green | 44654262240 — **green, all 8 jobs** |

Fixes, each its own commit on fe7-team-settings-20261119:

- **3ac448d** (FEF-43, the broader half): WebKit aborts a page's fetches as it navigates and rejects them as
  `network_error`; the session filed that as "no profile", read as an unverified account → /verify mid-navigation. A
  failure that never reached the API is no longer filed. With 10bf99d (stale answers dropped), **FEF-43 CLOSED**: the
  /verify jumps are gone in every project of the last two runs.
- **8c02bfb, f1ce4bb** (KY7-03): the workspace save bar covered the focused field on a WebKit phone; it is sticky only
  from the sm width now.

The JR-04 / AJ-* reds of push 2 did not reproduce locally and were green in push 3 — recorded as runner-only
intermittents in earlier stages' specs (no change made for them). The owner chose to push before the local full runs
finished (23:29Z) and later to stop the local backend lanes; FULL-B's 12 shards and FULL-F were therefore not completed
on this machine, and GitHub's runs above are the full check.

### Part C — previews

Account "Mohamedshhahat1@gmail.com's Account", 1cc89863… (= FE-6's). Sources since FE-6's deploy (5267367): web 45 app
files; console and marketing no app file but `@wasla/ui` and `@wasla/i18n` changed → all three rebuilt with
`build-workers-linux.mjs --env preview` (three workers.dev origins, `PUBLIC_PREVIEW_NO_API=true`; the first attempt lacked
the origins, the second lost Docker's container connection, the third built in 5 min). `workers:smoke`: every page 200,
session routes GET 405 / POST 503, console 404 for an unknown page. Every wrangler.jsonc unchanged; nothing on
iwasla.tech; no secret; no account setting.

| Worker | Previous (FE-6) | New version | Upload (gzip) |
| --- | --- | --- | --- |
| wasla-preview-web | e9d20bca | d1a2c7f6 | 9293.35 KiB (1823.84 KiB) |
| wasla-preview-console | d0520a0d | f3bd3605 | 6668.69 KiB (1409.55 KiB) |
| wasla-preview-marketing | 2d4d5535 | 91da1e05 | 0.35 KiB (0.24 KiB) |

Live checks: web `/`, `/login`, `/team`, `/settings/account`, `/settings/workspace`, console `/`, marketing `/ar/`
`/en/` → 200; HSTS, CSP, referrer and permissions policy, nosniff present; `/session/login`, `/session/refresh` and the new
`/session/google/link/*`, `/session/google/reauth/*`, `/session/account/close` alike: GET 405, cross-origin POST 403,
same-origin POST 403 `session_request_refused`.

### Still open

DK-05 (the measured code-only rebuild) not done; FEB-42 .. FEB-45; FEQ-08 (ar / fr / tr texts and e-mails); the owner's
Docker cache prune. This docs-only commit does not start frontend.yml (its path filter): the last frontend.yml on the
code, 44654262240, decides the verdict.
