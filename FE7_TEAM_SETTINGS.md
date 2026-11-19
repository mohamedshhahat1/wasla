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
