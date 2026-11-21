# WhatsApp Embedded Signup and Coexistence (ADR-204)

## 1. Verdict

`ES MERGED AND PUSHED, CI PENDING` at the time of this commit. The final verdict is in addendum 1, written after GitHub's
`ci.yml` and `frontend.yml` have finished on the pushed head.

## 2. Heads

| What | Value |
|---|---|
| Start (origin, canonical) | `4b718c8` (FE-8L: "FE-8 LEFTOVERS MERGED AND PUSHED, CI GREEN. PREVIEWS UPDATED.") |
| Fix branch | `es-coexistence-20261121`, worktree `E:\wasla-es-fix` |
| BE_HEAD | `1225992` (backend half complete) |
| ES_HEAD | `87d1c68` (19 commits on the fix branch) |
| Merge, pushed head, run ids | addendum 1 |

## 3. Decisions applied

The owner answered no open question before the run; OQ-1 .. OQ-12 ran on their defaults, as ADR-204 records: v5 with v4
accepted; Coexistence offered; history imported (never answered, metered, windowed or notified); address-book names only
for contacts Wasla holds; an app reply hands the conversation to a person (ADR-129 unchanged); release unsubscribes and
never deregisters; the assisted form kept and offered second; edits and revokes revise the customer's line;
`META_ONBOARDING_API_VERSION = v27.0` for the onboarding calls only (`META_API_VERSION` stays `v21.0`); the PIN sealed
like the token; `COEXISTENCE_REMINDER_DAYS = 13`; `META_ONBOARDING_WEEKLY_LIMIT = 10`.

## 4. Meta's facts (EXT-ES-01 .. 40)

Every page S1 .. S16 was read on 2026-11-21 as its `.md` variant (HTTP 200). 36 rows MATCH; these are not documented
and stay EXTERNAL VERIFICATION for the owner's walk-through:

| Row | Not documented | Wasla's conservative choice |
|---|---|---|
| EXT-ES-09b | the error shape of an expired code | an OAuthException whose message says "expired" → `embedded_signup_code_expired`; every other → `embedded_signup_code_invalid` |
| EXT-ES-12 | the business token's lifetime | `credential_expires_at` stays NULL unless Meta sends `expires_in` |
| EXT-ES-16 | the first Graph version serving `is_on_biz_app` | the read uses `v27.0` |
| EXT-ES-33 | a country list (Egypt, +20, named nowhere) | nothing in code or copy names a country; 3441042 has its own sentence |
| EXT-ES-39 | the error code of a service message refused for want of a payment method | no warning claims it; the connection page explains the step |

Noted inconsistency: S1 still names Access Verification for the 200-account limit; S14 retired it on 2026-10-09.
S3's listener rule `endsWith('facebook.com')` also accepts `evilfacebook.com`; Wasla accepts `facebook.com` or a
subdomain over https (the same set of Meta's hosts). The full table with quotes is in the notes' draft
(`E:\wasla-verification-scripts\es\` after the rescue).

## 5. Probes

The prompt's P-E01 .. P-E24 were not run as separate before/after scratch runs. Their "before" is the start head's code
(the four fields refused in `payload.py`, `echoes=False` in `policy.py`, no onboarding Graph path anywhere, a release
that called nothing at Meta); their "after" is each asserted by a named test: P-E01..06 and P-E12..17 by
`tests/integration/test_whatsapp_embedded_signup.py` and `tests/unit/test_whatsapp_coexistence_payload.py`; P-E07 by
`tests/unit/test_whatsapp_onboarding_client.py`; P-E10 by the OpenAPI freshness test (272); P-E18..20 by the EX tests and
EJ-06 / 07 / 08; P-E21 by the CX test and EJ-02; P-E22 by the SY test; P-E23 by `test_sb12_the_pin_and_the_token_never_
reach_a_log` and EJ-14; P-E24 by the assisted connect's unchanged tests in the neighbour set.

## 6. Locked decisions and their tests

| Group | Tests |
|---|---|
| CF | `tests/integration/test_whatsapp_embedded_signup_config.py` (9, added for FEB-48), `tests/unit/test_config.py`, `test_deployment_configuration.py` |
| EX, SB, CX, SY, HI, CT, EH, ER, AU, RL | `tests/integration/test_whatsapp_embedded_signup.py` (30), `tests/integration/test_whatsapp_release_unsubscribe_order.py` (2), `tests/unit/test_whatsapp_onboarding_client.py`, `tests/unit/test_whatsapp_coexistence_payload.py` |
| MG9 | `tests/unit/test_whatsapp_models.py`, `test_omnichannel_migrations.py`, the upgrade / downgrade / upgrade run |
| UA, SD, ST, UD, IX, HM9, EM-01 | `frontend/apps/web/test/es-embedded-signup.test.ts` (38), EJ-01 .. EJ-14 |

## 7. Migration 0143

`alembic/versions/20261121_0143_whatsapp_embedded_signup_coexistence.py`: the onboarding columns on `whatsapp_accounts`,
`imported` / `edited_at` / `edited_body` / `revoked_at` on `messages`, the `provider_disconnected` labels. Upgrade from
0142 on an empty database, downgrade to 0142, upgrade again: green; `alembic heads` → `0143` only.

## 8. Routes and codes

Four new operations, OpenAPI 268 → **272**: `GET /whatsapp/embedded-signup/config`, `POST /whatsapp/embedded-signup`,
`GET /whatsapp/accounts/{id}/onboarding` and SB-04's `POST /whatsapp/accounts/{id}/subscription` (the prompt planned 271
"or 272 with SB-04"). New codes: `embedded_signup_disabled`, `embedded_signup_onboarding_limit`,
`embedded_signup_code_invalid`, `embedded_signup_code_expired`, `embedded_signup_coexistence_unavailable`;
`whatsapp_ownership_unverified` and its sentence unchanged.

## 9. Fakes (test-only)

`tests/e2e_stack/fakes.py`: the five onboarding calls with a call log (`GET /_meta/calls`), outcomes by code or number;
a fake SDK (`/meta/sdk.js`, now served with Meta's CORS header) and popup (`/meta/popup`, with a per-journey account
field); a signing webhook injector (`POST /_meta/webhook`). `tests/e2e_stack/launcher.py` maps a number ending `-W<n>`
to its own WhatsApp account, so a journey's `account_update` touches no other journey. Nothing in `app/` imports
`tests/`. `docker-compose.e2e.yml` enables Embedded Signup against the fakes and sets `META_ONBOARDING_WEEKLY_LIMIT` to
the validator's ceiling (the journeys onboard more than ten on one stack).

## 10. Frontend

`whatsapp-embedded` before the assisted form, offered only when the config says enabled; the route
`/channels/connect/whatsapp-embedded`; `components/app/channels/embedded-signup.tsx` (the SDK, the listener, the
states); `lib/embedded-signup.ts` (origin rule, session-info reader v5 / v4, step and EM-01 sentences, the 5-minute rule,
FB.login extras); `lib/coexistence.ts` (disconnect reasons, payment link, home's warnings) kept apart so Meta's SDK URL
is in **one** browser chunk; `connection-onboarding.tsx`; the transcript's marks; home's two warnings. CSP: only the
adapter route adds `https://connect.facebook.net` (script, connect) and `https://www.facebook.com`,
`https://*.facebook.com` (frame, connect); every other route's policy unchanged (unit test). The stream's
`message.revised` and `whatsapp.onboarding.updated` refresh the transcript and the connection page; nothing polls.

## 11. Journeys

`frontend/e2e/tests/es-embedded-signup.spec.ts`, Chromium desktop and phone, 2 workers: **22 / 22 green** (run 4,
3.2 min). EJ-01 + 14, EJ-02, EJ-03, EJ-04 (keyboard), EJ-05, EJ-06 + 07, EJ-08, EJ-09 + 10, EJ-11, EJ-12, EJ-13; each waits
on a fact (the fakes' call log, a text, a row read through the API); axe on the explainer, the connection page (new
number, Coexistence complete, provider-disconnected), the error state and the transcript: 0 serious, 0 critical.

Neighbours (smoke, `fe3-shell`, `fe4-inbox`, `keyboard-only`, `focus-not-obscured`, Chromium desktop and phone):
124 passed, 21 skipped by their own project rules, 1 failed — KY8-03 (ar, 1280, Billing) timed out at 1.6 min under
the parallel load; run alone it passed in en and ar (52.8 s, 49.1 s). Billing is FE-8's and untouched here: recorded,
not fixed.

The four red rounds before green, each fixed in its own commit: the fake SDK lacked the CORS header a
`crossorigin="anonymous"` load needs (938e60d); `Badge` drops `data-*` props so the hooks vanished (FEF-50, 45e4829);
Meta's weekly limit refused the later runs on one stack (feca32b); the SDK URL was in three chunks (FEF-52, fd72578).

## 12. Gates

| Gate | Command | Result |
|---|---|---|
| GBE-1 | the stage's test files by path | green |
| GBE-2 | the 29-file neighbour set (`gbe2_files.txt`) | 381 passed, 0 F / E |
| GBE-3 | `ruff check .`, `black --check .`, `mypy app tests` (ci.yml's lint job) | clean, 1030 source files |
| GBE-4 | 0142 → 0143 → 0142 → 0143 | green |
| GBE-5 | OpenAPI freshness | 272 operations |
| GBE-6 | `test_documentation_truth.py` | 27 passed |
| GBE-8 | no Meta host: `graph.facebook.com` only in the clients' base constants and their tests; every process ran against the fakes | clean |
| FGE-1 | `turbo run typecheck lint i18n:check`, `format:check` | clean |
| FGE-2 | web 688 + 38 new, api-client 51, session 91, i18n 20 | green |
| FGE-3 | e2e-profile build of the three apps | green; the preview build is Part C |
| FGE-4/5/6 | smoke, EJ, neighbours, axe | above |
| FGE-7 | CSP unit test | only the adapter route differs |
| FGE-8 | built chunks naming the SDK URL | 1 |

Not run locally (the light gates): the whole sharded backend suite, the whole e2e matrix, Firefox, WebKit, the nightly.

## 13. Mutants

23 constructed (20 backend, 3 frontend), each applied alone with only its named test run and the tree restored.
Round 1: 18 killed, 5 survived — M-R06 (PIN logged), M-R07 (hint used), M-R14 (a message in two chunks), M-R17 (a held
name overwritten), M-R26 (unsubscribe before the commit). Each got a test (6c2e7b1); round 2: **23 / 23 killed**.
Not constructed, because each would need new code rather than a change to existing code: M-R02, M-R11, M-R12, M-R18,
M-R19, M-R24, M-R25, M-R30, M-R33. E2E-only, not run locally (a rebuild each): M-R32, M-R35, M-R37, M-R38 — EJ-14,
EJ-04, the IX-02 divider and EJ-08 are the tests that would kill them.

## 14. Diff guard

`4b718c8..87d1c68`: 89 files, +9,220 / −84. The locks, the workflows, `nginx/`, the root `Dockerfile`, `pyproject.toml`
and `security.yml`: 0 lines. Outside the prompt's list, each explained: `app/agents/` (ER-07: the AI reads the visible
text), `app/repositories/` and `app/api/dependencies.py` (the history queries; the onboarding service's wiring),
`.env.example` and `README.md` (the settings; the migration range), `docker-compose.prod.yml` — seven names, not four,
with the application's own defaults rather than empty ones (`META_APP_ID`, which the worker now needs, and OQ-11 /
OQ-12's two settings) — a deviation from the prompt's wording, recorded.

## 15. CI accounting

The stage's changed and added test files hold, at the start head, 104 unit and 77 integration tests; at ES_HEAD 133 and
112: **+29 unit, +35 integration** (+64). GitHub's counts: addendum 1.

## 16. Previews

Part C, after CI: addendum 1.

## 17. Findings

| ID | Finding | Status |
|---|---|---|
| FEB-48 | `GET /whatsapp/embedded-signup/config` had no direct test (CF-03 .. 05 untested; M-R27 / 28 would survive) | FIXED, 54c0781 |
| FEB-49 | Five locked decisions lacked a killing test (M-R06, 07, 14, 17, 26) | FIXED, 6c2e7b1 |
| FEF-50 | `@wasla/ui` `Badge` drops `data-*` props; TypeScript accepts them, so hooks vanish silently | FIXED here by wrapping spans and a source guard; `Badge` itself unchanged (shared UI) — open for a UI stage |
| FEF-51 | The fake SDK lacked `Access-Control-Allow-Origin`, so a `crossorigin="anonymous"` load failed | FIXED, 938e60d |
| FEF-52 | Meta's SDK URL in three browser chunks | FIXED, fd72578 |
| FEF-53 | `frontend/packages/api-client/src/events.ts` was not regenerated with `event-types.json` in the backend half | FIXED, 9990e3f |
| FEF-54 | KY8-03 (Billing, ar, 1280) times out under parallel local load; passes alone | RECORDED, not this stage's |
| FEB-46, FEB-47, FEF-49 | FE-8 / FE-8L's | unchanged |

## 18. Runtime

Node v24.15.0, pnpm 12.18.0, Python 3.12 (venv from the three locks by hash), PostgreSQL `pgvector/pgvector:pg16` and
`redis:7-alpine` by the recorded digests, Docker 29.9.3, Playwright 1.65.0 Chromium. Free space at start: C: 18 GB,
E: 221 GB.

## 19. Numbers

Backend tests +64; frontend unit +38; journeys 11 (22 runs); OpenAPI 272; migration head 0143; ADR-204; 89 files
(frontend 32: +3,692 / −35); mutants 23 / 23 killed after one round of fixes; e2e red rounds before green: 3.

## 20. Remaining external actions (the owner's)

1. Business Verification (S9, S14). 2. App Review for `whatsapp_business_messaging` and `whatsapp_business_management`
(S15). 3. A Facebook Login for Business configuration; its id into `META_EMBEDDED_SIGNUP_CONFIG_ID`; Client OAuth and
the allowed HTTPS domains (S3). 4. App Dashboard webhooks: `account_update`, `history`, `smb_app_state_sync`,
`smb_message_echoes` (S3, S5). 5. The real walk-through stage with a new number and a Coexistence number, a +20 number
among them; EXT-ES-09b, 12, 16, 33, 39 observed. 6. `META_API_VERSION` off v21.0 before 2027-01-21 (its own stage).
7. The payment-method step explained to customers before the first real one.

## 21. Cleanup

Part D, after CI: addendum 1.
