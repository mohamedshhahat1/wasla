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

## Origins

|           | development             | preview                                       | production (configuration only, FEQ-04) |
| --------- | ----------------------- | --------------------------------------------- | --------------------------------------- |
| API       | `http://localhost:8000` | `https://api.iwasla.tech`                     | `https://api.iwasla.tech`               |
| app       | `http://localhost:3000` | the `wasla-preview-web` workers.dev URL       | `https://app.iwasla.tech`               |
| console   | `http://localhost:3001` | the `wasla-preview-console` workers.dev URL   | `https://console.iwasla.tech`           |
| marketing | `http://localhost:4321` | the `wasla-preview-marketing` workers.dev URL | `https://www.iwasla.tech`               |

## Changing the look

Edit `packages/ui/src/tokens.css` (colours, type, space, radius, shadow, motion) and the components in
`packages/ui`. Screens never hold a colour, a size or a spacing value - a lint rule refuses them.
