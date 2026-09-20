# Security Findings Remediation — security-s9q4

**Scope:** remediation of [SECURITY_AUDIT.md](SECURITY_AUDIT.md), not a new audit. The repository release blocker is closed in code. Production deployment and merchant configuration remain unverified.

| Item | Value |
| --- | --- |
| Audit id | `security-s9q4` |
| Baseline canonical HEAD | `c1bf1e0ec4ebb14a22c4b5d0d2cba34603e38a3e` |
| Audit report commit | `3885286` |
| Remediation branch / worktree | `security-findings-remediation` / `E:\wasla-security-remediation` |
| Frozen code/test HEAD | `68ff951667c4f3b2ad2a1f33e1fba61973ce2ba1` |
| Documentation head before this report | `c14acbf5ac689d8a4762892cad23748cee6eaf71` |
| Alembic | `0068` → `0069` (one head; schema check clean) |
| Push / merge | Neither performed |

## Isolation and evidence

Verification used the isolated `wasla-security-rem-q8z4` stack, PostgreSQL system identifier `7687286831541780525`, and a dedicated Redis instance with run id `6c7f0581c737352b1e9155873639586aa0da6ede`. Model-built, migration-built, schema-check, focused-probe, and mutation databases were separate. Redis DBs used for concurrent lanes were separate. Mutable test databases were recreated before authoritative full runs. No developer Redis DB 0 or main worktree was used.

At the frozen code/test HEAD:

| Gate | Result |
| --- | --- |
| Ruff / Black | Passed; Black checked 679 files |
| MyPy | Passed; 607 source files, non-incremental |
| Alembic heads / check | `0069 (head)`; no new upgrade operations |
| Fresh Alembic upgrade | `0001` through `0069` passed |
| Model-built whole `tests/` | **5,386 passed, 132 skipped, 0 failed** (1,260.85 s) |
| Migration-built `tests/integration` + `tests/e2e` | **2,561 passed, 106 skipped, 0 failed** (1,159.11 s) |
| Focused security, model-built | **635 passed, 0 failed** across 27 files |
| Focused security, migration-built | **262 passed, 0 failed** across 15 integration files |
| Non-vacuous security invariant sweep | **8 passed, 0 failed** |
| Production Compose rendering | Passed with `config --no-interpolate --quiet` |

The audit baseline was 5,150 passed / 17 skipped model-built and 2,587 passed / 2 skipped migration-built. This remediation ran on Windows, where POSIX script execution is unavailable; the additional platform skips make passed/skipped counts unsuitable for a direct subtraction. Shell tests now check that the discovered shell actually runs, and Linux CI still executes them. Tests that spawn their own pytest process isolate their environment from the parent's strict database gate. The focused suites had no skips.

The runtime probe matrix is represented by real HTTP/integration tests and workflow/config structural tests. Its principal file populations passed: release trust and delivery pipeline **61**; request body policy **24**; malformed authenticity **30**; NUL boundaries **13**; production configuration and Compose **88**; Google OAuth concurrency **4** in each schema mode; Paymob webhook endpoint **19** and token/protection unit tests **32**; runtime PostgreSQL role **1** in each schema mode. The release tests evaluate fork origin, event, repository, branch and privileged-job gating. Request tests cover oversized, chunked and already-rate-limited bodies before parsing. Webhook tests cover Meta, Resend and Paymob non-ASCII authenticity values. The remaining focused files cover host poisoning, Graph redirects, pagination, account enumeration, tenant isolation, callback correlation, replay, and API/log secrecy. These counts name entire passing files; they are not a claim that every case in each file is an independent exploit probe.

## Finding ledger

| Finding | Status | Resolution / evidence |
| --- | --- | --- |
| SEC-01 HIGH | **CLOSED in repository** | `deploy.yml` publish and deploy require successful same-repository `push` to `main` for `workflow_run`. Privileged checkout is behind the guard, uses the CI `head_sha`, and does not persist credentials. Release tests simulate fork and pull-request provenance; deployment uses a scanned image digest. GitHub settings remain DV-S1. |
| SEC-02 MEDIUM | **CLOSED** | Public JSON receives are capped before expensive parsing; public rate limiting runs before body consumption. Bounded receive spies cover oversized, chunked and already-limited requests without an OOM probe. |
| SEC-03 LOW | **CLOSED** | Non-ASCII callback signatures, HMACs and verification tokens fail as controlled authenticity errors rather than raising a 500; constant-time byte comparison remains pinned. |
| SEC-04 LOW | **CLOSED** | NUL is refused in storable free text, including workspace names, outbound messages and lead search; regressions exercise the HTTP boundary. |
| SEC-05 LOW | **CLOSED locally; DV-S2 open** | Production Compose separates migration-owner and runtime database URLs. Idempotent provisioning refuses elevated preexisting runtime roles. A real PostgreSQL test proves runtime CRUD and future-table access, while `rolsuper`, `rolcreatedb`, `rolcreaterole`, `rolbypassrls`, schema creation, role creation and database creation are denied. Actual production identity remains unverified. |
| SEC-06 LOW | **CLOSED** | Internet-reachable staging and production reject wildcard/null/plain-HTTP CORS, all-address trusted proxies, disabled rate limiting and unsafe `APP_PUBLIC_URL` userinfo. Production docs stay disabled; tests pin the failures. |
| SEC-07 LOW | **HARDENED** | Python runtime/build lock files are versioned with hashes and consumed by Docker with `--require-hashes`; base images and production Compose images are digest-pinned. A local builder-image build and `pip check` passed. `pip-audit` remains in `security.yml` and Trivy gates release, but a local `pip-audit` attempt could not reach the package index (`WinError 10013`); no clean vulnerability result is claimed. Image updates are documented. A read-only root filesystem remains future hardening. |
| SEC-08 INFO | **CLOSED by PD-1** | Staging uses production-like docs and explicit HTTPS CORS defaults. |
| SEC-09 INFO | **CLOSED by PD-2** | New and existing-address registration return the same public `202` response; existing accounts are not changed and receive a safe mailbox notification. Concurrent registration tests pin the public answer. |
| SEC-10 INFO | **CLOSED by PD-3** | Paymob's reusable saved-card `obj.token` is authenticated, correlated and stored as a row-bound AES-GCM envelope with an HMAC fingerprint. Migration `0069` protects legacy rows transactionally; the exact token is recovered only at the renewal boundary. Live Paymob settings remain unverified. |
| SEC-11 INFO | **CLOSED** | Google ID tokens require one exact configured audience and validate `azp` when present. This follows the current [Google OIDC reference](https://developers.google.com/identity/openid-connect/reference) and [OIDC Core](https://openid.net/specs/openid-connect-core-1_0-18.html); OAuth race and token-shape tests pass. |
| SEC-12 INFO | **FUTURE HARDENING** | Minimum JWT secret length remains enforced. No home-grown entropy estimator was introduced; secret generation and rotation are deployment responsibilities. |
| SEC-13 INFO | **CLOSED** | Production configuration now requires `META_VERIFY_TOKEN`; absence already failed the subscription handshake closed with 503. |

The original structural gaps are closed: S04 and S04b are **structural constant-time-helper assertions**, not timing measurements; S10 pins production CORS rejection, S11 pins Graph bearer redirect refusal, and S19 pins the lead page cap. Permanent tests also cover OAuth state/identity races, password-reset host poisoning, release provenance, pre-parse request cost, malformed signatures and NUL validation. No grant or webhook replay semantics were widened.

## Product decisions

| Decision | Implemented outcome |
| --- | --- |
| PD-1 | Staging shares production's safe docs and HTTPS CORS policy; local/test development remains available. |
| PD-2 | Registration uses indistinguishable public `202` responses and a safe existing-account email. |
| PD-3 | Reusable Paymob card tokens remain reversible only through authenticated encryption; a separate keyed fingerprint supports deduplication. |
| PD-4 | `/metrics` remains without application authentication for internal scraping. Verify network/proxy exposure under DV-S3; metrics must exclude tokens and customer payloads. |
| PD-5 | Production OpenAPI/docs stay disabled; staging defaults disabled, while local/test docs remain available. |

## Paymob Saved-Card Contract

Official Paymob pages consulted, with their displayed Last Updated dates:

| Page | Last Updated |
| --- | --- |
| [Create Card Token](https://developers.paymob.com/paymob-docs/developers/pay-with-saved-cards/create-card-token) | August 24, 2026 |
| [HMAC Card Token Callback](https://developers.paymob.com/paymob-docs/developers/webhook-callbacks-and-hmac/hmac/hmac-for-card-tokens) | August 24, 2026 |
| [CIT](https://developers.paymob.com/paymob-docs/developers/pay-with-saved-cards/cit) | June 28, 2026 |
| [MIT](https://developers.paymob.com/paymob-docs/developers/pay-with-saved-cards/mit) | June 28, 2026 |
| [Card Token Inquiry](https://developers.paymob.com/paymob-docs/developers/transaction-inquiry-apis/card-token-inquiry) | August 4, 2026 |

Wasla creates an intention and sends the customer to Paymob's UI to save a card. Paymob posts a `type: TOKEN` card-token callback. Wasla stores **`obj.token`**, the reusable card token, rather than `obj.id` (the token record identifier), the intention's `client_secret`, or a one-payment `payment_token`. The callback HMAC is HMAC-SHA-512 over Paymob's documented ordered fields `card_subtype`, `created_at`, `email`, `id`, `masked_pan`, `merchant_id`, `order_id`, `token`; the digest in the query string is compared safely before persistence. The documented Paymob order identifier is resolved to Wasla's existing checkout/order and its tenant; callback email or masked PAN alone cannot attach a card. Invalid HMAC, unknown order, wrong customer correlation and duplicate delivery are exercised through the HTTP endpoint.

Wasla's later off-session renewal is the saved-card **MIT** flow, not Paymob's Subscriptions Module. It creates a MOTO intention, takes that intention's `payment_keys[0].key` as the distinct `payment_token`, and sends the original saved card token as `source.identifier` with `source.subtype: TOKEN` in the Pay Request. Fake-transport tests assert the exact decrypted token reaches that field; no live card or charge was used. Wasla does not currently initiate Paymob's saved-card CIT flow. Paymob documents Card Token Inquiry by order id; the authenticated callback remains the primary persistence path, and this remediation did not add an unnecessary inquiry call.

`payment_methods.provider_token` stores an AES-GCM envelope bound to the row and payment-token domain. The existing credential key ring supports rotation; the controlled renewal service decrypts immediately before the MIT request. A dedicated `PAYMENT_TOKEN_FINGERPRINT_KEY` produces an HMAC-SHA-256 lookup/deduplication fingerprint. Token plaintext is excluded from API responses, repr, logs and audit metadata. Tampered ciphertext, wrong keys and wrong-row use fail closed. The non-secret provider record id remains a separate 200-character field; ciphertext has a 512-character field.

Migration `0069` distinguishes legacy plaintext from protected envelopes, encrypts existing rows and computes fingerprints before dropping plaintext uniqueness. Its PostgreSQL transaction prevents partial rollout; tests seed a legacy method, verify exact token recovery, and verify downgrade refuses to expose saved cards as plaintext. Set encryption and fingerprint keys before migrating, retain old encryption keys during rotation, and keep the fingerprint key stable unless explicitly rehashing all rows. **Production token population, duplicates, nulls and provider distribution were not inspected**; obtain aggregate counts without printing token values before deployment. No production Paymob API or merchant setting was changed.

## Database, invariant and mutation proofs

The real PostgreSQL role test passed in both model and migration schema lanes. The migration owner upgraded a fresh database through `0069`; the runtime role performed application-style inserts, updates, selects and deletes, including a newly provisioned table, while privileged operations failed. This proves the local role design, not the role currently configured on a production host.

The eight-test security invariant sweep used populated fixtures rather than empty-table checks: a legacy saved-card method was protected; seeded cross-tenant relations had zero violations; two competing Google logins produced one identity; a genuinely revoked member was refused across workspace routes; reset and verification plaintext differed from stored hashes; platform access audit omitted customer payload; and a Paymob callback left no secret in logs. Production data was not swept, so production-wide counts for these properties remain unverified. The payment-token migration test also proves rollback cannot reintroduce plaintext.

The final replay of `scripts/run_security_mutations.py` applied **R01–R22** and original **S04, S04b, S10, S11, S19**: **27 applied, 27 killed, 0 survived, 0 inapplicable**. Every mutated source SHA-256 was restored. Results exactly matched the committed [remediation mutation evidence](security_mutation_results.jsonl) and [original gap evidence](original_security_mutation_results.jsonl). S04/S04b kills are reported as structural, not functional timing kills. No mutation was classified as killed merely because infrastructure was unavailable.

## Score

The original audit's dimensions and weights are unchanged. Scores below reflect repository evidence only; CI/supply chain stays capped while DV-S1 and live scanning are unverified. Operational readiness stays at 7.0 because deployment checks remain open.

| Dimension | Weight | Final /10 | Weighted |
| --- | ---: | ---: | ---: |
| Authentication boundary | 0.09 | 9.5 | 0.855 |
| Authorization / tenant isolation | 0.10 | 9.5 | 0.950 |
| OAuth / identity federation | 0.06 | 9.6 | 0.576 |
| Webhook authenticity / replay | 0.07 | 9.2 | 0.644 |
| Secret management | 0.06 | 9.0 | 0.540 |
| SSRF / outbound credential safety | 0.05 | 9.5 | 0.475 |
| Injection safety | 0.06 | 9.5 | 0.570 |
| Input / resource bounding | 0.07 | 9.0 | 0.630 |
| Security configuration | 0.06 | 9.0 | 0.540 |
| Privacy / logging | 0.05 | 9.0 | 0.450 |
| Abuse resistance | 0.05 | 8.0 | 0.400 |
| Queue / background trust | 0.04 | 9.0 | 0.360 |
| Storage security | 0.04 | 8.5 | 0.340 |
| CI / supply chain | 0.07 | 7.0 | 0.490 |
| Container / runtime hardening | 0.04 | 7.5 | 0.300 |
| Testing | 0.05 | 9.0 | 0.450 |
| Operational security readiness | 0.04 | 7.0 | 0.280 |
| **Total** | **1.00** | | **8.85 / 10** |

The audit baseline was **8.18 / 10**. The main gains are release provenance, pre-parse resource bounds, callback robustness, production configuration, saved-card protection, and permanent regression/mutation coverage. Unchanged dimensions retain their audit score.

## Deployment verification and future hardening

These are **not locally verified**:

| ID | Production check |
| --- | --- |
| DV-S1 | GitHub fork workflow approval, production environment reviewers/branch restrictions, and live GHCR `main`/`latest` provenance. |
| DV-S2 | Actual production API/worker PostgreSQL role and its four non-elevated flags. |
| DV-S3 | Public TLS, HSTS and proxy-only `TRUSTED_PROXY_IPS`; `/metrics` network restriction. |
| DV-S4 | S3, OTLP, database and Redis transport or trusted private network; Redis password. |
| DV-S5 | Production bucket private ACL and denial of anonymous list/get/put. |
| DV-S6 | Google production console redirect URI allow-list. |
| DV-S7 | nginx per-location request body limits. |
| DV-S8 | Secret-store ACLs and rotation procedure for JWT, Meta and encryption keys. |

Paymob deployment checks: separate live/test credentials; current integration and MOTO IDs; card-saving enabled where required; correct HTTPS callback notification URL and HMAC secret; dashboard callback configuration matching the code; and merchant capability for the MIT flow Wasla uses. Confirm aggregate legacy token population before `0069` on an existing volume. No live Paymob call or charge was performed.

The audit's earlier DV-1–DV-8 and other carried items remain open: real Meta inbound-media verification, production alert delivery, operator handling of CRM 409, a production CRM invariant sweep, and readiness/Alembic revision alignment. Future repository hardening includes SEC-12 secret-quality policy and a read-only container root filesystem. Neither changes this task's completed repository findings. Deployment verification, merge and push require separate instructions.
