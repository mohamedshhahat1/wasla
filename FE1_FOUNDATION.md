# FE-1 — Frontend Foundation (Monorepo, Generated Client, Design System, Four Locales With RTL, Test Harness, Frontend CI)

Stage FE-1 of `FRONTEND_PLAN.md`, 2026-11-07. Light gates by the owner's choice (FE-0): the frontend checks of section 8,
a small mutation check, the e2e smoke against FE-0's backend profile, and GitHub's `frontend.yml` and `ci.yml` runs
after the push. **No local run in this report is the backend's full suite; GitHub's `ci.yml` run is.**

## Phase status

| Phase | State | Evidence (E:\wasla-fe1-notes\) | UTC | Commit / merge / push / deployment / removal |
|---|---|---|---|---|
| A0 state, untracked files, infrastructure, versions | done | evidence/untracked_before.txt, logs/image_base_*.log | 10:00–10:25 | worktree `E:\wasla-fe1-fix` (branch `fe1-foundation-20261107` @ 86e3d89); images `wasla-fe1:base`, `wasla-fe1:base-b` |
| A1 workspace + config | done | config tests 24/24 | 10:33 | 038f253 |
| A2 api-client | done | 39 tests; evidence/ac02_drift_proof.txt | 10:35 | 7649e2e |
| A3 ui + Storybook + RTL keyboard | done | evidence/ds07_table.md, storybook_results_run3.json, contact-sheet/, screens/ | 10:40–13:17 | bcfbeee |
| A4 i18n | done | evidence/i18n_check.txt | 13:22 | 8852a7b |
| A5 session + signer | done | session tests | 13:25 | d1a2401 |
| A6 apps and screens | done | logs/turbo_pipeline_2.txt | 13:50 | 00196ea |
| A7 e2e harness | done | e2e/run1..3.log, e2e/run3_matrix.md | 13:57–14:11 | a70bb1e (fix found by e2e), eff6a11 |
| A8 frontend.yml | done | action-validator: VALID | 14:11 | d85afd9 |
| A9 mutation + docs + ADR | done | mutation/results.json, mutation/results-control-M-W06.json | 14:15–14:20 | 897bebc, 7c6a25f (format) |
| A10 diff guard + Part A gates | done | logs/gates_A_fix.txt, evidence/secret_scan.txt | 14:21–14:24 | **FE1_HEAD = 7c6a25f** |
| B verify worktree gates (G-1..G-5, G-7) | done | logs/gates_B_verify.txt, e2e/run_verify.log, logs/image_head.log | 14:24–14:50 | `E:\wasla-fe1-verify` detached @ 7c6a25f |
| B merge + tree equality | done | — | 14:45 | **62bb30b** (parents 86e3d89, 7c6a25f) |
| B final-tree gates (G-2, G-3) | done | logs/gates_B_final.txt | 14:46–14:52 | verify worktree @ 62bb30b |
| PV-03 local stream spike | done | spikes/pv03/ (frames.json, spike.log) | 14:53–15:05 | none (own profile cycle; down -v: 0 left) |
| B report commit, push | pushed with this report (see the addendum) | | | this commit |
| B frontend.yml + ci.yml of the push | recorded in the addendum | | | |
| C previews | EXTERNAL VERIFICATION — not signed in to Cloudflare | `wrangler whoami`: "You are not authenticated" | 10:05 | none |
| D cleanup | not started | | | only after CI GREEN |

(Times UTC; Cairo = UTC+2.)

## 1. Executive Summary

| Item | Result |
|---|---|
| What the owner can open now | the Storybook build (`pnpm storybook:build`, `packages/ui/storybook-static`), the screenshot contact sheet (`E:\wasla-fe1-notes\evidence\contact-sheet\`, 38 sheets, en/ar at 1280 and 390), and the three apps locally (`pnpm --filter @wasla/web dev` etc.) |
| Preview URLs | **none** — this machine is not signed in to Cloudflare (`wrangler whoami`: not authenticated). Part C is EXTERNAL VERIFICATION; no `wrangler login` was run, no token used, nothing created on Cloudflare |
| FEF-* the owner should know first | **FEF-04** OpenNext's build fails on Windows (pnpm symlinks, "Access is denied"); the Workers builds are proved on Linux by `frontend.yml`. **FEF-01** Radix's modal DropdownMenu fails axe (aria-hidden-focus); the wrapper is non-modal. **FEF-07** one high advisory in a dev-only build tool (`glob` via `@opennextjs/cloudflare`). **FEF-06** a few transitive licences outside the list (none direct). DS-07: **46 RTL/LTR keyboard assertions × 3 browsers, all pass** |
| Workspace | pnpm 12.18.0 + Turborepo 2.11.10, Node 24.15.0, one frozen lockfile, 3 apps + 5 packages + e2e; import directions and 7 workspace rules are lint errors, each proved by a fixture |
| Client | generated from FE-0's `openapi.json` (unchanged, sha256 57bed53c…), drift check fails on a one-character edit; every error → `ApiError` by code; FE-0's 14 real error bodies read correctly |
| Design system | tokens.css as a reset Tailwind 4 theme; 16 primitives + 3 composites; 38 stories in en/ar; axe 0 serious/critical on all 76 story×locale runs; contrast 15 pairs ≥ WCAG minimums |
| i18n | en/ar/fr/tr, 78 keys each, ICU plurals checked against CLDR 48; ar/fr/tr are unreviewed drafts; lang/dir in the first bytes of every app |
| Session | one refresh per browser: 50 calls / 5 tabs → 1 (unit); two real tabs × 10 rounds → exactly 10 calls in Chromium, Firefox and WebKit; the signer equals all 7 FE-0 vectors; a signed login on the e2e API → 401×10 then 429 on the signed address, another address untouched |
| E2E | 128 passed, 0 failed (3 browsers × 2 widths × en/ar); 0 requests to other hosts; 0 CSP violations; cookies `wasla_locale` only; storage empty; axe: **no violation of any impact** on FE-1 screens |
| Mutation | M-W01..M-W08 all killed, none hung; control green; tree restored each time |
| Backend image | 623,431,929 bytes at FE1_HEAD vs 623,432,054 at the base (Δ −125; two base builds differ by 4,683) — unchanged |
| Merge | one normal merge commit 62bb30b; merged tree byte-identical to FE1_HEAD |
| Verdict | **see section 29 (final after GitHub's runs, in the addendum)** |

## 2. Source Branches and Exact Heads

| Name | Value |
|---|---|
| FE0_CI_GREEN_HEAD | `86e3d89d94e36dc2f6c2654e90fa006b4474b617` (FE-0's addendum; FE-0's report b338a44; addendum run 42359174353 success) |
| ORIGIN_CANONICAL_START_HEAD | `86e3d89` = origin = local (0 ahead / 0 behind, at start and before the merge) |
| FE1_HEAD | `7c6a25f843d0f69475f586b50a7cdad55f454987` on `fe1-foundation-20261107` |
| CANONICAL_MERGED_HEAD | `62bb30b6c81e1887eac3bce86f91c9d45a270d47`, parents `86e3d89`, `7c6a25f` |
| main | `8e3a1ba5a63651b7b7da3bdf14845f40a9d93667` — untouched |
| Alembic | `0138 (head)`, single — before and after |
| LAST_ADR | ADR-191 → this stage ADR-192 |
| ID mapping | the prompt's `WS-*` → **`WSP-*`** (`WS-0n` already occurs inside the plan's `APP-WS-0n`); all other new prefixes (FEB-30.., FEF-, AC-, DS-, IN-, SS-, AP-, TE-, CI-, PV-, M-W) unused before (grep) |

## 3. Environment

| Tool | Version |
|---|---|
| Node | v24.15.0 (`.nvmrc`; `engines >=24 <25`) |
| pnpm | 12.18.0 via corepack 0.34.6, shim in `E:\wasla-fe1-notes\bin` (nothing installed globally) |
| Turborepo | 2.11.10 (remote cache off; cache pinned to `frontend/.turbo`, see FEF-09) |
| Browsers (Playwright 1.65.0) | Chrome Headless Shell 158.0.8127.18, Firefox 159.0, WebKit 27.2 |
| Docker | 29.8.0, Compose v5.5.1 |
| wrangler | 4.158.0 (local runtime workerd 1.20261104.1) |
| gh | 2.98.0, account mohamedshhahat1 |
| ICU / CLDR (Node) | 78.2 / 48.0 |

Direct dependencies (npm registry, 2026-11-07; publish dates from `npm view <pkg> time`): next 16.4.5, react / react-dom
19.3.1, astro 7.4.7, tailwindcss / @tailwindcss/postcss / @tailwindcss/vite 4.3.5, @tanstack/react-query 5.107.1,
next-intl 4.14.9, radix-ui 1.8.1, openapi-typescript 7.13.0, openapi-fetch 0.17.0, vitest 5.1.0, msw 3.0.4,
@playwright/test 1.65.0, @axe-core/playwright 4.14.0 (MPL-2.0), storybook / @storybook/react-vite 10.6.6,
@opennextjs/cloudflare 1.20.16, wrangler 4.158.0, lucide-react 1.70.0, @fontsource/ibm-plex-sans(-arabic) 5.3.0
(OFL-1.1), @testing-library/react 16.3.3, @testing-library/jest-dom 7.0.1, @testing-library/user-event 14.6.8,
jsdom 31.0.0, vite 8.3.6, esbuild 0.28.2, @formatjs/icu-messageformat-parser 3.5.22, @astrojs/check 0.9.12,
eslint 9.39.5, typescript-eslint 8.72.1, eslint-plugin-react 7.37.5, eslint-plugin-react-hooks 7.1.1,
eslint-plugin-jsx-a11y 6.10.2, eslint-plugin-import-x 4.18.0, globals 17.14.0, prettier 3.9.10, typescript 5.9.3,
@types/node 24.19.2, @types/react(-dom) 19.3.0. Every direct dependency is MIT, ISC, Apache-2.0, BSD, OFL-1.1 or
MPL-2.0 (evidence/licenses_all.json).

| Plan major moved since 2026-11-07? | Kept | Newer, recorded for the owner |
|---|---|---|
| TypeScript | 5.9.3 | 7.0.2 (and 6.0.3): typescript-eslint 8.72.1 accepts `<6.1`, openapi-typescript `^5` |
| ESLint | 9.39.5 | 10.14.0: eslint-plugin-react 7.37.5 accepts `^9.7`, jsx-a11y `^9` |
| others | the plan's majors | none moved |

`react-hook-form` and `zod` are **not installed** (no FE-1 use; FE-2 adds them, WSP-05). pnpm 12 wrote
`minimumReleaseAgeExclude` entries for eslint-plugin-import-x 4.18.0 and the tailwind 4.3.5 family (published inside
its default minimum release age) — kept, visible in `pnpm-workspace.yaml`.

`pnpm audit`: production — **0 high / critical**, 1 moderate (`minimatch` GHSA-g3hv-75wg-m249, via typescript-eslint and
@opennextjs); all — + 1 high and 1 moderate in `glob` via `@opennextjs/cloudflare` (dev, build-time; FEF-07).

## 4. Decisions Applied, and Decisions NOT Taken

| Decision | Applied | Deviation (recorded) |
|---|---|---|
| WSP-01 pins | Node 24.15.0, pnpm 12.18.0, turbo 2.11.10, exact versions, one lockfile | pnpm 12 reads its settings from `pnpm-workspace.yaml` (`saveExact`, `shellEmulator`, `allowBuilds`); `.npmrc` keeps `save-exact=true` for npm. `allowBuilds` replaces `onlyBuiltDependencies` (ignored by pnpm 12): esbuild, workerd, sharp, @tailwindcss/oxide allowed; msw, @parcel/watcher, @swc/core denied (prebuilt binaries) |
| WSP-02 layout | the plan's section 17, plus `frontend/e2e` (TE-02) | — |
| WSP-03 import directions | `no-restricted-imports` per kind; 9 fixture tests | the console's API paths: a custom rule (`/api/v1/platform/**`, `/api/v1/auth/**` only) — practical in FE-1 |
| WSP-04 lint rules | no `dangerouslySetInnerHTML`; physical direction (classes + style + CSS); upper-casing (class, style, and locale-free `toUpperCase/toLowerCase`); secret-looking public env; raw design values in apps; web storage / `document.cookie`; server-only imports in client files; console paths — each with a failing fixture and a clean twin; CSS and `.env*` by `wasla-check-files` | the e2e package is exempt from the storage rule and React's hooks rule (it reads storage to prove it empty; Playwright's `use`) |
| WSP-05 versions | section 3 | TypeScript 5.9 / ESLint 9 kept (majors moved, peers) |
| WSP-06 beside Python | `frontend/.gitignore`, `frontend/.gitattributes` (LF everywhere; `core.autocrlf=true` on this machine), README | no root file changed |
| AC-01 generator | openapi-typescript 7.13.0, `defaultNonNullable: false`, the Blob transform | FastAPI's OpenAPI 3.1 marks files with `contentMediaType`, not `format: binary`: the transform matches both. `defaultNonNullable: true` (the generator's default) would type the envelope's `details` / `request_id` as always present, which FE-0's 413 body is not |
| AC-02 drift | turbo `generate` + `git diff --exit-code`; the sha256 header test | — |
| AC-03 errors | `createApiClient` + `unwrap()` → `ApiError {status, code, messageKey, details, requestId, fieldErrors, diagnosticMessage, retryAfterSeconds}`; `Error.message` is the code | screens call `client.GET(...)` and pass the result to `unwrap` (the openapi-fetch result shape is kept) |
| AC-04 events, keys | `events.ts` = event-types.json; `[wsId, sessionGen, resource, params]` | — |
| AC-05 typed MSW | `typedHandler` / `errorHandler`; 4 `@ts-expect-error` refusals | MSW 3: `onUnhandledFrame` replaces `onUnhandledRequest` |
| DS-01 tokens | Tailwind 4 `@theme static { --*: initial; … }`; names follow Tailwind namespaces (`--text-*` = the plan's `--font-size-*`, `--spacing-*` = `--space-*`); `--spacing-target` 40px; console accent `--color-console` applied by `[data-app="console"]` | — |
| DS-02 fonts | 9 woff2 + 9 woff (fallback) per app, latin/latin-ext/arabic × 400/500/600; licences copied to each app's `public/licenses/` at build | — |
| DS-03 direction | `UiProvider` (Direction + Tooltip + Toast providers, locale context); `Icon` flip list | — |
| DS-04 primitives | 16 + FormField, EmptyState, ErrorState | Combobox: WAI-ARIA 1.2 combobox over Radix Popover (`aria-activedescendant`, focus stays in the input), Turkish case folding; DropdownMenu non-modal (FEF-01) |
| DS-05 Storybook | 10.6.6, telemetry off, en/ar/fr/tr toolbar, viewports 390/1280, 38 stories | default / disabled / invalid / long Arabic / long Turkish / mixed `<bdi>` are covered per primitive group (one story may carry several cases) rather than six stories per primitive |
| DS-06 axe per story | Playwright over the built Storybook | — |
| DS-07 RTL keyboard | section 8 | Radix RadioGroup checks on arrow only while the key is down (FEF-02); the test holds the key |
| DS-08 screenshots | 152 PNGs + 38 contact sheets in the notes; not committed, not a baseline | — |
| IN-01..IN-05 | section 9 | Arabic-Indic digits: `numberingSystem: "arab"` named explicitly (CLDR 48 gives plain `ar` Latin digits) |
| SS-01..SS-03 | section 10 | the marker-lock hand-off (a70bb1e), found by the e2e test |
| AP-01 apps | Next.js 16.4.5 (Node runtime, no server actions, images unoptimized), Astro 7.4.7 static | marketing ships **no JavaScript**: the switcher is links, so no React island (and no `@astrojs/react`) yet; OpenNext's `ASSETS` binding is the only binding (the static-assets mechanism itself) |
| AP-02 screens | APP-404/ERR and the signed-out shell (web, console), MKT home ×4 + 404 | the API `/health` footer is behind a compile-time `NODE_ENV === "development"` branch, so production builds contain no call; its development rendering was not asserted by a test (the PV-03 run proved CORS from the app origin to the API) |
| AP-03 origins | `@wasla/config/origins` table + validation; per-app env module | `next start` re-reads `next.config.ts`, so `WASLA_ENV` is needed at start as well as at build |
| AP-04 cookies/storage | `wasla_locale` set by `POST /locale` (Lax, `Path=/`, 1 year, Secure outside development) | — |
| AP-05 headers | `proxy.ts` nonce CSP + headers; marketing `_headers` generated after the build, which refuses inline script/style | CSP adds `'strict-dynamic'` and `object-src 'none'` (Next.js's documented nonce policy) |
| AP-06 responsive/motion | asserted in e2e | — |
| TE-01..TE-05 | sections 11–12 | jsdom for components |
| CI-01..CI-03 | section 13 | — |
| PV-01..PV-03 | section 22 | PV-01/PV-02 not run (not signed in) |

| Decision NOT taken | Owner |
|---|---|
| sign-up / log-in / session route handlers / the S-2 cookies | FE-2 |
| app shell after sign-in, gates, channels | FE-3 |
| inbox, customers, AI, knowledge, team, settings, billing | FE-4 .. FE-8 |
| the console's screens and access gate (FEQ-05) | FE-9 |
| marketing copy, pricing, legal, SEO beyond hreflang, analytics (FEQ-08, -12, -13, -15) | FE-10 |
| dark theme (FEQ-09), visual-regression baseline (FEQ-11), PWA (FEQ-10) | the owner |
| error reporting provider (FEQ-12) | FE-11 |
| custom domains, DNS, Access, production Workers, a deploy workflow | FE-11 |
| the Cloudflare-proxy half of the event stream (EXT-FE-03) | the first staging deploy |
| `paths-ignore` in ci.yml | audit 24 |
| nightly / real-clock / Linux-local lanes, performance budget, device lab | the owner's light-gate choice; FE-11 |

## 5. The Workspace

```text
frontend/  package.json pnpm-workspace.yaml turbo.json .nvmrc .npmrc .gitignore .gitattributes .prettierignore README.md
  apps/web apps/console apps/marketing
  packages/api-client packages/session packages/ui packages/i18n packages/config
  e2e
```

| Rule (WSP-03/04) | Fixture | Proof |
|---|---|---|
| import directions | 7 refused (marketing→session, marketing→api-client, web→console, ui→apps, i18n→apps, ui→session, i18n→ui, api-client→session) + 1 allowed (web→session/api-client/ui) | config tests |
| `wasla/no-physical-direction` | `ml-4 text-left` + `paddingRight` (app and ui) | ≥3 errors; twin 0 |
| `wasla/no-uppercase` | `uppercase` class + `toUpperCase()` | ≥2; twin 0 |
| `wasla/no-public-secret-env` | `NEXT_PUBLIC_PAYMOB_SECRET_KEY`, `PUBLIC_API_TOKEN` | ≥2; twin 0 |
| `wasla/no-raw-design-values` | `w-[13px] bg-[#ff0000]`, style colour/fontSize | ≥4; twin (`href="#main"`) 0 |
| `wasla/no-web-storage` | `localStorage`, `window.sessionStorage`, `document.cookie`; the allow-listed file storing a `token` key | ≥3 / 1; twins 0 |
| `wasla/no-server-only-in-client` | `"use client"` importing `@wasla/session/server`; a ui file importing it | 1 / 1 |
| `wasla/console-platform-paths` | `/api/v1/conversations` in console; not applied to web | 1; 0 |
| `react/no-danger` | `dangerouslySetInnerHTML` | 1; 0 |
| `wasla-check-files` (CSS, env) | `margin-left` + `#ff0000` + `text-transform: uppercase` in app CSS; `padding-right`, `text-align: left` in ui CSS; `NEXT_PUBLIC_STRIPE_SECRET_KEY` in `.env.example` | found; comment-only twin clean |

Config package: 30 tests (24 rules + 6 origins).

## 6. The API Client

| Item | Value |
|---|---|
| Generator | `node scripts/generate.mjs`: openapi-typescript 7.13.0, `defaultNonNullable: false`, transform `contentMediaType`/`format: binary` → `Blob` (nullable → `Blob \| null`) |
| `schema.d.ts` | 23,531 lines, 815,352 bytes; header records `openapi.json sha256: 57bed53ca5ba92560eb8ae90d3059f5b05fbc7b1235345c4fac4178f2e60505d`; 257 operations over the document's paths; 7 file fields typed `Blob` |
| Drift proof | a one-character edit staged → `generate` → `git diff --exit-code` rc **1**; restored → rc 0 (evidence/ac02_drift_proof.txt); M-W01 killed by the header test |
| Normaliser vs FE-0's OA-05 bodies | 14 bodies: 400 bad_request, 401 ×2 unauthenticated, 403 email_verification_required, 403 permission_denied, 404 ×2 not_found, 405 method_not_allowed, 409 conflict, 413 payload_too_large, 422 ×3 validation_error, 429 rate_limited — each → `ApiError` with that code, `messageKey errors.<code>`, the body's request id or the `X-Request-ID` header, the sentence only in `diagnosticMessage` |
| Other cases | 422 → dotted `fieldErrors` with the error type; `Retry-After` kept; HTML 502 → `unexpected_response` (header request id); a JSON body of another shape → `unexpected_response`; a code that is not a code → `unexpected_response`; network → `network_error`; abort re-thrown; bearer added only with a token; `onUnauthenticated` once per 401 |
| Events | `EVENT_TYPES` (5), `CLOSE_REASONS` (5), `STREAM_FRAMES` (2) = event-types.json; frame data types; a compile-time check that the map's keys are exactly those |
| Query keys | 6 tests (two workspaces, two generations never share a key; prefix; order-free params; refusals) |
| Tests | 39 |
| FEB-30.. | none: the generator typed the whole document with no hand edit |

## 7. The Design System

Contrast from `tokens.css` (WCAG 1.4.3 text 4.5:1, 1.4.11 non-text 3:1; 23 pairs tested, the main ones):

| Pair | Ratio | Minimum |
|---|---|---|
| text on surface | 17.92 | 4.5 |
| text-muted on surface / on surface-sunken | 6.13 / 5.67 | 4.5 |
| on-primary on primary / on primary-hover | 5.47 / 7.62 | 4.5 |
| primary on surface | 5.47 | 4.5 |
| danger on surface / on danger-subtle | 6.57 / 6.05 | 4.5 |
| warning / success / info on their -subtle | 5.20 / 5.40 / 6.16 | 4.5 |
| on-primary on console accent; console accent on surface | 7.10 / 7.10 | 4.5 |
| focus on surface / on surface-sunken | 5.17 / 4.78 | 3 |

| Item | Value |
|---|---|
| Fonts (woff2 bytes) | Plex Sans latin 22,588 / 24,184 / 24,252; latin-ext 15,980 / 16,456 / 16,448; Plex Sans Arabic 42,848 / 45,296 / 45,688 (400/500/600); the browser fetches a file only for a page's characters; OFL texts shipped |
| Primitives | Button, IconButton, Link, Input, Textarea, Checkbox, RadioGroup, Switch, Select, Combobox, Dialog + ConfirmDialog, DropdownMenu, Popover, Tooltip, Tabs, Toast (+ ToastViewport); FormField, EmptyState, ErrorState |
| Stories | 38 (Button 7 incl. IconButton/Link, Fields 19, Overlays 13 … see `storybook-static/index.json`) |
| axe per story | 76 runs (38 × en/ar): **0 critical, 0 serious**, 76 moderate = `page-has-heading-one` 70 + `region` 6 (a story is a fragment, not a page — Storybook context, not FE-1 code) |
| Unit tests | 42 (contrast 25, icon flip 11, FormField 2, ConfirmDialog 1, ErrorState 1, Turkish combobox 1, logical sides 1) |
| Storybook suite | 214 passed (76 axe + 46 × 3 keyboard), 152 screenshot tests skipped unless asked |

## 8. RTL Keyboard Proof (DS-07, EXT-FE-07)

Playwright over the built Storybook, Chromium / Firefox / WebKit, English (LTR) and Arabic (RTL). **All 138 = 46 × 3
pass.**

| Component | Assertion (LTR / RTL) | Chromium | Firefox | WebKit |
|---|---|---|---|---|
| Tabs | ArrowRight → next / **previous**; ArrowLeft → previous / **next**; Home/End first/last and selected; first tab at the inline start | pass ×8 | pass ×8 | pass ×8 |
| RadioGroup | ArrowRight → next / **previous** (checked); ArrowLeft → previous / **next**; ArrowDown/Up next/previous; Home/End | pass ×8 | pass ×8 | pass ×8 |
| DropdownMenu | Enter opens on the first item, Down/Up move, separator skipped; submenu opens with ArrowRight / **ArrowLeft**, closes with the opposite and returns to its trigger item; submenu opens towards the inline end; Escape closes, focus returns | pass ×8 | pass ×8 | pass ×8 |
| Popover | `inline-end` lands right / **left** of the trigger; opening moves focus in; Escape closes, focus returns | pass ×4 | pass ×4 | pass ×4 |
| Select | Enter opens on the first option, Down moves, Enter chooses, focus returns; the list aligns with the trigger's inline start (left / **right** edge); Escape | pass ×6 | pass ×6 | pass ×6 |
| Combobox | Down opens and moves `aria-activedescendant`, Enter chooses; focus stays in the input, Escape closes; the list aligns with the input's inline start | pass ×6 | pass ×6 | pass ×6 |
| Dialog | focus trapped (8 Tab, 4 Shift+Tab); Escape closes, focus returns; close button at the inline end of the title | pass ×6 | pass ×6 | pass ×6 |

What Radix does by itself (recorded): Tabs, RadioGroup and DropdownMenu read the direction from `Direction.Provider`
and reverse the horizontal arrows; `align="start"` follows the direction (the list's right edge meets the trigger's in
RTL); `side` is **physical** (`right` stays right) — the wrapper's `side="inline-start" | "inline-end"` maps it.
Findings: **FEF-01** fixed in the wrapper (non-modal menu); **FEF-02** RadioGroup checks on arrow only while the key is
held (a human press works; a zero-length synthetic press moves focus without checking) — test holds the key; **FEF-03**
two back-to-back synthetic ArrowDown presses in the non-modal menu under parallel load once moved one step in Chromium —
stable with per-step waits (4/4 runs) — for a device check (EXT-FE-06/13). Full table: evidence/ds07_table.md.

## 9. Localization

| Item | Value |
|---|---|
| Catalogues | `messages/{en,ar,fr,tr}/{common,errors,shell,marketing}.json`: 78 keys each; errors = 41 codes of Appendix C (internal † excluded) + `unexpected_response`, `network_error`, `fallback` |
| Review status | en reviewed (source); **ar (Modern Standard Arabic), fr, tr: `_meta.reviewed: false`** — drafts written in FE-1 (FEQ-08) |
| Plurals | 2 plural messages per locale (`retryIn`, `messagesCount`); ar zero/one/two/few/many/other, fr one/many/other, tr one/other — equal to this Node's CLDR 48 (ICU 78.2) data, which the checker cross-checks (EXT-FE-08: Turkish confirmed from CLDR 48 as shipped in ICU, not from the web chart) |
| `i18n:check` failure proofs | a key removed from tr; an extra key in ar; an Arabic plural lacking zero/two/few/many; a Turkish `few`; invalid ICU; a renamed argument — each fails (tests); M-W05 (ar ignored) killed |
| Runtime fallback | a missing key → English, never the key (test) |
| lang/dir before paint | web/console: server reads `wasla_locale`, then Accept-Language; `<html lang dir>` in the first bytes (e2e reads the raw response); marketing: static per URL; hreflang + x-default; `/` → `/en/` (302 at the Workers level, a meta-refresh page as Astro's fallback) |
| Formatters | money from decimal strings without a float (`"0.10"` → EGP 0.10; 9007199254740993.00 exact); Latin digits by default in ar, Arabic-Indic behind one flag (`arab` named, CLDR 48 gives plain `ar` Latin); dates in the workspace's zone (22:30Z → Nov 8 in Cairo, Nov 7 in UTC); relative time from an injected now; Turkish İ/ı casing and collation. 20 tests, run also under `TZ=Pacific/Kiritimati` |

## 10. The Session Skeleton and the Signer

| Proof | Result |
|---|---|
| 50 concurrent `refresh()` across 5 tabs (doubles) | fn ×1, every caller the same token; **without the lock ×5** |
| broadcast delivered after the lock grant (the Chromium order), 5 rounds × 20 calls | fn ×1 per round (the marker hand-off) |
| same, without the marker | fn > 1 (why the marker exists) |
| announced token never arrives (its tab closed) | the waiter refreshes after the bounded wait |
| a failure | releases the lock; the next caller retries once (fn ×2; 10 of 50 rejected — the first tab's callers) |
| sign-out on the channel | every holder cleared, every generation +1 |
| 80 % schedule | fires at 720 s of a 900 s token on an injected timer; re-arms; stops on sign-out |
| Two real tabs (Playwright, real Web Locks + BroadcastChannel), 10 rounds | **10 calls** in Chromium, Firefox and WebKit at both widths (before a70bb1e, Chromium made 3 calls in round 2) |
| Signer vs FE-0's vectors | **7/7 exact** (canonical string and signature), incl. the decoded-path case; one changed field (secret, address, method, path, second) changes the signature; M-W04 killed |
| Signed login on the e2e API | 11 logins signed for one address (fresh accounts): `401 ×10, 429`; another signed address: `401`; a forged signature: `401` (counted under the caller's address) |
| Tests | 28 + 5 header/CSP tests |

## 11. The Apps and the Screens

| App | Routes | Notes |
|---|---|---|
| web (3000) | `/` (shell), `/_not-found`, `POST /locale`, `/test-only/error` (404 unless `WASLA_E2E_ROUTES=1`) | all dynamic (nonce) |
| console (3001) | same; `data-app="console"` (accent), staleTime ∞ / no refetch | |
| marketing (4321) | `/en/ /ar/ /fr/ /tr/`, `/404.html`, `/` redirect | 0 `<script>`; dist 595 KB (fonts included) |

Headers as served (e2e, development build): web/console `Content-Security-Policy: default-src 'self'; script-src 'self'
'nonce-…' 'strict-dynamic'; style-src 'self' 'unsafe-inline'; connect-src 'self' http://localhost:8000; img-src 'self'
blob: data:; media-src 'self' blob:; font-src 'self'; frame-src 'none'; frame-ancestors 'none'; base-uri 'none';
form-action 'self'; object-src 'none'`, `Referrer-Policy: strict-origin-when-cross-origin`, `Permissions-Policy:
camera=(), microphone=(self), geolocation=()`, `X-Content-Type-Options: nosniff`; HSTS only outside development (unit
tests). Marketing (local Workers runtime): `default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'
data:; font-src 'self'; connect-src 'self' <api>; frame-ancestors 'none'; base-uri 'none'; form-action 'self';
object-src 'none'` and the same three headers. Cookies: `wasla_locale` only. Storage: empty.

| Size | web | console |
|---|---|---|
| client JS (`.next/static/**/*.js`) | 742,639 B | 742,660 B |
| Worker bundle (OpenNext) | not built on Windows (FEF-04); measured by `frontend.yml` checks job: recorded in the addendum | |

## 12. E2E, Accessibility and Origins

`pnpm e2e:local` (FE-0's profile, `wasla-fe-e2e`, ports 8000/8025/8026 on 127.0.0.1): Part A run 3 (14:08–14:11) and
Part B verify run (14:39–14:42): **128 passed, 0 failed, 10 skipped** (the two API-only tests run in one project).
After each `down -v`: 0 containers, 0 volumes, 0 networks.

| Test | Chromium 1280 / 390 | Firefox 1280 / 390 | WebKit 1280 / 390 |
|---|---|---|---|
| web shell, web 404, console shell, console 404, marketing home — each en and ar (10) | pass | pass | pass |
| marketing fr, tr and 404; `/` → 302 `/en/` | pass | pass | pass |
| web / console switcher (form post → lang, dir, cookie) | pass | pass | pass (SameSite attribute not asserted on Windows WebKit, FEF-05) |
| web / console error page (Arabic sentence by code, request id, no backend sentence, no stack) | pass | pass | pass |
| web / console test-only route 404 without the flag | pass | pass | pass |
| marketing switcher links | pass | pass | pass |
| reduced motion (motion tokens 0) | pass | pass | pass |
| 40×40 targets | pass | pass | pass |
| two-tab refresh, 10 rounds | pass (10 calls) | pass | pass |
| signed login bucket; helpers | pass (Chromium 1280 only) | skipped | skipped |

Each screen test also asserts: status, headers, lang/dir in the first bytes and after hydration, no horizontal scroll,
axe, cookies, storage, request origins and CSP violations. **axe: 0 violations of any impact** on every FE-1 screen
state. **Requests to other hosts: 0. CSP violations: 0.** Full matrix: e2e/run3_matrix.md.

## 13. frontend.yml

| Job | What | Timeout |
|---|---|---|
| install | setup-node from `.nvmrc`, corepack pnpm, store cached by the lockfile hash, `--frozen-lockfile` | 15 |
| generate | `turbo run generate` + `git diff --exit-code` | 15 |
| checks | Prettier; `turbo run typecheck lint test i18n:check build --continue`; OpenNext `build:worker` for web and console; `pnpm audit --prod --audit-level high` | 20 |
| storybook | browsers with deps; build; axe per story + RTL keyboard (3 browsers); results uploaded on failure | 20 |
| e2e | browsers; `pnpm e2e:local` (compose profile, smoke, `down -v`); an `always()` down; report uploaded on failure | 40 |

Triggers `frontend/**`, `scripts/export_openapi.py`, the workflow; push (all branches) and pull_request; no
`workflow_dispatch`; `permissions: contents: read`; no secret; actions pinned by SHA (checkout as ci.yml; setup-node
v7.1.0, cache v6.1.0, upload-artifact v7.0.2); telemetry off. Local proof: `@action-validator/cli` 0.6.0 — VALID; every
job's commands run locally in the same order (Part A and B gates), except the OpenNext build (FEF-04).

## 14. Mutation Check

| Mutant | Change | Killed by | Verdict | Restored |
|---|---|---|---|---|
| control | none | api-client 39, session 28, i18n 20, config 30, e2e `web shell [ar]` | green | — |
| M-W01 | the generator hashes its output instead of openapi.json | contract test (sha header) | killed | `git diff --exit-code` 0 |
| M-W02 | the normaliser takes the English message as the code | client tests (code/messageKey) | killed | yes |
| M-W03 | RefreshCoordinator runs fn without the lock | session tests (50 → 1, retry once, hand-off) | killed | yes |
| M-W04 | the signer drops the path | vector and one-field tests | killed | yes |
| M-W05 | i18n:check ignores ar | the ar extra-key and plural tests | killed | yes |
| M-W06 | the root layout renders `dir="ltr"` for ar (rebuilt) | e2e first-bytes assertion | killed | yes (rebuilt) |
| M-W07 | physical-direction rule off | the rule's fixture tests (app, ui) | killed | yes |
| M-W08 | the query key drops wsId | the two-workspace and prefix tests | killed | yes |

None survived; none hung. The first two runner attempts failed for the runner's own reasons (a console encoding
error; pnpm missing from the runner's PATH, so the e2e control could not start) and were discarded.

## 15. Documentation and ADR Changes

| File | Change |
|---|---|
| DECISIONS.md | ADR-192 (the next free number after ADR-191) |
| docs/DEPLOYMENT.md | "Frontend": apps, hosts, origins on iwasla.tech, build-time variables, nothing deployed to iwasla.tech yet, previews |
| docs/SECURITY.md | "The browser applications": headers and CSP, no token stored, the signer's place |
| frontend/README.md | install, commands, the e2e backend, origins, changing the look |

`tests/integration/test_documentation_truth.py`: 27 passed (FE-0's venv, same locks, `app` resolved to the worktree;
`check_installed_lock`: 60 pins, 1 differs = `uvloop`, not installable on Windows, as FE-0 recorded).

## 16. Part A Commits and the Diff Guard

| Commit | Message |
|---|---|
| 038f253 | build(frontend): pnpm + Turborepo workspace, shared config and lint rules (FE-1) |
| 7649e2e | feat(frontend): typed API client generated from the exported OpenAPI document (FE-1) |
| bcfbeee | feat(frontend): design system tokens, primitives and Storybook with RTL keyboard tests (FE-1) |
| 8852a7b | feat(frontend): four locales with ICU catalogues and checks (FE-1) |
| d1a2401 | feat(frontend): browser session skeleton and the forward signer (FE-1) |
| 00196ea | feat(frontend): marketing, customer app and console shells (FE-1) |
| a70bb1e | fix(frontend): hand a refreshed token to waiting tabs without a second refresh (FE-1) |
| eff6a11 | test(frontend): e2e harness against the backend profile, accessibility and origin checks (FE-1) |
| d85afd9 | ci(frontend): frontend workflow (FE-1) |
| 897bebc | docs: frontend foundation (ADR-192) |
| 7c6a25f | style(frontend): format the e2e lint exceptions (FE-1) |

Two commits beyond the expected nine, each its own logical change: the hand-off fix the e2e test found, and a
Prettier-only fix. `git diff --stat 86e3d89 7c6a25f`: 246 files, +47,585 / −0. Paths: `frontend/**`,
`.github/workflows/frontend.yml`, `DECISIONS.md`, `docs/DEPLOYMENT.md`, `docs/SECURITY.md` — nothing else. FE-0's
`openapi.json`, `event-types.json` and `fixtures/forward-signature.v1.json` unchanged. No `node_modules`, `.next`,
`.turbo`, `dist`, `.wrangler`, `.open-next`, `storybook-static`, report or result folder committed. Tracked `frontend/`:
245 files, 2,772,678 bytes; largest: openapi.json 1,088,769 (FE-0's), schema.d.ts 815,352, pnpm-lock.yaml 492,945,
rtl-keyboard.spec.ts 15,523, plugin.js 14,656, session.test.ts 13,003, smoke.spec.ts 9,985, client.test.ts 9,902,
i18n.test.ts 8,135, rules.test.ts 6,909. Secret scan of the added lines (lockfile and generated types excluded): 20
matches, all comments or test values (FE-0's public e2e secret, `not-the-password-0`, empty `SESSION_FORWARD_SECRET=`
placeholders) — evidence/secret_scan.txt. No merge commit, no amend, one author.

## 17. Gates at FE1_HEAD and in the Verify Worktree; the Backend Image

| Gate | Part A — fix worktree @ 7c6a25f | Part B — verify worktree @ 7c6a25f (fresh `node_modules`) | Merged tree @ 62bb30b |
|---|---|---|---|
| G-1 install / audit | frozen ok; prod 0 high/critical (1 moderate) | frozen ok (1,058 packages, 10 m 38 s); same audit | frozen ok |
| G-2 drift | 0 | 0 | 0 |
| G-3 pipeline | Prettier clean; 30/30 tasks (`--force`) | Prettier clean; 30/30 | Prettier clean; first attempt 29/30 — `@wasla/ui#test` "Failed to start forks worker" (vitest could not spawn its jsdom worker under load; FEF-08), the package alone 42/42, re-run 30/30 |
| G-4 Storybook | 214 passed | 214 passed | — |
| G-5 e2e | 128 passed (run 3) | 128 passed | — (tree identical) |
| G-6 mutation | 8/8 killed | — (same tree) | — |
| G-7 backend image | base 623,432,054 B (sha256:cc0dc00f…); second base 623,436,737 (sha256:c5f8043a…) — noise 4,683 | head 623,431,929 B (sha256:04f293db…): Δ −125 vs base — **unchanged** | — |
| docs test | 27 passed | — | — |

## 18. Untracked Files

`E:\wasla` held 14 untracked paths (11 `*.md`, `docs/ui/` × 3 `*.html`) with the same SHA-256 as FE-0 recorded (no new
path since). Before the merge, after the merge and after the report commit: identical list and hashes. No incoming path
collides; none lies under `docs/ui/`; `FE1_FOUNDATION.md` did not exist.

**One incident, repaired:** at 13:48 UTC Turborepo 2.11's worktree detection put its local cache in
`E:\wasla\.turbo\cache` (84 files, this stage's own run) — an untracked folder in the main checkout. It was removed
(that folder only) at 13:55 and `turbo.json` now pins `cacheDir: .turbo/cache` inside `frontend/` (FEF-09); the
untracked list was re-verified identical.

## 19. Merge and Tree Equality

`git merge --no-ff 7c6a25f` in `E:\wasla` → `62bb30b6c81e1887eac3bce86f91c9d45a270d47`, parents `86e3d89`, `7c6a25f`;
no conflict; `git diff --exit-code 7c6a25f HEAD` → 0; Alembic `0138 (head)`. No `pnpm install` ran in `E:\wasla`; the
development stack was not started.

## 20. Push

| Check before the push | Value |
|---|---|
| origin re-fetched | must still be `86e3d89` |
| fast-forward | `git merge-base --is-ancestor origin/worktree-billing-google-auth HEAD` |
| command | `git push origin worktree-billing-google-auth` (no force) |
| not pushed | `fe1-foundation-20261107`, any tag; `main` untouched |

The result (origin before and after) is in the addendum.

## 21. GitHub Actions

The push's `frontend.yml` (its first run) and `ci.yml` runs, with every job, are recorded in the addendum below.

## 22. Part C — Previews and Spikes

| Item | Result |
|---|---|
| Cloudflare session | `npx wrangler@4.158.0 whoami` at 10:05 UTC: "You are not authenticated" |
| PV-01 previews | **PREVIEW NOT DEPLOYED** — not signed in. Nothing created on Cloudflare; no `wrangler login`, no `--temporary` account, no token. The three `wrangler.jsonc` are ready (`wasla-preview-web`, `-console`, `-marketing`; `workers_dev: true`, `preview_urls: false`, no route, no custom domain, no binding but OpenNext's `ASSETS`) |
| PV-02 OpenNext spike (EXT-FE-02) | **EXTERNAL VERIFICATION**: needs a deployed Worker. What is proved without one: Next.js 16.4.5's `proxy.ts` (Node runtime) and route handler build; `node:crypto` HMAC equals the vectors in Node; the OpenNext build itself runs in `frontend.yml` on Linux (recorded in the addendum); on Windows it fails (FEF-04) |
| PV-03 stream spike, browser half (EXT-FE-03) | **CLOSED locally** (below) |
| Cloudflare-proxy half | EXTERNAL VERIFICATION at the first staging deploy (audit 20 / FE-4) |


**PV-03.** from `apps/web` on `next dev` (http://localhost:3000) against FE-0's profile (`--demo` seed), in
Chromium: `POST /api/v1/events/ticket` with the bearer from the app origin → 201 (CORS); `new EventSource(<api>/api/v1/
events/stream?ticket=…)` → open; a second ticket read with `fetch()` + a stream reader (EventSource never dispatches
`:` comments) → `200 text/event-stream`, first frame `retry: 3000`. A signed inbound webhook → `message.created` (×2:
inbound and the AI reply), `message.status`, `follow_up.updated` on both readers within 0.3 s. **Keep-alives: 40 in 10
minutes, every 14.996–15.003 s.** `docker restart wasla-fe-e2e-api` → the stream sends `event: closed {"reason":
"server_restart"}`; EventSource then reconnects **by itself** after `retry` (3 s) with the **same URL, i.e. the spent
ticket**: two `error` events while CONNECTING, then `readyState 2` (CLOSED, a non-200 answer) — it gives up. **The client
must**: on `closed` (or a CLOSED EventSource) take a new ticket and open a new EventSource — done in the spike: open
within 16 ms, the next inbound `message.created` delivered. FE-4's reconnect rule (plan section 10) therefore starts
from "never rely on EventSource's own reconnect". Frame log: spikes/pv03/frames.json

## 23. Findings

| ID | Finding | Status |
|---|---|---|
| FEB-30.. | none — the generator typed the document without a hand edit; every FE-0 error body was read; no backend change was needed | — |
| FEF-01 | Radix DropdownMenu in modal mode marks the page `aria-hidden` while its trigger stays focusable (axe `aria-hidden-focus`, serious) | CLOSED (wrapper `modal={false}`; axe 0) |
| FEF-02 | Radix RadioGroup checks the newly focused radio on an arrow key only while the key is down; a zero-length synthetic press only moves focus | ACCEPTED RESIDUAL (human presses work; test holds the key; owner's device check) |
| FEF-03 | Non-modal menu, Chromium, under parallel load: two back-to-back synthetic ArrowDown presses once moved one step | ACCEPTED RESIDUAL (stable with per-step waits; device check EXT-FE-06/13) |
| FEF-04 | `@opennextjs/cloudflare` 1.20.16 build fails on Windows: "Access is denied" reading pnpm's symlinked `node_modules/.pnpm/...` inside `.open-next/server-functions` | ACCEPTED RESIDUAL (built on Linux in `frontend.yml`; a Windows developer previews with `next start` or WSL) |
| FEF-05 | Playwright's WebKit on Windows reports every cookie's `SameSite` as `None` | ACCEPTED RESIDUAL (the header is unit-tested; Linux WebKit in CI asserts it) |
| FEF-06 | transitive licences outside the list: `@img/sharp-win32-x64` Apache-2.0 AND LGPL-3.0-or-later (libvips; Next.js/Astro image tooling, unused — images unoptimized, nothing shipped to browsers), BlueOak-1.0.0 (glob family), CC0-1.0, CC-BY-4.0 (caniuse-lite data), Python-2.0 (argparse), 0BSD (tslib); every **direct** dependency is in the list | DEFERRED PRODUCT DECISION (the owner's licence policy for transitive build tooling) |
| FEF-07 | `glob` high GHSA-5j98-mcp5-4vw2 and moderate GHSA-3g3m-2fjr-6pw4 via `@opennextjs/cloudflare` (dev dependency, build-time); `minimatch` moderate GHSA-g3hv-75wg-m249 in prod via typescript-eslint/@opennextjs | ACCEPTED RESIDUAL (no fixed release reachable without an upstream bump; 0 high/critical in production dependencies) |
| FEF-08 | vitest 5.1 "Failed to start forks worker" once under a parallel turbo run on this Windows machine | ACCEPTED RESIDUAL (infrastructure; re-run green; watch on CI) |
| FEF-09 | Turborepo 2.11 put its cache in the main worktree (`E:\wasla\.turbo`) | CLOSED (`cacheDir` pinned; folder removed) |
| FEF-10 | Astro 7's telemetry is on by default: the first marketing build (13:42 UTC, which failed before building) printed its telemetry notice and may have sent one anonymous event | CLOSED (every script sets `ASTRO_TELEMETRY_DISABLED`, `NEXT_TELEMETRY_DISABLED`, `WRANGLER_SEND_METRICS=false`, Storybook telemetry off; CI too) — recorded because Astro's endpoint is not in section 7's list |

Network destinations beyond section 7's list, recorded: PyPI during the e2e image build (`pip --require-hashes` of the
locks — implied by TE-02's `up --build`; FE-0 recorded the same), GitHub's API to read the pinned SHAs of three
actions, and FEF-10.

## 24. Decisions Ledger

| Item | Status |
|---|---|
| FE-1 card — builds: monorepo; generated client + drift; tokens + primitives + Storybook en/ar; four catalogues + key check; session skeleton; Vitest/MSW/Playwright/axe harness; frontend.yml | CLOSED |
| FE-1 card — preview deployment of the three apps | EXTERNAL VERIFICATION (not signed in) |
| FE-1 card — screens APP-404/ERR, signed-out shells, marketing placeholder | CLOSED |
| Acceptance — `turbo build typecheck lint test i18n:check` green | CLOSED |
| Acceptance — RTL keyboard tests for dialog, menu, popover, select, tabs, combobox in ar | CLOSED (all pass in 3 browsers; FEF-02/03 noted) |
| Acceptance — SSE spike (EXT-FE-03) recorded | CLOSED for the browser half (section 22); Cloudflare half EXTERNAL VERIFICATION |
| Acceptance — OpenNext spike (EXT-FE-02) recorded | EXTERNAL VERIFICATION (not signed in; build proved on Linux) |
| Acceptance — backend image size unchanged | CLOSED |
| WSP-01..06, AC-01..05, DS-01..08, IN-01..05, SS-01..03, AP-01..06, TE-01..05, CI-01..02 | CLOSED (deviations in section 4) |
| CI-03 (ci.yml untouched) | CLOSED BY DESIGN |
| PV-01, PV-02 | EXTERNAL VERIFICATION |
| PV-03 | CLOSED (browser half, local) |
| EXT-FE-02 | EXTERNAL VERIFICATION (build on Linux in CI; Worker runtime unproved) |
| EXT-FE-03 | browser half CLOSED locally; proxy half EXTERNAL VERIFICATION |
| EXT-FE-06 | EXTERNAL VERIFICATION (desktop engines proved: Web Locks and BroadcastChannel in Chromium, Firefox, WebKit; real iOS/Android not) |
| EXT-FE-07 | CLOSED (section 8) |
| EXT-FE-08 | CLOSED for the data: 4.5:1 / 3:1 applied from WCAG 2.2; Turkish one/other from CLDR 48 in ICU 78.2 (the normative chart itself not re-read) |
| EXT-FE-09 | PARTLY: `EGP` and `latn`/`arab` verified in Node 24 / ICU 78.2 and in three desktop engines through the apps; real devices remain EXTERNAL VERIFICATION |
| FEF-01, FEF-09, FEF-10 | CLOSED |
| FEF-02, FEF-03, FEF-04, FEF-05, FEF-07, FEF-08 | ACCEPTED RESIDUAL |
| FEF-06 | DEFERRED PRODUCT DECISION |
| Decisions NOT taken (section 4) | DEFERRED PRODUCT DECISION (their stages) |
| Light gates instead of nightly / real-clock / Linux-local lanes | ACCEPTED RESIDUAL (the owner's choice) |

## 25. Infrastructure Created by This Stage

| Kind | Name |
|---|---|
| Images | `wasla-fe1:base`, `wasla-fe1:base-b`, `wasla-fe1:head`; `wasla-fe-e2e-app:local` (built by the e2e runs from FE-0's Dockerfile) |
| Worktrees | `E:\wasla-fe1-fix` (branch `fe1-foundation-20261107`), `E:\wasla-fe1-verify` (detached @ 62bb30b) |
| Folder | `E:\wasla-fe1-notes` (draft, logs, evidence, e2e, mutation, spikes, tools, the pnpm shim) |
| Branch | `fe1-foundation-20261107` (kept) |
| Shared caches (kept) | pnpm store, Playwright browsers (`%LOCALAPPDATA%\ms-playwright`) |
| Cloudflare | nothing |

## 26. Part D — Cleanup (planned)

Only if the verdict is `FE-1 MERGED AND PUSHED, CI GREEN` (the addendum's runs green too): rescue the spike scripts and
frame log, the mutation runner and results, the DS-07 table, the e2e matrix and axe outputs, the contact sheet and the
draft to `E:\wasla-verification-scripts\fe1\` (SHA-256 verified, never `node_modules`); then remove the images
`wasla-fe1:base`, `wasla-fe1:base-b`, `wasla-fe1:head` and `wasla-fe-e2e-app:local` (after proving no `wasla-fe-e2e-*`
container), the worktrees `E:\wasla-fe1-verify` and `E:\wasla-fe1-fix` (`git worktree remove`, branch kept),
`git worktree prune`, then `E:\wasla-fe1-notes`. Kept: every branch, earlier stages' leftovers, the developer stack, the
pnpm store and the Playwright browsers. Record: `E:\wasla-cleanup\CLEANUP_fe1_20261107.md` (not committed).

## 27. Remaining External Actions

| Action | Owner |
|---|---|
| Sign in to Cloudflare on this machine (`wrangler login`) if previews are wanted, then FE-1's Part C can deploy the three `wasla-preview-*` Workers and run PV-02 | the owner |
| Open the Storybook build and the contact sheet; a redesign edits `tokens.css` / `packages/ui` | the owner |
| Review the ar, fr and tr drafts (FEQ-08; native reviewer before FE-11); decide FEQ-07 (digits in data, register) | the owner |
| iwasla.tech: finish moving the nameservers to Cloudflare (keep every record, MX above all); no record for app/console/www/api until FE-11 or the first staging deploy | the owner |
| EXT-FE-03's Cloudflare half: EventSource on staging through Cloudflare's proxy for 30 minutes | first staging deploy |
| EXT-FE-06 / EXT-FE-13: real iOS Safari and Android Chrome (Web Locks, BroadcastChannel, fonts, RTL, FEF-02/03) | FE-11 |
| FEF-06 licence policy for transitive build tooling; FEF-07 advisories (watch `@opennextjs/cloudflare` releases) | the owner |
| FE-2 (sign-in, session, workspace) — FEQ-06 (app locale: cookie now), EXT-FE-10 later; FEB-21 (the Google binding cookie name over http) | its prompt |
| Backend stages before FE-3 (FEB-07) and FE-4 (FEB-03/04/05) | backend stages |
| audit 24: `paths-ignore` for ci.yml on frontend-only pushes (CI-03) | audit 24 |
| `security.yml` stays red for RGM-23/24/27 (not this stage's) | the owner |
| Local branch `fe1-foundation-20261107`: keep or delete after review | the owner |
| Merging the canonical branch into `main` | the owner |

## 28. What FE-2 Can Rely On

| Package / path | What it gives FE-2 |
|---|---|
| `@wasla/api-client` | `createApiClient({ baseUrl, getAccessToken, onUnauthenticated })`, `unwrap()`, `ApiError` (code, messageKey, requestId, fieldErrors, retryAfterSeconds), `paths`/`components` types, `queryKey()`, stream frame types; `@wasla/api-client/msw` typed handlers |
| `@wasla/session` | `TokenHolder`, `RefreshCoordinator` (Web Lock + marker hand-off), `SessionChannel`, `scheduleRefresh`, `createBrowserSession()` |
| `@wasla/session/server` | `signForward()`, `forwardPath()` — equal to FE-0's vectors; for the `/session/*` route handlers |
| `@wasla/session/headers` | `buildCsp()`, `securityHeaders()`, `createNonce()` |
| `@wasla/ui` | tokens, UiProvider, 16 primitives, FormField (aria wiring), ErrorState (request id), EmptyState, ConfirmDialog |
| `@wasla/i18n` | four catalogues incl. every Appendix C code, `errorMessageKey()`, `resolveLocale()`, formatters |
| `@wasla/config` | lint rules (no storage, no script cookie, signer server-only), origins per environment |
| apps | root layouts with lang/dir from the server, providers, nonce CSP in `proxy.ts`, `/locale`, error boundary |
| `frontend/e2e` | `pnpm e2e:local`; helpers `mailbox`, `freshEmail`, `signedForward`, `e2eApi`; the page guard (origins, CSP, cookies, storage) |
| CI | `frontend.yml`; ADR-192 |

## 29. Final Verdict

```text
Provisional, before GitHub's runs: every local gate green (G-1..G-7), merged as one normal merge commit with the merged
tree byte-identical to FE1_HEAD. The verdict is set in the addendum from the push's frontend.yml and ci.yml runs.
PREVIEW NOT DEPLOYED   this machine is not signed in to Cloudflare (Part C: EXTERNAL VERIFICATION)
```
