# Wasla — Frontend Plan (Marketing Site, Customer App, Platform Console)

| | |
|---|---|
| Date | 2026-11-07 |
| Type | **Plan only.** No frontend code, scaffold or package file was added to the repository. No backend, migration, test, deploy, script, workflow or documentation file was changed. |
| Base | `origin/worktree-billing-google-auth` at `9c7b4e48512885f5cbb76cc7a8b02a5520e86a3d` (= local `HEAD`, 0 ahead / 0 behind) |
| RAG remediation on origin | **Yes.** Merge `52af1a5` is an ancestor of origin; Alembic head `0138`. The RAG deferred-items stage (ADR-188: RGM-16 after-commit publish `ba45b54`, RGM-13 `501bccf`, the pypdf bump `fbb05aa`, security.yml lock install `97bec9f`) has also landed (`1ff78e7`, `fe2ee64`, `9c7b4e4`). |
| Alembic head | `0138` (single head) |
| Plan branch / worktree | `frontend-plan-20261107` / `E:\wasla-fep-plan` (notes: `E:\wasla-fep-notes`, outside the repository) |
| Backend CI at HEAD | **CI green**: GitHub run `42319764942` (push of `9c7b4e4`, success, 56 min). The last `security` run (`42315411238`, workflow_dispatch, 2026-11-06 23:59 UTC) **failed**. Recorded, not investigated, not fixed (audit 24 owns it). |
| Owner decisions | D-01 … D-12 (section 2), taken 2026-10-31, not reopened |
| Finding ID series | `FEB-*` backend changes · `FEQ-*` owner decisions · `EXT-FE-*` external verification · `LP-FE-*` later product · `MKT-*` / `APP-*` / `CON-*` screens · `FE-0 … FE-14` stages |
| Supersedes | `FRONTEND_MASTER_PLAN.md` (2026-09-13), which is **not edited or deleted**; the owner decides its fate. Nothing below is inherited from it unless re-proved from code (section 35). |
| External systems | **Production / Cloudflare / Vercel / Meta / Paymob / Google / Resend / email / WhatsApp / customers: not touched.** No account, project, key, DNS record or deployment was created. Meta was replaced by the repository's `FakeOwnershipVerifier` and a local fake Graph server; email by the repository's `fake` provider; nothing left the machine except reads of public documentation and the npm registry. |

---

## 1. Executive Summary

**Verdict: `FRONTEND PLAN READY — FE-1 CAN START AFTER PREREQUISITES`.**

| Score | Value |
|---|---|
| Backend readiness for a browser frontend | **64 / 100** |
| Plan completeness | **86 / 100** |

The backend is a strong, honest base for a browser. It serves **258 operations** in its production shape (19 unauthenticated), exactly the figure `docs/API.md` states today; the 198 and 209 in the brief are both stale. Every failure uses one envelope `{"error": {"code", "message", "details?", "request_id"}}`; another workspace's id answers 404; a revoked member, a suspended workspace and a stream ticket behave as documented; the realtime stream works from a plain HTTP client, delivers id-only hints within milliseconds and closes with a reason. FastAPI's OpenAPI document generates a usable typed client with three generators **without hand edits**.

What a browser hits first:

1. **The refresh race is real and total.** Two refreshes with one token: one wins, the replay raises `token_version`, and *the winner's brand-new access token is dead too* (probe `J6`, 2026-11-07). The browser session therefore needs one refresh per browser, enforced in the browser (Web Locks), and the refresh token must never be reachable from page script.
2. **The backend rate-limits sign-in, refresh and Google by client IP** (`AuthRateLimit`, 10/min). Any design in which a Next.js server calls those routes makes every user share the server's IP. **FEB-01 (P0)** fixes that before FE-2.
3. **The inbox cannot be drawn from today's read models**: a message carries no media reference (**FEB-03**), a conversation carries no customer name (**FEB-04**), and there is no closed / mine / unassigned / search view (**FEB-05**). These are P0/P1 for FE-4, not for FE-1.
4. **Only 102 of 645 raised errors carry a specific code**; the rest are `validation_error` / `conflict` / `permission_denied` with an English sentence. A four-language UI must translate by code, so **FEB-06** adds codes stage by stage.
5. **Nothing a marketing site needs is anonymous**: plans and prices answer 401 (**FEB-08**).
6. **There is no language field on the user and no locale or currency on the workspace** (the workspace *does* have a timezone). **FEB-09**.
7. **Cloudflare is viable, with a twist**: Cloudflare's own Next.js guide now recommends *vinext* (beta) on Workers and lists the OpenNext adapter as the alternative for existing apps; Pages is for static exports only. Astro static output deploys to Workers static assets without an adapter. The plan recommends Workers + OpenNext for the two Next.js apps, Workers static assets for Astro, and the browser talking to the API origin directly for data and the event stream — which removes the long-response question from the Next.js host entirely. That still needs the owner's yes (**FEQ-01**) and a real preview (EXT-FE-01).

**What can start tomorrow, and what first.** FE-1 (monorepo, tooling, generated client, design-system primitives, four locales with RTL, test harness, frontend CI, an empty preview) depends only on two backend items — the committed OpenAPI export with a declared error envelope (**FEB-02**) — and on the owner answering **FEQ-01** (hosting) and **FEQ-03** (session design). FE-2 additionally needs **FEB-01**. Both backend items are small; together they are stage **FE-0**.

---

## 2. Owner Decisions and Plan Decisions

### 2.1 Owner decisions (inputs, not reopened)

| ID | Decision | How this plan builds on it | Risk recorded |
|---|---|---|---|
| D-01 | Marketing: Astro + TS, React islands | `apps/marketing`, static output, islands only for the language switcher, pricing toggle and inquiry form | none |
| D-02 | Customer app: Next.js + React + TS | `apps/web`, App Router, client-rendered data | Next.js had 6 middleware/proxy bypass advisories in 12 months (section 37): the app never relies on `proxy.ts` for authorization |
| D-03 | Console: separate Next.js app | `apps/console`, own origin, own session, no shared tenant-data components | none |
| D-04 | `frontend/` in this repository | sections 17, 23 | backend image must not grow (`.dockerignore`) |
| D-05 | pnpm + Turborepo | pnpm 12.18.0, turbo 2.11.10 (npm registry, 2026-11-06) | pnpm not installed on this machine (recorded only) |
| D-06 | Client generated from OpenAPI | openapi-typescript + openapi-fetch (section 7) | production does not serve the document → FEB-02 |
| D-07 | en (default), ar (RTL), fr, tr in all three apps | section 20 | backend sentences are English → FEB-06 |
| D-08 | Vitest + Playwright | section 23 | none |
| D-09 | Cloudflare Pages — *proposed* | Settled in section 26: **Workers** (not Pages) for both Next.js apps and for Astro; FEQ-01 | Cloudflare now steers Next.js to vinext (beta); see EXT-FE-02 |
| D-10 | Simple, complete, replaceable design system | tokens + headless primitives, screens never style (section 19) | none |
| D-11 | Plan from the code | every claim in this plan names a probe, a file or an operation | none |
| D-12 | First-version scope | screens in section 27; campaigns, follow-ups, templates, analytics in v2 | none |

### 2.2 Plan decisions (the owner may override any)

| ID | Sub-choice | Decision | Why | Rejected |
|---|---|---|---|---|
| PD-01 | Next.js router | App Router, Node runtime, **client-side data fetching** for every authenticated screen | the session design keeps tokens out of server components; the App Router is what OpenNext/vinext support; Pages Router is legacy | Pages Router; server components fetching API data (would put the access token on the server and reintroduce the IP problem) |
| PD-02 | Use of `proxy.ts` (ex-middleware) | locale negotiation and security headers only; **never** authorization | 6 proxy-bypass advisories in 2026 (GHSA-6gpp-xcg3-4w24, -26hh-7cqf-hhc6, -267c-6grr-h53f, -492v-c6pp-mqqv, -36qx-fr4f-26g5, -3g8h-86w9-wvmq); the backend is the authority | auth gating in proxy |
| PD-03 | Server actions | **not used** | 8 server-action advisories in 12 months; nothing needs them — forms post to the API from the client | server actions |
| PD-04 | API client | `openapi-typescript` 7.13 + `openapi-fetch` 0.17, hooks via `openapi-react-query` 0.5 or a 40-line in-house wrapper | spike: compiles with 0 errors, 712 KB of types and no runtime per operation; path-keyed, so FastAPI's 58-character operationIds do not leak into code | `@hey-api/openapi-ts` 0.99 (0 errors, 1.3 MB, function names like `listAgentsApiV1AgentsGet`), `orval` 8.46 (0 errors, 1.4 MB, hook names `useCreateAgentApiV1AgentsPost`, React Query v5 option warning) |
| PD-05 | Data fetching | TanStack Query 5 (5.107.1) | cursor pages, invalidation from stream events, mutation retries | SWR (weaker mutation/invalidation model) |
| PD-06 | Query keys | `[wsId, sessionGen, resource, params]`; a switch or sign-out bumps `sessionGen` and clears the cache | makes cross-workspace leakage structurally impossible (J7 shows the old token still reads the old workspace until expiry) | keys without workspace |
| PD-07 | Forms | React Hook Form 7.91 + Zod 4.6 | schemas checked against the generated types; backend stays authority | Formik |
| PD-08 | Headless primitives | Radix (`radix-ui` 1.8.1) + `Direction.Provider` | Radix documents that primitives "inherit global reading direction"; keyboard/placement RTL proved per component in FE-1 (EXT-FE-07) | Base UI (1.0.0-rc.0, not stable); React Aria (heavier, fine fallback) |
| PD-09 | Styling | Tailwind CSS 4 over CSS custom-property tokens, logical utilities only | tokens are plain CSS so Astro and both apps share them; a redesign edits tokens | CSS-in-JS (runtime cost, RSC friction) |
| PD-10 | i18n | next-intl 4.14 in both Next.js apps; Astro built-in i18n routing in marketing; one ICU catalogue set in `packages/i18n` | next-intl supports App Router, ICU plurals and `localePrefix: 'never'` with a cookie | i18next (heavier, ICU via plugin) |
| PD-11 | Locale in URL | marketing: always (`/en`, `/ar`, `/fr`, `/tr`); apps: **never** (cookie, then FEB-09 account preference) | emailed links the backend builds carry no locale (`/invitations/accept`, `/reset-password`, `/billing/checkout/return`) and app URLs need not be indexed | locale-prefixed app routes (every backend link would need a redirect) |
| PD-12 | Session (customer app) | **S-2 "hybrid"** — section 8 | the only design that needs no server-side lock, keeps the refresh token out of script, and keeps the stream and the bulk of the traffic browser → API | S-1 BFF; S-3 tokens in storage |
| PD-13 | Session (console) | S-2 with stricter CSP, behind an access gate (FEQ-05) | low traffic, no stream needed, but the highest-value token | S-1 BFF is the documented alternative if the owner wants zero tokens in script |
| PD-14 | Realtime | one stream per browser per workspace, leader-elected with Web Locks, fanned out with BroadcastChannel | `RT_STREAMS_PER_USER=5` (a 6th stream answered 429 `stream_limit`) | one per tab |
| PD-15 | Component catalogue | Storybook 10 for `packages/ui` only | RTL and token review need a place outside screens; free | Ladle (smaller community) |
| PD-16 | Unit tests / mocks | Vitest 5.1 + Testing Library + MSW 3.0 typed from the generated `paths` | a mock that drifts from the contract fails to typecheck | hand-typed mocks |
| PD-17 | E2E | Playwright 1.65 + `@axe-core/playwright` 4.14 against the **real backend** in `wasla-fe-e2e-*` containers | the probes in this plan are the seed of those journeys | mocked-backend E2E |
| PD-18 | Fonts | IBM Plex Sans Arabic (OFL-1.1) + IBM Plex Sans (OFL-1.1), self-hosted, subset | one designed family covering Latin, Turkish and Arabic | Noto pair (fine alternative); Google Fonts CDN (third-party request) |
| PD-19 | Node | Node 24 LTS pinned in `.nvmrc` and `engines` (machine has v24.15.0) | current LTS line | — |

---

## 3. Repository Verification

| Item | Value (2026-11-07, 04:39–05:08 UTC) |
|---|---|
| `git branch --show-current` (E:\wasla) | `worktree-billing-google-auth` |
| `git rev-parse HEAD` | `9c7b4e48512885f5cbb76cc7a8b02a5520e86a3d` |
| `git status --short` | 11 untracked owner reports + `docs/ui/` (untouched) |
| `git stash list` | empty |
| `git fetch origin --prune` → `origin/worktree-billing-google-auth` | `9c7b4e4…` ; left/right `0 0` |
| `git worktree list` | 20 worktrees (`E:\wasla` + 19 `E:\wasla-*` stages); this plan added `E:\wasla-fep-plan` |
| `alembic heads` | `0138 (head)` |
| Python | 3.12.7 |
| PostgreSQL | 16.15 (pgvector 0.8.6) in `wasla-fep-pg` |
| Redis | 7.4.11 in `wasla-fep-redis` |
| Docker | Engine 29.8.0; 19 exited containers of earlier stages, none touched |
| Node.js / pnpm | v24.15.0 / **pnpm not installed** (recorded; nothing installed into the repository) |
| Clock | local `2026-11-07T05:08:29Z` = `Date:` header of an external HTTPS server `05:08:29 GMT` |
| `app.__file__` | `E:\wasla-fep-plan\app\__init__.py` when run from the worktree ✔. **Trap observed:** a helper script launched with `python <script>` from the notes folder imported `E:\wasla\app` (editable install). Same commit, so no result changed, but every helper was then pinned with `sys.path.insert(0, worktree)`. |
| `frontend/`, `package.json`, `pnpm-workspace.yaml`, `*.ts/*.tsx/*.astro` tracked | **none** |
| OpenAPI export script | **none** (`scripts/` has no `*openapi*`; no test dumps the document) → FEB-02 |
| RAG merge on origin | yes (`52af1a5` ancestor; head `0138`) |
| RAG deferred items (ADR-188) | **landed** |

**Frontend-relevant settings** (`app/core/config.py`, `.env.example`):

| Setting | Default | Used for |
|---|---|---|
| `CORS_ORIGINS` | `[]` (example `http://localhost:3000`) | explicit list; `allow_credentials=True`, `allow_methods/headers=*`, `expose_headers=[X-Request-ID]` (`app/main.py:180`); staging/production refuse `*` and non-https |
| `DOCS_ENABLED` | true in developer environments, **must be false** in staging/production (validator `config.py:1518`) | `/docs`, `/openapi.json` |
| `APP_PUBLIC_URL` | none; required when `EMAIL_ENABLED` or Paymob; https in staging/production | base of every emailed link and of the Paymob return |
| `GOOGLE_REDIRECT_URI` | none; required when `GOOGLE_ENABLED`; https in production; no fragment | Google redirects **to the frontend** (ADR-047) |
| `ACCESS_TOKEN_TTL_SECONDS` / `REFRESH_TOKEN_TTL_SECONDS` | 900 / 1 209 600 | session |
| `RT_STREAMS_PER_USER` / `_PER_WORKSPACE` / `RT_TICKET_TTL_SECONDS` / `RT_KEEPALIVE_SECONDS` | 5 / 100 / 60 / 15 | stream |
| `MAX_JSON_REQUEST_BYTES` / `MAX_AUTHENTICATED_REQUEST_BYTES` / `MAX_DOCUMENT_REQUEST_BYTES` / `MAX_REQUEST_BYTES` | 64 KiB / 1 MiB / 5 MiB / 32 MiB | body caps (uploads elevated only with a verifiable bearer, `app/core/limits.py:210`) |
| `MEDIA_MAX_BYTES` | 25 MiB | media |
| `TRUSTED_PROXY_IPS` | empty | which peers may supply `X-Real-IP` / `X-Forwarded-For` (`app/api/rate_limits.py:58`) |
| `RATE_LIMIT_AUTH_PER_MINUTE` / `_LOGIN_PER_ACCOUNT_PER_MINUTE` / `_WORKSPACE_PER_MINUTE` | 10 / 5 / 300 | buckets |

**URLs the backend builds and sends** (from code, confirmed in probe J3):

| Link | Built at | Shape |
|---|---|---|
| Invitation | `app/services/email_templates.py:204` | `{APP_PUBLIC_URL}/invitations/accept?token=…` (observed: `http://localhost:3000/invitations/accept?token=<r>`) |
| Password reset | `email_templates.py:222` | `{APP_PUBLIC_URL}/reset-password?token=…` |
| Email verification | — | **no link**: a 6-digit code, 10 minutes, single use ("Enter it in the tab you started from") |
| Paymob return | `app/integrations/billing/__init__.py:81` | `{APP_PUBLIC_URL}/billing/checkout/return` |
| Paymob callback (server) | same file `:80` | `{APP_PUBLIC_URL}{CALLBACK_PATH}` — **note:** the server callback is built on `APP_PUBLIC_URL`, i.e. on the *frontend* origin; the frontend host must route that path to the API or the deployment must set a different base (intersection for audit 20/22, **FEB-12**) |
| Google redirect | `GOOGLE_REDIRECT_URI` | a frontend route that posts `code` + `state` to `/auth/google/callback` |

---

## 4. Methodology

discover (code) → observe (running app) → record contract → judge for a browser → classify → place on a screen and a stage.

| Step | What ran | Evidence file (notes folder) |
|---|---|---|
| OpenAPI export | `create_app().openapi()` with `DOCS_ENABLED=true` | `openapi-dev.json` (257 operations, 392 schemas, OpenAPI 3.1.0) |
| Production walk | `create_app(Settings(environment="test", docs_enabled=False))`, recursive walk through `original_router`, dependency names resolved | `scripts/inventory.py`, `inventory.json` |
| Generator spike | three generators on the real document + `tsc --strict` | `spikes/gen/` |
| Session probe | register → code → verify → refresh rotation → replay → concurrent refresh → switch → logout → envelopes → CORS preflight | `scripts/probe_session*.py`, `probe_session_s2.json`, `probe_session2_s2.json` |
| Journeys probe | real app under a launcher that swaps Meta for `tests/fake_ownership.FakeOwnershipVerifier` and a local fake Graph server; SSE read with timestamps | `scripts/fep_server.py`, `scripts/probe_journeys.py`, `probe_journeys_j5.json` (also runs j1–j4, k1) |
| J4/J5/J9 probe | knowledge upload, checkout (manual provider), Google disabled | `scripts/probe_j4_j5_j9.py` |
| Repository suites | Google endpoints, Paymob checkout, knowledge upload routes, invitations, events stream / capacity / shutdown, refresh reuse, RAG ingestion lifecycle on `wasla-fep-pg/wasla_test` + `wasla-fep-redis` | `journey_tests*.log` |
| Documentation | five readers of official pages (section 37) + npm registry metadata | section 37 |

**Deviations from the brief, stated plainly.** (1) The API ran as a host process from the worktree (`python -m app.serve` / the launcher, port 8911), not in a container; PostgreSQL and Redis ran in `wasla-fep-*` containers. (2) There is no SMTP mail catcher: the repository's `fake` email provider keeps messages in worker memory, so a plan-only reader (`scripts/mailbox.py`) renders the queued `email_messages` row with the application's own `open_email_context` + `render`. It only works before the worker sends, because sending clears the sealed context — so the journeys ran with the worker stopped. (3) The Meta Graph base URL is a module constant, not a setting; the launcher repointed it at a local fake and let only `127.0.0.1:8912` bypass `GuardedTransport` *inside the launcher process*. No repository file changed. (4) No OpenAI key and no default agent: the AI turn of J2 was not walked at runtime (section 28).

## 5. File Inventory

| | Count |
|---|---|
| Router modules under `app/api/v1/` | 34 (+ `health.py`, `metrics.py`) |
| Files fully read for this plan | `app/api/dependencies.py` (auth, roles, entitlement and platform guards), `app/api/v1/events.py`, `app/api/v1/google_oauth.py` (header + routes), `app/core/oauth_binding.py`, `app/core/exceptions.py` (envelope), `app/core/events.py` (vocabulary), `app/services/realtime.py` (close reasons), `app/integrations/billing/__init__.py`, `app/integrations/email/fake.py`, `tests/fake_ownership.py`, `tests/integration/test_route_authorization.py` (open-route table), `tests/integration/test_documentation_truth.py` (the walk) |
| Programmatically analysed | **all 258 operations** (dependency graph, roles, entitlement guards, query/header parameters, request/response schemas); **all `raise …Error(` sites in `app/`** (645) for error codes; all 392 component schemas for enums, money and nullability |
| Read in part | `app/core/config.py`, `app/core/limits.py`, `app/core/net.py`, `app/api/rate_limits.py`, `app/api/v1/conversations.py` (list, send, media), `app/api/v1/auth.py`, `app/services/whatsapp_account_service.py`, `app/services/email_templates.py`, `app/schemas/whatsapp.py`, `docs/API.md` |
| Unreviewed surface a browser can reach | **0 operations** (every operation is in Appendix A with its gates); individual service internals behind them were not re-read, by design (no re-audit) |

---

## 6. Operation Inventory (summary — full table in Appendix A)

| Measure | Value | Evidence |
|---|---|---|
| Operations, production shape (`DOCS_ENABLED=false`) | **258** | dependency walk (`inventory.py`), 2026-11-07 |
| Unauthenticated | **19** (`/health`, `/health/live`, `/health/ready`, `/metrics`, register, login, refresh, logout, password-reset request/confirm, Google authorize/callback, invitation accept, `/timezones`, `/events/stream` (ticket), WhatsApp webhook GET/POST, Paymob webhook, email webhook) | walk + `tests/integration/test_route_authorization.py::OPEN_ROUTES` |
| Operations in the dev OpenAPI document | 257 = 258 − `/metrics` (`include_in_schema=False`) | diff walk ↔ document: no other difference |
| `/openapi.json` with `DOCS_ENABLED=false` | absent (validator refuses `true` in staging/production) | `config.py:1518` |
| Documented figure in `docs/API.md` | **258 / 19 — correct today** (asserted by `test_documentation_truth.py:103`) | the brief's **198/17** and **209** are both stale |
| Auth class | 151 bearer + active workspace · 88 bearer only (account or platform) · 19 none | walk |
| Email-verification gate | 227 operations | walk (`require_verified_user`) |
| Workspace role gates | 58 owner/admin · 23 owner-only · the rest any member | walk (`require_tenant_roles`) |
| Platform | 74 operations, all `PLATFORM_OWNER` or `PLATFORM_ADMIN` (`require_platform_roles`); destructive user routes also `require_platform_authority` (hierarchy) | walk |
| Entitlement guards at the route | `POST /agents` (agents), `POST /invitations` and `POST /workspace/members/{id}/reinstate` (team_members); channel capacity is enforced inside the connect service (ENT-05/08); document slots inside knowledge | walk + `dependencies.py:918-951` |
| Pagination | 15 cursor (`{items, next_cursor}`) · 17 limit/offset · 226 none (bare object or bare array) | walk — **contradicts** the `docs/API.md` sentence "only conversations and messages are cursor-paged": contacts, leads, notes, activity, follow-ups, knowledge bases/documents, header media, campaigns, campaign activity/recipients are too (DOCUMENT ONLY drift; intersection for audit 25) |
| Multipart | 6: send media, document upload, document replace, template header media, campaign header media, rate-card upload | OpenAPI |
| Idempotency | `Idempotency-Key` header on the three send routes and `POST /campaigns`; `idempotency_key` **body field** on `POST /billing/checkout`; `If-Match` on campaign, segment and follow-up-settings PATCH/PUT | OpenAPI header params |
| Never called by a frontend | 8: health ×3, metrics, 4 webhooks | Appendix A, app = none |
| By app | customer app 176 · console 74 | Appendix A |

Operations a first-version screen needs that do not exist are in the ledger (section 30): FEB-03, -04, -05, -07, -08, -09, -10, -11, -13, -14.

## 7. The OpenAPI Document as a Contract, and the Generator Choice

| Property | Finding | Class | Basis |
|---|---|---|---|
| Version | OpenAPI **3.1.0**, `info.version` `0.1.0` (never bumped) | DERIVED | OPENAPI |
| operationId | present and unique (257/257), FastAPI default `name_path_method`, mean 58 chars, longest `list_workspace_capacity_reductions_api_v1_platform_billing_tenants__tenant_id__capacity_reductions_get`; changes whenever a path changes | SCALE-TRIGGERED → FEB-02 (custom `generate_unique_id_function`, P2) | OPENAPI |
| Error envelope | **not declared.** 223 operations declare a `422` with FastAPI's `HTTPValidationError` (`{"detail": [...]}`); the real 422 body is `{"error": {"code": "validation_error", "message", "details": {"errors": [{type, loc, msg}]}, "request_id"}}` (probe `422 envelope`); 401/403/404/409/429 are mostly undeclared | **CONFIRMED DEFECT** (contract ≠ runtime) → FEB-02 P0; intersection audit 23 | RUNTIME + OPENAPI |
| Enums | 78 named enum schemas, 15 inline | fine | OPENAPI |
| Nullability | 875 `anyOf [..., {"type": "null"}]`; required vs optional distinguished | fine | OPENAPI |
| Money | decimal **strings** (`"price": "0.00"`, `amount_due: string` with a numeric pattern), with `currency` beside | correct by design (no float); UI formats with `Intl.NumberFormat` | OPENAPI + RUNTIME |
| Datetimes | `format: date-time`, UTC with offset (`2026-11-07T04:47:04.656329Z`) | fine | RUNTIME |
| Multipart | described (`Body_upload_document_…` with `file: string/binary`) | generator types `file` as `string`; needs a one-line override to `Blob` (openapi-typescript `transform`) | SPIKE |
| Discriminated unions | none (0 discriminators) | fine for generation | OPENAPI |
| Response models | 206×200, 31×201, 10×202, 10×204; `GET …/media/{id}` declares `{}` (binary not described) | P3 | OPENAPI |
| Stream vocabulary | not expressible; documented only in the route description | hand-written `packages/api-client/src/events.ts` with a contract test against `app/core/events.py::EVENT_TYPES` → FEB-02 asks the backend to publish a JSON Schema of the frames (P2) | CODE |
| Production document | not served → **the build must use an exported file** | FEB-02 P0 | RUNTIME |

**Generator spike** (`E:\wasla-fep-notes\spikes\gen`, document of 2026-11-07):

| | openapi-typescript 7.13.0 + openapi-fetch 0.17.0 | @hey-api/openapi-ts 0.99.0 | orval 8.46.0 |
|---|---|---|---|
| Generates without hand edits | yes (4 s) | yes | yes |
| `tsc --strict` (lib ES2022, DOM, DOM.Iterable) | **0 errors** | 0 errors | 0 errors |
| Output size | 712 KB, types only (21 072 lines `.d.ts`) | 1.3 MB (types + SDK + query) | 1.4 MB single file |
| Naming | path-keyed: `client.GET("/api/v1/conversations", …)` | `listAgentsApiV1AgentsGet` | `useCreateAgentApiV1AgentsPost` |
| Error envelope typing | from the document → today `HTTPValidationError` (wrong until FEB-02) | same | same |
| Multipart | `bodySerializer` builds `FormData` (documented); `file` typed `string` until override | generated | generated |
| TanStack Query | `openapi-react-query` 0.5.4 (third-party wrapper by the openapi-ts project) or in-house | plugin | native |
| Licence / last publish | MIT / 2026-06-15; MIT / 2026-10-30 | MIT / 2026-09-30 | MIT |

**Choice: openapi-typescript + openapi-fetch.** It is the smallest, emits no per-operation runtime, survives FastAPI operationId churn because calls are keyed by path and method, and its typed `paths` object also types the MSW handlers (section 23). `@hey-api` is the fallback if the owner wants generated hooks.

**How the frontend gets the document when production does not serve it:** FEB-02 adds `scripts/export_openapi.py` (builds the app with `docs_enabled=True` in-process and writes `frontend/packages/api-client/openapi.json`, sorted keys). The frontend CI regenerates `schema.d.ts` from the committed file and fails on any diff; the backend CI runs the export and fails if the committed file is stale. Neither CI ever needs a running server.

## 8. The Browser Session Design

### 8.1 What the backend does today (runtime, probes `probe_session_s2` and `probe_journeys_j5`, 2026-11-07)

| Behaviour | Observed |
|---|---|
| Register | `202 {"status":"accepted"}`, no session; then login |
| Login | `200 {access_token, refresh_token, token_type, expires_in: 900, active_workspace: {id, name, slug, role}}`; **no `Set-Cookie`** |
| Unverified session | `/auth/me` 200 (`email_verified_at: null`); `/conversations` 403 `email_verification_required`; same access token works after `verify` (no re-login) |
| Refresh | 200 new pair; old refresh → **401 with the message "The email address or password is incorrect."** (misleading sentence; code `unauthenticated`) |
| Replay | after one replay, the newest access token **and** the newest refresh token are both refused (token_version bump) |
| **Concurrent refresh (two tabs)** | one 200, one 401; **the 200's access token is then refused** (`401 The credentials are not valid.`) — the whole account is signed out |
| Rate limit | after 8 auth calls inside a minute from one IP, the 9th: `429 rate_limited "Try again in 50 seconds."` |
| Switch workspace | `POST /auth/workspace {workspace_slug}` → new access token; **old access token keeps working for the old workspace until expiry** |
| Refresh after switch | **returns the account's default workspace unless `workspace_slug` is sent** (`active_workspace` came back as the first workspace) |
| Logout | 204; refresh refused; access token still valid until expiry (by design) |
| CORS | allowed origin: preflight 200 with `allow-credentials: true`, `max-age 600`; other origin: 400, no allow-origin |
| Google | `/auth/google/authorize` 404 `Google sign-in is not available.` when disabled; when enabled it sets the **only cookie the API sets**, `__Host-wasla_oauth` (HttpOnly, Lax, host-only, flow TTL) on the API host, and the callback requires it (SEC-07, ADR-066) — so the authorize and callback calls must reach the API from the same browser cookie jar |

### 8.2 Designs compared

| | S-1 BFF (Next server holds both tokens) | **S-2 hybrid (recommended)** | S-3 tokens in storage |
|---|---|---|---|
| Where tokens live | server session store; browser holds opaque cookie | access token **in memory** of the page; refresh token in a `__Host-` HttpOnly SameSite=Strict cookie on the **app origin**, readable only by three route handlers | `localStorage` |
| XSS exposure | no token reachable; attacker can still act through the BFF while the page is open | ≤15-minute access token while the page is open; refresh token unreachable | 14-day credential stolen — **rejected** (OWASP: "Do not store session identifiers in local storage") |
| CSRF | every BFF route is cookie-authenticated → needs CSRF defence on all of them | only the 3 `/session/*` routes are cookie-authenticated: POST only, `SameSite=Strict`, `Origin` must equal the app origin, custom header `X-Wasla-Session: 1` (forces preflight), Fetch-Metadata `Sec-Fetch-Site: same-origin` | n/a |
| Refresh race | needs a cross-isolate lock per session (KV cannot: "eventually consistent… not ideal for atomic operations"; needs a Durable Object) | **one browser = one cookie**; the browser serialises with `navigator.locks.request('wasla-refresh')` (Web Locks: origin-scoped, shared across tabs, Baseline since 2022) and broadcasts the new access token with `BroadcastChannel`; a second device has its own refresh token | n/a |
| Rate limits | **all** traffic from the host's IPs | only `/session/*` from the host's IPs → **FEB-01** | n/a |
| Stream | proxy a long response through the Next host, or hand the ticket to the browser anyway | browser `POST /events/ticket` with bearer, then `EventSource` to the **API origin** | — |
| Media / uploads | proxied through the host (body limits, egress) | browser → API directly with bearer (`fetch` → `Blob` → object URL; uploads with progress via XHR) | — |
| Host needs | Durable Object session store + lock, encrypted cookie | three route handlers, a secret for the forwarded-IP signature, no store | — |
| Backend needs | FEB-01 for every route; CORS not needed | FEB-01 for `/session/*`; CORS lists the app origin (already supported) | — |
| Latency | +1 hop on every call | +1 hop only on login/refresh/logout | — |

### 8.3 S-2 in detail (customer app)

```text
Login / register-login / Google callback / refresh / logout / logout-all   → POST /session/<verb> (Next route handler, Node runtime)
    → server-to-server call to the API with X-Wasla-Client-IP + HMAC (FEB-01)
    → response: { access_token, expires_in, active_workspace } to the page; Set-Cookie __Host-wasla_rt=<refresh>; Secure; HttpOnly; SameSite=Strict; Path=/
    → the refresh token never reaches page script
Every other API call                                                         → browser → https://api.<domain>/api/v1/... with Authorization: Bearer (memory)
Refresh                                                                      → only under navigator.locks 'wasla-refresh'; at 80 % of expires_in or on a 401;
                                                                               body carries the current workspace_slug (8.1: otherwise the default workspace returns)
                                                                               → BroadcastChannel 'wasla-session' {access_token, expires_in, ws}
Google                                                                       → /session/google/authorize relays the API's __Host-wasla_oauth Set-Cookie onto the app host;
                                                                               /session/google/callback forwards it back — the API's binding check is unchanged
Logout                                                                       → /session/logout spends the refresh, clears the cookie, BroadcastChannel 'signed-out',
                                                                               every tab clears its query cache and blob URLs
```

The console uses the same shape (separate origin, separate cookie name, separate route handlers) plus FEQ-05's access gate and a CSP with no third-party origin at all. S-1 remains the documented alternative if the owner prefers no token in script (it costs a Durable Object and FEB-01 on every route).

**FEB-14 (P2)**: a short refresh-reuse grace (e.g. 10 s, returning the *same* successor) would make the race survivable without the browser lock (a common refresh-rotation pattern; not sourced in this plan). It weakens replay detection slightly and is the owner's and audit 23's call; the plan does not depend on it.

**FEQ-03** puts S-2 (both apps) to the owner.

## 9. Roles, Gates and Entitlements in the UI

**Roles that exist** (`TenantRole`: `tenant_owner`, `tenant_admin`, `member`; `PlatformRole`: `platform_owner`, `platform_admin`; from `dependencies.py:902-985`). Per-operation roles are in Appendix A. What a `member` can and cannot do was proved in J3 (probe `j5`): reads conversations, members, entitlements, agents; **refused** invitations, billing summary and checkout (`403 permission_denied "This action requires a different role in this workspace."`).

**Gates that are not roles**, with what the UI shows and the one action that clears it:

| Gate | How the backend says it (observed) | Screen state | Clearing action |
|---|---|---|---|
| Email unverified | 403 `email_verification_required` on every verified route | full-page "Enter the code we sent" with resend; only account menu reachable | verify |
| Account disabled / deleted | 401 `unauthenticated` "The credentials are not valid." | signed out with a neutral message | contact support |
| Session torn down (replay, logout-all, password change) | 401 on refresh | signed out, "Signed out on all devices" | sign in |
| Workspace suspended | 403 `permission_denied` **"This workspace is not available."**; stream `closed {reason: workspace_unavailable}` within ~6 s (J8) | suspended banner + read-only shell; switcher offers other workspaces | contact support → **FEB-07** (a stable `workspace_suspended` code and `status` in `/auth/me` workspaces) |
| Membership revoked | **404 `not_found`** on the next call; stream `closed {membership_revoked}` within ~12 s (J3) | "You no longer have access to this workspace" | switch workspace → FEB-07 (`membership_revoked` code) |
| Payment required / past_due / trial | `TenantBillingSummary.payment_required`, `subscription.status` (`trialing, active, past_due, suspended, cancelled, expired`); 402 `payment_required` | banner on every page for owner; quieter notice for others | pay (owner) / ask owner |
| Capacity reached | 409 `channel_capacity_exceeded` / `channel_type_not_allowed`; `/billing/channel-capacity` (`effective_limit, active, remaining, over_limit, reduction, scheduled, …`) | connect button disabled with reason | top up / change plan |
| Capacity reduced, connection disabled | `ChannelConnectionRead.disabled_reason`, `disabled_at`; `ChannelCapacityRead.reduction` | connection card in "disabled by plan" state | choose which connection stays (`POST /billing/channel-capacity/selection`) |
| Allowance exhausted | 402 `plan_limit_exceeded`; `EntitlementRead.remaining/over_limit` | usage meter red; AI paused notice | top up / upgrade |

**One call for the shell?** No. The shell needs `/auth/me` (identity, verified, workspaces, platform role) + `/billing/summary` (owner/admin only — members get 403) + `/billing/entitlements` (any member). A member therefore cannot learn "payment required" at all, and nobody learns a workspace's status from `/auth/me`. → **FEB-07 (P0 for FE-3)**: add `status` to `WorkspaceSummary` and a member-readable gate summary (`GET /workspace/gates` or fields on `/auth/me`: `status`, `payment_required`, `subscription_status`, `trial_ends_at`) — no amounts.

## 10. The Realtime Inbox from a Browser

**Observed** (probe `j5`, 2026-11-07, `httpx` streaming client, timestamps in seconds from start):

| Fact | Evidence |
|---|---|
| Ticket: `POST /api/v1/events/ticket` (any member, bearer) → `201 {ticket, expires_at}` (+60 s), single use, bound to user, workspace, token_version | `[5.23]` |
| Stream: `GET /api/v1/events/stream?ticket=…` → `200 text/event-stream; charset=utf-8`, `Cache-Control: no-store`, `X-Accel-Buffering: no`; first frame `retry: 3000` | `[5.25]` |
| `message.created {conversation_id, message_id, direction, channel_type, created_at}` arrived **~10 ms** before the webhook's own 200 | `[6.72]` vs `[6.73]` |
| `conversation.updated {conversation_id, fields: ["assignee","owner"]}` on takeover | `[10.89]` |
| `message.status {conversation_id, message_id, status: "sent"}` | `[11.44]` |
| Keep-alive `: keep-alive` every 15 s | `[21.32]`, `[34.54]` … |
| Event id format `1794027098619-0` (Redis stream id) | frames |
| **Reconnect with the same URL (what `EventSource` does) → `401 "This stream ticket is not valid. Ask for a new one."`** | `[14.55]` |
| Close reasons: `membership_revoked` (12 s after revoke), `workspace_unavailable` (6 s after suspend); code also has `token_changed`, `workspace_switched`, `server_restart` (`realtime.py:288-296, 323`) | `[34.57]`, `[79.59]` |
| `workspace_switched` fires when the person **opens a stream on another workspace**, not on `POST /auth/workspace` (an old stream stayed open 30 s after a switch) | `[47.58]→[79.59]`, `realtime.py:19` |
| Limit: 5 streams per user (any workspace, any device); the 6th → `429 {"code":"stream_limit"}` | `[44.44]` |
| No event for knowledge documents, payments, connections or members | `app/core/events.py:49-57` |
| Server restart sends `closed {server_restart}` (FRM-03) | suite `test_events_shutdown_signal` (OS-signal cases skipped on Windows, run on Linux CI) |

**Design.**

1. **Do not rely on `EventSource`'s automatic reconnect.** On `error` or `closed`, close the source; on `server_restart` or a network error: back off (3 s from `retry`, then ×2 to 60 s, jitter), get a **new ticket**, reopen with `&lastEventId=<last id>` (the route accepts the query alias because a fresh `EventSource` cannot set the header). On `token_changed`: run the refresh flow; if it fails, sign out. On `membership_revoked` / `workspace_unavailable`: show the gate, do not reconnect. On `workspace_switched`: show "This workspace was opened in another tab or device", do not reconnect until the user clicks (otherwise two tabs ping-pong).
2. **One stream per browser per workspace**, held by a leader tab chosen with `navigator.locks.request('wasla-stream-<ws>')`; followers receive frames over `BroadcastChannel`. Five streams per *user* across devices are plenty for that; one stream per tab would hit 429 at the sixth tab. SharedWorker was rejected because its Safari-iOS / Android support could not be confirmed (EXT-FE-06).
3. **Events invalidate queries, never carry data**: `message.created` → `['conv', id, 'messages']` first page + `['inbox', ...]`; `message.status` → patch the message's status in cache, else invalidate; `conversation.updated` → `['conv', id]` + inbox lists; `follow_up.updated` / `campaign.changed` → v2 keys.
4. **After a gap** (`resync` frame, or reconnect without replay): `GET /conversations?updated_since=<last seen>` (offset required: `updated_since_needs_offset`) and refetch the open conversation. The first page carries an `ETag`; `If-None-Match` answers 304.
5. **Fallback** when no stream: poll the inbox every `poll_after_seconds` (the API returns **30 without a stream, 60 with one**), pause in hidden tabs, banner "Live updates paused — retrying".
6. **Origin**: the stream is opened **on the API origin**, never through the Next.js host. Through Cloudflare, the documented proxy read timeout is **125 s** and the 15 s keep-alive is well inside it; whether the proxy streams `text/event-stream` unbuffered could not be confirmed from Cloudflare documentation → **EXT-FE-03** (FE-1 preview spike).

## 11. Media and Documents

| Need | Today | Verdict |
|---|---|---|
| Show an inbound image/audio/video/document | `GET /conversations/{cid}/media/{mid}` streams bytes with bearer, `Content-Disposition: attachment`, `X-Content-Type-Options: nosniff`, `Cache-Control: private, no-store`; unknown id → 404 | works with `fetch` + bearer → `Blob` → `URL.createObjectURL` (revoked on unmount and on sign-out); `<img src>` cannot carry a bearer |
| **Know which media belongs to a message** | `MessageRead` has **no** media id, type, size, filename, caption, voice flag or transcript, although `message_media` holds `message_id, mime_type, filename, byte_size, is_voice, transcript, status, position` | **CONFIRMED MISSING → FEB-03 (P0 for FE-4)** |
| Send media | multipart `file` + `caption`, `Idempotency-Key`; cap `MAX_REQUEST_BYTES` (32 MiB) with a verifiable bearer, `MEDIA_MAX_BYTES` 25 MiB; canonical types: jpeg, png, gif, webp; ogg, mpeg, mp4, amr, aac, wav, webm audio; mp4, 3gpp, webm video; pdf, txt, csv, doc, xls, docx, xlsx, pptx (`app/core/media_types.py:113`) | READY; the composer checks type and 25 MiB before upload |
| Knowledge documents | `POST /knowledge/bases/{id}/documents/upload` (multipart, `file`, `title`, `source ∈ text, markdown, pdf, csv, xlsx, docx, html`, validity dates, supersedes) → `202 {document: {status: "pending", …}}`; > 5 MiB → `413 document_too_large`; binary junk → `422 validation_error` "must not contain NUL characters" (J4) | READY, with lifecycle `pending → processing → ready | failed` (`DocumentStatus`), `error`, `extraction_warnings`, `needs_reindex`, `indexing` |
| Learn a document's status changed | no stream event | **poll** `GET /knowledge/documents/{id}` every 3 s → 15 s backoff while `pending/processing` (SCALE-TRIGGERED past ~50 pending documents per tab) → FEB-11 (P2: `document.updated` event) |

## 12. Subscription, Checkout and the Return

| Step | Contract | Observed |
|---|---|---|
| Plans | `GET /billing/plans` (member) → `PlanRead[] {code, name, description, version, price "0.00", currency "EGP", interval, billing_required, prices[], trial_days, limits, allowed_channel_types, is_custom}` | 200 for a member; **401 anonymous** |
| Summary | `GET /billing/summary` (owner/admin) → `{subscription, entitlements, active_topups, recent_purchases, topups_available, open_offer, payment_required, automatic_renewal}` | 200 owner, 403 member |
| Start checkout | `POST /billing/checkout {plan_code | plan_price_id | invoice_id, idempotency_key}` (owner) → `201 CheckoutStarted {redirect_url, payment_id, invoice_id, amount, currency}` | manual provider: `422 "No payment provider is configured."`; Paymob path proved by `test_paymob_checkout.py` (section 28) |
| Return | Paymob sends the browser to `{APP_PUBLIC_URL}/billing/checkout/return` | frontend route **must** exist at exactly that path (no locale) |
| Outcome | the signed callback (`/webhooks/paymob`) decides; the browser polls `GET /billing/payments/{payment_id}` (owner) → `PaymentRead.status` | no stream event for payments |
| Invoices | `GET /invoices`, `GET /invoices/{id}`, `GET /invoices/{id}/payments` | **no PDF / download route** → FEB-13 (P1; Egyptian e-invoicing is a separate decision) |
| Top-ups | `GET /billing/topups`, `POST /billing/topups/{id}/checkout`, `GET /billing/topup-purchases` | READY |
| Capacity | `GET /billing/channel-capacity`, `POST /billing/channel-capacity/selection` | READY |

**Return page design.** Before redirecting, the app stores `{payment_id, started_at}` in `sessionStorage` (not a credential). The return page shows "Confirming your payment…" and polls the payment every 2 s for 60 s, then every 10 s with a "We'll email you when it's confirmed" message; it never treats Paymob's query string as proof. If `sessionStorage` is empty (another tab), it shows the latest `recent_purchases` from `/billing/summary`. What Paymob's redirect carries could not be read from Paymob's documentation (site is script-rendered; `docs.paymob.com` fails TLS) → **EXT-FE-04**.

## 13. Channel Connection

| Fact | Evidence |
|---|---|
| Connect is an **assisted credential form**: `POST /whatsapp/accounts {phone_number_id, access_token, waba_id?, display_name?}` (owner/admin); ownership is verified with Meta before any slot is taken; the token is sealed with `CREDENTIAL_ENCRYPTION_KEYS` | `app/schemas/whatsapp.py:24`, `whatsapp_account_service.py:95`; J2 `201` |
| A phone number id is unique **across the installation** (`409 "That WhatsApp number is already connected."`) | probe j2 |
| Lifecycle: `active → disabled → released` (`ConnectionStatus`); `POST /whatsapp/accounts/{id}/verify|enable|disable|release`; `GET /whatsapp/accounts/{id}/messaging-limit` | OPENAPI |
| Generic list: `GET /channel-connections` → `{id, channel, external_account_id, status, health, counts_toward_capacity, disabled_reason, disabled_at, ownership_started_at}` | J2 |
| Channels connectable today: **WhatsApp only** (Instagram / Messenger have app-secret settings but no connect route) | walk |
| Embedded Signup: **no backend support**. Meta's flow needs the Facebook JS SDK + a Facebook Login for Business configuration, returns `waba_id`, `phone_number_id` and a **30-second** exchangeable code to the page, and the backend must exchange it for a business token, register the number and subscribe webhooks; Meta also caps onboarding (10 new customers / 7 days before review) and says Embedded Signup v2/v3 were deprecated on 2026-10-15 | Meta docs (section 37) → **LP-FE-01 / FEQ-14** |

**UI shape**: one channel list (`/channel-connections`), one connection detail, and a per-type **connect adapter** (`connect/whatsapp-assisted` now; `connect/whatsapp-embedded`, `connect/instagram` later) — a new channel is a new adapter, not a new screen family. The assisted form must explain where to find the phone number id and a system-user token (copy owned by FEQ-14).

## 14. Errors, Limits and States

**Envelope (runtime):** `{"error": {"code": str, "message": str, "details"?: object, "request_id"?: str}}`. 422 field errors: `details.errors[] = {type, loc: ["body","email"], msg}` — map `loc[1:]` to the form field; `extra_forbidden` (unknown field) is a programming error. `X-Request-ID` is exposed by CORS, so the UI can show it in "Something went wrong (ref …)".

**Codes:** static scan of 645 `raise` sites: **102 carry a specific code**, 543 fall back to the class code (`validation_error` 193, `conflict` 125, `not_found` 79, `unauthenticated` 36, `external_service_error` 35, `dependency_unavailable` 31, `permission_denied` 25 …). 42 distinct codes found (Appendix C). The UI can switch on codes for auth, verification, capacity, documents, topups, rate limits, streams and conflicts of state; elsewhere it would have to show a generic translated sentence. **FEB-06** (P1, delivered per stage): every error a first-version screen can show gets a specific code; the stage's prompt lists them.

**429:** auth bucket (10/min/IP; login 5/min/account; reset 3/h/account), workspace bucket (300/min), campaign (30/min), Google, public (`/timezones`). `Retry-After` is not asserted by this plan for every bucket (`retryable_conflict` 503 carries `Retry-After: 1`); the client honours it when present and backs off otherwise.

**Idempotency:** the three send routes take `Idempotency-Key`; a retry with the same key returned **the same message id** (J2, `[11.46]`), including after a first attempt that failed after the row was written. The composer generates one UUID per bubble and reuses it for every retry. Checkout takes `idempotency_key` in the body.

**Backend sentences are never shown.** They are English, and some are wrong for the context (a spent refresh token answers "The email address or password is incorrect."). The UI shows a translated message chosen by code; the English `message` goes to the error report only.

## 15. The Platform Console Surface

74 operations under `/api/v1/platform/**` (Appendix A, app = console). Staff sign in through the same `/auth/login`; the platform role sits on the user (`ProfileResponse.platform_role`). Every staff read writes an audit row (**proved**: two reads → `platform audit rows 71 → 73`, probe j5; ADR-095). Platform-only routes refuse workspace owners (`403 "This action requires platform administration rights."`).

| Console area | Operations (summary) | Gap |
|---|---|---|
| Overview | `GET /platform/overview` (`tenants_total/active/suspended, whatsapp_numbers(+_active), connections_by_channel, usage`, window) | — |
| Tenants | `GET /platform/tenants?search,status,since,until,limit,offset`; suspend / restore / ownership transfer | **no tenant detail** (members, connections, subscription in one read) → FEB-10 (P1); billing has `/platform/billing/tenants/{id}/summary|channel-capacity|channel-connections|capacity-reductions|custom-offers` |
| Users | `POST /platform/users/{id}/disable|enable`, `DELETE /platform/users/{id}` | **no user search / list / detail** → FEB-10 (P0 for the users screen) |
| Invoices / payments | list, detail, record payment, void, refund, reconciliation, incidents | — |
| Plans, prices, versions, features, channel types | full CRUD-ish set incl. preview and migrations | — |
| Top-ups, grants, reductions, custom plans and offers | present (ADR-131/132) | staff acting on a reduction and shortening a grant were deferred in ADR-132 (CORRECT BY DESIGN for v1) |
| Audit logs | `GET /platform/audit-logs` (keyset `before_occurred_at/before_id`) | — |
| Rate cards | upload (multipart) / activate | — |
| Re-authentication for irreversible actions | Google reauth exists for account deletion (`reauthentication_token`); **no reauth proof on platform routes** | FEB-15 (P2) |
| 2FA for staff | none | LP-FE-03 / FEQ-05 (access gate compensates) |

**Console rules**: TanStack Query `staleTime: Infinity`, `refetchOnWindowFocus: false`, `refetchOnReconnect: false`, no prefetch on hover, no polling — every audited read is a click. Irreversible actions (delete user, void invoice, suspend) behind a dialog that requires typing the target's slug/email. No component that renders tenant content is shared with the customer app except primitives.

## 16. The Marketing Site Surface

| Need | Today | Item |
|---|---|---|
| Plan prices, limits, allowed channels | member-only `GET /billing/plans` (401 anonymous) | **FEB-08 (P0 for FE-10)**: `GET /public/plans` — active, non-custom plans and prices only, cacheable, rate-limited by `PublicRateLimit` |
| Channel list | static content (WhatsApp now; others "coming") | — |
| Contact / enterprise inquiry | no route | FEQ-13 (a backend route with a bounded public bucket, or a third-party form) |
| Hand-off to sign-up | link `https://app.<domain>/signup?plan=<code>&lang=<locale>`; the app reads `plan` only as a pre-selection | — |
| Static content | home, features, channels, pricing, about, privacy, terms — supplied by the owner in four languages (FEQ-08, FEQ-15) | — |
| SEO | per-locale URLs `/en /ar /fr /tr`; Astro does **not** emit `hreflang` itself (Astro i18n doc) → the layout emits `<link rel="alternate" hreflang>` + `x-default`; sitemap via `@astrojs/sitemap`; Open Graph per locale; Organization/Product JSON-LD | — |
| Analytics / consent | none in the customer app; marketing only after FEQ-12 | FEQ-12 |

## 17. Architecture and the Monorepo

```text
frontend/
  package.json                 private root; "packageManager": "pnpm@12.x"; engines.node ">=24 <25"
  pnpm-workspace.yaml          apps/*, packages/*
  turbo.json                   tasks: generate, typecheck, lint, test, build, e2e, i18n:check, openapi:check
  .nvmrc                       24
  apps/
    marketing/                 Astro 7, static output, React islands only where interactive
    web/                       Next.js 16 App Router — the customer app
    console/                   Next.js 16 App Router — the platform console
  packages/
    api-client/                openapi.json (committed, exported by FEB-02) → schema.d.ts (generated, committed)
                               client.ts (openapi-fetch + auth middleware + error normaliser)
                               events.ts (hand-written stream frame types, contract-tested)
                               query.ts (query-key factory scoped by workspace + session generation)
    session/                   browser half of S-2: token holder, Web Locks refresh, BroadcastChannel,
                               stream leader election; framework-free, unit-tested
    ui/                        design system: tokens.css, primitives (Radix), composites, icons, Storybook
    i18n/                      messages/{en,ar,fr,tr}/*.json (ICU), locale utils, formatters, key checker
    config/                    tsconfig bases, ESLint flat config, Prettier, Tailwind preset
```

**Dependency direction** (enforced by ESLint `import/no-restricted-paths` and Turborepo's graph):

```text
apps/web ──┐                 apps/console ──┐            apps/marketing ──┐
           ├─► session ─► api-client         ├─► session ─► api-client     ├─► ui ─► i18n ─► config
           ├─► ui ─► i18n ─► config          ├─► ui ─► i18n                └─► i18n
           └─► api-client                    └─► api-client (platform paths only, by lint rule)
No app imports another app. packages never import apps. marketing never imports session or api-client
(except the public-plans type once FEB-08 lands).
```

**Rendering rules.** Server components render layout, translations and static chrome; every authenticated data read is a client component using TanStack Query (PD-01). The only server-side code that talks to the API is the `/session/*` route handlers.

**Beside the Python project.** `frontend/` gets its own `.gitignore` (node_modules, .next, dist, .turbo, .wrangler). The repository `.dockerignore` adds `frontend/` so the backend image does not grow (verified later by image size in FE-1). A separate workflow `.github/workflows/frontend.yml` runs only on `frontend/**` and `scripts/export_openapi.py` changes; the backend workflow ignores `frontend/**` except the committed `openapi.json` check (FEB-02).

## 18. Origins, Domains, CORS and CSP

| Origin (placeholder) | App | Notes |
|---|---|---|
| `https://www.<domain>` (+ apex redirect) | marketing | static |
| `https://app.<domain>` | customer app | S-2 cookie `__Host-wasla_rt` (host-only, never parent-domain) |
| `https://console.<domain>` | console | own cookie `__Host-wasla_crt`; behind FEQ-05 gate |
| `https://api.<domain>` | backend | `CORS_ORIGINS=https://app.<domain>,https://console.<domain>`; Google binding cookie stays host-only on the API **or** is relayed by `/session/google/*` (section 8.3) |

All on one registrable domain (same-site), which `oauth_binding.py` already assumes. **Settings that will hold the domain** (FEQ-04): `APP_PUBLIC_URL=https://app.<domain>`, `GOOGLE_REDIRECT_URI=https://app.<domain>/auth/google/callback`, `CORS_ORIGINS`, Paymob's dashboard callback/redirect (EXT-FE-04), Meta app domains (later), the frontend's `PUBLIC_API_ORIGIN`, `PUBLIC_APP_ORIGIN`, `PUBLIC_MARKETING_ORIGIN`. Local: `http://localhost:3000` (web), `:3001` (console), `:4321` (marketing), `:8000` (API).

**CSP** (set by the host's headers for static files, and by `proxy.ts`/`next.config` headers for rendered responses; Pages `_headers` does not apply to Functions output — Cloudflare doc):

```text
web/console: default-src 'self'; script-src 'self' 'nonce-…'; style-src 'self' 'unsafe-inline';
             connect-src 'self' https://api.<domain>; img-src 'self' blob: data:; media-src 'self' blob:;
             font-src 'self'; frame-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'
             (+ https://connect.facebook.net / *.facebook.com only in the Embedded Signup adapter, later)
marketing:   default-src 'self'; script-src 'self'; img-src 'self' data:; connect-src 'self' https://api.<domain>;
             frame-ancestors 'none'
all: Referrer-Policy strict-origin-when-cross-origin; Permissions-Policy camera=(), microphone=(self), geolocation=();
     X-Content-Type-Options nosniff; Strict-Transport-Security max-age=63072000; includeSubDomains
```

**Previews** call the **staging** API only; staging `CORS_ORIGINS` lists the preview hostname pattern explicitly (the validator refuses wildcards in staging, so previews get a fixed alias such as `https://preview.app.<domain>` rather than per-commit hashes) — production CORS never widens. Preview URLs are public by default; restrict them with Cloudflare Access (Cloudflare docs).

## 19. The Design System

**Principle**: screens compose components; components read tokens; only `tokens.css` holds raw values. A redesign edits `tokens.css` and `packages/ui` styles, and never touches `apps/*`.

**Starting tokens (placeholders the owner will replace):**

| Token | Light | Notes |
|---|---|---|
| `--color-surface` / `--color-surface-raised` / `--color-surface-sunken` | `#FFFFFF` / `#FFFFFF` / `#F5F6F8` | |
| `--color-text` / `--color-text-muted` / `--color-text-inverse` | `#14171F` / `#5B6270` / `#FFFFFF` | muted ≥ 4.5:1 on surface |
| `--color-primary` / `--color-primary-hover` / `--color-on-primary` | `#0F766E` / `#0B5E58` / `#FFFFFF` | teal, ≥ 4.5:1 with white |
| `--color-danger` / `--color-warning` / `--color-success` / `--color-info` | `#B42318` / `#B54708` / `#067647` / `#1D4ED8` | each with `-subtle` background |
| `--color-border` / `--color-border-strong` / `--color-focus` | `#E3E6EB` / `#C4C9D2` / `#2563EB` | focus ring 3:1 against adjacent colours |
| `--font-sans` | `"IBM Plex Sans", "IBM Plex Sans Arabic", system-ui, sans-serif` | Arabic glyphs from the Arabic face |
| `--font-size-{xs,sm,md,lg,xl,2xl,3xl}` | 12 / 14 / 16 / 18 / 20 / 24 / 30 px | Arabic line-height `1.7`, Latin `1.5` via `:lang(ar)` |
| `--space-{0..10}` | 0, 2, 4, 8, 12, 16, 20, 24, 32, 40, 48 px | |
| `--radius-{sm,md,lg,full}` | 4 / 8 / 12 / 9999 px | |
| `--shadow-{sm,md,lg}` | three elevation shadows | |
| `--motion-{fast,base}` / `--ease-standard` | 120 / 200 ms / `cubic-bezier(.2,0,0,1)` | disabled under `prefers-reduced-motion` |
| `--z-{dropdown,sticky,overlay,modal,toast}` | 1000 … 1400 | |
| breakpoints | `sm 640`, `md 768`, `lg 1024`, `xl 1280` | |

Dark theme: a second token block under `[data-theme="dark"]`, delivered when FEQ-09 says so; every component is built against tokens from day one so dark mode is a token file.

**Primitives**: Radix (`radix-ui` 1.8.1) wrapped once in `packages/ui`, with `Direction.Provider dir={dir}` at the root. RTL keyboard and placement are proved per component in FE-1 Storybook + Playwright (EXT-FE-07), because Radix documents direction inheritance but not the per-component behaviours.

**Inventory (first version)**: button, icon button, link, input, textarea, select, combobox/search select, checkbox, radio group, switch, date picker and time picker (Intl-formatted, workspace timezone), file drop zone with progress and pre-checks, form field (label/hint/error, `aria-describedby`), dialog, confirm dialog with typed confirmation, drawer/sheet, popover, tooltip, dropdown menu, tabs, toast (`aria-live=polite`), inline alert / gate banner, badge / status pill, avatar, card, table (sort, empty state, responsive stack), cursor "load more" list + infinite list, skeleton, spinner, empty state, error state (with request id), pagination (limit/offset lists), stepper, sidebar navigation, top bar with workspace switcher, breadcrumb, chat transcript bubble (inbound / outbound / AI / human / system / template / media / failed / pending), composer (text, template picker, attachment, window-closed state from `reply_policy`), channel icon, usage meter, price card, language switcher, OTP code input (6 digits, paste, `autocomplete="one-time-code"`), copy-to-clipboard, connection health pill.

**Rules**: logical properties only (`margin-inline-start`, `inset-inline-end`, `text-align: start`); lint bans physical `left/right` utilities. Icons from one set (Lucide), with a `flipInRtl` list (arrows, chevrons, send, reply, undo/redo) and never flipping logos, media controls, checkmarks or clocks. **Accessibility target WCAG 2.2 AA**, including the four new AA criteria: 2.4.11 Focus Not Obscured (sticky composer and top bar must not hide focus), 2.5.7 Dragging Movements (drop zone has a button), 2.5.8 Target Size (24×24 minimum; the plan uses 40×40 on touch), 3.3.8 Accessible Authentication (paste allowed in OTP and password, no cognitive test). Contrast per 1.4.3 checked from tokens in CI (EXT-FE-08 confirms the ratio text). New inbound messages announced through a polite live region (rate-limited). Catalogue: Storybook 10 (PD-15).

## 20. Localization (English, Arabic, French, Turkish)

| Topic | Decision |
|---|---|
| Libraries | next-intl 4.14 (web, console); Astro built-in i18n (`locales`, `defaultLocale: 'en'`, `routing.prefixDefaultLocale: true`, `fallback`) for marketing; one ICU catalogue in `packages/i18n` |
| Locale in URL | marketing: yes. Apps: `localePrefix: 'never'` + cookie `wasla_locale`; next-intl disables alternate hreflang in this mode, which is fine for unindexed apps |
| Stored preference | **none in the backend** (`users` has no locale column; `tenants` has `timezone` but no locale/currency/country) → FEB-09 (P1): `users.locale` (en/ar/fr/tr) read/written via `/auth/me`; emails rendered in it. Until then: cookie per device |
| Emails | English only today (templates are code in `email_templates.py`) → FEB-09 |
| Fallback | English for a missing key at runtime; CI fails if any key is missing in ar/fr/tr (`i18n:check`) |
| Plurals | ICU `plural` with all CLDR categories: Arabic **zero, one, two, few, many, other**; French **one, many, other**; Turkish **one, other** (EXT-FE-09 confirms Turkish from the verbatim chart) |
| Direction | `<html lang dir>` set on the server from the cookie before first paint; Arabic `dir="rtl"`; no client-side flip |
| User content | every customer message, name, note, document title, template text and lead field rendered inside `<bdi>` or with `dir="auto"` (MDN: "should be used for data with an unknown directionality"); `unicode-bidi: isolate` on transcript bubbles |
| Numbers / dates / money | `Intl.NumberFormat(locale, {style:'currency', currency})` with the currency from the API (`"EGP"` today); `Intl.DateTimeFormat` in the **workspace timezone** (`WorkspaceRead.timezone`, e.g. `Africa/Cairo`, `utc_offset`) for business times and the viewer's zone for "seen at"; relative time via `Intl.RelativeTimeFormat` |
| Arabic digits | `ar-EG` defaults to Arabic-Indic digits (MDN); `-u-nu-latn` or `numberingSystem: 'latn'` forces Latin → FEQ-07 |
| Turkish casing | `toLocaleUpperCase('tr')`/`toLocaleLowerCase('tr')` for labels and search folding (İ/i, I/ı); `Intl.Collator('tr')` for sorting; never CSS `text-transform: uppercase` on Turkish strings |
| Backend sentences | never displayed; codes map to keys (section 14) |
| Copy ownership | FEQ-08 (who writes/reviews ar/fr/tr) and FEQ-07 (Arabic register: MSA vs Egyptian) |

## 21. State and Data

| State | Where | Rule |
|---|---|---|
| Server data | TanStack Query only | key `[wsId, sessionGen, resource, params]`; sign-out / switch bumps `sessionGen` and `queryClient.clear()` |
| Cursor lists | `useInfiniteQuery` on `next_cursor`; stop on `null`, never on a short page; never construct cursors (malformed → 422) | |
| Bare-array lists | members, invitations, agents (`limit`), invoices (`limit`), templates, plans | SCALE-TRIGGERED: agents/invoices beyond 100 rows; show "showing first 100" |
| Optimistic send | bubble with `pending`, key = `Idempotency-Key`; reconciled by the 201 and by `message.created`/`message.status` (dedupe on message id); retry reuses the key — proved not to duplicate | |
| Open conversation, filters, tabs | URL search params | never a token, phone or text in the URL |
| Composer draft | per conversation, in memory + `sessionStorage` (per tab), cleared on send and sign-out | |
| Access token, stream leader | `packages/session` in memory | never persisted |
| Kept across reload | URL + locale cookie + refresh cookie | |

## 22. Security of the Browser Applications

- No token in `localStorage`, `sessionStorage`, IndexedDB or a script-readable cookie (S-2).
- Customer content is untrusted: React text rendering only; **no `dangerouslySetInnerHTML` anywhere** (lint rule); links in messages rendered inert with an explicit "open link" confirmation showing the full URL; media only as blob URLs with `nosniff` already set server-side.
- CSRF: only `/session/*` accept a cookie; `SameSite=Strict` + exact `Origin` check + `X-Wasla-Session` header + `Sec-Fetch-Site: same-origin` (OWASP: Fetch Metadata with Origin fallback).
- Next.js hardening: no server actions (PD-03), no authorization in `proxy.ts` (PD-02), image optimisation off (`unoptimized`; several critical 2026 advisories were in the Image Optimization API: GHSA-2464-fhr4-w9xw, GHSA-2xp9-vwfh-vxw4, GHSA-cjq9-62q9-8jv4), `next/og` unused, dev MCP endpoint never exposed; keep Next.js on the latest 16.x patch (Active LTS) — Renovate/Dependabot weekly, security releases same day.
- Env vars: nothing secret under `NEXT_PUBLIC_`/`PUBLIC_`; CI fails on names matching `SECRET|KEY|TOKEN|PASSWORD` under those prefixes, except the allowlisted public origins.
- Console: separate origin and cookie, access gate (FEQ-05), no third-party script, typed confirmations, FEB-15 for re-authentication.
- Dependencies: one lockfile, pinned versions, `pnpm audit` in CI (fail on high/critical in production deps), no install at runtime.
- Privacy: error reports and logs carry route templates, codes and request ids — never message text, names, phone numbers, emails, tokens or query strings.

## 23. Testing and the Frontend CI

| Layer | Tool | Covers |
|---|---|---|
| Unit | Vitest 5.1 + Testing Library | query keys, gate rules, formatters (ar/fr/tr), refresh lock, stream leader/backoff, error-code mapping |
| Contract | MSW 3.0 handlers typed from `paths`; `events.ts` checked against an exported list of `EVENT_TYPES` | a mock cannot drift from the generated types |
| E2E | Playwright 1.65 against the **real backend** in `wasla-fe-e2e-*` containers (API + worker + PG + Redis, fake email/Meta/Paymob/Google as in this plan's launcher, promoted to a test-only compose profile by FE-0) | J1–J10 (section 28) in **en and ar**, desktop 1280 and phone 390 widths |
| Accessibility | `@axe-core/playwright` 4.14 on every screen state | WCAG 2.2 AA rules |
| Visual | Playwright screenshots of Storybook stories in en/ar, local baseline, no paid service (FEQ-11) | RTL regressions |
| i18n | key completeness for all four catalogues; ICU syntax check | |
| Drift | regenerate `schema.d.ts` from committed `openapi.json`; fail on diff | |

`.github/workflows/frontend.yml` (created in FE-1, **not now**): triggers on `frontend/**`; jobs `install (pnpm, cached)` → `generate + drift` → `typecheck` → `lint` → `unit` → `i18n:check` → `build (3 apps)` → `e2e (backend containers, en+ar)` → `axe`. Green = all jobs pass. It never runs or edits the backend workflow; the backend CI only gains the `openapi.json` freshness check (FEB-02).

## 24. Responsive and Mobile

Breakpoints from tokens. Inbox: **≥1024 px** three panes (list | conversation | customer panel); **768–1023** two panes, customer panel as a drawer; **<768** three steps (list → conversation → panel) with the URL tracking the step so Back works. Touch targets 40×40; composer stays above the on-screen keyboard (`visualViewport` resize, `interactive-widget=resizes-content`); weak networks: the stream banner, send queue with retry, no auto-retry of non-idempotent calls. PWA: installable manifest only if FEQ-10 says so; no push (audit 13's NTQ decisions first). Browser matrix recommendation (FEQ-10): last 2 versions of Chrome, Edge, Firefox, Safari; iOS Safari 16.4+; Android Chrome current — all have Web Locks and BroadcastChannel (Baseline 2022, MDN).

## 25. Observability

Error reporting with redaction (no tokens, message text, PII, query strings); private source maps uploaded at build; Web Vitals per app sampled at 10 %; each report carries the backend `X-Request-ID` of the failing call (exposed by CORS today — no FEB needed). Provider is FEQ-12 (recommendation: Sentry EU region or self-hosted GlitchTip, chosen by data-location preference). No third-party analytics in the customer app or console.

## 26. Deployment

**Facts (section 37):** Cloudflare's Next.js guide now says "Cloudflare recommends vinext as the default way to run Next.js applications on Cloudflare Workers" (vinext is a Vite reimplementation of the Next.js API, **beta**); OpenNext (`@opennextjs/cloudflare` 1.20.16) is listed for existing OpenNext apps; Pages is for **static exports** of Next.js. OpenNext supports Next.js 16 (all minors), App Router, route handlers, middleware, SSR/SSG/ISR; **not** Node middleware (15.2+) and **not** the edge runtime; requires `nodejs_compat` (on by default from compatibility date 2026-08-04). Workers: no hard wall-clock limit for HTTP requests while the client is connected; CPU 30 s default on Paid (10 ms Free); request body limited by the zone plan (100 MB Free/Pro); worker size 64 MiB (Cloudflare limits page, 2026-10-29). KV is eventually consistent (≥60 s) and unsuitable for locks; Durable Objects are strongly consistent and available on Free. Astro static sites deploy to Workers static assets **without** an adapter. Proxy read timeout 125 s.

| | **Cloudflare Workers** (OpenNext for web/console, static assets for marketing) | Vercel | Self-hosted Node containers behind the existing nginx |
|---|---|---|---|
| Next.js 16 App Router + route handlers | yes (OpenNext); vinext beta | native | native (`next start`) |
| S-2 needs (3 route handlers, cookies, `node:crypto` HMAC) | yes (`node:crypto` supported) | yes | yes |
| SSE | not through the host (browser → API) | same | same; could also pass through nginx |
| Static Astro | static assets, no adapter | yes | nginx |
| Previews per branch | yes (+ Access to restrict) | yes | manual |
| Data location / Egypt latency | global edge; API latency set by audit 20 | global edge | same host as the API |
| Ops load for one person | low | lowest | highest (patching Node, TLS, scaling) |
| Cost at v1 scale | not verified (EXT-FE-05) | not verified (EXT-FE-05) | server cost only |
| Risk | adapter lag behind Next.js security patches (EXT-FE-02) | vendor lock on Next features | Next.js self-hosted advisories (cache poisoning, response injection — GHSA-4jqv-mc3x-m676, GHSA-pc58-hwjq-p385) apply directly |

**Recommendation (FEQ-01):** marketing → Cloudflare **Workers static assets** (Pages also works for a static site); web and console → Cloudflare **Workers with OpenNext**, because the apps keep almost all logic in the browser and need only three route handlers, which makes the adapter's surface small; keep **self-hosted Node behind nginx** as the documented fallback if the FE-1 spike (EXT-FE-02) fails or OpenNext lags a security release by more than a week. vinext is re-evaluated when it leaves beta. Environment variables per environment: `PUBLIC_API_ORIGIN`, `PUBLIC_APP_ORIGIN`, `PUBLIC_MARKETING_ORIGIN`, `SESSION_FORWARD_SECRET` (FEB-01, secret), `COOKIE_NAME`, error-reporting DSN (public) — secrets in Wrangler secrets / GitHub Actions secrets, never in the repo.

## 27. Screen Inventory

Columns: **ID · Name · Route · Who · Operations (method path, `/api/v1` omitted) · Events · States beyond loading/error · FEB · FEQ · Build status · Stage · Version.** Routes are unprefixed in the apps (PD-11); marketing routes exist under `/en /ar /fr /tr`.

States legend: E empty · NF not found · F forbidden · U unverified · S suspended · P payment required / past_due · T trial · C over capacity · A allowance exhausted · O offline / stream down.

### 27.1 Customer app (`apps/web`) — first version

| ID | Name | Route | Who | Operations | Events | States | FEB | FEQ | Status | Stage |
|---|---|---|---|---|---|---|---|---|---|---|
| APP-AUTH-01 | Sign up | `/signup` | anon | POST auth/register, GET timezones, then /session/login | — | rate-limited, email taken (202 regardless — always "check your inbox") | 01, 06 | 03 | BUILD AFTER BACKEND | FE-2 |
| APP-AUTH-02 | Log in | `/login` | anon | /session/login → POST auth/login | — | wrong credentials, 429, workspace_slug unknown | 01 | 03 | BUILD AFTER BACKEND | FE-2 |
| APP-AUTH-03 | Google login + callback | `/login` button, `/auth/google/callback` | anon | /session/google/authorize|callback → POST auth/google/authorize|callback | — | disabled (404 → hide button: **FEB-16** public auth config), cancelled, binding missing | 01, 16 | 03 | BUILD AFTER BACKEND | FE-2 |
| APP-AUTH-04 | Verify email (code) | `/verify` | bearer, unverified | POST auth/email/verification/send|verify, GET auth/me | — | expired, wrong code, resend cooldown | 06 | — | READY TO BUILD | FE-2 |
| APP-AUTH-05 | Forgot / reset password | `/forgot`, `/reset-password?token=` | anon | POST auth/password-reset/request|confirm | — | token invalid/used | 06 | — | READY TO BUILD | FE-2 |
| APP-AUTH-06 | Accept invitation | `/invitations/accept?token=` | anon or bearer | POST invitations/accept, then /session/login, then verify | — | expired, used, already member; **invitee is unverified after accepting** (J3) | 17 | — | READY TO BUILD (FEB-17 improves) | FE-2 |
| APP-WS-01 | Create workspace | `/workspaces/new` | verified | POST workspaces, GET timezones | — | slug taken, limit | 06 | — | READY TO BUILD | FE-2 |
| APP-WS-02 | Choose / switch workspace | `/workspaces`, switcher | bearer | GET auth/me, /session/switch → POST auth/workspace | closed: workspace_switched | none, S per workspace | 07 | — | BUILD AFTER BACKEND | FE-2 |
| APP-WS-03 | Onboarding checklist | `/` (first run) | owner/admin | derived: GET channel-connections, agents, knowledge/bases, workspace/members, billing/summary | — | each step done/undone | 18 | — | READY TO BUILD (derived; FEB-18 for cross-device skips, P3) | FE-3 |
| APP-SHELL | App shell | all | member | GET auth/me, billing/entitlements, billing/summary (owner/admin), events ticket+stream | all | U S P T C A O | 07 | 06 | BUILD AFTER BACKEND | FE-3 |
| APP-HOME | Home | `/` | member | GET conversations (counts by page), usage (owner/admin), leads/statistics | message.created | E | — | — | READY TO BUILD | FE-3 |
| APP-CH-01 | Channels list | `/channels` | member (read), owner/admin (act) | GET channel-connections, billing/channel-capacity | — | E, C | — | 14 | READY TO BUILD | FE-3 |
| APP-CH-02 | Connect WhatsApp (assisted) | `/channels/connect/whatsapp` | owner/admin | POST whatsapp/accounts | — | ownership unverified (`whatsapp_ownership_unverified`), already connected (409), C (`channel_capacity_exceeded`, `channel_type_not_allowed`) | 06 | 14 | READY TO BUILD | FE-3 |
| APP-CH-03 | Connection detail | `/channels/:id` | member / owner/admin | GET whatsapp/accounts, whatsapp/accounts/{id}/messaging-limit; POST …/verify|enable|disable|release; POST billing/channel-capacity/selection | — | disabled by reduction, released | — | — | READY TO BUILD | FE-3 |
| APP-INBOX-01 | Inbox list + filters | `/inbox` | member | GET conversations (cursor, priority, channel, connection_id, updated_since, ETag), events | message.created, conversation.updated | E, O | **04, 05** | — | BUILD AFTER BACKEND | FE-4 |
| APP-INBOX-02 | Conversation | `/inbox/:id` | member | GET conversations/{id}, …/messages (cursor), POST …/messages (Idempotency-Key), …/messages/template, …/messages/media, …/mode, …/assignment (expected_assigned_to_id), …/priority, …/close, …/reopen; GET templates (composer picker); GET …/media/{mid} | message.created, message.status, conversation.updated | window closed (`reply_policy.out_of_window` → template only), opted out (`recipient_opted_out`), NF, O | **03**, 04, 06 | — | BUILD AFTER BACKEND | FE-4 |
| APP-INBOX-03 | Customer panel | in conversation | member | GET contacts?q= (workaround), leads?conversation_id=, leads/{id} | conversation.updated | no lead | **04, 19** | — | BUILD AFTER BACKEND | FE-4 |
| APP-CUS-01 | Contacts list + detail | `/contacts`, `/contacts/:id` | member | GET contacts (q, account_id, cursor); POST/DELETE contacts/{id}/opt-out | — | E, opted out | **19** (contact detail/update/history) | — | BUILD AFTER BACKEND | FE-5 |
| APP-CUS-02 | Leads list | `/leads` | member | GET leads (status, source, assigned_to_id, unassigned, tag, search, cursor), leads/statistics | — | E | — | — | READY TO BUILD | FE-5 |
| APP-CUS-03 | Lead detail | `/leads/:id` | member | GET leads/{id}, …/notes, …/activity; PATCH leads/{id}; POST …/status, …/score, …/assignment, …/notes; POST leads | — | NF | 06 | — | READY TO BUILD | FE-5 |
| APP-AI-01 | AI agent settings | `/ai/agents`, `/ai/agents/:id` | owner/admin (write), member (read) | GET/POST agents, GET/PATCH agents/{id}, POST …/default, GET agents/available-tools, GET/PUT agents/{id}/tools, DELETE …/tools/{name} | — | no default agent (**J2: a new workspace answers nothing until one exists**), plan_limit_exceeded (agents) | 06 | — | READY TO BUILD | FE-6 |
| APP-KB-01 | Knowledge bases | `/knowledge` | owner/admin | GET/POST knowledge/bases, GET/PATCH/DELETE …/{id}, GET/PATCH knowledge/settings, GET knowledge/gaps, POST knowledge/search (test) | — | E, `knowledge_base_not_empty` | — | — | READY TO BUILD | FE-6 |
| APP-KB-02 | Documents | `/knowledge/:baseId` | owner/admin | GET …/documents (cursor), POST …/documents (text), …/documents/upload (multipart), PUT documents/{id}/content(/upload), POST …/ingest (retry), DELETE documents/{id}, GET documents/{id}/similar | (poll) | pending/processing/ready/failed, `document_too_large`, `document_format_mismatch`, `document_limit_exceeded`, duplicates | 11 | — | READY TO BUILD | FE-6 |
| APP-TEAM-01 | Members | `/team` | member (read), owner/admin (act), owner (transfer) | GET workspace/members (include_revoked), DELETE …/{user_id}, POST …/{user_id}/reinstate, POST workspace/ownership | closed: membership_revoked (revoked user) | seats exhausted (plan_limit_exceeded) | — | — | READY TO BUILD | FE-7 |
| APP-TEAM-02 | Invitations | `/team/invitations` | owner/admin | GET/POST invitations, DELETE invitations/{id} | — | pending/expired | — | — | READY TO BUILD | FE-7 |
| APP-SET-01 | Workspace settings | `/settings/workspace` | owner/admin | GET/PATCH workspace (name, slug, timezone, automation_disclosure), GET workspace/local-time?at= | — | — | 09 | — | READY TO BUILD | FE-7 |
| APP-SET-02 | Workspace activity log | `/settings/activity` | owner/admin | GET audit-logs | — | E | — | — | READY TO BUILD | FE-7 |
| APP-SET-03 | Close workspace | `/settings/workspace/close` | owner | DELETE workspace | — | retention 30 days | — | — | READY TO BUILD | FE-7 |
| APP-ACC-01 | Account settings | `/settings/account` | bearer | GET auth/me, POST auth/password, auth/password/set, auth/identities/google/authorize|link, DELETE auth/identities/google, /session/logout-all, DELETE auth/me (+ google reauth) | — | Google-only account (set password) | 09, 16 | — | READY TO BUILD | FE-7 |
| APP-BILL-01 | Plans and subscription | `/billing` | owner/admin (read), owner (act) | GET billing/summary, billing/subscription, billing/plans, billing/custom-offers; POST billing/subscription, …/plan, …/cancel, …/resume, …/scheduled-change/cancel; POST billing/custom-offers/{id}/accept|decline | — | T P, scheduled change, open offer | — | — | READY TO BUILD | FE-8 |
| APP-BILL-02 | Checkout + return | `/billing/checkout`, `/billing/checkout/return` | owner | POST billing/checkout; GET billing/payments/{id} | (poll) | pending, failed, uncertain | 12 | — | READY TO BUILD (EXT-FE-04) | FE-8 |
| APP-BILL-03 | Payment methods | `/billing/payment-methods` | owner | GET billing/payment-methods, POST …/{id}/default, DELETE …/{id} | — | E | — | — | READY TO BUILD | FE-8 |
| APP-BILL-04 | Invoices | `/billing/invoices` | owner/admin | GET invoices, invoices/{id}, invoices/{id}/payments; POST billing/payments/{id}/refund | — | E | 13 | — | READY TO BUILD (PDF later) | FE-8 |
| APP-BILL-05 | Usage & AI allowance | `/billing/usage` | owner/admin | GET usage, usage/daily, billing/entitlements | — | A | — | — | READY TO BUILD | FE-8 |
| APP-BILL-06 | Capacity, top-ups, grants | `/billing/capacity` | owner/admin | GET billing/channel-capacity, billing/topups, billing/topup-purchases; POST billing/topups/{id}/checkout, billing/channel-capacity/selection | — | reduction pending, C | — | — | READY TO BUILD | FE-8 |
| APP-404 / APP-ERR | Not found / error | — | any | — | — | request id shown | — | — | READY TO BUILD | FE-1 |

**Second version (contract recorded, not staged in detail):** APP-CMP-* campaigns (`/campaigns/**`, 24 operations, `If-Match`, MARKETING templates only, contact import blocked by design until CMQ-07), APP-FUP-* follow-ups (`/follow-ups/**`, `/workspace/follow-up-settings`, quiet hours), APP-TPL-* template management (`/templates`, `/templates/sync`, opt-out payloads, `/template-header-media`), APP-ANL-* analytics (`/analytics`, `/analytics/conversations/{id}/events`). Status **LATER (v2)**.

### 27.2 Console (`apps/console`)

| ID | Name | Route | Who | Operations | FEB | Status | Stage |
|---|---|---|---|---|---|---|---|
| CON-01 | Sign in | `/login` | staff | /session/login (console) → POST auth/login, GET auth/me (`platform_role`) | 01 | BUILD AFTER BACKEND | FE-9 |
| CON-02 | Overview | `/` | staff | GET platform/overview | — | READY TO BUILD | FE-9 |
| CON-03 | Tenants + detail | `/tenants`, `/tenants/:id` | staff | GET platform/tenants; GET platform/billing/tenants/{id}/summary|channel-capacity|channel-connections; POST platform/tenants/{id}/suspend|restore|ownership | 10 | BUILD AFTER BACKEND (detail) | FE-9 |
| CON-04 | Users | `/users` | staff (authority) | POST platform/users/{id}/disable|enable, DELETE platform/users/{id} | **10** (search/list) , 15 | BUILD AFTER BACKEND | FE-9 |
| CON-05 | Invoices, payments, reconciliation, incidents | `/billing/invoices` … | staff | platform/billing/invoices*, payments*, reconciliation*, incidents*; platform/invoices/{id}/payments|void | 15 | READY TO BUILD | FE-9 |
| CON-06 | Plans, versions, prices, features | `/plans` | staff | platform/billing/plans*, plan-versions*, prices*, features | — | READY TO BUILD | FE-9 |
| CON-07 | Channel types | `/channel-types` | staff | GET platform/billing/channel-types | — | READY TO BUILD | FE-9 |
| CON-08 | Top-ups | `/topups` | staff | platform/billing/topups*, topup-purchases* | — | READY TO BUILD | FE-9 |
| CON-09 | Grants | `/tenants/:id/grants` | staff | POST platform/billing/tenants/{id}/topups/grant | — | READY TO BUILD | FE-9 |
| CON-10 | Capacity reductions | `/reductions` | staff | GET platform/billing/capacity-reductions*, tenants/{id}/capacity-reductions | — | READY TO BUILD (read; acting deferred by ADR-132) | FE-9 |
| CON-11 | Subscriptions | `/subscriptions` | staff | platform/billing/subscriptions*, timeline, cancel, change-plan, resume | — | READY TO BUILD | FE-9 |
| CON-12 | Custom plans & offers | `/tenants/:id/offers` | staff | platform/billing/tenants/{id}/custom-plan(/preview), custom-offers, custom-offers/{id}/cancel | — | READY TO BUILD | FE-9 |
| CON-13 | Audit logs | `/audit` | staff | GET platform/audit-logs | — | READY TO BUILD | FE-9 |
| CON-14 | WhatsApp rate cards | `/rate-cards` | staff | platform/whatsapp-rate-cards* | — | READY TO BUILD | FE-9 |

### 27.3 Marketing (`apps/marketing`), each in en / ar / fr / tr

| ID | Page | Data | FEB / FEQ | Status | Stage |
|---|---|---|---|---|---|
| MKT-01 | Home | static | FEQ-08 | BLOCKED ON DECISION (copy) | FE-10 |
| MKT-02 | Features | static | FEQ-08 | BLOCKED ON DECISION | FE-10 |
| MKT-03 | Channels | static | FEQ-08 | BLOCKED ON DECISION | FE-10 |
| MKT-04 | Pricing | `GET /public/plans` | **FEB-08** | BUILD AFTER BACKEND | FE-10 |
| MKT-05 | About | static | FEQ-08 | BLOCKED ON DECISION | FE-10 |
| MKT-06 | Contact / enterprise | route or form | FEQ-13 | BLOCKED ON DECISION | FE-10 |
| MKT-07 | Privacy | static | FEQ-15 | BLOCKED ON DECISION | FE-10 |
| MKT-08 | Terms | static | FEQ-15 | BLOCKED ON DECISION | FE-10 |
| MKT-09 | Not found | static | — | READY TO BUILD | FE-10 |

## 28. Critical Journeys J1–J10 (results)

All on the running backend (`127.0.0.1:8911`, migration-built `wasla-fep-pg`, 2026-11-07), plus the repository suites run against `wasla-fep-pg/wasla_test` and `wasla-fep-redis` (per-file: Google endpoints 36 passed; Paymob checkout 38; knowledge upload routes 12; invitation endpoints 8; events stream 14; events capacity 1; refresh reuse 20; RAG ingestion lifecycle 32; events shutdown signal 5 skipped on Windows — they are the Linux OS-signal cases CI runs). An earlier combined run showed 32 failures + 32 errors that were all `ConnectionRefused localhost:6379` — suites that default to the developer Redis when `WASLA_TEST_REDIS_URL` is unset; with it set, all pass. **Not a product defect; recorded for audit 24.**

| # | Journey | Result | Evidence / why |
|---|---|---|---|
| J1 | sign up → code → verify → create workspace → home | **PASSED** | register 202, login 200, `/conversations` 403 `email_verification_required`, verify 200, same token then 200; workspace 2 created 201 |
| J2 | connect number → customer writes → stream → fetch → AI or handoff → owner replies | **PASSED WITH FEB-03, FEB-04** (AI turn not walked) | connect 201; webhook 200 and `message.created` on the stream ~10 ms earlier; conversation + messages 200; takeover 200 + `conversation.updated`; reply 201 + `message.status sent`; retry with same key → same id. **AI not walked**: the new workspace has no default agent (`agent.no_active_default` in the worker log) and no OpenAI key is used in this plan; the AI turn is covered by `tests/integration/ai_harness.py` suites in backend CI (green). What would settle it at runtime: FE-0's e2e compose profile with the deterministic Responses fake and a seeded default agent |
| J3 | invite → accept from the email link → role-limited → revoke → stream closes, next request refused | **PASSED WITH FEB-07, FEB-17** | invite 201; link `http://localhost:3000/invitations/accept?token=…`; accept 200 creates an **unverified** account (403 everywhere until it verifies); member: invite/billing/checkout 403, conversations 200; revoke 200; stream `closed membership_revoked` +12 s; next request **404** |
| J4 | upload document → statuses → AI uses it | **PASSED (upload, statuses) / AI use not walked** | upload 202 `pending`; 413 `document_too_large`; 422 for binary; lifecycle and retrieval proven by `test_rag_ingestion_lifecycle.py` (32 passed) and `test_knowledge_upload_routes.py` (12 passed); no stream event → poll (FEB-11) |
| J5 | plan → checkout → return → callback → entitlements change | **PASSED VIA SUITE; runtime walk impossible here** | with the manual provider checkout answers 422 "No payment provider is configured."; the Paymob path (intention, redirect URL, signed callback, idempotency) is proven by `test_paymob_checkout.py` (38 passed). Real browser return: EXT-FE-04 |
| J6 | two tabs refresh at once | **PASSED (behaviour proven) — design required** | without single-flight: one 200, one 401, then the winner's access token 401 → full sign-out. With the recommended design only one refresh is ever in flight per browser (Web Locks); proven in FE-2's Playwright journey |
| J7 | switch workspace → old stream closes → nothing of the old workspace with the new token | **PASSED WITH A DESIGN NOTE** | new token reads ws1 conversation → 404; ws2 list empty; the old **token** still reads ws1 until expiry; the old **stream** closes only when a stream is opened on the new workspace (`workspace_switched`) — the client closes its own stream on switch |
| J8 | staff suspends → app and stream show suspended → restore | **PASSED WITH FEB-07** | suspend 200; both streams `closed workspace_unavailable` +6 s; API 403 `permission_denied` "This workspace is not available."; `/auth/me` still 200 with no status; restore 200 → 200 |
| J9 | Google login end to end | **PASSED VIA SUITE; runtime: disabled path only** | runtime: `/auth/google/authorize` 404 "Google sign-in is not available."; the flow with binding cookie, PKCE, state, nonce, link/unlink, reauth: `test_google_endpoints.py` 36 passed. Real Google: EXT-FE-10 |
| J10 | private media/document in the browser | **PASSED WITH FEB-03** | route returns attachment + `private, no-store`; unknown → 404; a browser can render it via fetch→blob, but cannot find the media id from the message (FEB-03) |

## 29. Stages

Each stage is implemented later by **its own prompt**, ends with something the owner can open, and is sized S / M / L (no dates).

**FE-0 — Backend prerequisites for the frontend (backend stage, S–M)**
- Goal: the backend changes FE-1 and FE-2 need, and the e2e harness the frontend's journeys run against.
- Items: **FEB-01** (client IP from the frontend tier for auth/public buckets), **FEB-02** (OpenAPI export script, declared error envelope, stable operationIds, committed `openapi.json` check), and a test-only compose profile `e2e` with the fakes this plan's launcher used (FakeOwnershipVerifier, fake Graph, fake Paymob, fake Google, deterministic Responses, mail capture that survives sending).
- Acceptance: `scripts/export_openapi.py` reproduces the committed document byte-for-byte; every non-2xx response in the document references `ErrorEnvelope`; a request through the frontend tier with a valid signature is counted under the browser's IP and an unsigned `X-Wasla-Client-IP` is ignored (tests); `docker compose --profile e2e up` serves J1–J10.
- Tests: backend unit + integration for each item. Risks: FEB-01 must not open a spoofing path. Owner sees: an exported `openapi.json` and a green backend CI.

**FE-1 — Foundation (L)**
- Screens: APP-404/ERR, an empty signed-out shell in web and console, a placeholder marketing home.
- Builds: monorepo (section 17), generated client + drift check, `packages/ui` tokens + 15 primitives with Storybook in en/ar, `packages/i18n` with four catalogues and the key check, `packages/session` skeleton, Vitest/MSW/Playwright/axe harness, `frontend.yml`, preview deployment of the three apps on the chosen host.
- Waits for: FEB-02, FEQ-01, FEQ-03 (FEQ-04 placeholders are fine).
- Acceptance: `pnpm turbo build typecheck lint test i18n:check` green; Storybook RTL stories for dialog, menu, popover, select, tabs, combobox pass keyboard tests in ar; the SSE spike (EXT-FE-03) and OpenNext spike (EXT-FE-02) recorded; backend image size unchanged.
- Risks: adapter vs Next.js patch lag. Owner sees: three preview URLs, a Storybook.

**FE-2 — Authentication, session and workspace (M)**
- Screens: APP-AUTH-01…06, APP-WS-01, APP-WS-02. Operations: section 27. Waits for: FEB-01, FEQ-03, FEQ-06 (FEB-16 optional; a build-time flag hides the Google button meanwhile).
- Acceptance: J1, J6 (two tabs refresh 50 times concurrently without sign-out), J9 (fake Google) green in en and ar at 1280/390; refresh token never readable by page script (Playwright `document.cookie` and storage checks); CSRF checks on `/session/*` (cross-origin POST refused).
- Tests: session package unit tests (lock, broadcast, backoff), MSW for error codes, Playwright journeys, axe.

**FE-3 — App shell, gates and channels (M)**
- Screens: APP-SHELL, APP-HOME, APP-WS-03, APP-CH-01…03. Waits for: **FEB-07**, FEQ-14.
- Acceptance: each gate in section 9 has a screen state reproduced in e2e (unverified, suspended via console API, member without billing, capacity reached, reduction); connect / disable / enable / release with the fake verifier.

**FE-4 — The realtime inbox (L)**
- Screens: APP-INBOX-01…03. Waits for: **FEB-03, FEB-04, FEB-05**.
- Builds: stream leader election, reconnect rules (section 10), event → invalidation map, optimistic send with idempotency, media viewer (blob), composer with window/template rules, phone three-step layout.
- Acceptance: J2 and J10 green; 6 tabs open → one stream; kill the API → banner → recovers with `resync`; switching workspace closes the stream; no duplicate bubble after 10 forced retries.

**FE-5 — Customers (M)** — APP-CUS-01…03; waits for FEB-19 (contact detail/update). Acceptance: lead status/score/notes/assignment flows; Turkish search folding.

**FE-6 — AI agent and knowledge base (M)** — APP-AI-01, APP-KB-01/02; FEB-11 optional. Acceptance: J4 green; upload pre-checks (type, 5 MiB); status polling stops at ready/failed; "no default agent" warning on home until one exists.

**FE-7 — Team and settings (M)** — APP-TEAM-01/02, APP-SET-01…03, APP-ACC-01; FEB-09 for language. Acceptance: J3 green; ownership transfer; account deletion with Google reauth.

**FE-8 — Subscription and payment (M)** — APP-BILL-01…06; waits for FEB-12 (callback base), EXT-FE-04. Acceptance: J5 green against the fake Paymob; the return page never shows success before `PaymentRead.status` says so.

**FE-9 — Platform console (M)** — CON-01…14; waits for **FEB-10**, FEB-15, FEQ-05. Acceptance: J8 from the console UI; no audited read fires without a click (network log asserts request count per screen); typed confirmations.

**FE-10 — Marketing site (M)** — MKT-01…09; waits for **FEB-08**, FEQ-08, FEQ-12, FEQ-13, FEQ-15. Acceptance: four locales, hreflang + sitemap, Lighthouse ≥ 95 performance/accessibility on phone, pricing matches `/public/plans`.

**FE-11 — Hardening and launch readiness (M)** — accessibility pass, native-speaker RTL/translation review, performance budgets (inbox TTI on mid Android), security headers verified on the real host, staging on the chosen host with real Google/Paymob test lanes (EXT-FE-*), J1–J10 green on staging.

**Second version:** FE-12 campaigns, FE-13 follow-ups and templates, FE-14 analytics; notifications after audit 13's NTQ decisions.

**Stage dependency graph and order**

```text
FE-0 ──► FE-1 ──► FE-2 ──► FE-3 ──► FE-4 ──► FE-5 ─┐
                     │        └──► FE-6 ───────────┤
                     │        └──► FE-7 ───────────┼──► FE-11
                     │        └──► FE-8 ───────────┤
                     └──► FE-9 (console; needs FEB-10) ┤
          FE-1 ──► FE-10 (marketing; needs FEB-08 + copy) ┘
Backend: FEB-07 before FE-3 · FEB-03/04/05 before FE-4 · FEB-10 before FE-9 · FEB-08 before FE-10
```

Recommended order: FE-0, FE-1, FE-2, FE-3, FE-4 (the product's heart, earliest real use), then FE-6, FE-8 (an owner must be able to pay before launch), FE-7, FE-5; **FE-9 runs in parallel after FE-2** (shares only `session`, `ui`, `i18n`; needed by staff before the first paying customer to handle suspensions and invoices); **FE-10 runs in parallel after FE-1** once the owner supplies copy and FEB-08 lands (it only needs to be live at launch) — **FEQ-02**. FE-5/6/7/8 can run in parallel with each other after FE-3 if separate people/sessions build them; they touch different routes and only add to `packages/ui`.

## 30. Backend Gap Ledger (FEB-*)

Priority: **P0** blocks a stage · **P1** before release · **P2** strongly recommended · **P3** later.

| ID | Title | Class | Basis | Where observed | Without it | Stage / screens | Pri | Recommended remediation |
|---|---|---|---|---|---|---|---|---|
| FEB-01 | Auth and public rate-limit buckets see the frontend tier's IP | CONFIRMED MISSING | RUNTIME + CODE | `app/api/rate_limits.py:58` (`client_identity` trusts only `TRUSTED_PROXY_IPS`); probe s2: 429 after 8 auth calls from one IP | every user signing in through `/session/*` shares one 10/min bucket → sign-in outage at launch | FE-0 → FE-2, FE-9; APP-AUTH-*, CON-01 | **P0** | accept `X-Wasla-Client-IP` only with a valid HMAC (`SESSION_FORWARD_SECRET`, timestamped, ±60 s) from the frontend tier and use it as the bucket identity; ignore it otherwise; per-account buckets unchanged |
| FEB-02 | OpenAPI as a build artefact | CONFIRMED DEFECT (envelope undeclared) + CONFIRMED MISSING (export) | RUNTIME + OPENAPI | 223 operations declare `HTTPValidationError`; real body is `{"error":…}`; no export script; production serves no document | generated error types are wrong; the client cannot be built from production | FE-0 → FE-1 | **P0** (export, envelope) / P2 (operationIds, `info.version`, stream JSON Schema) | `scripts/export_openapi.py`; an `ErrorEnvelope` component referenced by every non-2xx response (FastAPI `responses=` default on the router); `generate_unique_id_function` = `tag_function` names; publish the event-frame schema; backend CI checks the committed `frontend/packages/api-client/openapi.json` |
| FEB-03 | Media references on messages | CONFIRMED MISSING | OPENAPI + RUNTIME + DB | `MessageRead` keys (J2); `message_media` has `message_id, mime_type, filename, byte_size, is_voice, transcript, status, position` | the inbox cannot show any attachment, voice note or transcript | FE-4; APP-INBOX-02, J10 | **P0** | `MessageRead.media: [{id, kind, mime_type, filename, byte_size, is_voice, transcript_status, transcript?, status, position, caption}]`, no storage keys |
| FEB-04 | Customer identity on conversations | CONFIRMED MISSING | OPENAPI | `ConversationRead.participant = {id, channel, kind}` only; contacts filter by `q`, not by id | inbox rows show no name/number; N extra searches per page | FE-4; APP-INBOX-01/03 | **P0** | `participant.display_name`, `participant.handle` (masked as the workspace may see it), `contact_id`, `lead_id?`; or `GET /contacts?ids=` batch |
| FEB-05 | Inbox views | CONFIRMED MISSING | OPENAPI + CODE | list filters: `priority, channel, connection_id, updated_since`; default excludes closed | no "Mine", "Unassigned", "Closed", "All", search | FE-4; APP-INBOX-01 | P1 | `status=open|pending|closed|all`, `assigned_to=me|none|<id>`, `q=` (name/handle), `mode=ai|human`, keyset-compatible |
| FEB-06 | Specific error codes for first-version screens | CONFIRMED MISSING | CODE (static scan) + RUNTIME | 543 of 645 raises use the class code; refresh replay answers "The email address or password is incorrect." | four-language UI can only show generic messages | every stage | P1 (each stage lists its codes) | add `error_code=` at the raise sites a screen can reach; correct the refresh sentences; a test that every code is in a published list |
| FEB-07 | Gate visibility for every member | CONFIRMED MISSING | RUNTIME | `/auth/me` workspaces carry no status; suspended → generic `permission_denied`; revoked → `not_found`; members get 403 on `/billing/summary` | shell cannot show the right banner; revoked looks like a bug | FE-3; APP-SHELL, APP-WS-02 | **P0** | `WorkspaceSummary.status`; codes `workspace_suspended`, `membership_revoked`; a member-readable gate summary (`payment_required`, `subscription_status`, `trial_ends_at`) without amounts |
| FEB-08 | Public plans | CONFIRMED MISSING | RUNTIME | `GET /billing/plans` anonymous → 401 | marketing pricing hard-coded and drifting from billing | FE-10; MKT-04 | **P0** for FE-10 | `GET /public/plans` (active, non-custom, prices, limits, channels), `PublicRateLimit`, `Cache-Control: public, max-age=300` |
| FEB-09 | Language preference and localized email | CONFIRMED MISSING | DB + CODE | `users` has no locale; `tenants` no locale/currency/country; email templates English only | language follows the device, emails in English for Arabic users | FE-7, FE-11 | P1 (locale + emails) / P3 (workspace currency, country) | `users.locale ∈ {en, ar, fr, tr}`, `PATCH /auth/me`; templates per locale; workspace `default_locale` for customer-facing defaults |
| FEB-10 | Console reads for tenants and users | CONFIRMED MISSING | OPENAPI | no `GET /platform/users*`; no `GET /platform/tenants/{id}` | staff cannot find a user to disable; tenant detail assembled from five billing reads (five audit rows) | FE-9; CON-03, CON-04 | **P0** (users) / P1 (tenant detail) | `GET /platform/users?search=` (email/name, keyset), `GET /platform/users/{id}` (memberships, status, platform role), `GET /platform/tenants/{id}` (members, connections, subscription) — each one audit row |
| FEB-11 | Document status on the stream | CONFIRMED MISSING | CODE | `EVENT_TYPES` has no knowledge event | polling | FE-6 | P2 | `document.updated {document_id, status}` |
| FEB-12 | Paymob callback built on the frontend origin | DERIVED / NEEDS VERIFICATION | CODE | `notification_url = APP_PUBLIC_URL + "/api/v1/webhooks/paymob"` (`app/integrations/billing/__init__.py:80`); with the origin layout of section 18 that is `https://app.<domain>/api/v1/webhooks/paymob` | callbacks land on the Next.js host → payments never recorded unless the host forwards them | FE-8 | P1 (intersection audits 20, 22) | separate `API_PUBLIC_URL` for server callbacks, or document that the app host must route `/api/v1/webhooks/*` to the API |
| FEB-13 | Invoice document | CONFIRMED MISSING | OPENAPI | no PDF/download route | owners cannot keep an invoice | FE-8; APP-BILL-04 | P1 | `GET /invoices/{id}/document` (PDF), tax fields per FEQ |
| FEB-14 | Refresh reuse grace | PRODUCT DECISION (security) | RUNTIME | J6 | the browser lock must be perfect | FE-2 | P2 | 10 s grace returning the same successor; owner + audit 23 decide |
| FEB-15 | Re-authentication for irreversible platform actions | CONFIRMED MISSING | OPENAPI | reauth proof exists only for account deletion | a stolen staff session can delete users at once | FE-9; CON-04/05 | P2 | require `reauthentication_token` (password or Google reauth) on delete user, void invoice, ownership transfer |
| FEB-16 | Public auth configuration | CONFIRMED MISSING | RUNTIME | Google disabled is only discoverable as 404 | Google button shown then fails | FE-2 | P2 (workaround: build-time env flag) | `GET /public/auth-config {google_enabled, password_min_length}` |
| FEB-17 | Invitation acceptance verifies the address | PRODUCT DECISION | RUNTIME (J3) | invitee proves inbox control by the token, then must verify again | extra step for every colleague | FE-2 | P2 | mark `email_verified_at` when an emailed invitation token is redeemed by a new account with the same address |
| FEB-18 | Onboarding state | LATER PRODUCT | CODE | none | checklist derived from data, skips not remembered | FE-3 | P3 | per-workspace onboarding facts |
| FEB-19 | Contact detail, edit, history | CONFIRMED MISSING | OPENAPI | `/contacts` list + opt-out only | customer panel by search; no edit | FE-5; APP-CUS-01, APP-INBOX-03 | P1 | `GET/PATCH /contacts/{id}`, consent/opt-out history, conversations of a contact |

**FE-0 (backend) contains:** FEB-01, FEB-02 (P0 parts), the e2e compose profile. **Before FE-3:** FEB-07. **Before FE-4:** FEB-03, FEB-04 (+ FEB-05 before FE-4 ends). **Before FE-9:** FEB-10. **Before FE-10:** FEB-08. Each is a small backend stage of its own or folded into the backend stage that precedes the frontend stage.

## 31. Product Decisions (FEQ-*)

| ID | Question | Evidence | Recommended default |
|---|---|---|---|
| FEQ-01 | Hosting per app | section 26 | Cloudflare Workers: static assets (marketing), OpenNext (web, console); fallback self-hosted Node behind nginx |
| FEQ-02 | Where console and marketing land | section 29 | console in parallel after FE-2; marketing in parallel after FE-1, live by launch |
| FEQ-03 | Session design | section 8 | S-2 for both apps (console with access gate); S-1 only if zero tokens in script is required |
| FEQ-04 | Domains and origin layout | section 18 | `www`, `app`, `console`, `api` under one registrable domain |
| FEQ-05 | Console gate | sections 15, 22 | Cloudflare Access (email OTP; Google IdP unconfirmed, EXT-FE-11) in front of `console.` + backend auth |
| FEQ-06 | App locale | section 20 | cookie now, account preference after FEB-09; no locale in app URLs |
| FEQ-07 | Arabic digits and register | section 20 | Latin digits in data-dense UI (tables, prices) with Arabic-Indic optional per user later; Modern Standard Arabic for UI copy, Egyptian Arabic only in marketing tone if the owner wants |
| FEQ-08 | Who writes and reviews ar/fr/tr copy | — | a paid native reviewer per language before FE-11; machine drafts allowed in development only |
| FEQ-09 | Dark theme | section 19 | later (tokens ready) |
| FEQ-10 | PWA and browser matrix | section 24 | manifest only (no service worker) in v1; matrix as section 24 |
| FEQ-11 | Catalogue and visual regression | section 23 | Storybook + local Playwright screenshots, no paid service |
| FEQ-12 | Error reporting, analytics, consent | section 25 | Sentry (EU) or GlitchTip; no analytics in apps; marketing analytics only cookieless or after consent |
| FEQ-13 | Inquiry form | section 16 | backend route `POST /public/inquiries` (bounded, rate-limited) → becomes a platform lead; not a third-party form (data stays in Wasla) |
| FEQ-14 | WhatsApp connection in v1 | section 13 | ship the assisted form with a guided explainer; plan Embedded Signup as LP-FE-01 after Meta app review |
| FEQ-15 | Legal pages | section 16 | lawyer-written privacy and terms in en + ar before launch; fr/tr translations before marketing in those markets |
| FEQ-16 | Two workspaces live in two tabs | section 10 | accept today's one-active-stream-workspace rule (ADR-155); show the "opened in another tab" state |
| FEQ-17 | Invoice tax/e-invoicing scope (Egypt ETA) | FEB-13 | decide before FE-8 |

## 32. External Verification (EXT-FE-*)

| ID | Fact | Needed evidence | Stage |
|---|---|---|---|
| EXT-FE-01 | The S-2 route handlers on the chosen host (cookies, `node:crypto` HMAC, Set-Cookie relay for Google) | a real preview deployment with a staging API | FE-1/FE-2 |
| EXT-FE-02 | OpenNext 1.20 runs Next 16.4 App Router with the three route handlers and `proxy.ts` headers; patch lag after Next security releases | preview + a recorded lag over two security releases | FE-1 |
| EXT-FE-03 | SSE through Cloudflare's proxy to the API origin is not buffered and survives > 125 s with 15 s keep-alives | browser EventSource on staging for 30 min, timestamps per frame | FE-1 spike, FE-4 |
| EXT-FE-04 | What Paymob's browser redirect carries and its HMAC; dashboard redirect/callback configuration | Paymob docs were unreadable (script-rendered / TLS mismatch); a Paymob Test integration walk | FE-8 |
| EXT-FE-05 | Hosting prices at v1 scale (Cloudflare Workers Paid, Vercel Pro) | current pricing pages | FEQ-01 |
| EXT-FE-06 | SharedWorker / Web Locks / BroadcastChannel versions on iOS Safari and Android Chrome | MDN compat tables did not render; BrowserStack or device test | FE-1 |
| EXT-FE-07 | Radix per-component RTL keyboard and placement | Storybook + Playwright in ar | FE-1 |
| EXT-FE-08 | WCAG 1.4.3 ratio text (4.5:1 / 3:1) verbatim; Turkish plural categories verbatim | re-read the normative sections | FE-1 |
| EXT-FE-09 | `Intl` currency `EGP` and `latn`/`arab` numbering in target browsers | `Intl.supportedValuesOf` on real devices | FE-1 |
| EXT-FE-10 | Google OAuth with real client and redirect URIs per environment (https, exact match, no fragment) | Google Cloud console + staging walk | FE-2 / FE-11 |
| EXT-FE-11 | Cloudflare Access with Google IdP and free-tier seats | Access settings | FE-9 |
| EXT-FE-12 | Real emails (verification, invitation, reset) land on the real frontend routes | staging with Resend | FE-11 |
| EXT-FE-13 | Fonts and RTL on real Safari iOS and Android Chrome | device test | FE-11 |
| EXT-FE-14 | Meta Embedded Signup (if FEQ-14 changes) incl. the v2/v3 deprecation of 2026-10-15 | Meta app + review | later |

## 33. Later Product (LP-FE-*)

LP-FE-01 WhatsApp Embedded Signup · LP-FE-02 Instagram/Messenger connect adapters · LP-FE-03 staff 2FA / passkeys · LP-FE-04 per-member unread markers and counts · LP-FE-05 AI suggestions/rewrite in the composer · LP-FE-06 conversation notes, tags and mentions · LP-FE-07 web push notifications (after audit 13) · LP-FE-08 dark theme · LP-FE-09 saved inbox views · LP-FE-10 white-label theming via tokens.

## 34. Correct by Design

| Item | Why the plan agrees |
|---|---|
| Bearer-only API, no session cookie (ADR-047, SECURITY.md) | keeps CSRF out of the API; the frontend tier owns its one cookie |
| Logout spends only the refresh token; access tokens expire in ≤ 15 min | bounded; `logout-all` / password change bump `token_version` and kill access tokens at once (`dependencies.py:245`) |
| Replay of a refresh token tears down every session (ADR-039) | the browser serialises refreshes instead |
| Other workspace's id → 404 | the UI shows "not found" |
| Stream carries ids only, never content (RT-02) | refetch is cheap and private |
| One active stream workspace per person (ADR-155) | FEQ-16 |
| Media as `attachment`, `no-store`, through the API | blob URLs in the browser |
| Every platform read audited (ADR-095) | console never prefetches |
| Google callback is a POST from the frontend (ADR-047) with a binding cookie (ADR-066) | relayed by `/session/google/*` |
| Paymob return is not proof of payment | return page polls the payment |
| Campaign templates MARKETING only; contact import blocked until CMQ-07 | v2 screens respect it |

## 35. What Was Kept From and Dropped From FRONTEND_MASTER_PLAN.md

Kept (each re-proved here from code or runtime): TanStack Query with keys scoped by workspace and session generation (J7 shows why); React Hook Form + Zod; Radix-style primitives; Vitest/MSW/Playwright/axe; host-only HttpOnly cookie in the frontend tier with Origin checks; refresh carrying `workspace_slug` (probe: plain refresh returns the default workspace); relaying the Google binding cookie (`oauth_binding.py`); MessageRead lacking media references (still true: FEB-03); contacts/conversation identity gaps (re-proved differently: `/contacts` now exists as a cursor list, but there is no detail and conversations still carry no name — FEB-04, FEB-19); no public plans (still true: FEB-08); no per-member read markers (still true: LP-FE-04).

Dropped as stale or superseded: the single Next.js app with route groups (now three apps, D-01..D-03); polling as the realtime strategy and the "future SSE vs WS" question (the SSE stream exists: ADR-151..157); "no monorepo tool" (D-05); a server-side session store as the default (replaced by S-2, because KV cannot lock and the stream no longer needs a server); the BE-FE-BLOCK-* / FE-* / DEC-* / SCR-* identifiers; its route map; its account-capacity notes (`whatsapp_numbers` is no longer the limit — channel capacity per type, ADR-131/132); its Instagram/Messenger and coexistence screens (not built).

## 36. Intersections for Remaining Audits (18–26)

| Audit | Item |
|---|---|
| 18 Privacy | frontend error reporting redaction; marketing consent (FEQ-12); invitee-verification rule (FEB-17) |
| 19 Observability | `X-Request-ID` exposed and shown in UI errors; correlate frontend reports |
| 20 Deployment | API origin and TLS edge; SSE through the edge (EXT-FE-03); FEB-12 callback base; latency Egypt/Turkey/France |
| 21 Backup / DR | none |
| 22 Config / Environment | `APP_PUBLIC_URL` serves two roles (email links and Paymob callback); `CORS_ORIGINS` for previews; new `SESSION_FORWARD_SECRET` (FEB-01) |
| 23 API Design | FEB-02 (error envelope undeclared — CONFIRMED DEFECT), FEB-05, FEB-06 (refresh sentence), FEB-07 (revoked = 404), operationIds, `info.version`, pagination sentence in `docs/API.md` |
| 24 Testing / CI | suites default to the developer Redis when `WASLA_TEST_REDIS_URL` is unset (64 errors in this plan's first run); `security` workflow failing on 2026-11-06 |
| 25 Documentation truth | `docs/API.md` says only conversations and messages are cursor-paged (15 are) |
| 26 Final | FEB-01 and FEB-12 before launch |

## 37. Official Documentation Review

Access date for every row: **2026-11-07**. Versions from the npm registry show the package's `time.modified` (metadata timestamp, not necessarily the release date of that version).

| # | Fact | Source | Verdict |
|---|---|---|---|
| 1 | Next.js 16.4 is the latest named release (blog post 2026-10-06); `next` 16.4.5 on npm | https://nextjs.org/blog ; npm | CONFIRMED |
| 2 | 16.x Active LTS; 15.x Maintenance LTS (security releases through 2027-01-31); 14.x unsupported | https://nextjs.org/support-policy | CONFIRMED |
| 3 | `proxy.ts` replaces `middleware.ts` in Next 16 | https://nextjs.org/blog/next-16 | CONFIRMED |
| 4 | 12-month advisories incl. 6 middleware/proxy bypasses, 8 server-action/function issues, image-optimisation RCEs (GHSA-2464-fhr4-w9xw, GHSA-2xp9-vwfh-vxw4), next/og RCE (GHSA-vcvr-r3jv-pc5j), self-hosted cache poisoning/response injection | https://github.com/vercel/next.js/security/advisories | CONFIRMED (affected ranges not opened) |
| 5 | Cloudflare recommends vinext (beta) on Workers for Next.js; OpenNext for existing OpenNext apps; Pages for static exports | https://developers.cloudflare.com/workers/framework-guides/web-apps/nextjs/ ; https://developers.cloudflare.com/pages/framework-guides/nextjs/ | CONFIRMED — **changes D-09** |
| 6 | OpenNext supports Next 16 (all minors), 14/15 latest minors; App Router, route handlers, middleware, SSR/SSG/ISR, PPR; no Node middleware, no edge runtime; image optimisation via Cloudflare Images | https://opennext.js.org/cloudflare | CONFIRMED |
| 7 | `nodejs_compat` with compatibility date ≥ 2024-09-23; default-on from 2026-08-04; Wrangler ≥ 3.99 | https://opennext.js.org/cloudflare/get-started ; https://developers.cloudflare.com/workers/runtime-apis/nodejs/ | CONFIRMED |
| 8 | Workers: no hard duration limit for HTTP requests; CPU 10 ms Free / 30 s default Paid; body size by zone plan (100 MB Free/Pro); 64 MiB script | https://developers.cloudflare.com/workers/platform/limits/ | CONFIRMED (OpenNext page says 3/10 MiB — older, contradicted) |
| 9 | KV eventually consistent, ≥ 60 s propagation, not for atomic operations | https://developers.cloudflare.com/kv/concepts/how-kv-works/ | CONFIRMED |
| 10 | Durable Objects strongly consistent storage, Free and Paid | https://developers.cloudflare.com/durable-objects/ | CONFIRMED |
| 11 | `node:crypto` supported on Workers | https://developers.cloudflare.com/workers/runtime-apis/nodejs/ | CONFIRMED |
| 12 | Proxy read timeout **125 s**, idle 900 s | https://developers.cloudflare.com/fundamentals/reference/connection-limits/ ; error 524 page | CONFIRMED (brief said 100 s — CONTRADICTED) |
| 13 | Whether the proxy buffers `text/event-stream` | — | UNCLEAR → EXT-FE-03 |
| 14 | Astro static site on Cloudflare Workers static assets needs no adapter; Cloudflare recommends Workers for new projects | https://docs.astro.build/en/guides/deploy/cloudflare/ | CONFIRMED |
| 15 | Astro i18n options; no automatic hreflang | https://docs.astro.build/en/guides/internationalization/ | CONFIRMED |
| 16 | Pages previews per branch, public by default, restrictable with Access; `_headers` sets CSP for static assets but not Functions output | https://developers.cloudflare.com/pages/configuration/preview-deployments/ ; https://developers.cloudflare.com/pages/configuration/headers/ | CONFIRMED |
| 17 | Cloudflare Access protects a self-hosted hostname, deny-by-default, email OTP; Google IdP and free seat count not confirmed | https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/self-hosted-public-app/ | PARTLY → EXT-FE-11 |
| 18 | next-intl: App Router, `localePrefix` `always`/`as-needed`/`never`, cookie mode, ICU plurals | https://next-intl.dev/docs/routing ; https://next-intl.dev/docs/routing/configuration | CONFIRMED |
| 19 | Radix `Direction.Provider` makes primitives inherit reading direction; per-component RTL behaviours not documented | https://www.radix-ui.com/primitives/docs/utilities/direction-provider | PARTLY → EXT-FE-07 |
| 20 | openapi-fetch multipart via `bodySerializer`; openapi-react-query is a third-party wrapper by the openapi-ts project | https://openapi-ts.dev/openapi-fetch/api ; https://openapi-ts.dev/openapi-react-query/ | CONFIRMED |
| 21 | Versions: astro 7.4.7, @astrojs/cloudflare 14.4.6, @opennextjs/cloudflare 1.20.16, wrangler 4.158.0, next-intl 4.14.9, @tanstack/react-query 5.107.1, react-hook-form 7.91.0, zod 4.6.7, radix-ui 1.8.1, turbo 2.11.10, pnpm 12.18.0, vitest 5.1.0, @playwright/test 1.65.0, msw 3.0.4, storybook 10.6.6, @axe-core/playwright 4.14.0 (MPL-2.0), react 19.3.1, tailwindcss 4.3.5, openapi-typescript 7.13.0, openapi-fetch 0.17.0, @hey-api/openapi-ts 0.99.0, orval 8.46.0, @fontsource/ibm-plex-sans-arabic 5.3.0 (OFL-1.1); all others MIT | https://registry.npmjs.org (via `npm view`) | CONFIRMED |
| 22 | EventSource: no custom headers (`withCredentials` only); reconnect reuses the stored request URL and sends `Last-Event-ID`; `retry` sets the delay; non-200 stops reconnection | https://html.spec.whatwg.org/multipage/server-sent-events.html ; MDN EventSource | CONFIRMED |
| 23 | Web Locks: origin-scoped, shared across tabs, secure contexts, Baseline since 2022-03; BroadcastChannel same-origin across tabs, Baseline 2022-03 | MDN Web Locks API ; MDN BroadcastChannel | CONFIRMED (per-browser versions → EXT-FE-06) |
| 24 | SharedWorker: Baseline since 2026-05 on latest devices; older devices may lack it | MDN SharedWorker | PARTLY → EXT-FE-06 |
| 25 | WCAG 2.2 new AA: 2.4.11, 2.5.7, 2.5.8, 3.3.8; 4.1.1 removed | https://www.w3.org/TR/WCAG22/ | CONFIRMED (1.4.3 ratios from summary → EXT-FE-08) |
| 26 | CLDR cardinal plurals: ar zero/one/two/few/many/other; fr one/many/other; tr one/other | https://www.unicode.org/cldr/charts/latest/supplemental/language_plural_rules.html | CONFIRMED ar, fr; tr from summary → EXT-FE-08 |
| 27 | `Intl.NumberFormat` currency style with ISO 4217 codes; `numberingSystem` / `-u-nu-`; ar uses Arabic-Indic digits by default | MDN Intl.NumberFormat | CONFIRMED (EGP availability → EXT-FE-09) |
| 28 | `dir="auto"` for unknown-direction data; `<bdi>` isolates | MDN `dir`, `<bdi>` | CONFIRMED |
| 29 | OWASP: do not store session identifiers in localStorage | https://cheatsheetseries.owasp.org/cheatsheets/HTML5_Security_Cheat_Sheet.html | CONFIRMED |
| 30 | OWASP CSRF: framework protection, synchronizer token / signed double-submit, Fetch Metadata (`Sec-Fetch-Site`) with mandatory Origin fallback, custom request headers for APIs | https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html | CONFIRMED |
| 31 | `__Host-` cookies: Secure, no Domain, Path=/; SameSite default Lax | MDN Cookies | CONFIRMED (note: `__Host-` requires `Path=/`, so the S-2 cookie uses `Path=/`, not `/session`) |
| 32 | Google web-server OAuth: redirect URI exact match (scheme, case, trailing slash), https except localhost, no fragment; `state` against CSRF; PKCE documented for installed apps | https://developers.google.com/identity/protocols/oauth2/web-server ; …/resources/best-practices | CONFIRMED (https/fragment from summary) |
| 33 | Meta Embedded Signup needs the JS SDK + Facebook Login for Business config; returns `waba_id`, `phone_number_id`, a 30-s code; backend exchanges, registers, subscribes; v2/v3 deprecated 2026-10-15; onboarding caps | https://developers.facebook.com/docs/whatsapp/embedded-signup ; …/implementation | CONFIRMED |
| 34 | Paymob redirect contents, HMAC, callback authority | https://developers.paymob.com/ (script shell) ; https://docs.paymob.com/ (TLS mismatch) | UNCLEAR → EXT-FE-04 |

## 38. Infrastructure and Spikes Created by This Plan

| Kind | Name | Notes |
|---|---|---|
| Worktree / branch | `E:\wasla-fep-plan` / `frontend-plan-20261107` | holds only `FRONTEND_PLAN.md` as a new file |
| Notes folder | `E:\wasla-fep-notes` | `openapi-dev.json`, `inventory.json`, `appendix_a.md`, `error_codes.txt`, probe scripts and JSON transcripts, test logs, `media/`, `plan/` parts |
| Spike | `E:\wasla-fep-notes\spikes\gen` | npm project with the three generators (node_modules ~ hundreds of MB) |
| Containers | `wasla-fep-pg` (127.0.0.1:55911, pgvector/pgvector:pg16), `wasla-fep-redis` (127.0.0.1:56911, redis:7-alpine) | databases `wasla` (probe data) and `wasla_test` (suite schema dropped at session end) |
| Volumes | anonymous volumes of the two containers | removed with `docker rm -v` |
| Images | `pgvector/pgvector:pg16`, `redis:7-alpine` (pulled/already present; shared with other stages — do not remove) | |
| Networks | none created (default bridge) | |
| Processes | the launcher (`fep_server.py`, ports 8911 + 8912) — stopped at the end of the plan | |
| Not touched | `wasla_postgres-data`, the developer Redis, `E:\wasla-verification-scripts`, every other worktree, the owner's untracked files | |

## 39. Scores

**Backend readiness for a browser frontend: 64 / 100**

| Dimension | Score | Deduction reason |
|---|---|---|
| Operation coverage of first-version screens | 7/10 | FEB-03, -04, -05, -19, -10 |
| OpenAPI quality for generation | 6/10 | envelope undeclared, no export, long ids (FEB-02); generates cleanly otherwise |
| Session fit for a browser | 6/10 | IP-keyed auth buckets (FEB-01); race is total (by design, no deduction for the design itself) |
| Realtime fit | 8/10 | ticket model, ids-only events, reasons, replay; −2 no knowledge/payment events, reconnect needs custom client |
| Media and documents | 5/10 | media unreachable from messages (FEB-03) |
| Checkout and return | 7/10 | FEB-12, FEB-13 |
| Gate and entitlement readability | 5/10 | FEB-07 |
| Error code stability | 4/10 | 102/645 coded (FEB-06) |
| Localization support | 5/10 | timezone yes; no user locale, English emails (FEB-09) |
| Console surface | 7/10 | FEB-10, FEB-15 |
| Marketing data | 0/10 → weighted lightly | FEB-08 |
| *Weighted total* | **64** | |

**Plan completeness: 86 / 100**

| Dimension | Score | Deduction |
|---|---|---|
| Every v1 screen inventoried with operations and states | 9/10 | per-screen error-code lists deferred to each stage's prompt |
| Journeys proved on the running backend | 7/10 | AI turn (J2, J4) and Paymob/Google browser returns (J5, J9) proved by suites, not at runtime |
| Session design decided with evidence | 10/10 | |
| Hosting settled or precisely stated | 8/10 | EXT-FE-02/03 spikes pending |
| Design system complete and replaceable | 9/10 | RTL per component unproved (EXT-FE-07) |
| Localization complete for four languages and RTL | 9/10 | copy ownership open (decision, not deducted); EXT-FE-08/09 |
| Stages small, ordered, with acceptance criteria | 9/10 | FE-5…FE-8 cards brief |
| Backend gaps placed | 9/10 | |
| Decisions separated with recommendations | 10/10 | |
| Provider facts sourced and dated | 6/10 | Paymob unreadable; some summary-only facts |
| *Total* | **86** | |

Not deducted: FEQ-*, LP-FE-*, section 34.

## 40. Final Verdict

**FRONTEND PLAN READY — FE-1 CAN START AFTER PREREQUISITES.**

Prerequisites, all small: **FE-0** with **FEB-02** (OpenAPI export + declared envelope; FE-1 needs it) and **FEB-01** (client IP from the frontend tier; FE-2 needs it, and building it with FEB-02 keeps FE-0 one stage); owner answers to **FEQ-01** (hosting) and **FEQ-03** (session design). Every journey was walked or its untested segment is named with what settles it (section 28); none failed.

## 41. Required Final Questions

1. **258** operations, **19** unauthenticated (production shape, dependency walk, 2026-11-07). `docs/API.md` states 258/19 and is right; the brief's 198/17 and 209 are both stale.
2. It generates a client without hand edits (three generators, 0 strict errors), but its error types are **wrong** (`HTTPValidationError` instead of the real envelope). FEB-02: declare `ErrorEnvelope` on every non-2xx, stable operationIds, publish the stream frame schema.
3. From `scripts/export_openapi.py` (FEB-02), committed under `frontend/packages/api-client/openapi.json`, regenerated and diff-checked in both CIs.
4. openapi-typescript + openapi-fetch: smallest, types-only, path-keyed (immune to operationId churn), types the MSW mocks.
5. One refresh wins, the other is a replay, `token_version` rises, and **the winner's new tokens are refused too** — the account is signed out everywhere (J6).
6. Both apps: **S-2** — access token in memory, refresh token in a `__Host-` HttpOnly SameSite=Strict cookie handled by three route handlers, refresh serialised with Web Locks and shared with BroadcastChannel. Backend: FEB-01 (and CORS for the two origins, already supported). Host: route handlers with `node:crypto` and cookies — no store, no lock.
7. Yes — the `/session/*` cookie. Closed by POST-only, `SameSite=Strict`, exact `Origin` check, a custom header forcing preflight and `Sec-Fetch-Site: same-origin`. The API itself stays cookieless apart from the Google binding cookie, which is not a session.
8. `POST /events/ticket` with the bearer (any member) → `EventSource('https://api.<domain>/api/v1/events/stream?ticket=…')` on the API origin. Not through the Next.js host. Survival through Cloudflare's proxy: 125 s timeout vs 15 s keep-alives is fine; buffering unconfirmed (EXT-FE-03).
9. One per browser per workspace (leader tab via Web Locks, BroadcastChannel fan-out), because the limit is 5 per user and the 6th answers 429.
10. `fetch` with bearer → `Blob` → object URL, revoked on unmount/sign-out; documents the same way. Blocked for messages until FEB-03 exposes media ids.
11. No. `/auth/me` + `/billing/entitlements` + `/billing/summary` (owner/admin only); workspace status and payment state are invisible to members (FEB-07).
12. Only partly: 102 of 645 raise sites carry a specific code (FEB-06).
13. User language (`users.locale`), localized email templates, workspace currency/country (later). Workspace timezone exists.
14. WhatsApp only, through the assisted form (phone number id + system-user token, verified with Meta). No Embedded Signup.
15. "Confirming your payment…" while polling `GET /billing/payments/{payment_id}` (id kept in sessionStorage from `CheckoutStarted`); never trusts the redirect.
16. No — 401 (FEB-08).
17. Every platform read writes an audit row (two reads → two rows). The console never prefetches, polls or refetches on focus; reads happen on click; FEB-10 gives single-read detail pages.
18. Yes, with OpenNext (Next 16, App Router, route handlers, middleware; no edge runtime, no Node middleware; `nodejs_compat`); Cloudflare's own first recommendation is now vinext (beta). Limits: CPU 30 s default Paid, no wall-clock limit while connected, 100 MB bodies (Free/Pro zone), 64 MiB script.
19. Marketing: Workers static assets. Web and console: Workers + OpenNext; fallback self-hosted Node behind nginx (FEQ-01).
20. Tokens + Radix-based primitives + ~45 composites listed in section 19; screens never use raw values, so a redesign edits tokens and `packages/ui`.
21. `Direction.Provider` + logical properties only (lint-enforced) + server-set `dir` + Storybook/Playwright RTL tests per component; user text in `<bdi>`/`dir="auto"`.
22. ICU plurals with CLDR categories (ar six, fr three, tr two); `toLocaleUpperCase('tr')` and `Intl.Collator('tr')`; `Intl.NumberFormat(locale, {style:'currency', currency: 'EGP'})` with the digit choice of FEQ-07.
23. READY: 27 customer-app screens (auth except login/Google, workspace create, onboarding, channels, leads, AI, knowledge, team, settings, billing) + 11 console screens + MKT-09. BUILD AFTER BACKEND: APP-AUTH-01/02/03 (FEB-01), APP-WS-02 and APP-SHELL (FEB-07), APP-INBOX-01…03 (FEB-03/04/05), APP-CUS-01 (FEB-19), CON-01/03/04 (FEB-01/10), MKT-04 (FEB-08). BLOCKED ON DECISION: MKT-01…03, 05…08 (copy, legal, inquiry).
24. J1, J3, J6, J7, J8, J10 passed at runtime (J3, J8, J10 with FEB items); J2 and J4 passed except the AI turn (no agent/OpenAI in this plan; suites cover); J5 and J9 passed via the repository suites (38 and 36 tests) — the manual provider and disabled Google prevent a runtime walk here. None failed.
25. P0: FEB-01 (FE-2), FEB-02 (FE-1), FEB-07 (FE-3), FEB-03 and FEB-04 (FE-4), FEB-10 users (FE-9), FEB-08 (FE-10). FE-0 = FEB-01 + FEB-02 + the e2e compose profile with fakes.
26. FE-0 → FE-1 → FE-2 → FE-3 → FE-4 → FE-6 → FE-8 → FE-7 → FE-5 → FE-11; FE-9 parallel after FE-2; FE-10 parallel after FE-1; FE-5…FE-8 parallel after FE-3 if built by separate sessions.
27. Console after FE-2 in parallel (staff must handle suspensions and invoices before the first paying customer); marketing after FE-1 in parallel, live at launch (needs copy, legal text and FEB-08) — FEQ-02.
28. Typecheck, lint, unit, contract drift, i18n completeness, build of three apps, Playwright journeys in en+ar at two widths, axe. A separate `frontend.yml` triggered by `frontend/**`; the backend workflow is untouched except the committed-document check.
29. Section 35.
30. Section 36 (23: FEB-02 envelope defect, FEB-05/06/07; 20: SSE edge, FEB-12; 22: `APP_PUBLIC_URL` dual role, `SESSION_FORWARD_SECRET`, preview CORS).
31. Not tomorrow morning: FE-0's FEB-02 (and FEB-01 for FE-2) must land, and the owner must answer FEQ-01 and FEQ-03. All three are small; with them, FE-1 can start.

---

## Appendix B. Event Stream Vocabulary

| Frame | Fields | Source |
|---|---|---|
| `retry: 3000` | first frame | runtime |
| `: keep-alive` | every `RT_KEEPALIVE_SECONDS` (15) | runtime |
| `message.created` | `conversation_id, message_id, direction (inbound|outbound), channel_type, created_at` | `app/core/events.py:161` |
| `message.status` | `conversation_id, message_id, status (received|pending|sent|delivered|read|failed)` | `:177` |
| `conversation.updated` | `conversation_id, fields ⊆ [status, owner, assignee, priority]` (`owner` = mode) | `:192` |
| `follow_up.updated` | `conversation_id, state` | `:209` |
| `campaign.changed` | `campaign_id` | `:226` |
| `resync` | sent when the replay window no longer holds `Last-Event-ID` | `RESYNC` |
| `closed` | `{reason: token_changed | membership_revoked | workspace_unavailable | workspace_switched | server_restart}` | `app/services/realtime.py:288-296, 323` |
| ids | Redis stream ids (`1794027098619-0`); retention `RT_STREAM_RETENTION_SECONDS` 900 / `RT_STREAM_MAXLEN` 5000 | runtime, `.env.example` |
| errors before the stream | 401 `unauthenticated` (ticket unknown/used/expired), 429 `stream_limit` | runtime |

## Appendix C. Error Code Dictionary (as found)

Static scan of literal `error_code` values in `app/` plus runtime and HTTP-mapped codes (2026-11-07). Internal-only codes (worker/email transport) are marked †.

| Group | Codes |
|---|---|
| Class / HTTP defaults | `validation_error`, `unauthenticated`, `permission_denied`, `not_found`, `conflict`, `plan_limit_exceeded`, `payment_required`, `rate_limited`, `external_service_error`, `dependency_unavailable`, `internal_error`, `bad_request`, `method_not_allowed`, `payload_too_large`, `retryable_conflict` (503 + `Retry-After: 1`) |
| Auth / account | `email_verification_required` |
| Channels / capacity | `channel_capacity_exceeded`, `channel_type_not_allowed`, `channel_capacity_selection_invalid`, `whatsapp_ownership_unverified` |
| Messaging | `recipient_opted_out`, `suppressed`, `template_refused`, `marketing_template_admin_only`, `updated_since_needs_offset` |
| Knowledge | `document_too_large`, `document_format_mismatch`, `document_limit_exceeded`, `document_content_exists`, `document_source_exists`, `document_exists_in_other_knowledge_base`, `knowledge_base_not_empty`, `knowledge_base_delete_incomplete`, `knowledge_search_unavailable`, `indexing_refused`, `embedding_switch_refused`, `switch_cancelled` |
| Billing | `custom_plan_not_available_for_workspace`, `topup_not_available` |
| Follow-ups | `follow_up_settings_version_required` |
| Stream | `stream_limit` |
| Internal † | `provider_exception`, `provider_throttled`, `render_error`, `storage_error`, `timeout`, `transport_error` |

## Appendix A. Full Operation Inventory

Columns: auth (`none`, `bearer (account)`, `bearer + workspace`, `ticket`, `signature`) · verified gate · workspace role (`member` = any active member; `admin,owner`; `owner`) · platform role · route-level entitlement guard · paging · body · app (`web`, `console`, `none`) · first-version screen (section 27; `V2` = second version). Generated from the production-shape dependency walk on 2026-11-07 (`E:\wasla-fep-notes\scripts\inventory.py`).

| # | Method | Path | Auth | Verified | Workspace role | Platform role | Entitlement | Paging | Body | App | Screen |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | GET | `/api/v1/agents` | bearer + workspace | yes | member | - | - | limit | - | web | APP-AI-01 |
| 2 | POST | `/api/v1/agents` | bearer + workspace | yes | admin,owner | - | agents | none | json | web | APP-AI-01 |
| 3 | GET | `/api/v1/agents/available-tools` | bearer + workspace | yes | member | - | - | none | - | web | APP-AI-01 |
| 4 | GET | `/api/v1/agents/{agent_id}` | bearer + workspace | yes | member | - | - | none | - | web | APP-AI-01 |
| 5 | PATCH | `/api/v1/agents/{agent_id}` | bearer + workspace | yes | admin,owner | - | - | none | json | web | APP-AI-01 |
| 6 | POST | `/api/v1/agents/{agent_id}/default` | bearer + workspace | yes | admin,owner | - | - | none | - | web | APP-AI-01 |
| 7 | GET | `/api/v1/agents/{agent_id}/tools` | bearer + workspace | yes | member | - | - | none | - | web | APP-AI-01 |
| 8 | PUT | `/api/v1/agents/{agent_id}/tools` | bearer + workspace | yes | admin,owner | - | - | none | json | web | APP-AI-01 |
| 9 | DELETE | `/api/v1/agents/{agent_id}/tools/{name}` | bearer + workspace | yes | admin,owner | - | - | none | - | web | APP-AI-01 |
| 10 | GET | `/api/v1/analytics` | bearer + workspace | yes | member | - | - | none | - | web | V2 |
| 11 | GET | `/api/v1/analytics/conversations/{conversation_id}/events` | bearer + workspace | yes | member | - | - | limit | - | web | APP-INBOX |
| 12 | GET | `/api/v1/audit-logs` | bearer + workspace | yes | admin,owner | - | - | limit | - | web | APP-SET-02 |
| 13 | POST | `/api/v1/auth/email/verification/send` | bearer (account) | no | - | - | - | none | - | web | APP-AUTH-04 |
| 14 | POST | `/api/v1/auth/email/verification/verify` | bearer (account) | no | - | - | - | none | json | web | APP-AUTH-04 |
| 15 | POST | `/api/v1/auth/google/authorize` | none | no | - | - | - | none | - | web | APP-AUTH-03 |
| 16 | POST | `/api/v1/auth/google/callback` | none | no | - | - | - | none | json | web | APP-AUTH-03 |
| 17 | POST | `/api/v1/auth/google/reauth/authorize` | bearer (account) | no | - | - | - | none | - | web | APP-AUTH-03 |
| 18 | POST | `/api/v1/auth/google/reauth/callback` | bearer (account) | no | - | - | - | none | json | web | APP-AUTH-03 |
| 19 | DELETE | `/api/v1/auth/identities/google` | bearer (account) | no | - | - | - | none | - | web | APP-ACC-01 |
| 20 | POST | `/api/v1/auth/identities/google/authorize` | bearer (account) | no | - | - | - | none | - | web | APP-ACC-01 |
| 21 | POST | `/api/v1/auth/identities/google/link` | bearer (account) | no | - | - | - | none | json | web | APP-ACC-01 |
| 22 | POST | `/api/v1/auth/login` | none | no | - | - | - | none | json | web | APP-AUTH-02 |
| 23 | POST | `/api/v1/auth/logout` | none | no | - | - | - | none | json | web | APP-ACC-01 |
| 24 | POST | `/api/v1/auth/logout-all` | bearer (account) | no | - | - | - | none | - | web | APP-ACC-01 |
| 25 | DELETE | `/api/v1/auth/me` | bearer (account) | no | - | - | - | none | json | web | APP-ACC-01 |
| 26 | GET | `/api/v1/auth/me` | bearer (account) | no | - | - | - | none | - | web | APP-ACC-01 |
| 27 | POST | `/api/v1/auth/password` | bearer (account) | no | - | - | - | none | json | web | APP-ACC-01 |
| 28 | POST | `/api/v1/auth/password-reset/confirm` | none | no | - | - | - | none | json | web | APP-AUTH-05 |
| 29 | POST | `/api/v1/auth/password-reset/request` | none | no | - | - | - | none | json | web | APP-AUTH-05 |
| 30 | POST | `/api/v1/auth/password/set` | bearer (account) | no | - | - | - | none | json | web | APP-ACC-01 |
| 31 | POST | `/api/v1/auth/refresh` | none | no | - | - | - | none | json | web | session |
| 32 | POST | `/api/v1/auth/register` | none | no | - | - | - | none | json | web | APP-AUTH-01 |
| 33 | POST | `/api/v1/auth/workspace` | bearer (account) | yes | - | - | - | none | json | web | APP-SHELL |
| 34 | GET | `/api/v1/billing/channel-capacity` | bearer + workspace | yes | member | - | - | none | - | web | APP-BILL |
| 35 | POST | `/api/v1/billing/channel-capacity/selection` | bearer + workspace | yes | owner | - | - | none | json | web | APP-BILL |
| 36 | POST | `/api/v1/billing/checkout` | bearer + workspace | yes | owner | - | - | none | json | web | APP-BILL |
| 37 | GET | `/api/v1/billing/custom-offers` | bearer + workspace | yes | owner | - | - | none | - | web | APP-BILL |
| 38 | POST | `/api/v1/billing/custom-offers/{offer_id}/accept` | bearer + workspace | yes | owner | - | - | none | json | web | APP-BILL |
| 39 | POST | `/api/v1/billing/custom-offers/{offer_id}/decline` | bearer + workspace | yes | owner | - | - | none | json | web | APP-BILL |
| 40 | GET | `/api/v1/billing/entitlements` | bearer + workspace | yes | member | - | - | none | - | web | APP-BILL |
| 41 | GET | `/api/v1/billing/payment-methods` | bearer + workspace | yes | owner | - | - | none | - | web | APP-BILL |
| 42 | DELETE | `/api/v1/billing/payment-methods/{method_id}` | bearer + workspace | yes | owner | - | - | none | - | web | APP-BILL |
| 43 | POST | `/api/v1/billing/payment-methods/{method_id}/default` | bearer + workspace | yes | owner | - | - | none | - | web | APP-BILL |
| 44 | GET | `/api/v1/billing/payments/{payment_id}` | bearer + workspace | yes | owner | - | - | none | - | web | APP-BILL |
| 45 | POST | `/api/v1/billing/payments/{payment_id}/refund` | bearer + workspace | yes | owner | - | - | none | json | web | APP-BILL |
| 46 | GET | `/api/v1/billing/plans` | bearer + workspace | yes | member | - | - | none | - | web | APP-BILL |
| 47 | GET | `/api/v1/billing/subscription` | bearer + workspace | yes | member | - | - | none | - | web | APP-BILL |
| 48 | POST | `/api/v1/billing/subscription` | bearer + workspace | yes | owner | - | - | none | json | web | APP-BILL |
| 49 | POST | `/api/v1/billing/subscription/cancel` | bearer + workspace | yes | owner | - | - | none | json | web | APP-BILL |
| 50 | POST | `/api/v1/billing/subscription/plan` | bearer + workspace | yes | owner | - | - | none | json | web | APP-BILL |
| 51 | POST | `/api/v1/billing/subscription/resume` | bearer + workspace | yes | owner | - | - | none | - | web | APP-BILL |
| 52 | POST | `/api/v1/billing/subscription/scheduled-change/cancel` | bearer + workspace | yes | owner | - | - | none | - | web | APP-BILL |
| 53 | GET | `/api/v1/billing/summary` | bearer + workspace | yes | owner | - | - | none | - | web | APP-BILL |
| 54 | GET | `/api/v1/billing/topup-purchases` | bearer + workspace | yes | owner | - | - | limit | - | web | APP-BILL |
| 55 | GET | `/api/v1/billing/topups` | bearer + workspace | yes | member | - | - | none | - | web | APP-BILL |
| 56 | POST | `/api/v1/billing/topups/{topup_id}/checkout` | bearer + workspace | yes | owner | - | - | none | json | web | APP-BILL |
| 57 | GET | `/api/v1/campaign-segments` | bearer + workspace | yes | member | - | - | none | - | web | V2 |
| 58 | POST | `/api/v1/campaign-segments` | bearer + workspace | yes | admin,owner | - | - | none | json | web | V2 |
| 59 | DELETE | `/api/v1/campaign-segments/{segment_id}` | bearer + workspace | yes | admin,owner | - | - | none | - | web | V2 |
| 60 | GET | `/api/v1/campaign-segments/{segment_id}` | bearer + workspace | yes | member | - | - | none | - | web | V2 |
| 61 | PATCH | `/api/v1/campaign-segments/{segment_id}` | bearer + workspace | yes | admin,owner | - | - | none | json | web | V2 |
| 62 | GET | `/api/v1/campaigns` | bearer + workspace | yes | member | - | - | cursor | - | web | V2 |
| 63 | POST | `/api/v1/campaigns` | bearer + workspace | yes | admin,owner | - | - | none | json | web | V2 |
| 64 | POST | `/api/v1/campaigns/audience/preview` | bearer + workspace | yes | admin,owner | - | - | none | json | web | V2 |
| 65 | POST | `/api/v1/campaigns/header-media` | bearer + workspace | yes | admin,owner | - | - | none | multipart | web | V2 |
| 66 | GET | `/api/v1/campaigns/header-media/{asset_id}` | bearer + workspace | yes | member | - | - | none | - | web | V2 |
| 67 | GET | `/api/v1/campaigns/header-media/{asset_id}/file` | bearer + workspace | yes | member | - | - | none | - | web | V2 |
| 68 | GET | `/api/v1/campaigns/progress` | bearer + workspace | yes | member | - | - | none | - | web | V2 |
| 69 | GET | `/api/v1/campaigns/summary` | bearer + workspace | yes | member | - | - | none | - | web | V2 |
| 70 | DELETE | `/api/v1/campaigns/{campaign_id}` | bearer + workspace | yes | admin,owner | - | - | none | - | web | V2 |
| 71 | GET | `/api/v1/campaigns/{campaign_id}` | bearer + workspace | yes | member | - | - | none | - | web | V2 |
| 72 | PATCH | `/api/v1/campaigns/{campaign_id}` | bearer + workspace | yes | admin,owner | - | - | none | json | web | V2 |
| 73 | GET | `/api/v1/campaigns/{campaign_id}/activity` | bearer + workspace | yes | member | - | - | cursor | - | web | V2 |
| 74 | POST | `/api/v1/campaigns/{campaign_id}/audience` | bearer + workspace | yes | admin,owner | - | - | none | json | web | V2 |
| 75 | POST | `/api/v1/campaigns/{campaign_id}/cancel` | bearer + workspace | yes | admin,owner | - | - | none | - | web | V2 |
| 76 | GET | `/api/v1/campaigns/{campaign_id}/cost-estimate` | bearer + workspace | yes | admin,owner | - | - | none | - | web | V2 |
| 77 | GET | `/api/v1/campaigns/{campaign_id}/delivery-plan` | bearer + workspace | yes | admin,owner | - | - | none | - | web | V2 |
| 78 | POST | `/api/v1/campaigns/{campaign_id}/duplicate` | bearer + workspace | yes | admin,owner | - | - | none | - | web | V2 |
| 79 | POST | `/api/v1/campaigns/{campaign_id}/pause` | bearer + workspace | yes | admin,owner | - | - | none | - | web | V2 |
| 80 | POST | `/api/v1/campaigns/{campaign_id}/preview-messages` | bearer + workspace | yes | admin,owner | - | - | none | json | web | V2 |
| 81 | GET | `/api/v1/campaigns/{campaign_id}/recipients` | bearer + workspace | yes | member | - | - | cursor | - | web | V2 |
| 82 | GET | `/api/v1/campaigns/{campaign_id}/recipients/export.csv` | bearer + workspace | yes | admin,owner | - | - | none | - | web | V2 |
| 83 | POST | `/api/v1/campaigns/{campaign_id}/schedule` | bearer + workspace | yes | admin,owner | - | - | none | json | web | V2 |
| 84 | GET | `/api/v1/campaigns/{campaign_id}/statistics` | bearer + workspace | yes | member | - | - | none | - | web | V2 |
| 85 | POST | `/api/v1/campaigns/{campaign_id}/test-send` | bearer + workspace | yes | admin,owner | - | - | none | json | web | V2 |
| 86 | GET | `/api/v1/channel-connections` | bearer + workspace | yes | member | - | - | none | - | web | APP-CH-01 |
| 87 | GET | `/api/v1/contacts` | bearer + workspace | yes | member | - | - | cursor | - | web | APP-CUS-01 |
| 88 | DELETE | `/api/v1/contacts/{contact_id}/opt-out` | bearer + workspace | yes | admin,owner | - | - | none | - | web | APP-CUS-01 |
| 89 | POST | `/api/v1/contacts/{contact_id}/opt-out` | bearer + workspace | yes | member | - | - | none | json | web | APP-CUS-01 |
| 90 | GET | `/api/v1/conversations` | bearer + workspace | yes | member | - | - | cursor | - | web | APP-INBOX |
| 91 | GET | `/api/v1/conversations/{conversation_id}` | bearer + workspace | yes | member | - | - | none | - | web | APP-INBOX |
| 92 | POST | `/api/v1/conversations/{conversation_id}/assignment` | bearer + workspace | yes | member | - | - | none | json | web | APP-INBOX |
| 93 | POST | `/api/v1/conversations/{conversation_id}/close` | bearer + workspace | yes | member | - | - | none | - | web | APP-INBOX |
| 94 | DELETE | `/api/v1/conversations/{conversation_id}/follow-up` | bearer + workspace | yes | member | - | - | none | - | web | APP-INBOX |
| 95 | GET | `/api/v1/conversations/{conversation_id}/follow-ups` | bearer + workspace | yes | member | - | - | cursor | - | web | APP-INBOX |
| 96 | GET | `/api/v1/conversations/{conversation_id}/media/{media_id}` | bearer + workspace | yes | member | - | - | none | - | web | APP-INBOX |
| 97 | GET | `/api/v1/conversations/{conversation_id}/messages` | bearer + workspace | yes | member | - | - | cursor | - | web | APP-INBOX |
| 98 | POST | `/api/v1/conversations/{conversation_id}/messages` | bearer + workspace | yes | member | - | - | none | json | web | APP-INBOX |
| 99 | POST | `/api/v1/conversations/{conversation_id}/messages/media` | bearer + workspace | yes | member | - | - | none | multipart | web | APP-INBOX |
| 100 | POST | `/api/v1/conversations/{conversation_id}/messages/template` | bearer + workspace | yes | member | - | - | none | json | web | APP-INBOX |
| 101 | POST | `/api/v1/conversations/{conversation_id}/mode` | bearer + workspace | yes | member | - | - | none | json | web | APP-INBOX |
| 102 | POST | `/api/v1/conversations/{conversation_id}/priority` | bearer + workspace | yes | member | - | - | none | json | web | APP-INBOX |
| 103 | POST | `/api/v1/conversations/{conversation_id}/reopen` | bearer + workspace | yes | member | - | - | none | - | web | APP-INBOX |
| 104 | GET | `/api/v1/events/stream` | ticket | no | - | - | - | none | - | web | APP-INBOX |
| 105 | POST | `/api/v1/events/ticket` | bearer + workspace | yes | member | - | - | none | - | web | APP-INBOX |
| 106 | GET | `/api/v1/follow-ups` | bearer + workspace | yes | member | - | - | cursor | - | web | V2 |
| 107 | POST | `/api/v1/follow-ups` | bearer + workspace | yes | member | - | - | none | json | web | V2 |
| 108 | GET | `/api/v1/follow-ups/{follow_up_id}` | bearer + workspace | yes | member | - | - | none | - | web | V2 |
| 109 | POST | `/api/v1/follow-ups/{follow_up_id}/cancel` | bearer + workspace | yes | member | - | - | none | json | web | V2 |
| 110 | GET | `/api/v1/invitations` | bearer + workspace | yes | admin,owner | - | - | none | - | web | APP-TEAM-02 |
| 111 | POST | `/api/v1/invitations` | bearer + workspace | yes | admin,owner | - | team_members | none | json | web | APP-TEAM-02 |
| 112 | POST | `/api/v1/invitations/accept` | none | no | - | - | - | none | json | web | APP-AUTH-06 |
| 113 | DELETE | `/api/v1/invitations/{invitation_id}` | bearer + workspace | yes | admin,owner | - | - | none | - | web | APP-TEAM-02 |
| 114 | GET | `/api/v1/invoices` | bearer + workspace | yes | owner | - | - | limit | - | web | APP-BILL-04 |
| 115 | GET | `/api/v1/invoices/{invoice_id}` | bearer + workspace | yes | owner | - | - | none | - | web | APP-BILL-04 |
| 116 | GET | `/api/v1/invoices/{invoice_id}/payments` | bearer + workspace | yes | owner | - | - | none | - | web | APP-BILL-04 |
| 117 | GET | `/api/v1/knowledge/bases` | bearer + workspace | yes | member | - | - | cursor | - | web | APP-KB-01/02 |
| 118 | POST | `/api/v1/knowledge/bases` | bearer + workspace | yes | admin,owner | - | - | none | json | web | APP-KB-01/02 |
| 119 | DELETE | `/api/v1/knowledge/bases/{knowledge_base_id}` | bearer + workspace | yes | admin,owner | - | - | none | - | web | APP-KB-01/02 |
| 120 | GET | `/api/v1/knowledge/bases/{knowledge_base_id}` | bearer + workspace | yes | member | - | - | none | - | web | APP-KB-01/02 |
| 121 | PATCH | `/api/v1/knowledge/bases/{knowledge_base_id}` | bearer + workspace | yes | admin,owner | - | - | none | json | web | APP-KB-01/02 |
| 122 | GET | `/api/v1/knowledge/bases/{knowledge_base_id}/documents` | bearer + workspace | yes | member | - | - | cursor | - | web | APP-KB-01/02 |
| 123 | POST | `/api/v1/knowledge/bases/{knowledge_base_id}/documents` | bearer + workspace | yes | admin,owner | - | - | none | json | web | APP-KB-01/02 |
| 124 | POST | `/api/v1/knowledge/bases/{knowledge_base_id}/documents/upload` | bearer + workspace | yes | admin,owner | - | - | none | multipart | web | APP-KB-01/02 |
| 125 | GET | `/api/v1/knowledge/documents` | bearer + workspace | yes | member | - | - | cursor | - | web | APP-KB-01/02 |
| 126 | DELETE | `/api/v1/knowledge/documents/{document_id}` | bearer + workspace | yes | admin,owner | - | - | none | - | web | APP-KB-01/02 |
| 127 | GET | `/api/v1/knowledge/documents/{document_id}` | bearer + workspace | yes | member | - | - | none | - | web | APP-KB-01/02 |
| 128 | PUT | `/api/v1/knowledge/documents/{document_id}/content` | bearer + workspace | yes | admin,owner | - | - | none | json | web | APP-KB-01/02 |
| 129 | PUT | `/api/v1/knowledge/documents/{document_id}/content/upload` | bearer + workspace | yes | admin,owner | - | - | none | multipart | web | APP-KB-01/02 |
| 130 | POST | `/api/v1/knowledge/documents/{document_id}/ingest` | bearer + workspace | yes | admin,owner | - | - | none | - | web | APP-KB-01/02 |
| 131 | GET | `/api/v1/knowledge/documents/{document_id}/similar` | bearer + workspace | yes | member | - | - | none | - | web | APP-KB-01/02 |
| 132 | GET | `/api/v1/knowledge/gaps` | bearer + workspace | yes | admin,owner | - | - | limit | - | web | APP-KB-01/02 |
| 133 | POST | `/api/v1/knowledge/search` | bearer + workspace | yes | admin,owner | - | - | none | json | web | APP-KB-01/02 |
| 134 | GET | `/api/v1/knowledge/settings` | bearer + workspace | yes | admin,owner | - | - | none | - | web | APP-KB-01/02 |
| 135 | PATCH | `/api/v1/knowledge/settings` | bearer + workspace | yes | admin,owner | - | - | none | json | web | APP-KB-01/02 |
| 136 | GET | `/api/v1/leads` | bearer + workspace | yes | member | - | - | cursor | - | web | APP-CUS-02/03 |
| 137 | POST | `/api/v1/leads` | bearer + workspace | yes | member | - | - | none | json | web | APP-CUS-02/03 |
| 138 | GET | `/api/v1/leads/statistics` | bearer + workspace | yes | admin,owner | - | - | none | - | web | APP-CUS-02/03 |
| 139 | GET | `/api/v1/leads/{lead_id}` | bearer + workspace | yes | member | - | - | none | - | web | APP-CUS-02/03 |
| 140 | PATCH | `/api/v1/leads/{lead_id}` | bearer + workspace | yes | member | - | - | none | json | web | APP-CUS-02/03 |
| 141 | GET | `/api/v1/leads/{lead_id}/activity` | bearer + workspace | yes | member | - | - | cursor | - | web | APP-CUS-02/03 |
| 142 | POST | `/api/v1/leads/{lead_id}/assignment` | bearer + workspace | yes | admin,owner | - | - | none | json | web | APP-CUS-02/03 |
| 143 | GET | `/api/v1/leads/{lead_id}/notes` | bearer + workspace | yes | member | - | - | cursor | - | web | APP-CUS-02/03 |
| 144 | POST | `/api/v1/leads/{lead_id}/notes` | bearer + workspace | yes | member | - | - | none | json | web | APP-CUS-02/03 |
| 145 | POST | `/api/v1/leads/{lead_id}/score` | bearer + workspace | yes | member | - | - | none | json | web | APP-CUS-02/03 |
| 146 | POST | `/api/v1/leads/{lead_id}/status` | bearer + workspace | yes | member | - | - | none | json | web | APP-CUS-02/03 |
| 147 | GET | `/api/v1/platform/audit-logs` | bearer (account) | yes | - | admin,owner | - | limit | - | console | APP-SET-02 |
| 148 | GET | `/api/v1/platform/billing/capacity-reductions` | bearer (account) | yes | - | admin,owner | - | limit | - | console | APP-BILL |
| 149 | GET | `/api/v1/platform/billing/capacity-reductions/{reduction_id}` | bearer (account) | yes | - | admin,owner | - | none | - | console | APP-BILL |
| 150 | GET | `/api/v1/platform/billing/channel-types` | bearer (account) | yes | - | admin,owner | - | none | - | console | APP-BILL |
| 151 | POST | `/api/v1/platform/billing/custom-offers/{offer_id}/cancel` | bearer (account) | yes | - | admin,owner | - | none | json | console | APP-BILL |
| 152 | GET | `/api/v1/platform/billing/features` | bearer (account) | yes | - | admin,owner | - | none | - | console | APP-BILL |
| 153 | GET | `/api/v1/platform/billing/incidents` | bearer (account) | yes | - | admin,owner | - | limit | - | console | APP-BILL |
| 154 | POST | `/api/v1/platform/billing/incidents/{incident_id}/resolve` | bearer (account) | yes | - | admin,owner | - | none | json | console | APP-BILL |
| 155 | GET | `/api/v1/platform/billing/invoices` | bearer (account) | yes | - | admin,owner | - | limit | - | console | APP-BILL |
| 156 | GET | `/api/v1/platform/billing/invoices/{invoice_id}` | bearer (account) | yes | - | admin,owner | - | none | - | console | APP-BILL |
| 157 | POST | `/api/v1/platform/billing/invoices/{invoice_id}/payments` | bearer (account) | yes | - | admin,owner | - | none | json | console | APP-BILL |
| 158 | POST | `/api/v1/platform/billing/invoices/{invoice_id}/void` | bearer (account) | yes | - | admin,owner | - | none | json | console | APP-BILL |
| 159 | GET | `/api/v1/platform/billing/payments` | bearer (account) | yes | - | admin,owner | - | limit | - | console | APP-BILL |
| 160 | GET | `/api/v1/platform/billing/payments/{payment_id}` | bearer (account) | yes | - | admin,owner | - | none | - | console | APP-BILL |
| 161 | POST | `/api/v1/platform/billing/payments/{payment_id}/refund` | bearer (account) | yes | - | admin,owner | - | none | json | console | APP-BILL |
| 162 | GET | `/api/v1/platform/billing/plan-versions/{version_id}` | bearer (account) | yes | - | admin,owner | - | none | - | console | APP-BILL |
| 163 | GET | `/api/v1/platform/billing/plan-versions/{version_id}/prices` | bearer (account) | yes | - | admin,owner | - | none | - | console | APP-BILL |
| 164 | POST | `/api/v1/platform/billing/plan-versions/{version_id}/prices` | bearer (account) | yes | - | admin,owner | - | none | json | console | APP-BILL |
| 165 | GET | `/api/v1/platform/billing/plans` | bearer (account) | yes | - | admin,owner | - | limit | - | console | APP-BILL |
| 166 | POST | `/api/v1/platform/billing/plans` | bearer (account) | yes | - | admin,owner | - | none | json | console | APP-BILL |
| 167 | DELETE | `/api/v1/platform/billing/plans/{plan_id}` | bearer (account) | yes | - | owner | - | none | - | console | APP-BILL |
| 168 | GET | `/api/v1/platform/billing/plans/{plan_id}` | bearer (account) | yes | - | admin,owner | - | none | - | console | APP-BILL |
| 169 | PATCH | `/api/v1/platform/billing/plans/{plan_id}` | bearer (account) | yes | - | admin,owner | - | none | json | console | APP-BILL |
| 170 | POST | `/api/v1/platform/billing/plans/{plan_id}/activate` | bearer (account) | yes | - | admin,owner | - | none | json | console | APP-BILL |
| 171 | POST | `/api/v1/platform/billing/plans/{plan_id}/deactivate` | bearer (account) | yes | - | admin,owner | - | none | json | console | APP-BILL |
| 172 | POST | `/api/v1/platform/billing/plans/{plan_id}/migrations` | bearer (account) | yes | - | admin,owner | - | none | json | console | APP-BILL |
| 173 | GET | `/api/v1/platform/billing/plans/{plan_id}/versions` | bearer (account) | yes | - | admin,owner | - | none | - | console | APP-BILL |
| 174 | POST | `/api/v1/platform/billing/plans/{plan_id}/versions` | bearer (account) | yes | - | admin,owner | - | none | json | console | APP-BILL |
| 175 | POST | `/api/v1/platform/billing/plans/{plan_id}/versions/preview` | bearer (account) | yes | - | admin,owner | - | none | json | console | APP-BILL |
| 176 | GET | `/api/v1/platform/billing/prices/{price_id}` | bearer (account) | yes | - | admin,owner | - | none | - | console | APP-BILL |
| 177 | PATCH | `/api/v1/platform/billing/prices/{price_id}` | bearer (account) | yes | - | admin,owner | - | none | json | console | APP-BILL |
| 178 | POST | `/api/v1/platform/billing/prices/{price_id}/retire` | bearer (account) | yes | - | admin,owner | - | none | json | console | APP-BILL |
| 179 | GET | `/api/v1/platform/billing/reconciliation` | bearer (account) | yes | - | admin,owner | - | none | - | console | APP-BILL |
| 180 | POST | `/api/v1/platform/billing/reconciliation/{payment_id}/run` | bearer (account) | yes | - | admin,owner | - | none | - | console | APP-BILL |
| 181 | GET | `/api/v1/platform/billing/subscriptions` | bearer (account) | yes | - | admin,owner | - | limit | - | console | APP-BILL |
| 182 | GET | `/api/v1/platform/billing/subscriptions/{subscription_id}` | bearer (account) | yes | - | admin,owner | - | none | - | console | APP-BILL |
| 183 | POST | `/api/v1/platform/billing/subscriptions/{subscription_id}/cancel` | bearer (account) | yes | - | admin,owner | - | none | json | console | APP-BILL |
| 184 | POST | `/api/v1/platform/billing/subscriptions/{subscription_id}/change-plan` | bearer (account) | yes | - | admin,owner | - | none | json | console | APP-BILL |
| 185 | POST | `/api/v1/platform/billing/subscriptions/{subscription_id}/resume` | bearer (account) | yes | - | admin,owner | - | none | json | console | APP-BILL |
| 186 | GET | `/api/v1/platform/billing/subscriptions/{subscription_id}/timeline` | bearer (account) | yes | - | admin,owner | - | none | - | console | APP-BILL |
| 187 | GET | `/api/v1/platform/billing/tenants/{tenant_id}/capacity-reductions` | bearer (account) | yes | - | admin,owner | - | limit | - | console | APP-BILL |
| 188 | GET | `/api/v1/platform/billing/tenants/{tenant_id}/channel-capacity` | bearer (account) | yes | - | admin,owner | - | none | - | console | APP-BILL |
| 189 | GET | `/api/v1/platform/billing/tenants/{tenant_id}/channel-connections` | bearer (account) | yes | - | admin,owner | - | none | - | console | APP-CH-01 |
| 190 | GET | `/api/v1/platform/billing/tenants/{tenant_id}/custom-offers` | bearer (account) | yes | - | admin,owner | - | none | - | console | APP-BILL |
| 191 | POST | `/api/v1/platform/billing/tenants/{tenant_id}/custom-offers` | bearer (account) | yes | - | admin,owner | - | none | json | console | APP-BILL |
| 192 | POST | `/api/v1/platform/billing/tenants/{tenant_id}/custom-plan` | bearer (account) | yes | - | admin,owner | - | none | json | console | APP-BILL |
| 193 | POST | `/api/v1/platform/billing/tenants/{tenant_id}/custom-plan/preview` | bearer (account) | yes | - | admin,owner | - | none | json | console | APP-BILL |
| 194 | GET | `/api/v1/platform/billing/tenants/{tenant_id}/summary` | bearer (account) | yes | - | admin,owner | - | none | - | console | APP-BILL |
| 195 | POST | `/api/v1/platform/billing/tenants/{tenant_id}/topups/grant` | bearer (account) | yes | - | admin,owner | - | none | json | console | APP-BILL |
| 196 | GET | `/api/v1/platform/billing/topup-purchases` | bearer (account) | yes | - | admin,owner | - | limit | - | console | APP-BILL |
| 197 | GET | `/api/v1/platform/billing/topup-purchases/{purchase_id}` | bearer (account) | yes | - | admin,owner | - | none | - | console | APP-BILL |
| 198 | POST | `/api/v1/platform/billing/topup-purchases/{purchase_id}/refund-review` | bearer (account) | yes | - | admin,owner | - | none | json | console | APP-BILL |
| 199 | POST | `/api/v1/platform/billing/topup-purchases/{purchase_id}/withdraw` | bearer (account) | yes | - | admin,owner | - | none | json | console | APP-BILL |
| 200 | GET | `/api/v1/platform/billing/topups` | bearer (account) | yes | - | admin,owner | - | limit | - | console | APP-BILL |
| 201 | POST | `/api/v1/platform/billing/topups` | bearer (account) | yes | - | admin,owner | - | none | json | console | APP-BILL |
| 202 | DELETE | `/api/v1/platform/billing/topups/{topup_id}` | bearer (account) | yes | - | owner | - | none | - | console | APP-BILL |
| 203 | GET | `/api/v1/platform/billing/topups/{topup_id}` | bearer (account) | yes | - | admin,owner | - | none | - | console | APP-BILL |
| 204 | PATCH | `/api/v1/platform/billing/topups/{topup_id}` | bearer (account) | yes | - | admin,owner | - | none | json | console | APP-BILL |
| 205 | POST | `/api/v1/platform/billing/topups/{topup_id}/activate` | bearer (account) | yes | - | admin,owner | - | none | json | console | APP-BILL |
| 206 | POST | `/api/v1/platform/billing/topups/{topup_id}/deactivate` | bearer (account) | yes | - | admin,owner | - | none | json | console | APP-BILL |
| 207 | POST | `/api/v1/platform/invoices/{invoice_id}/payments` | bearer (account) | yes | - | admin,owner | - | none | json | console | APP-BILL-04 |
| 208 | POST | `/api/v1/platform/invoices/{invoice_id}/void` | bearer (account) | yes | - | admin,owner | - | none | json | console | APP-BILL-04 |
| 209 | GET | `/api/v1/platform/overview` | bearer (account) | yes | - | admin,owner | - | none | - | console | CON-02 |
| 210 | GET | `/api/v1/platform/tenants` | bearer (account) | yes | - | admin,owner | - | limit | - | console | CON-03 |
| 211 | POST | `/api/v1/platform/tenants/{tenant_id}/ownership` | bearer (account) | yes | - | admin,owner | - | none | json | console | CON-03 |
| 212 | POST | `/api/v1/platform/tenants/{tenant_id}/restore` | bearer (account) | yes | - | admin,owner | - | none | - | console | CON-03 |
| 213 | POST | `/api/v1/platform/tenants/{tenant_id}/suspend` | bearer (account) | yes | - | admin,owner | - | none | json | console | CON-03 |
| 214 | DELETE | `/api/v1/platform/users/{user_id}` | bearer (account) | yes | - | admin,owner | - | none | - | console | CON-04 |
| 215 | POST | `/api/v1/platform/users/{user_id}/disable` | bearer (account) | yes | - | admin,owner | - | none | - | console | CON-04 |
| 216 | POST | `/api/v1/platform/users/{user_id}/enable` | bearer (account) | yes | - | admin,owner | - | none | - | console | CON-04 |
| 217 | GET | `/api/v1/platform/whatsapp-rate-cards` | bearer (account) | yes | - | admin,owner | - | none | - | console | CON-14 |
| 218 | POST | `/api/v1/platform/whatsapp-rate-cards` | bearer (account) | yes | - | admin,owner | - | none | multipart | console | CON-14 |
| 219 | GET | `/api/v1/platform/whatsapp-rate-cards/{card_id}` | bearer (account) | yes | - | admin,owner | - | none | - | console | CON-14 |
| 220 | POST | `/api/v1/platform/whatsapp-rate-cards/{card_id}/activate` | bearer (account) | yes | - | admin,owner | - | none | - | console | CON-14 |
| 221 | GET | `/api/v1/template-header-media` | bearer + workspace | yes | member | - | - | cursor | - | web | V2 |
| 222 | POST | `/api/v1/template-header-media` | bearer + workspace | yes | admin,owner | - | - | none | multipart | web | V2 |
| 223 | DELETE | `/api/v1/template-header-media/{asset_id}` | bearer + workspace | yes | admin,owner | - | - | none | - | web | V2 |
| 224 | GET | `/api/v1/templates` | bearer + workspace | yes | member | - | - | none | - | web | V2 (read in composer: APP-INBOX) |
| 225 | POST | `/api/v1/templates/sync` | bearer + workspace | yes | admin,owner | - | - | none | - | web | V2 (read in composer: APP-INBOX) |
| 226 | GET | `/api/v1/templates/{template_id}` | bearer + workspace | yes | member | - | - | none | - | web | V2 (read in composer: APP-INBOX) |
| 227 | PUT | `/api/v1/templates/{template_id}/opt-out-payloads` | bearer + workspace | yes | admin,owner | - | - | none | json | web | V2 (read in composer: APP-INBOX) |
| 228 | GET | `/api/v1/timezones` | none | no | - | - | - | none | - | web | APP-WS-01 |
| 229 | GET | `/api/v1/usage` | bearer + workspace | yes | admin,owner | - | - | none | - | web | APP-BILL-05 |
| 230 | GET | `/api/v1/usage/daily` | bearer + workspace | yes | admin,owner | - | - | none | - | web | APP-BILL-05 |
| 231 | POST | `/api/v1/webhooks/email` | signature | no | - | - | - | none | - | none | - |
| 232 | POST | `/api/v1/webhooks/paymob` | signature | no | - | - | - | none | - | none | - |
| 233 | GET | `/api/v1/webhooks/whatsapp` | signature | no | - | - | - | none | - | none | - |
| 234 | POST | `/api/v1/webhooks/whatsapp` | signature | no | - | - | - | none | - | none | - |
| 235 | GET | `/api/v1/whatsapp/accounts` | bearer + workspace | yes | member | - | - | none | - | web | APP-CH-02/03 |
| 236 | POST | `/api/v1/whatsapp/accounts` | bearer + workspace | yes | admin,owner | - | - | none | json | web | APP-CH-02/03 |
| 237 | POST | `/api/v1/whatsapp/accounts/{account_id}/disable` | bearer + workspace | yes | admin,owner | - | - | none | - | web | APP-CH-02/03 |
| 238 | POST | `/api/v1/whatsapp/accounts/{account_id}/enable` | bearer + workspace | yes | admin,owner | - | - | none | - | web | APP-CH-02/03 |
| 239 | GET | `/api/v1/whatsapp/accounts/{account_id}/messaging-limit` | bearer + workspace | yes | member | - | - | none | - | web | APP-CH-02/03 |
| 240 | POST | `/api/v1/whatsapp/accounts/{account_id}/release` | bearer + workspace | yes | admin,owner | - | - | none | - | web | APP-CH-02/03 |
| 241 | POST | `/api/v1/whatsapp/accounts/{account_id}/verify` | bearer + workspace | yes | admin,owner | - | - | none | json | web | APP-CH-02/03 |
| 242 | DELETE | `/api/v1/workspace` | bearer + workspace | yes | owner | - | - | none | json | web | APP-SET-01 |
| 243 | GET | `/api/v1/workspace` | bearer + workspace | yes | member | - | - | none | - | web | APP-SET-01 |
| 244 | PATCH | `/api/v1/workspace` | bearer + workspace | yes | admin,owner | - | - | none | json | web | APP-SET-01 |
| 245 | GET | `/api/v1/workspace/campaign-settings` | bearer + workspace | yes | member | - | - | none | - | web | V2 |
| 246 | PUT | `/api/v1/workspace/campaign-settings` | bearer + workspace | yes | admin,owner | - | - | none | json | web | V2 |
| 247 | GET | `/api/v1/workspace/follow-up-settings` | bearer + workspace | yes | member | - | - | none | - | web | V2 |
| 248 | PUT | `/api/v1/workspace/follow-up-settings` | bearer + workspace | yes | admin,owner | - | - | none | json | web | V2 |
| 249 | GET | `/api/v1/workspace/local-time` | bearer + workspace | yes | member | - | - | none | - | web | APP-SET-01 |
| 250 | GET | `/api/v1/workspace/members` | bearer + workspace | yes | member | - | - | none | - | web | APP-TEAM-01 |
| 251 | DELETE | `/api/v1/workspace/members/{user_id}` | bearer + workspace | yes | member | - | - | none | - | web | APP-TEAM-01 |
| 252 | POST | `/api/v1/workspace/members/{user_id}/reinstate` | bearer + workspace | yes | admin,owner | - | team_members | none | json | web | APP-TEAM-01 |
| 253 | POST | `/api/v1/workspace/ownership` | bearer + workspace | yes | owner | - | - | none | json | web | APP-TEAM-01 |
| 254 | POST | `/api/v1/workspaces` | bearer (account) | yes | - | - | - | none | json | web | APP-WS-01 |
| 255 | GET | `/health` | none | no | - | - | - | none | - | none | - |
| 256 | GET | `/health/live` | none | no | - | - | - | none | - | none | - |
| 257 | GET | `/health/ready` | none | no | - | - | - | none | - | none | - |
| 258 | GET | `/metrics` | none | no | - | - | - | none | - | none | - |
