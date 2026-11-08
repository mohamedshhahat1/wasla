# FE-2 — Authentication, Session and Workspace (S-2 Session Routes, Sign-in Screens, Workspace Switch, FE-1 Leftovers)

## Phase status

| Phase | State | Evidence (E:\wasla-fe2-notes\) | Time (UTC) | Made |
|---|---|---|---|---|
| A0 state, untracked, infrastructure | done | evidence/untracked_before.txt, logs/image_base_rebuild.log | 2026-11-07/08 | worktree `E:\wasla-fe2-fix`, branch `fe2-auth-session-20261107` from 31d8be6; images wasla-fe2:base, :base-b |
| A1 FE-1 leftovers (FX-01, -03, -04, -05, -08) | done | logs/fx01_*, fx08_*, evidence/audit_* | 2026-11-07/08 | b6d20a4, 73f126d |
| A2 session route handlers | done | apps/web/test/session-handlers.test.ts | 2026-11-08 | 8765782 |
| A3 browser session | done | packages/session tests | 2026-11-08 | 758fd91, 7b29c5b |
| A4 screens, workspace, i18n | done | apps/web tests | 2026-11-08 | 29b51c8, 4980dad, 3acdaa4 |
| A5 FX-07 stream helper | done | logs/e2e_stream1.log | 2026-11-08 | dbcfbf6 |
| A6 journeys and proofs | done | logs/gates_a3/g5_e2e.log | 2026-11-08 | 7291da4, 8b219aa, 129a0eb |
| A7 frontend.yml | done | logs/action_validator.log | 2026-11-08 | fa6e05e |
| A8 mutation, docs, ADR-193 | done | mutation/results*.json | 2026-11-08 | 191edae |
| A9 FX-06 re-opened and closed; gates at FE2_HEAD | done | logs/fx06_*, logs/gates_a3 | 2026-11-08 05:00 | 1e20410 = FE2_HEAD |
| B verify worktree, G-7, merge | done | logs/gates_b2a, gates_b2b, merge.log | 2026-11-08 05:40 | worktree `E:\wasla-fe2-verify`; merge 6bdd405 |
| B report, push, workflows | see section 19 / 20 | | | report commit; push |
| C previews | see section 21 (addendum) | | | |
| D cleanup | see section 25 | | | |

## 1. Executive Summary

FE-2 is built and merged into `worktree-billing-google-auth`. On localhost against FE-0's e2e profile a person can sign up, read the code from the mailbox, verify, create a workspace, sign out, sign in with a password or with the fake Google, reset a forgotten password, accept an invitation and switch workspaces, in English and Arabic, at 1280 and 390, in Chromium, Firefox and WebKit. `frontend/README.md` ("Signing in") has the local walk.

- The refresh token exists only in an HttpOnly, SameSite=Strict, host-only cookie (`__Host-wasla_rt`; `wasla_rt` on plain-HTTP localhost) that only the eight `/session/*` route handlers read; they refuse every cross-origin, header-less or non-POST request before any API call, and sign every API call with the browser's own address (FEB-01's frontend half).
- J6: two tabs refreshing together 50 times — one refresh per round, nobody signed out, in all six projects; the same race without the Web Lock signs the account out (the test can fail). A third tab joins by broadcast without refreshing.
- How "fix the other things" was read: FE-1's open findings FEF-04, -05, -06, -07, -08, -11 and PV-03, plus FEF-02 / -03 re-checked (section 5). FEF-08 was first believed closed (warm runs never showed it) and was then reproduced on a cold start in Part B; its fix (and a second cold-start race it uncovered, FEF-15) is commit 1e20410, after which every Part B gate was run again.
- The owner should know first: **FEB-31** (the API's CORS hides `Retry-After`, so the verification screen shows "too many requests" without a countdown), **FEB-30** (the refresh lifetime is not in the session answer; the cookie uses the configured 14 days), **FEF-13** (Playwright's Node worker crashed natively three times in ~1300 local test runs on Windows — never an assertion), **FEF-12** (axe's two label rules crash Playwright's Firefox; skipped there), **FEF-06's licence policy** is the owner's to accept (section 5).
- Previews (Part C) and the GitHub runs: the addendum.

## 2. Source Branches and Exact Heads

| Name | Commit |
|---|---|
| FE1_CI_GREEN_HEAD = ORIGIN_CANONICAL_START_HEAD | 31d8be6bff893a84634b98ca429bea1c5a960f09 |
| FE2_HEAD (fix branch `fe2-auth-session-20261107`) | 1e204103c37a25ef5ceabb68118bb6f467ea5dbb |
| CANONICAL_MERGED_HEAD | 6bdd40533335cd07634fdeee0e2e3379e852b9f7 (parents 31d8be6, 1e20410) |
| main | 8e3a1ba (untouched) |
| Alembic head | 0138 (unchanged) |
| LAST_ADR before / this stage | ADR-192 / ADR-193 |
| OPENAPI_SHA256 | 57bed53ca5ba92560eb8ae90d3059f5b05fbc7b1235345c4fac4178f2e60505d (unchanged) |

## 3. Environment

| Item | Value |
|---|---|
| Node / pnpm / Turborepo | 24.15.0 / 12.18.0 (corepack shim in E:\wasla-fe2-notes\bin) / 2.11.10 |
| Browsers | Playwright 1.65.0's Chromium, Firefox, WebKit |
| Docker | 29.8.0 (Docker Desktop; its engine stopped once when C: filled, restarted by the owner) |
| wrangler | 4.158.0 (Part C: the addendum) |
| Linux build image (FX-01) | node:24.15.0-bookworm-slim@sha256:4e6b70dd6cbfc88c8157ba19aa3d9f9cce6ba4703576d55459e45efcbc9c5f5d |
| Added (apps/web) | react-hook-form 7.91.0 (MIT), zod 4.6.7 (MIT), @hookform/resolvers 5.9.2 (MIT, modified 2026-11-05 — the latest 5.x on the day), lucide-react 1.70.0 (already a ui dependency); e2e: @wasla/i18n (workspace) |
| Bumped | none: next 16.4.5, @opennextjs/cloudflare 1.20.16 and wrangler 4.158.0 were already the latest patches (FX-03) |
| Overrides | glob and minimatch to their fixed releases (FX-04); sharp not installed (FX-05) |
| pnpm audit (prod / all) | before 1 moderate / 1 high + moderates; after **0 / 0 at every severity** |

## 4. Decisions Applied

| ID | Applied |
|---|---|
| FX-01 | `frontend/scripts/build-workers-linux.mjs` (`pnpm build:workers:linux`): digest-pinned Node 24 Debian, read-only mount, frozen install inside, outputs copied back with links dereferenced (Windows cannot stat Linux links); `wrangler deploy --dry-run` on Windows bundles the copied web output (6948 KiB, 1437 KiB gzip, ASSETS only). The cheaper Windows paths (OpenNext latest patch — already latest; node-linker=hoisted throwaway copy, spikes/hoisted/build.log) did not remove "Access is denied" |
| FX-02 | every cookie assertion reads the Set-Cookie header of the handler's answer; the jar's sameSite asserted except Windows WebKit; a cross-site form POST carries no cookie in three browsers |
| FX-03 | nothing to bump (already latest) |
| FX-04 | pnpm overrides for glob / minimatch; audit 0 everywhere |
| FX-05 | sharp not installed (override "-"); `scripts/check-licences.mjs` in frontend.yml; policy proposal in section 5 |
| FX-06 | vitest worker threads, maxWorkers 2 in all eight configs; `@wasla/marketing#build` after its typecheck |
| FX-07 | `@wasla/api-client/stream`; restart proof in e2e |
| FX-08 | `agentRules: false` in both Next configs (no ignore line needed) |
| FX-09 | DS-07 re-run green; FEF-03 0 failures in 20 repeats |
| SR-01..09 | as ADR-193 item 1–4; deviations: `__Host-wasla_oauth` keeps Path=/ (the prefix requires it; the plain-HTTP name is scoped to /session/google/); the 405 / CSRF envelope codes are frontend codes; the e2e address header is honoured when WASLA_E2E_ROUTES=1 on a development build (the prompt said NODE_ENV=test; the e2e build is a development build) |
| SB-01..03 | as ADR-193 item 5, plus the `token-request` hand-off (JT-03's third tab) |
| AU-01..04 | as ADR-193 item 6; one client rule beyond shape: none (the password policy is the server's); routing in the browser only |
| WK-01..03 | FEQ-16's default: one active workspace per browser, the other tabs follow with a notice |
| JT-01..08 | as section 10; J6 holds each refresh 250 ms and signs each under a new address (section 10) |
| PC-01..05 | the addendum |
| ID mapping | the prompt's M-S01..M-S10 → **M-X01..M-X10** (M-S01..M-S40 exist in earlier reports); FEB-30 free (FE-1 wrote only "none"); all other new prefixes unused |

**Decisions NOT taken** (each with its owner): the app shell, gates, home, channels (FE-3, after FEB-07); any screen opening the stream (FE-3 / FE-4); FE-4..FE-8's screens; the console's sign-in (FE-9); FEB-14, FEB-16, FEB-17, FEB-07, FEB-20, FEB-21's backend side, FEB-30, FEB-31 (backend stages); staging API, full S-2 walk on a Worker, custom domains, DNS, Access, production Workers, a deploy workflow (first staging deploy / FE-11); real Google (FE-11); account locale (FE-7); FEF-06's policy (the owner); dark theme, visual baseline, PWA, error reporting (the owner); `paths-ignore` (audit 24); nightly / real-clock / Linux-local lanes (the owner's light gates); deleting E:\wasla-fe1-fix and E:\wasla-fe1-notes (the user).

## 5. FE-1's Leftovers

| Finding | Before | Done | After | Status |
|---|---|---|---|---|
| FEF-04 OpenNext build on Windows | fails ("Access is denied") | FX-01 Linux build script + host deploy | a Windows machine builds (Docker) and wrangler bundles the output; G-8: web 3811 files / 81 492 388 B, console 3686 / 78 405 806 B, marketing 29 / 548 291 B; worker.js sha256 d05223bf… | CLOSED (CI entry hash: addendum) |
| FEF-05 WebKit cookie attributes on Windows | not asserted | FX-02 | attributes from the header in every browser; Strict's behaviour (cross-site POST without cookie) in three browsers; Windows WebKit's jar reports sameSite None (recorded) | CLOSED |
| FEF-06 transitive licences | LGPL libvips via sharp | sharp removed; direct check enforced | 108 direct dependencies, 0 outside FE-1's list; 15 transitive licence kinds printed | sharp CLOSED; policy DEFERRED PRODUCT DECISION |
| FEF-07 advisories | 1 high (glob, dev) + moderates | overrides | 0 at every severity, prod and all | CLOSED |
| FEF-08 vitest worker flake | one failure seen | reproduced cold; threads + maxWorkers 2 | 10 cold runs and 15 warm forced runs green, 0 worker failures | CLOSED |
| FEF-11 next dev writes AGENTS.md | appears | agentRules: false | absent; the control without it writes it | CLOSED |
| PV-03 EventSource ends CLOSED | spent ticket | FX-07 helper | after docker restart: new ticket, the next message.created arrives (timeline section 11) | CLOSED (EXT-FE-03's Cloudflare half stays EXTERNAL VERIFICATION) |
| FEF-02 | synthetic keys | re-checked | unchanged; device check | ACCEPTED RESIDUAL (EXT-FE-06 / 13) |
| FEF-03 DropdownMenu under load | per-step waits | 20 repeats × 4 workers | 160/160 passed (0/20 failed), twice | ACCEPTED RESIDUAL |

**FEF-06 proposal (not a decision).** Allowed for build-only transitive tooling: MIT, ISC, Apache-2.0, BSD-*, 0BSD, BlueOak-1.0.0, CC0-1.0, CC-BY-4.0 (data only), Python-2.0, MPL-2.0, OFL-1.1. Refused for anything shipped to browsers: every licence outside MIT / ISC / Apache-2.0 / BSD-* / 0BSD / OFL-1.1. `scripts/check-licences.mjs` enforces the direct rule now and prints the transitive kinds; the transitive rule is enforced only after the owner accepts it.

## 6. The Session Routes

| Route | Checks (SR-02, in order) | API call | Cookie | Body |
|---|---|---|---|---|
| login | 405 / Origin exact / Sec-Fetch-Site same-origin when sent / X-Wasla-Session: 1 / JSON / 16 KiB | POST /auth/login (email, password, workspace_slug) | set | access_token, expires_in, active_workspace |
| register-login | same | /auth/register (202) then /auth/login | set; none for a taken address | as login, or {status: check_inbox} |
| refresh | same | /auth/refresh with the cookie's token and workspace_slug; no cookie → 401 without a call | rotated; cleared on 401/403 | as login |
| switch | same | /auth/workspace with the bearer | unchanged (no refresh token in AccessTokenResponse) | as login |
| logout | same | /auth/logout with the cookie's token | cleared whatever the API says | {} |
| logout-all | same | /auth/logout-all with the bearer | cleared | {} |
| google/authorize | same | /auth/google/authorize | the API's binding cookie relayed under its name (wasla_oauth over HTTP, scoped /session/google/; __Host-wasla_oauth over HTTPS, Path=/) | {authorization_url} |
| google/callback | same | /auth/google/callback with code, state and the relayed binding | set; binding cleared | as login |

Every answer: `Cache-Control: no-store`, `Vary: Origin`, no CORS header, security headers from proxy.ts (matcher covers /session/*). Missing secret → 503 session_unavailable (one log line, no value); API slower than 10 s → 504 upstream_timeout; API errors by code, request id, Retry-After. Client address: CF-Connecting-IP outside development; the socket / forwarded address or 127.0.0.1 in development; the test header only with WASLA_E2E_ROUTES=1 on a development build (tested both ways). Tests: 85 handler tests against a fake API (every route × 6 refusals, GET 405, 413, missing secret, per-route paths, bodies, cookies, the signature against FE-0's algorithm, timeout, the API origin only) + the package helpers' tests; SR-01's lint rule (`next/headers` cookies and Set-Cookie only in the session routes and the locale route) with its fixture.

## 7. The Browser Session

`apps/web/lib/session/web-session.ts` on FE-1's TokenHolder, RefreshCoordinator (Web Lock + marker) and SessionChannel: one refresh per browser with the current workspace_slug; the token to every tab; refresh at 80 %; a 401 → one refresh and one retry; on load a tab asks the others for a valid token (`token-request`) and refreshes only when none answers in 300 ms; network failures back off 1, 2, 4 … 30 s (jittered, injected clock) while the token is valid, then "connection lost" with a retry — never a sign-out; sign-out clears every tab's token and query cache and routes to /login; a switch bumps the generation. The profile (/auth/me) is keyed to the session state. Unit tests: session 77 (FE-1 28 + backoff, hand-off, route helpers), web 121.

## 8. The Screens

| Screen | Path | States and codes |
|---|---|---|
| APP-AUTH-01 sign up | /signup | name, email, password, workspace name and address, time zone (GET /timezones, device default); taken address → "check your inbox"; 429 with seconds; field errors by the API's 422 |
| APP-AUTH-02 log in | /login | one neutral sentence for invalid_credentials and an unknown account; ?workspace=; unverified → /verify |
| APP-AUTH-03 Google | /login button, /auth/google/callback | behind PUBLIC_GOOGLE_ENABLED; cancelled → /login with a note; missing binding → refused by code |
| APP-AUTH-04 verify | /verify | 6-digit code (one-time-code, numeric); wrong / expired by code; resend to the API's limit (3 per 15 min); countdown when Retry-After is readable, else the rate-limit sentence (FEB-31) |
| APP-AUTH-05 forgot / reset | /forgot, /reset-password | always "if an account exists…"; token removed first; Referrer-Policy: no-referrer |
| APP-AUTH-06 invitation | /invitations/accept | token removed first; name + password for a new account; then /verify (FEB-17) |
| APP-WS-01 create | /workspaces/new | slug suggested, taken / limit by code |
| APP-WS-02 choose / switch | /workspaces and the switcher | name and role; switching every tab |
| placeholder | / | who, which workspace, role, switcher, sign out, sign out everywhere (ConfirmDialog), language |

Every error is the catalogue sentence for its code with the request id (tested for every code in errors.json, en and ar); autocomplete right; show/hide password; Enter submits; keyboard-only log-in proved in en and ar. `?next=` is honoured only for a same-origin path (16 refused forms tested).

## 9. Localization

New namespace `auth` (114 lines per locale) and frontend codes session_request_refused, session_unavailable, upstream_timeout, check_inbox, connection_lost. i18n:check: 170 keys × 4 locales (FE-1: 78), plurals and CLDR checks green. en is the reviewed source; ar (Modern Standard Arabic), fr, tr are drafts (`_meta.reviewed: false`, FEQ-08). Addresses, slugs and codes inside Arabic text are in `<bdi>`.

## 10. Journeys, CSRF, Cookies, Storage, Origins, Rate Limit, axe

Gated run at FE2_HEAD (fix worktree, `pnpm e2e:local`, 740 s): **312 passed, 25 skipped, 0 failed**.

| Project | passed | skipped |
|---|---|---|
| chromium-desktop | 56 | 0 |
| chromium-phone / firefox-desktop / firefox-phone / webkit-desktop / webkit-phone | 51 each | 5 each (one-project tests) |
| stream-restart | 1 | 0 |

| Journey | Result (every project, en and ar) |
|---|---|
| J1 sign up → wrong code → resend → verify → placeholder → second workspace | passed |
| J6 two tabs × 50 rounds | 50 rounds, 50 refreshes, 0 sign-outs in each of the six projects; third tab joined with 0 refreshes |
| J6 control (no lock) | signed out in every project; a locked refresh cannot recover; cookie gone |
| J7 switch | the other tab follows with the notice; the old workspace's invitation DELETE with the new token → 404 |
| J3 invitee | accepts, verifies, member of the inviter's workspace |
| J9 fake Google | binding cookie `wasla_oauth` relayed and cleared (all six projects); code and state gone from URL and history; lands on /verify (an example.com address is not one Google is authoritative for); cancelled and no-binding cases refused by code |
| log in / out, taken address, forgot → reset | passed |

J6's two test decisions: each /session/refresh answer is held 250 ms so the second tab always calls during the first's refresh (without it, Playwright's two dispatches sometimes did not overlap and the second refresh was legitimate); each refresh is signed under a new test address because the API counts refreshes with sign-ins, 10 a minute per address. FE-1's SS-02 test got the same 250 ms hold (it failed once in Firefox for the same reason; 30/30 after).

CSRF (one project, direct requests): 8 routes × {cross-origin, no X-Wasla-Session, Sec-Fetch-Site cross-site, form body} → 403 session_request_refused, GET → 405 Allow: POST (40 rows); a real login from the same address afterwards is still answered (401), so none of the 32 refused POSTs reached the API's 10/min bucket. Cross-site form POST from http://127.0.0.1:3998 to /session/refresh: no wasla_rt in its Cookie header, 403, still signed in — three browsers.

Cookie (FX-02): from the header — `wasla_rt`, HttpOnly, SameSite=Strict, Path=/, Max-Age=1209600, no Domain, no Secure (plain-HTTP development), no-store, no CORS header; in every browser from the login response header; jar httpOnly everywhere, sameSite Strict in Chromium and Firefox, "None" recorded in Windows WebKit (FEF-05).

Storage and origins (JT-06 / JT-07), after every step: no refresh cookie in document.cookie, local/session storage empty, no IndexedDB database, no token-like string in URL or history.state; requests only to http://localhost:3000 and http://localhost:8000 (plus http://localhost:8026 in J9); 0 CSP violations (the zod JIT probe was found and fixed).

Rate limit (SR-06): 11 logins through /session/login from one signed address → 10 × 401 then 429 with Retry-After; another address → 401.

axe (JT-08): 16 screen states × en/ar × six projects — 480 scans, **0 serious / critical**; moderate only: page-has-heading-one (64). FEF-12: Firefox skips `label` and `label-title-only`.

## 11. The Stream Helper (FX-07)

Rule: never rely on EventSource's own reconnect. Terminal `closed` reasons (membership_revoked, workspace_unavailable, workspace_switched) end the stream; any other reason, an error or a CLOSED source → close it, new ticket, new EventSource, backoff 1, 2, 4 … 30 s; a 401 on the ticket → one refresh, one retry. The table is asserted against event-types.json. Unit tests with a fake EventSource and fetch. Restart proof against the profile (stream-restart project): `connecting → open → message.created → closed → waiting → connecting → waiting → connecting → waiting → connecting → open → message.created → follow_up.updated` — a new ticket after `docker restart wasla-fe-e2e-api`, the next frame arrived.

## 12. frontend.yml

Checks job: the licence check; the Workers' entry hashes printed for FX-01's comparison. E2E job unchanged in shape: `pnpm e2e:local` now builds apps/web with PUBLIC_GOOGLE_ENABLED and starts it with the e2e stack's public secret and WASLA_E2E_ROUTES from the committed Playwright config; no GitHub secret, nothing deployed. action-validator 0.6.0: VALID. The local e2e run took 11–12 minutes, inside FE-1's 40-minute job limit.

## 13. Mutation Check (M-X01..M-X10)

| Mutant | Killed by |
|---|---|
| M-X01 no Origin check | session-route + handler unit tests |
| M-X02 cookie without HttpOnly | session-route + handler unit tests |
| M-X03 SameSite=Lax | unit tests |
| M-X04 refresh without workspace_slug | handler test |
| M-X05 no forward signature | unit tests; SR-06's rate-limit journey (mutant built into apps/web) |
| M-X06 refresh without the Web Lock | session unit tests; J6 |
| M-X07 relay drops the binding cookie | handler test |
| M-X08 reset leaves the token in the URL | screens test |
| M-X09 ?next= accepts //evil.example | routing test |
| M-X10 the helper relies on EventSource's reconnect | stream unit tests; the restart journey |

Control green; every mutant killed, none hung; `git diff --exit-code` clean after each. Re-run at FE2_HEAD (threads pool): every mutant killed.

## 14. Documentation and ADR Changes

ADR-193 (DECISIONS.md); docs/SECURITY.md "Sign-in through the session routes"; docs/DEPLOYMENT.md "Frontend" (SESSION_FORWARD_SECRET as a Worker secret, PUBLIC_GOOGLE_ENABLED, how previews are built and deployed); frontend/README.md (the Workers on Windows, the e2e environment, signing in, a local walk). `tests/integration/test_documentation_truth.py`: 27 passed (inside wasla-fe-e2e-app:local, --network none, the worktree mounted read-only).

## 15. Part A Commits and the Diff Guard

| Commit | Message |
|---|---|
| b6d20a4 | build(frontend): audit overrides, no sharp, licence check |
| 73f126d | build(frontend): build the Workers on Linux; ignore next dev's AGENTS.md |
| 8765782 | feat(frontend): S-2 session route handlers with CSRF checks and signed API calls |
| 758fd91 | feat(frontend): browser session wired to the session routes |
| 29b51c8 | feat(frontend): sign-up, log-in, Google, verification, reset and invitation screens |
| 4980dad | feat(frontend): create and switch workspace |
| dbcfbf6 | feat(frontend): event-stream helper that takes a new ticket |
| 3acdaa4 | fix(frontend): the screens' test uses ApiError's diagnosticMessage (pipeline after commit 7) |
| 7b29c5b | feat(frontend): a tab that opens takes the token from an open tab (JT-03) |
| 7291da4 | fix(frontend): route on the signed-in profile only; jitless zod; the Google flag through turbo (found by the journeys) |
| 8b219aa | test(frontend): journeys J1, J3, J6, J7, J9; CSRF, cookie and storage proofs |
| fa6e05e | ci(frontend): licence check and the e2e build of the customer app |
| 191edae | docs: authentication, session and workspace (ADR-193) |
| 129a0eb | test(frontend): FE-1's two-tab refresh test holds its routed answer (G-5) |
| 1e20410 | build(frontend): vitest on threads; marketing's build after its typecheck (FX-06, Part B) |

Order differs from the prompt's 1–10 where a later step found a defect (each fix its own commit). Diff guard 31d8be6..1e20410: only frontend/, .github/workflows/frontend.yml, DECISIONS.md, docs/SECURITY.md, docs/DEPLOYMENT.md; openapi.json, event-types.json, fixtures and schema.d.ts unchanged; no artefact folder; no merge; secret scan: only test values and FE-0's public e2e values.

## 16. Gates

| Gate | Fix worktree at FE2_HEAD | Verify worktree at FE2_HEAD (round 2) |
|---|---|---|
| G-1 install / audit / licences | 0 / 0,0 / 0 outside (at 191edae; no dependency change since) | 0 / 0,0 / 108 direct, 0 outside |
| G-2 drift | 0 | 0 |
| G-3 pipeline × 3 forced | 30/30 ×3 | 30/30 ×3 (twice), 0 worker failures |
| G-4 Storybook, axe, RTL | 214 passed, 152 skipped (191edae) | 214 passed, 152 skipped; FEF-03 160/160 |
| G-5 journeys | 312 passed, 0 failed | 310 passed, 1 = Playwright worker native crash (FEF-13) in J7 ar firefox-phone; J7 there ×10 → 10/10, 0 crashes; stream-restart (blocked) → passed |
| G-6 mutation | every mutant killed | not repeated (same tree) |
| G-7 backend image | — | head 623 432 889 B = base = base-b (noise 0) |
| G-8 Workers build | rc 0, entry d05223bf… | rc 0, identical files, bytes and hashes |

Part B round 1 (at 129a0eb) is void: it found FEF-08 open (and FEF-15), fixed in 1e20410; round 1's other differences (a Firefox Popover RTL failure — 80/80 on repeat, FEF-14; one worker crash) are recorded.

## 17. Untracked Files

14 paths in E:\wasla (11 *.md, 3 docs/ui/*.html), SHA-256 identical at the start, before the merge, after the merge and after the report commit (evidence/untracked_*.txt). No incoming path under docs/ui/ or equal to an untracked path. No new untracked path created by this stage in E:\wasla.

## 18. Merge and Tree Equality

`git merge --no-ff 1e20410` in E:\wasla → 6bdd405 (parents 31d8be6, 1e20410), no conflict; `git diff --exit-code 1e20410 HEAD` → 0; Alembic head 0138. On the merged tree (verify worktree re-pointed): G-2 0, G-3 30/30 ×3 (logs/gates_merged).

## 19. Push

See the addendum.

## 20. GitHub Actions

See the addendum.

## 21. Part C — Previews and Worker Proofs

See the addendum.

## 22. Findings

| ID | Finding | Status |
|---|---|---|
| FEB-30 | the refresh token's lifetime is not in SessionResponse; the cookie's Max-Age is the configured 14 days (REFRESH_TOKEN_TTL_SECONDS) | backend stage |
| FEB-31 | CORS expose_headers lists only X-Request-ID (app/main.py:189): a browser cannot read Retry-After on direct API calls, and the 429 body has no seconds → no countdown on verify resend; fix: expose Retry-After | backend stage |
| FEF-12 | Playwright 1.65 Firefox crashes in axe's label / label-title-only rules on a page with a filled input (105 rules probed singly) | ACCEPTED RESIDUAL (skipped in Firefox; the DOM rules run in Chromium and WebKit) |
| FEF-13 | Playwright's Node worker exits 0xC0000409 on Windows, three times in ~1300 test executions, in different tests; never an assertion; not reproducible on repeat (J7 ×10) | ACCEPTED RESIDUAL (CI on Linux is the cross-check) |
| FEF-14 | FE-1's Firefox Popover RTL position case failed once in the verify worktree; 80/80 on repeat | ACCEPTED RESIDUAL |
| FEF-15 | marketing's typecheck and build raced on Vite's optimizer folder on a cold start (EPERM rename on Windows) | CLOSED (1e20410) |
| — | "web server died" twice: both ~600 s into foreground commands the tool moved to the background; no crash or low-memory event; runs started in the background never lost it | operator artefact, not a product defect |

## 23. Decisions Ledger

| Item | Status |
|---|---|
| APP-AUTH-01..06, APP-WS-01, APP-WS-02, placeholder | CLOSED |
| FE-2 card: J1, J6, J9 in en and ar at 1280/390 in three browsers; refresh token unreadable; cross-origin POSTs refused | CLOSED |
| FX-01..FX-09 | CLOSED (FX-03: nothing to bump) |
| SR-01..09, SB-01..03, AU-01..04, WK-01..03, JT-01..08 | CLOSED |
| PC-01..05 | the addendum |
| FEF-04, -05, -07, -08, -11, PV-03 | CLOSED |
| FEF-06 | sharp CLOSED; policy DEFERRED PRODUCT DECISION |
| FEF-02, FEF-03 | ACCEPTED RESIDUAL |
| EXT-FE-01 | routes proved locally; the Worker part: the addendum; the full walk on a Worker: EXTERNAL VERIFICATION |
| EXT-FE-02 | the addendum |
| EXT-FE-03 | browser half CLOSED (FX-07); Cloudflare half EXTERNAL VERIFICATION |
| EXT-FE-06 / 13 | EXTERNAL VERIFICATION |
| EXT-FE-10 | EXTERNAL VERIFICATION (real Google) |
| FEB-14 | worked around (one refresh per browser) — DEFERRED PRODUCT DECISION |
| FEB-16 | worked around (PUBLIC_GOOGLE_ENABLED) |
| FEB-17 | worked around (the invitee verifies after accepting) |
| FEB-21 | worked around (the relay accepts both names) |
| FEB-30, FEB-31 | backend stage |
| FEF-12, -13, -14 | ACCEPTED RESIDUAL; FEF-15 CLOSED |
| Decisions NOT taken (section 4) | each DEFERRED to its owner |

## 24. Infrastructure Created by This Stage

| Item | Kind |
|---|---|
| E:\wasla-fe2-fix (branch fe2-auth-session-20261107), E:\wasla-fe2-verify (detached), E:\wasla-fe2-preview (Part C) | worktrees |
| E:\wasla-fe2-notes | notes (draft, logs, evidence, scripts, mutation, spikes, pnpm shim) |
| wasla-fe2:base, wasla-fe2:base-b, wasla-fe2:head | images |
| wasla-fe2-workerbuild-store | Docker volume (FX-01's pnpm store) |
| node:24.15.0-bookworm-slim (by digest) | pulled image (shared, kept) |
| wasla-fe-e2e-app:local | FE-0's profile image, built by the e2e runs |
| wasla-preview-web / -console / -marketing | Workers (Part C; they stay) |

## 25. Part D — Cleanup (planned)

Only if the verdict is `... CI GREEN` (the addendum's run green too): rescue scripts, logs, evidence, mutation results and the draft to E:\wasla-verification-scripts\fe2\ (SHA-256 verified, never node_modules or a secret); remove wasla-fe2:base, :base-b, :head, the volume wasla-fe2-workerbuild-store, wasla-fe-e2e-app:local (after proving no wasla-fe-e2e-* container); `git worktree remove` E:\wasla-fe2-verify, E:\wasla-fe2-preview, E:\wasla-fe2-fix (the branch stays), `git worktree prune`, then E:\wasla-fe2-notes. Kept: the three preview Workers, every branch, the developer stack, the pnpm store, the Playwright browsers, the Node image, earlier stages' leftovers. Record: E:\wasla-cleanup\CLEANUP_fe2_20261108.md (not committed).

## 26. Remaining External Actions

| Action | Owner |
|---|---|
| open the three preview URLs (addendum); walk sign-up / log-in on localhost (frontend/README.md) | the owner |
| review the new ar, fr, tr texts (FEQ-08); decide FEQ-07 | the owner |
| FEF-06: accept or change the proposed transitive licence policy | the owner |
| free space on C: (Docker's disk; it filled once during this stage and stopped the engine) | the owner |
| delete E:\wasla-fe1-fix, E:\wasla-fe1-notes, and what Part D could not delete | the user |
| iwasla.tech nameservers; no record for app / console / www / api until the first staging deploy | the owner |
| first staging deploy: staging API reachable from Cloudflare, the same SESSION_FORWARD_SECRET on the API and as the app's Worker secret, CORS_ORIGINS with the app's origin; then the full S-2 walk on a Worker (EXT-FE-01) and EventSource through Cloudflare for 30 minutes (EXT-FE-03) | first staging deploy |
| real Google (EXT-FE-10): an OAuth client per environment, then PUBLIC_GOOGLE_ENABLED=true | FE-11 |
| backend: FEB-07 before FE-3; FEB-30, FEB-31; decide FEB-14, FEB-16, FEB-17; FEB-21's backend side | backend stages, owner + audit 23 |
| EXT-FE-06 / 13: real iOS Safari and Android Chrome | FE-11 |
| audit 24: paths-ignore for ci.yml | audit 24 |
| security.yml's RGM-23 / 24 / 27 | the owner |
| local branch fe2-auth-session-20261107: keep or delete | the owner |
| merge the canonical branch into main | the owner |

## 27. What FE-3 Can Rely On

| Piece | What it gives FE-3 |
|---|---|
| /session/* routes | sign-in, refresh, switch, sign-out (everywhere), Google; refusals; signed addresses |
| `lib/session/web-session.ts`, SessionProvider, `useSession()` | the session in every tab, `session.api` (the typed client with the bearer), profile, workspace, generation, `reloadProfile`, `switchWorkspace` |
| PageGate, `lib/routing.ts`, `safeNext` | routing by session state; same-origin `next` |
| the auth screens and the placeholder | replace the placeholder at "/" with the shell |
| `@wasla/api-client/stream` | `openEventStream` for the stream (FE-3 / FE-4 open it; close it on switch, SB-03) |
| e2e support/fe2.ts | `verifiedOwner`, `logInThroughTheApp`, `useAddress`, `useLocale`, `field`, `nothingReachable`, `api`, mailbox helpers; the stream-restart project pattern |
| decisions | ADR-193 |
| waits for | FEB-07 (the workspace status FE-3's gates read) |

## 28. Final Verdict

Set in the addendum from the push's frontend.yml and ci.yml runs.

```text
Provisional (before GitHub's runs): every local gate green at FE2_HEAD 1e20410 and in the verify worktree (one
Playwright worker native crash, FEF-13, re-run green), merged as one normal merge commit 6bdd405 with the merged
tree byte-identical to FE2_HEAD.
```
