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
```

The generated client is regenerated from the committed `packages/api-client/openapi.json`, which the backend exports
(`python -m scripts.export_openapi` at the repository root); CI fails when `schema.d.ts` differs from a regeneration.

## The backend for e2e

`pnpm e2e:local` runs `docker compose -f ../docker-compose.e2e.yml --profile e2e up -d --build` from the repository
root, waits for `GET http://127.0.0.1:8000/health`, runs Playwright, then `down -v` whatever happened. Ports: API 8000
(`WASLA_E2E_API_PORT`), mailbox 8025, fakes 8026 - all on 127.0.0.1. See `../tests/e2e_stack/README.md` for the
accounts, the mailbox and the stack's public test secrets.

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
