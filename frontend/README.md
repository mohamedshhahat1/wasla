# Wasla frontend

The three browser applications and their shared packages (FRONTEND_PLAN.md; FE-1, ADR-192). Hosting and the browser
session: ADR-189. The API contract they are built from: ADR-191.

```text
apps/marketing   Astro 7, static: /en /ar /fr /tr                       http://localhost:4321
apps/web         Next.js 16: the customer app                            http://localhost:3000
apps/console     Next.js 16: the platform console                        http://localhost:3001
packages/api-client  the typed client generated from openapi.json, ApiError, stream events, query keys
packages/session     the browser half of the S-2 session; the server-only forward signer and headers
packages/ui          tokens.css, fonts, the primitives, Storybook
packages/i18n        catalogues (en, ar, fr, tr), locale negotiation, formatters, i18n:check
packages/config      TypeScript, ESLint (with the workspace's own rules), Prettier, the origins per environment
e2e                  Playwright against FE-0's backend profile
```

Who may import whom is enforced by lint: no app imports another app, packages never import apps, marketing never
imports `session` or `api-client`, `ui` and `i18n` import no other package but `config`.

## Install

Node 24 (`.nvmrc`) and pnpm 12.18.0 through corepack - nothing global:

```bash
corepack enable
pnpm install --frozen-lockfile
```

## Commands

Every build reads `WASLA_ENV` (`development`, `preview` or `production`) and the origins of
`packages/config/origins.js`; set `WASLA_ENV=development` locally.

```text
pnpm turbo run build typecheck lint test i18n:check     the whole pipeline
pnpm generate                                           regenerate schema.d.ts from openapi.json (FE-0's export)
pnpm format / pnpm format:check                         Prettier
pnpm --filter @wasla/web dev                            one app in development (also @wasla/console, @wasla/marketing)
pnpm storybook:build && pnpm storybook:test             Storybook, axe on every story, the RTL keyboard proof
pnpm e2e:local                                          build, start the backend profile, run Playwright, take it down
pnpm build:workers:linux -- --env preview               the Workers' outputs built on Linux in Docker (any machine)
pnpm workers:smoke                                      the built Workers served by workerd: pages 200, session routes 405 / 503
```

### Building the Workers on Windows

OpenNext's build fails on Windows ("Access is denied" on pnpm's links, FEF-04). `pnpm build:workers:linux` runs it in
`node:24.15.0-bookworm-slim` (pinned by digest): `frontend/` is mounted read-only, copied without `node_modules`,
installed from the frozen lockfile inside the container, built (OpenNext for web and console, Astro for marketing), and
only `apps/web/.open-next`, `apps/console/.open-next` and `apps/marketing/dist` are copied back, links dereferenced.
`--env` is `development`, `preview` or `production`; `PUBLIC_*` variables set on the host are passed through. Deploying
stays on the host (`pnpm exec wrangler deploy` in the app's folder): the container never sees wrangler's credentials.

The generated client is regenerated from the committed `packages/api-client/openapi.json`, which the backend exports
(`python -m scripts.export_openapi` at the repository root); CI fails when `schema.d.ts` differs from a regeneration.

## The backend for e2e

`pnpm e2e:local` runs `docker compose -f ../docker-compose.e2e.yml --profile e2e up -d --build` from the repository
root, waits for `GET http://127.0.0.1:8000/health`, runs Playwright, then `down -v` whatever happened. Ports: API 8000
(`WASLA_E2E_API_PORT`), mailbox 8025, fakes 8026 - all on 127.0.0.1. See `../tests/e2e_stack/README.md` for the
accounts, the mailbox and the stack's public test secrets.

The e2e build of the customer app sets `PUBLIC_GOOGLE_ENABLED=true` (the fake Google answers), and its `next start`
on :3000 runs with `SESSION_FORWARD_SECRET` = the stack's public test secret and `WASLA_E2E_ROUTES=1` (the test-only
page `/test-only/session`, and the `X-Wasla-E2E-Client-IP` header that gives each test its own signed address). The
FX-07 test restarts the API and runs last, alone (`stream-restart` project).

## Signing in (FE-2, ADR-193)

The customer app signs in through its own route handlers, `POST /session/{login, register-login, refresh, switch,
logout, logout-all, google/authorize, google/callback}` (`apps/web/app/session/`, logic in
`lib/server/session-handlers.ts`). Each accepts only a same-origin JSON POST with `X-Wasla-Session: 1`, signs its call
to the API with the browser's address, and keeps the refresh token in an HttpOnly cookie (`__Host-wasla_rt`;
`wasla_rt` on plain-HTTP localhost). The page holds the access token in memory only (`lib/session/web-session.ts`).

| Variable                 | Where                    | What                                                      |
| ------------------------ | ------------------------ | --------------------------------------------------------- |
| `SESSION_FORWARD_SECRET` | server (a Worker secret) | the API's value; unset → every session route answers 503  |
| `PUBLIC_GOOGLE_ENABLED`  | build                    | `true` shows "Continue with Google"                       |
| `WASLA_E2E_ROUTES`       | server, development only | `1` enables the test-only page and the e2e address header |

To try it locally: start the e2e profile (`docker compose -f ../docker-compose.e2e.yml --profile e2e up -d --build`
from `frontend/`), build with `WASLA_ENV=development PUBLIC_GOOGLE_ENABLED=true pnpm turbo run build --filter=@wasla/web`,
start with `SESSION_FORWARD_SECRET=e2e-not-a-secret-0123456789abcdef0123456789abcdef pnpm --filter @wasla/web exec next
start -p 3000`, open `http://localhost:3000/signup`, and read the code from `http://127.0.0.1:8025/messages`.

**A suspended workspace on a reload (FEF-24, ADR-197).** When the refresh is refused with `403 workspace_suspended`,
the session is not signed out: `refreshOutcome` (in `@wasla/session`) maps it to the state `workspace-suspended`, the
`/session/refresh` route keeps the cookie (the API does not spend the token on that refusal), and every page but the
auth pages shows GT-03's suspended state with "Check again" (another refresh) and "Sign out". Every other refusal signs
out as before.

## Origins

|           | development             | preview                                       | production (configuration only, FEQ-04) |
| --------- | ----------------------- | --------------------------------------------- | --------------------------------------- |
| API       | `http://localhost:8000` | `https://api.iwasla.tech`                     | `https://api.iwasla.tech`               |
| app       | `http://localhost:3000` | the `wasla-preview-web` workers.dev URL       | `https://app.iwasla.tech`               |
| console   | `http://localhost:3001` | the `wasla-preview-console` workers.dev URL   | `https://console.iwasla.tech`           |
| marketing | `http://localhost:4321` | the `wasla-preview-marketing` workers.dev URL | `https://www.iwasla.tech`               |

## The signed-in app (FE-3, ADR-196)

- **The shell** - `apps/web/app/(app)/layout.tsx` renders `components/app/app-frame.tsx` around every signed-in page:
  FE-2's `PageGate` routes first, then `WorkspaceProvider` (keyed by workspace and session generation) and the
  `AppShell` composite. The auth pages, `/verify` and `/workspaces*` keep FE-2's frame.
- **Navigation** - `apps/web/lib/navigation.ts` is the registry: route, label key, icon, roles, and the page file a
  unit test checks exists. A later stage adds its entry together with its screen; nothing else is drawn.
- **Gates** - `apps/web/lib/gates.ts` is the one rule (facts in, full state and banners out); the facts come from
  `components/app/workspace-context.tsx`: /auth/me, `GET /workspace/gates` (read on entering a workspace and on a
  signal only), the AI allowance, the error codes of any call (one middleware on the session's client) and the
  stream's reasons.
- **The stream** - `apps/web/lib/stream-coordinator.ts`: one leader per browser per workspace (Web Lock
  `wasla-stream-<workspace id>`), frames on a BroadcastChannel of the same name, FE-2's `openEventStream`
  unchanged. **FE-4 plugs in** by replacing the entries of `apps/web/lib/stream-handlers.ts` (typed by the event
  vocabulary) - message.created already invalidates home's conversations.
- **Connect adapters** - `apps/web/lib/channels.ts` `CONNECT_ADAPTERS`: FE-3 registers the assisted WhatsApp form
  (`/channels/connect/whatsapp`); Embedded Signup adds an entry and a component beside it.
- **Walking it locally** - bring FE-0's profile up (`docker compose -f docker-compose.e2e.yml --profile e2e up -d`),
  build the apps with `WASLA_ENV=development` and start the web app (`pnpm --dir apps/web exec next start -p 3000`),
  sign up at http://localhost:3000/signup (the code arrives in the mailbox at http://127.0.0.1:8025/messages), connect a
  number whose id starts with `PNID-E2E` (the fake verifier owns those). The gate states the API cannot reach quickly:
  `docker exec wasla-fe-e2e-api python -m tests.e2e_stack.gate_states trialing|past-due|ai-allowance-used <slug> [days]`.

## Changing the look

The design language is [packages/ui/DESIGN.md](packages/ui/DESIGN.md): one blue family and white in three
colour roles (60 % dominant, 30 % secondary, 10 % accent), the buttons, the composites, the motion rules.
Edit `packages/ui/src/tokens.css` (colours, type, space, radius, shadow, motion) and the components in
`packages/ui`. Screens never hold a colour, a size or a spacing value - a lint rule refuses them, and a unit
test refuses the raw blue scale outside `tokens.css`. Every colour token has one role in
`packages/ui/src/tokens.roles.json`.

```text
node scripts/measure-colour-roles.mjs <png | folder>          the role shares of screenshots (VQ-03)
node scripts/measure-colour-roles.mjs --check <folder>        exit 1 outside the bands (frontend.yml does this)
```

## Licences

`node scripts/check-licences.mjs` checks every direct dependency against FE-1's list and every package in the
lockfile against `licence-policy.json` (FEF-06, accepted by the owner on 2026-11-12): a stricter list for the
production closure of the three apps, a wider one for build-only tooling, exceptions for build-only packages only,
by exact name, version and licence. frontend.yml fails on a violation or a stale exception.
