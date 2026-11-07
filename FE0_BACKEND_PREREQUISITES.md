# FE-0 — Backend Prerequisites For The Frontend (Signed Client IP, OpenAPI Export And Envelope, E2E Profile)

Stage FE-0 of `FRONTEND_PLAN.md`, 2026-11-07. Light gates by the owner's choice: focused new tests, the nearby suites,
static checks, the document's freshness, one small mutation check, and one GitHub `ci.yml` run as the full-suite gate.
**No local run in this report is "the full suite"; only GitHub's run is.**

## Phase status

| Phase | State | Evidence (E:\wasla-fe0-notes\) | UTC | Commit / merge / push / removal |
|---|---|---|---|---|
| A0 state, untracked files, infrastructure, guard | done | evidence/untracked_before.txt, evidence/scripts_copied_sha256.txt | 06:42–07:20 | wasla-fe0-pg/-redis/-net created; worktrees fe0-fix, fe0-base |
| A1 PL-01 plan on the line | done | sha256 479659cc… at source and on the line | 07:19 | a52f733 (`cherry-pick -x e492f5a`) |
| A2 before probes at 9c7b4e4 | done | probes/probe_before.json | ~07:35 | — |
| A3 FEB-01 (tests first) | done | evidence/f6_at_base.txt (3/3 failed with 429) | ~07:55 | 1bcfc05 |
| A4 FEB-02 | done | evidence/oa08_diff.json, evidence/oa_freshness_fail.txt | ~08:20 local | 3162ebc |
| A5 e2e profile | done | e2e/smoke_A.txt, e2e/canary_A.txt, e2e/down_A.txt | | b9e54de, 0748887 |
| A6 mutation check | done | mutation/results.json, mutation/run_full.log | | — |
| A7 docs and ADRs | done | | | 6115432 |
| A8 diff guard | done | evidence/diff_names.txt, evidence/secret_scan.txt | | — |
| A9 Part A gates at FE0_HEAD | done | logs/gates_A_fix.txt, probes/probe_after.json | 07:46 | FE0_HEAD = 6115432 |
| B verify worktree gates + e2e | done | logs/gates_B_verify.txt, e2e/smoke_B.txt, e2e/canary_B.txt, e2e/down_B.txt | 07:52–08:00 | E:\wasla-fe0-verify @ 6115432 |
| B merge + tree equality | done | | 08:01 | cc74d09 (parents 9c7b4e4, 6115432) |
| B final-tree gates | done | logs/gates_B_final.txt | 08:01 | verify worktree @ cc74d09 |
| B report commit, push | done | | 08:05 | report b338a44; pushed `9c7b4e4..b338a44`, no force |
| B ci.yml run of the push | done — green | logs/ci_tests_job.log | 08:05–09:16 | run 42354012088, 5/5 jobs |
| B addendum | done | this section | 09:20 | this commit; its run in the next line of the cleanup record |
| C cleanup | not started | | | only after CI green |

(Times are UTC; Cairo = UTC+2.)

## 1. Executive Summary

| Item | Result |
|---|---|
| FRONTEND_PLAN.md | on the line byte for byte (SHA-256 `479659cc8ce7fd63f7256447454b028482cb0d7fdbe4498d577e2e3bc6f066b1`), via `git cherry-pick -x e492f5a` |
| Owner decisions | HD-01 Cloudflare hosting, HD-02 S-2 session — ADR-189; nothing created on Cloudflare |
| FEB-01 | the auth/public buckets count the address the frontend tier **signs**; unsigned, stale, wrongly signed, other-route and malformed claims change nothing. Before: 12 sign-ins through one tier address → 10×401 then 429. After: 12×401, no 429; one signed browser still meets 429 at its 11th attempt; an unsigned claim behaves exactly as before |
| FEB-02 | `scripts/export_openapi.py` writes `frontend/packages/api-client/openapi.json` (+ `event-types.json`) with no server, database, Redis, network or secret; 257 operations = the production walk's 258 less self-excluded `/metrics`; every operation has `default → ErrorEnvelope`, every declared 4xx/5xx references the envelope, every 422 `ValidationErrorEnvelope`; FastAPI's two error components are gone; 14 real error bodies (400, 401×2, 403×2, 404×2, 405, 409, 413, 422×3, 429) validate strictly against the exported schemas; a freshness test fails when the file is stale |
| E2E profile | `docker compose -f docker-compose.e2e.yml --profile e2e up -d --build` — real API and worker, fakes for Meta, OpenAI, Paymob, Google and email, a mailbox that survives sending. J1–J10 walked twice (Part A, Part B): 8 PASSED, J6 EXPECTED UNTIL FE-2, J10 PASSED WITH FEB-03. Canary refused; 0 provider hosts in the logs; `down -v` leaves nothing |
| Mutation | M-F01..M-F04 and M-O01 all killed; control green; tree restored each time |
| Gates | G-1 144 new tests green; G-2 24 nearby files green; G-3 ruff/black/mypy clean; G-4 export fresh, deterministic, Alembic 0138 single head — at FE0_HEAD, in the verify worktree, and (G-1, G-3, G-4) on the merged tree |
| Merge | one normal merge commit cc74d09, merged tree byte-identical to FE0_HEAD |
| Verdict | **FE-0 MERGED AND PUSHED, CI GREEN** (ci.yml 42354012088, 5/5, 8,077 passed) |

## 2. Source Branches and Exact Heads

| Name | Value |
|---|---|
| ORIGIN_CANONICAL_START_HEAD | `9c7b4e48512885f5cbb76cc7a8b02a5520e86a3d` (= origin, 0 ahead / 0 behind at start and before the merge) |
| PLAN_SOURCE | `e492f5aa5788953d35c633d24dcb1cdc84693194` on `frontend-plan-20261107` (adds only FRONTEND_PLAN.md); file SHA-256 above |
| FE0_HEAD | `6115432252d4bf1ff91ee90aa6da9624424c6991` on `fe0-backend-prereqs-20261107` |
| CANONICAL_MERGED_HEAD | `cc74d099874d1067ad12f16e7acc12ec9f69860b`, parents `9c7b4e4`, `6115432` |
| main | `8e3a1ba5a63651b7b7da3bdf14845f40a9d93667` — untouched |
| Alembic head | `0138`, single (no migration in this stage) |
| LAST_ADR before | ADR-188; this stage ADR-189, ADR-190, ADR-191 (no renumbering) |
| New ID prefixes | FEB-20.., HD-, PL-, FW-, OA-, E2E-, M-F, M-O, P-F, P-O — none used before in the repository or reports (grep) |

## 3. Infrastructure and the Guard

| Item | Value |
|---|---|
| Network | `wasla-fe0-net` |
| PostgreSQL | `wasla-fe0-pg`, `pgvector/pgvector:pg16@sha256:ccc6e83d…` (PostgreSQL 16.15, pgvector 0.8.6), 127.0.0.1:55921 |
| Redis | `wasla-fe0-redis`, `redis:7-alpine@sha256:ff02b58f…` (7.4.11), 127.0.0.1:56921 |
| Databases | wasla_fe0_test, wasla_fe0_probe_before, wasla_fe0_probe_after, wasla_fe0_mut, wasla_fe0_verify, wasla_fe0_final |
| venv | `E:\wasla-fe0-notes\venv`, Python 3.12.7, both locks by hash (uvloop excluded on Windows) then `-e ".[dev]"`; `check_installed_lock`: 65 pins, 0 differ; `pip check` clean |
| Guard | dead proxy `127.0.0.1:9` for HTTP(S)/ALL_PROXY; `NO_PROXY=127.0.0.1,localhost`; `OPENAI_API_KEY` unset; a `sitecustomize` DNS/connect wall (copied from the rdi stage, names changed to `wasla-fe0-`) on every local Python process; refusals logged: **0** |
| Scripts copied | the plan's 7 scripts (`fep_server.py` … `probe_session2.py`) → `E:\wasla-fe0-notes\scripts\`, hashes in evidence/scripts_copied_sha256.txt; nothing run from `E:\wasla-fep-notes` |
| e2e canary | from inside `wasla-fe-e2e-api` and `-worker`: `curl https://graph.facebook.com/` and `https://api.openai.com/` → `curl: (7) Failed to connect to 127.0.0.1 port 9`; Python `httpx.get` → `ConnectError [Errno 111] Connection refused`; the wall → `e2e wall: lookup of graph.facebook.com refused` (Part A and Part B) |
| Real provider responses | **0** in the whole stage |
| Every run's interpreter | `app.__file__` asserted equal to its worktree's `app\__init__.py`; no run counted otherwise |
| Network destinations | GitHub (read runs; push); the Docker host pulled nothing (images present); **PyPI during the e2e image build** (`pip install --require-hashes` of the locks, as ci.yml does) — a build-time destination E2E-01 implies, never an application process |

## 4. Decisions Applied, and Decisions NOT Taken

| Decision | Applied |
|---|---|
| Infrastructure | a new `wasla-fe0-*` line, fresh data only; the plan's scripts copied and read; the plan's containers and notes untouched |
| PL-01 | cherry-pick of the plan commit; the plan not edited |
| HD-01, HD-02 | ADR-189; FEB-14 not taken; CORS unchanged |
| FW-01..FW-10 | as specified; section 7 |
| OA-01..OA-09 | as specified, with two recorded deviations: the contract check uses a JSON Schema subset validator (`tests/json_schema_subset.py`, refuses unknown keywords) because `jsonschema` is not a dependency and the locks may not change; the post-processor lives in a new `app/api/openapi.py`, installed by `app/main.py` |
| E2E-01..E2E-10 | as specified, with three recorded deviations: (1) `OPENAI_API_KEY` is a public dummy (`sk-e2e-public-dummy-NOT-A-KEY`) — the worker refuses to run an agent or embed without *a* key (`ingestion_worker.py:175`, the Responses client), and every request is rerouted; (2) the fakes service publishes `127.0.0.1:8026` because a browser must open the fake Google and Paymob pages; (3) media storage is the local filesystem on a shared volume (`MEDIA_STORAGE_BACKEND=local`, the settings default) instead of MinIO; `ENVIRONMENT=test`, `DOCS_ENABLED=false` (the suites' production shape) |

| Decision NOT taken | Owner |
|---|---|
| stable operationIds, `info.version`, a JSON Schema of the stream frames (FEB-02 P2) | audit 23 |
| FEB-03 .. FEB-19 | their backend stages (plan §30) |
| FEB-14 refresh-reuse grace | owner + audit 23 |
| a second forward secret for rotation | audit 22 |
| changing readers of the address that bypass `client_identity` (none security-relevant found, section 6) | a later backend stage |
| CORS_ORIGINS for the real domains | FEQ-04, then audit 22 |
| the Paymob callback base on the frontend origin (FEB-12) — the e2e fake posts to the API directly | FE-8's backend stage; audits 20, 22 |
| whether the API sits behind Cloudflare's proxy | audit 20 |
| nightly job, real-clock lanes, quiet hours, time-shifted lane, kept-data sweep, local Linux gates, full local lanes, race suite, populated upgrade, first-deploy rehearsal, load | the owner's light-gate choice: a failure only the full suite would reveal is found by GitHub after the push |
| the plan's leftovers (wasla-fep-pg/-redis, E:\wasla-fep-notes, E:\wasla-fep-plan) | the owner |

## 5. FRONTEND_PLAN.md on the Line

Landed as `a52f733` = `git cherry-pick -x e492f5a`; SHA-256 equal at the source and on the line. Where this stage's
results differ from the plan, the plan stays as written:

| Plan says | This stage measured |
|---|---|
| "after 8 auth calls inside a minute from one IP, the 9th answered 429" | 429 from the **11th** call from a fresh minute (bucket 10/min); the plan's 9th followed earlier calls in the same minute |
| 223 operations declare HTTPValidationError | 223 (equal); the 9c7b4e4 document equals the plan's `openapi-dev.json` byte for byte as JSON |
| the e2e launcher exempted one loopback host from `GuardedTransport` | the profile reroutes **every** `GuardedTransport` request to the fakes service and adds a socket wall |
| J5 and J9 only through suites; J2's AI turn not walked | J2's AI turn, J4's AI search, J5 and J9 walked on the profile |

## 6. FEB-01: Reproduction

| Probe at 9c7b4e4 (host process, 127.0.0.1:8921, wasla_fe0_probe_before) | Statuses |
|---|---|
| P-F01: 12 accounts, one peer, 12 different `X-Wasla-Client-IP` | `401 ×10, 429, 429` |
| P-F02: 12 accounts, one peer, 12 different unsigned claims | `401 ×10, 429, 429` (first 429 at 11) |
| F-6 copied to `E:\wasla-fe0-base` (never committed there), 3 runs | **3/3 failed**: `assert 429 not in [401, 401, …]` — the FEB-01 reason |

Every reader of the client address in `app/` (grep for `client_identity`, `request.client`, `.client.host`,
`X-Forwarded-For`, `X-Real-IP`, `remote_addr`, `ip_address`, `client_ip`):

| Reader | Through `client_identity` | Role |
|---|---|---|
| `app/api/rate_limits.py` `guard` (`_limit_by_client`) | yes | the auth, Google and public buckets |
| `app/api/rate_limits.py` `before_body` | yes | the same buckets, counted before the body |
| `app/core/middleware.py:125` `SecurityHeadersMiddleware._is_https` | no (peer + `X-Forwarded-Proto` from trusted proxies) | HSTS decision, not an identity — unchanged, not security-relevant under S-2 |
| `app/core/proxy.py`, `app/core/config.py`, `app/serve.py` | — | parsing, documentation, uvicorn's `proxy_headers=False` |

No audit row, refresh-token record, login history or log context stores a client address — so no FEB-20 for readers.
3 readers counted (2 through `client_identity`, 1 not).

## 7. FEB-01: The Signed Forward

| Item | Value |
|---|---|
| Files | `app/core/client_ip_forward.py` (new verifier), `app/api/rate_limits.py` (`client_identity` → `connection_identity` + forward), `app/core/config.py` (`session_forward_secret: SecretStr | None`, blank → unset, ≥32 bytes outside `test`), `app/core/telemetry.py` (counter), `docker-compose.prod.yml` (+1 line), `.env.example`, `tests/integration/test_deployment_configuration.py` (`FEATURE_SETTINGS`) |
| Canonical | `"v1\n" + ts + "\n" + client_ip + "\n" + METHOD + "\n" + path`, `path = request.url.path` (percent-decoded, no query; no `root_path` is configured) |
| Clock seam | `app.core.client_ip_forward.forward_clock` (tests monkeypatch it; the verifier takes `now`) |
| Counter | `wasla_client_ip_forward_total{result}`, domain {valid, absent, disabled, bad_signature, stale, malformed} = `ForwardResult` (test-held); `absent` counted only with the secret set; `disabled` only when a forward was sent |
| Logging | `client_ip_forward.ignored` WARNING at most once a minute per result; never the address, signature, timestamp or secret |
| Vectors | `frontend/packages/api-client/fixtures/forward-signature.v1.json`: **7** cases (IPv4, IPv6, IPv4-mapped, Google callback, GET, encoded character, logout); signature of case 1 independently checked with `openssl dgst -sha256 -hmac` |

| Test | File | Count |
|---|---|---|
| F-1 vectors accepted; each with one field changed (ts, address, method, path, signature, secret) refused | unit | 7 + 42 |
| F-2 ±60 accepted, ±61 and −3600 stale | unit | 6 |
| F-3 malformed (port, brackets, zone id, CIDR, not an address, 46 chars, empty, list, repeated header ×3, ts ×5); signature not 64 hex → bad_signature (×5) | unit | 21 |
| F-4 secret unset/blank → disabled | unit | 3 |
| F-5 IPv4-mapped = IPv4; IPv6 compressed; surrounding space | unit | 3 |
| F-6 12 accounts × 12 signed addresses → no 429; one signed address → 429 at 11; the tier's spent budget does not touch a signed browser | integration | 3 |
| F-7 unsigned, another secret, stale, malformed, feature off → the tier's bucket; a bad forward answers the route's own 401 | integration | 6 |
| F-8 a login signature on refresh; a POST signature on another method | integration | 2 |
| F-9 trusted proxy's `X-Real-IP` honoured as before (feature off and on); a valid forward wins over it | integration | 3 |
| F-10 per-account bucket trips across 6 signed addresses (real `AuthService`, model-built DB) | integration | 1 |
| F-11 default off; blank unset; staging/production refuse 31 bytes, accept unset and 32; never in repr/dump | unit | 8 |
| F-12 no secret, signature or address in any log record, the counter's exposition or any response | integration | 1 |
| Counter domain = enum | unit | 1 |
| **Total** | `test_client_ip_forward.py` 98 + `test_client_ip_forward_endpoints.py` 16 | **114** |

| After probe at FE0_HEAD (secret set on both sides, wasla_fe0_probe_after) | Statuses |
|---|---|
| P-F01: 12 accounts × 12 signed addresses | `401 ×12`, 0 × 429 |
| P-F01: one signed address × 12 | `401 ×10, 429, 429` (its own bucket) |
| P-F02: unsigned claims | `401 ×10, 429, 429` — **identical to before** |

## 8. FEB-02: The Envelope

Handlers as they are (`app/core/exceptions.py`), all through `error_payload(code, message, details=, request_id=)`,
which writes `{"error": {"code", "message"}}` and adds `details` only when truthy and `request_id` only when present:

| Handler | Status | Body |
|---|---|---|
| `handle_wasla_error` | the error's | `error_payload(error.error_code, error.message, details=error.details, request_id=…)` + the error's headers |
| `handle_request_validation_error` | 422 | `error_payload("validation_error", "The request payload is invalid.", details={"errors": [{type, loc, msg}]}, …)` (input/url/ctx stripped) |
| `handle_http_exception` | the exception's | `error_payload(_HTTP_ERROR_CODES.get(status, f"http_{status}"), detail or "The request could not be completed.", …)` |
| `handle_database_error` | 503 / 409 / → 500 | `retryable_conflict` (+ `Retry-After: 1`) / `conflict` / unexpected |
| `handle_unexpected_error` | 500 | `internal_error`, details only in developer environments |
| `BodySizeLimitMiddleware` (`app/core/limits.py:134`) | 413 | `error_payload(code, message)` built by hand |

Models (`app/schemas/errors.py`): `ErrorBody {code, message, details?: object|null, request_id?: string|null}`,
`ErrorEnvelope {error}`, `ValidationErrorItem {type, loc: (string|int)[], msg}`, `ValidationErrorDetails {errors}`,
`ValidationErrorBody`, `ValidationErrorEnvelope`. No handler changed; no field renamed or dropped; no field missing.
Post-processor: `app/api/openapi.py`, installed on `app.openapi()` by `app/main.py`, cached in `app.openapi_schema`.

## 9. FEB-02: The Export

| Measure | Value |
|---|---|
| Operations in the export | 257 |
| Production walk (`create_app(Settings(environment="test", docs_enabled=False))`, through `original_router`) | 258 |
| Self-excluding routes | `GET /metrics` (`include_in_schema=False`) — the only difference |
| Operations with 422 / other declared non-2xx / `default` | 223 / 20 / 257 (before: 223 / 20 / 0) |
| Non-2xx responses referencing the envelope | 507; the other 3 are `304 Not Modified` (no body — correct by design) |
| Components | 392 → 396: +ErrorBody, ErrorEnvelope, ValidationErrorBody, ValidationErrorDetails, ValidationErrorEnvelope, ValidationErrorItem; −HTTPValidationError, ValidationError |
| Bytes (sorted, indent 2) | 1,000,565 → 1,088,769 |
| OA-08 diff | 257 operations touched, **0 unexplained** (no path, parameter, request body, 2xx response, operationId or `info` changed) |
| Determinism | two exports with `PYTHONHASHSEED` 1 and 4242 → identical; G-4 runs → one hash `57bed53ca5ba9256…`; `event-types.json` `f9ecfde3…` |
| Isolation | run with the guard, no `DATABASE_URL`, `REDIS_URL`, `JWT_SECRET` or `ENVIRONMENT`: succeeded, 0 refusals |
| Freshness test | `tests/unit/test_openapi_export.py` (collected by ci.yml's `tests` job, `testpaths = ["tests"]`, no workflow edit). Proof: a scratch summary change on `/auth/login` → `2 failed … openapi.json out of date: run python -m scripts.export_openapi`, `--check` rc=1; regenerated → `2 passed`; source restored, `git diff` empty, hash back |
| event-types.json | `event_types` = sorted `EVENT_TYPES` (5); `close_reasons` (5) from the label domain of `wasla_events_stream_closed_total` (`app/core/telemetry.py:1824`, the only closed list); `stream_frames` = closed, resync |

| Probe | Before (9c7b4e4) | After (FE0_HEAD) |
|---|---|---|
| P-O01 422 of `POST /auth/login` without `password` | body `{"error": {"code": "validation_error", …, "details": {"errors": [{"type": "missing", "loc": ["body", "password"], …}]}, "request_id": …}}`; declared `HTTPValidationError` `{detail: ValidationError[]}`, no required field, so lenient validation passes vacuously; **strict** (closed object): `$: unexpected property 'error'` | declared `ValidationErrorEnvelope`; lenient and strict **valid** |
| P-O02 | default 0; 33 operations declare nothing non-2xx; HTTPValidationError and ValidationError present | default 257; 0 declare nothing; both gone |

Method note: the prompt expected jsonschema to *fail* the before-body; FastAPI's `HTTPValidationError` has no required
field, so it accepts any object. The strict check is what makes the mismatch measurable; the plan's finding reproduces.

## 10. FEB-02: Real Bodies against the Contract

`tests/integration/test_error_envelope_contract.py`, real app, model-built database, schemas from the committed file,
lenient **and** strict validation:

| Route | Status | Code | Valid |
|---|---|---|---|
| `POST …/knowledge/bases/{id}/documents/upload` with a malformed multipart body | 400 | bad_request (Starlette) | yes |
| `GET /auth/me` without a bearer | 401 | unauthenticated | yes |
| `POST /auth/refresh` with a spent refresh token | 401 | unauthenticated | yes |
| `GET /conversations`, unverified account | 403 | email_verification_required | yes |
| `GET /audit-logs` as a member | 403 | permission_denied | yes |
| `GET /conversations/{random id}` | 404 | not_found | yes |
| `GET /api/v1/no-such-thing` | 404 | not_found | yes |
| `DELETE /auth/login` | 405 | method_not_allowed | yes |
| `POST /workspaces` with a taken slug | 409 | conflict | yes |
| `POST /auth/login` with a 200 KB body | 413 | payload_too_large (middleware) | yes |
| `POST /auth/login` missing a field | 422 | validation_error | yes |
| `POST /auth/login` with an unknown field | 422 | validation_error (`extra_forbidden`) | yes |
| `GET /conversations?limit=many` | 422 | validation_error (`loc ["query","limit"]`) | yes |
| `POST /auth/login` past the bucket | 429 | rate_limited | yes |
| 503 `retryable_conflict` | — | not reachable without a fault injector; not sampled | — |
| CORS preflight refused by Starlette | 400 text | not an API operation — correct by design | — |

No body failed: no FEB-20 from OA-05.

## 11. The E2E Profile

| Item | Value |
|---|---|
| Files | `docker-compose.e2e.yml`, `tests/e2e_stack/` (`Dockerfile`, `Dockerfile.dockerignore`, `__init__.py`, `guard.py`, `launcher.py`, `fakes.py`, `mailbox.py`, `seed.py`, `smoke.py`, `README.md`) |
| Image | `wasla-fe-e2e-app:local` (id `5fdcf442d1c9`, 1.17 GB) from `python:3.12-slim@sha256:78387bc3…` (the production base); locks by hash then `-e ".[dev]"`; build log: `66 pinned packages checked, 0 differ from the lock`, `No broken requirements found` |
| Services | pg (`pgvector/pgvector:pg16@sha256:ccc6e83d…`), redis (`redis:7-alpine@sha256:ff02b58f…`), migrate (alembic + seed, exits 0), api, worker, fakes, mailbox; volumes `wasla-fe-e2e-pg`, `-media`, `-mailbox`; network `wasla-fe-e2e-net` |
| Published | api 127.0.0.1:8000, mailbox 127.0.0.1:8025, fakes 127.0.0.1:8026 — loopback only |
| Health | pg, redis, fakes, mailbox healthy before api/worker; api healthy ≈ 5 s after start |
| Meta | `tests/fake_ownership.FakeOwnershipVerifier` (dependency override in the launcher); Graph answered by `fakes.py` (sends `wamid.E2E<n>`, media → 1×1 PNG on lookaside.fbsbx.com) |
| OpenAI | `tests.integration.ai_harness.embedding_response` (fake embeddings), `text_response` bodies; scripted agent: language-matched reply, `request_human_handoff` on agent/موظف, `search_knowledge` on price/سعر |
| Paymob | intention answer with `tests.paymob_orders.order_for`; checkout page posts the HMAC-signed `TRANSACTION` callback (`hmac_signature`) to the API (FEB-12: the API's `notification_url` is on the frontend origin) and 302s to `/billing/checkout/return` |
| Google | authorize page 302s to `GOOGLE_REDIRECT_URI` with code+state; token endpoint checks PKCE S256 and issues RS256 ID tokens; JWKS served; the API's binding cookie, state and nonce checks are the real ones. Over plain HTTP the binding cookie is `wasla_oauth` (no `__Host-` prefix), as the API sets it in developer environments |
| Email | `CapturingEmailProvider` (the repository's fake + a JSON line per message at send time) |
| No egress | launcher: socket wall (service names and private addresses only), reroute of every `GuardedTransport` request to `fakes`, `net.system_resolver` answering provider names with the placeholder 192.88.99.1 (nothing connects to it); dead proxy in every container |
| Findings while building it | none needing `app/` (no FEB-20): every fake installed from the launcher |

## 12. The Smoke Walk J1–J10

Part A (fix worktree, fresh stack, `e2e/smoke_A.txt`) and Part B (verify worktree @ FE0_HEAD, `e2e/smoke_B.txt`) gave
the same table:

| Journey | Result | What was walked |
|---|---|---|
| J1 | PASSED | register 202, mailbox code, verify 200, `/auth/me` verified, workspace 201 |
| J2 | PASSED | connect 201, signed webhook 200, `message.created` on the stream, scripted AI reply, takeover 200 + `conversation.updated`, owner reply 201, retry with the same Idempotency-Key → same id |
| J3 | PASSED | invite 201, mailbox link, accept 200, member 403/403/200, stream `closed membership_revoked`, then 404 |
| J4 | PASSED | knowledge base 201, upload 202, `pending → ready`, inbound "What is the price?" → the agent called `search_knowledge` and replied |
| J5 | PASSED | `pro` checkout 201, fake pay page 302 to the return URL, payment `succeeded`, subscription `pro` |
| J6 | EXPECTED UNTIL FE-2 | two refreshes with one token: one 200, one 401, the winner's access token then 401 (own account: the race signs the account out) |
| J7 | PASSED | switch 200; the first workspace's conversation 404 with the new token |
| J8 | PASSED | seeded staff suspends 200, stream `closed workspace_unavailable`, 403, restore 200, 200 |
| J9 | PASSED | authorize 200 + binding cookie, fake Google 302, callback 200 with tokens |
| J10 | PASSED WITH FEB-03 | inbound image stored (`storage_state=stored`); media id read from the database through `docker exec` because no message carries it (FEB-03); 200 `attachment`, `private, no-store` |
| Forward | — | signed 203.0.113.200: `401 ×10, 429`; signed 203.0.113.201: `401` (its own bucket); unsigned claim: `401 ×10, 429` (the caller's address) |

Mailbox after sending: 6 messages, all `sent` in `email_messages`, codes and the invitation link readable (the plan's
deviation 2 closed). Logs: 0 lines naming a provider host in api and worker (A and B). Wall refusals: 0 in A's final
run; 4 in B = the canary's own four deliberate lookups. `seed.py` twice → the second run "already present", staff row
unchanged; `--demo` in a second up/down cycle → workspace, agent, document and number present, second run "already
present". After every `down -v`: 0 containers, 0 networks, 0 volumes.

## 13. Mutation Check

| Mutant | Change | Killed by | Verdict | Restored |
|---|---|---|---|---|
| control | none | 130 passed (F + OA tests) | green | — |
| M-F01 | verifier returns the header's address whatever the signature | 67 tests incl. F-1 (one-field-changed), unsigned-claim, F-7 (another secret), F-8 | killed | `git diff --exit-code -- app` = 0 |
| M-F02 | freshness check removed | F-2 stale ×3, F-7 stale | killed | yes |
| M-F03 | path dropped from the canonical string | F-1 vectors ×7, F-8 | killed | yes |
| M-F04 | an unset secret becomes the empty key (forward honoured when off) | F-4 ×2 | killed | yes |
| M-O01 | post-processor not installed | OA-04 tests ×6, freshness ×2 | killed | yes |

None survived, none hung.

## 14. Documentation and ADR Changes

| File | Change |
|---|---|
| DECISIONS.md | ADR-189 (HD-01, HD-02), ADR-190 (FEB-01), ADR-191 (FEB-02 + e2e) |
| docs/API.md | "The OpenAPI document" (OA-09); 258/19 unchanged (`test_documentation_truth.py` green) |
| docs/SECURITY.md | the signed forward (FW-10) and the e2e stack's public secrets |
| docs/AUTH.md | one paragraph: the browser session (HD-02) |
| docs/DEPLOYMENT.md | `SESSION_FORWARD_SECRET`; "the e2e stack is never deployed" (E2E-10) |
| .env.example | `SESSION_FORWARD_SECRET=` with its comment (FW-09) |
| tests/e2e_stack/README.md | start, reset, accounts, mailbox, fakes, signing |

## 15. Part A Commits and the Diff Guard

| Commit | Message | Files |
|---|---|---|
| a52f733 | docs: frontend plan from the code - marketing site, customer app, platform console (cherry-pick -x) | 1 |
| 1bcfc05 | feat(auth): take the browser's address from a signed frontend-tier forward (FEB-01) | 10 |
| 3162ebc | feat(api): export the OpenAPI document and declare the error envelope (FEB-02) | 11 |
| b9e54de | test(e2e): a compose profile with deterministic fakes for the frontend journeys (FE-0) | 11 |
| 0748887 | chore(e2e): keep the e2e image's ignore file at LF | 1 (`.gitattributes`) |
| 6115432 | docs: frontend hosting and session decisions, the signed forward, the exported document (ADR-189..191) | 5 |

`git diff --stat 9c7b4e4 6115432`: 38 files, +43,536 / −4. Every path is in the union of the FEB-01, FEB-02, e2e and
docs sets plus FRONTEND_PLAN.md (`evidence/diff_names.txt`). No `alembic/versions/`, lock, `pyproject.toml`,
`.github/`, `Dockerfile`, `docker-compose.yml` or `app/core/exceptions.py` change; `docker-compose.prod.yml` +1 line;
under `frontend/` only the three JSON files. No merge commit, no amend, one author. The extra `chore(e2e)` commit
(not in the expected five) pins `*.dockerignore` to LF so a Windows checkout cannot give the e2e image's ignore patterns
a CR; it is its own logical change. Secret scan (grep of the added lines; the repository's gitleaks runs from a
container image this stage does not pull): 48 matches, every one a marked public test value or code that names a
setting; 0 real-looking keys (`evidence/secret_scan.txt`). Also public: `CREDENTIAL_ENCRYPTION_KEYS` in the e2e compose
file decodes to `e2e-public-test-key-NOT-A-SECRET`. `security.yml`'s gitleaks may report these test values (as it does
the fixtures of RGM-24); recorded, not a ci.yml gate.

## 16. Gates at FE0_HEAD and in the Verify Worktree

| Gate | Part A — fix worktree @ 6115432 (MODEL-BUILT, Windows, venv) | Part B — verify worktree @ 6115432 |
|---|---|---|
| G-1 `test_client_ip_forward.py` | 98 passed | 98 passed |
| G-1 `test_client_ip_forward_endpoints.py` | 16 passed | 16 passed |
| G-1 `test_openapi_export.py` | 16 passed | 16 passed |
| G-1 `test_error_envelope_contract.py` | 14 passed | 14 passed |
| G-2 (24 files) | auth_hardening 28, rate_limit_endpoints 11, refresh_reuse 20, google_endpoints 36, request_body_policy 24, environment_hardening 17, route_authorization 7, documentation_truth 27, deployment_configuration 69, metrics_endpoint 12, webhook_body_cap 6, trusted_proxies 19, config 78, security_config_regressions 28, rate_limit 11, rate_limit_degradation 18, metrics_collection 18, metrics 35, oauth_binding 19, inbound_metric_domains 9, telemetry_privacy 7, database_error_translation 7, body_allowances 126, request_limits 9 — 0 failed, 0 skipped | identical |
| G-3 ruff / black / mypy (`app tests`, ci.yml's flags) | clean / 1135 unchanged / 980 files no issues | identical |
| G-3 `type: ignore` | 257 → 263: `app/api/openapi.py` method-assign (FastAPI's documented `app.openapi` replacement); e2e harness: GuardedTransport method-assign, socket connect/connect_ex assignment ×2, socket flag attr-defined, a `Final` URL constant misc | 263 |
| G-4 | `--check` rc 0; two exports one hash; alembic `0138 (head)` | identical |
| G-5 | section 13 | not repeated (same tree) |
| G-6 | section 12 (A) | section 12 (B) |

UTC: Part A 07:46, Part B 07:52.

## 17. Untracked Files

`E:\wasla` held 11 `*.md` and `docs/ui/` (3 `*.html`), hashes in `evidence/untracked_before.txt`; no new file since
the last stage. Before the merge, after the merge and after the report commit: identical list and hashes. No incoming
path collides with an untracked file; no incoming path under `docs/ui/`; the report's path did not exist.

## 18. Merge and Tree Equality

`git merge --no-ff 6115432` in `E:\wasla` → `cc74d099874d1067ad12f16e7acc12ec9f69860b`, parents `9c7b4e4`, `6115432`;
no conflict; `git diff --exit-code 6115432 HEAD` → 0; Alembic `0138`, single head. The development stack was not
started, migrated or rebuilt.

## 19. Gates on the Final Canonical Tree

Verify worktree (clean) re-pointed to `cc74d09`, database `wasla_fe0_final`, 08:01 UTC: G-1 98 / 16 / 16 / 14 passed;
ruff clean, black 1135 unchanged, mypy 980 files clean; export `--check` rc 0; Alembic `0138 (head)`.

## 20. Push

| | Value |
|---|---|
| origin before | `9c7b4e48512885f5cbb76cc7a8b02a5520e86a3d` (re-fetched just before the push: unmoved) |
| fast-forward check | `git merge-base --is-ancestor origin/worktree-billing-google-auth HEAD` → true |
| command | `git push origin worktree-billing-google-auth` (no force) → `9c7b4e4..b338a44` |
| origin after | `b338a446b205741264fecf2e622a5b333d449e22` = local |
| Not pushed | `fe0-backend-prereqs-20261107`, `frontend-plan-20261107`, any tag; `main` untouched (`8e3a1ba`) |

## 21. GitHub Actions

`ci.yml` run **42354012088** on `b338a44` — https://github.com/mohamedshhahat1/wasla/actions/runs/42354012088 —
GitHub ci.yml, Linux runners, **success**:

| Job | Conclusion | Duration |
|---|---|---|
| Lint, format, types | success | 1 m 33 s |
| Tests and migrations | success | 70 m 36 s |
| Migration-built schema | success | 49 m 0 s |
| Docker build | success | 1 m 29 s |
| Alert rules and receivers | success | 2 m 35 s |

Tests job: `8077 passed, 17 skipped, 2 deselected` (the full suite; 9c7b4e4's run 42319764942 had 7,932 passed / 17
skipped: +145 = this stage's 144 new tests and one new `FEATURE_SETTINGS` parameter); the skip gate passed (17, all
accepted reasons); the drift guard `66 pinned packages checked, 0 differ from the lock`, `No broken requirements found`.
Under `-q` the log names no test individually; the freshness test, F-1..F-12, the OA tests and the contract test are in
the collected `tests/` and in the count, and the migration-built job ran the integration ones against the migrated
schema. No section 23 fix was needed; nothing was re-run. `security.yml` was not dispatched; its last run
(42315411238) stays red for RGM-23, RGM-24, RGM-27 — recorded, not this stage's. The addendum's own run is recorded in
`E:\wasla-cleanup\CLEANUP_fe0_20261107.md`.

## 22. Findings (FEB-20 ..)

| ID | Finding | Status |
|---|---|---|
| FEB-20 | The stream's close reasons have no named constant: the five strings are written at `app/services/realtime.py:290-296` and `app/serve.py:42`, and the only closed list is the metric label domain at `app/core/telemetry.py:1824`, which `event-types.json` exports. A reason added in realtime.py without the domain would be counted as `other` and missing from the file. | ACCEPTED RESIDUAL (for audit 23 with the frame schema) |
| FEB-21 | Over plain HTTP (developer environments) the Google binding cookie is `wasla_oauth`, not `__Host-wasla_oauth`; FE-2's `/session/google/*` relay must handle both names, or the e2e stack must run behind TLS. | EXTERNAL VERIFICATION (FE-2) |

No reader of the address bypassing `client_identity` is security-relevant; no response failed the envelope; no fake
needed an application change.

## 23. Decisions Ledger

| Item | Status |
|---|---|
| FEB-01 | CLOSED (F-1..F-12 green locally and in ci.yml 42354012088, M-F01..M-F04 killed, P-F01/P-F02 after as required) |
| FEB-02 (P0 parts) | CLOSED (export without dependencies, envelope on every operation, real bodies valid, freshness test green in ci.yml, M-O01 killed) |
| FEB-02 (P2: operationIds, info.version, frame schema) | DEFERRED PRODUCT DECISION (audit 23) |
| E2E profile | CLOSED (up/down clean twice, canary refused, J1–J10 each PASSED, EXPECTED UNTIL FE-2 or PASSED WITH FEB-03) |
| HD-01, HD-02 | CLOSED (ADR-189; nothing created outside the repository) |
| PL-01 | CLOSED |
| FW-01 .. FW-10 | CLOSED |
| OA-01 .. OA-09 | CLOSED (OA-05 with the subset validator, recorded) |
| E2E-01 .. E2E-10 | CLOSED (E2E-02 fakes published on loopback, E2E-04 dummy OpenAI key — both recorded) |
| FEB-20 | ACCEPTED RESIDUAL |
| FEB-21 | EXTERNAL VERIFICATION |
| J1, J2, J3, J4, J5, J7, J8, J9 | CLOSED (PASSED on the profile) |
| J6 | ACCEPTED RESIDUAL — EXPECTED UNTIL FE-2 |
| J10 | ACCEPTED RESIDUAL — PASSED WITH FEB-03 |
| FEB-03 .. FEB-19, FEB-14, forward-secret rotation, real CORS domains, FEB-12, Cloudflare proxy | DEFERRED PRODUCT DECISION (their owners, section 4) |
| Light gates in place of nightly / time-shifted / kept-data / local Linux / full local lanes | ACCEPTED RESIDUAL (the owner's choice) |

## 24. Infrastructure Created by This Stage

| Kind | Name |
|---|---|
| Containers | `wasla-fe0-pg` (55921), `wasla-fe0-redis` (56921) |
| Network | `wasla-fe0-net` |
| Image | `wasla-fe-e2e-app:local` (no `wasla-fe-e2e-*` container, network or volume remains) |
| Worktrees | `E:\wasla-fe0-fix` (branch `fe0-backend-prereqs-20261107`), `E:\wasla-fe0-base` (detached 9c7b4e4), `E:\wasla-fe0-verify` (detached, now cc74d09) |
| Folder | `E:\wasla-fe0-notes` (venv, draft, logs, evidence, probes, mutation, e2e, scripts) |
| Branch | `fe0-backend-prereqs-20261107` (kept) |

## 25. Part C — Cleanup (planned)

Only if the verdict is `FE-0 MERGED AND PUSHED, CI GREEN` (the addendum's run green too): rescue scripts, probes, the
mutation runner, smoke tables, logs, evidence and the draft to `E:\wasla-verification-scripts\fe0\` (SHA-256 verified,
never the venv); then remove `wasla-fe0-pg`, `wasla-fe0-redis` (`docker rm -v`), `wasla-fe0-net`, the
`wasla-fe-e2e-app:local` image (after proving no `wasla-fe-e2e-*` container), the worktrees `E:\wasla-fe0-base`,
`-verify`, `-fix` (`git worktree remove`, branch kept), `git worktree prune`, then `E:\wasla-fe0-notes`. Kept: every
branch, the plan's and the last stage's leftovers, the developer stack, `docker-compose.e2e.yml` and
`tests/e2e_stack/`. The record goes to `E:\wasla-cleanup\CLEANUP_fe0_20261107.md` (not committed).

## 26. Remaining External Actions

| Action | Owner |
|---|---|
| FE-1 (the monorepo under `frontend/`, generated from `frontend/packages/api-client/openapi.json`) | its own prompt |
| FEQ-04 domains, FEQ-05 console gate, FEQ-06 app locale, the other FEQ-* of plan §31 | the owner |
| At frontend deployment: one `SESSION_FORWARD_SECRET` (≥32 bytes) on the API and the app's and console's Workers; `CORS_ORIGINS`; the Cloudflare account, Workers, Access, DNS | operator, after FE-1's preview step |
| FEB-07 before FE-3; FEB-03, FEB-04 (FEB-05) before FE-4; FEB-10 before FE-9; FEB-08 before FE-10; FEB-06 per stage; FEB-12 before FE-8; FEB-20, FEB-21 | backend stages |
| audit 23 (operationIds, info.version, frame schema, FEB-14); audit 22 (forward-secret rotation, real domains); audit 20 (Cloudflare in front of the API) | audits |
| `security.yml` red (RGM-23, RGM-24, RGM-27) | the owner, unchanged |
| The plan's leftovers (wasla-fep-pg, wasla-fep-redis, E:\wasla-fep-notes with spikes\gen, E:\wasla-fep-plan and its branch); the last stage's (E:\wasla-rdi-notes, E:\wasla-rdi-fix) | the owner |
| Local branches (`fe0-backend-prereqs-20261107`, `frontend-plan-20261107`, report branches) | keep or delete after review |
| Merging the canonical branch into `main` | the owner |

## 27. What FE-1 Can Rely On

| Path / contract | What it is |
|---|---|
| `FRONTEND_PLAN.md` | the plan, byte for byte |
| `DECISIONS.md` ADR-189 | Cloudflare (Workers + OpenNext; static assets for marketing); S-2 session |
| `frontend/packages/api-client/openapi.json` | the generator's input; every error typed as `ErrorEnvelope` / `ValidationErrorEnvelope`; regenerate with `python -m scripts.export_openapi`; the backend CI refuses a stale file |
| `frontend/packages/api-client/event-types.json` | event types, close reasons, control frames for `events.ts` |
| `frontend/packages/api-client/fixtures/forward-signature.v1.json` | FE-2's signer's test vectors |
| docs/SECURITY.md "The browser's address through the frontend tier" | the signed-forward specification |
| `docker-compose.e2e.yml` + `tests/e2e_stack/README.md` | the backend for Playwright: api :8000, mailbox :8025, fakes :8026; staff `staff.e2e@example.com` |
| `python -m tests.e2e_stack.smoke` | the API-side walk of J1–J10 |
| FE-1 owns everything else under `frontend/` and must keep these paths | — |

## 28. Final Verdict

```text
FE-0 MERGED AND PUSHED, CI GREEN
FE-1 CAN START                  FEB-02 CLOSED and on origin; HD-01 and HD-02 recorded (FEQ-04 domains may stay placeholders)
FE-2 CAN START AFTER FE-1       FEB-01 CLOSED and on origin
CLEANUP                         runs after this addendum's ci.yml run is green; result in E:\wasla-cleanup\CLEANUP_fe0_20261107.md
```

FEB-01 CLOSED, FEB-02 (P0) CLOSED, the e2e profile CLOSED, HD-01 and HD-02 CLOSED — locally and on GitHub's full
suite. If FE-1 started tomorrow from origin it would find the plan, the two decisions, and a committed contract that
tells the truth about every error and that the backend CI refuses to let drift; FE-2's sign-in through the tier would
give each browser its own bucket and no browser a way to choose one; and every later stage can bring the real backend
up with one command and nothing leaving the machine. The full suite behind that answer was run by GitHub, not locally,
by the owner's choice; the inbox, the console and the marketing site still wait for FEB-03/04/05, FEB-07, FEB-08 and
FEB-10.
