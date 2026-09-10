# Wasla Authentication & Account Security Remediation

Report date: 2026-09-10

Audit reference: `AUTH_ACCOUNT_SECURITY.md` (2026-09-08)

Scope: AUTH-01 through AUTH-10 at the current working tree

## 1. Executive Summary

All ten reported findings were revalidated against the current branch rather than assumed from the audit. AUTH-01 through AUTH-10 are remediated in code and have focused regression coverage. The central outcomes are: deleted accounts are irreversible authentication tombstones; database uniqueness races return controlled responses; trusted-client identity is not rewritten by Uvicorn; password-reset mail has a canonical-account budget; unverified users are limited to onboarding/recovery; credential-bearing outbox context is AES-GCM encrypted; auth events are measurable without identity labels; and critical OAuth tests fail loudly and exercise browser binding directly.

Code readiness: **ready for pre-production integration review**.

Deployment readiness: **not yet verified**. A deployment must provide and retain the encryption key ring, verify proxy topology, connect metrics/alerts, and complete provider/browser checks. No deployment, push, merge, or production configuration change was made.

## 2. Repository State

| Item | Value |
|---|---|
| Initial branch | `worktree-billing-google-auth` |
| Initial HEAD | `4f837ab6c7b4145cbeaadbca976040528ff01325` |
| Audit revision | `79e9a15e73ee3772d525f5628f24f77aafd66712` |
| Initial dirty state | Clean |
| Migration head | `0048` |
| Final content state | Remediation code, tests, CI and documentation committed locally; final commit is reported in the handoff |
| External changes | None |

No `AGENTS.md` was present. Changes between the audited revision and initial HEAD were reviewed; unrelated currency work was preserved.

## 3. Findings Revalidation

| Finding | Audit status | Reproduced before fix? | Still applicable at initial HEAD? | Action | Final status |
|---|---|---:|---:|---|---|
| AUTH-01 deleted authentication | High | Yes | Yes | Tombstone-aware repositories, lifecycle checks, atomic delete/backfill | Fixed |
| AUTH-02 registration race 500 | Medium | Yes | Yes | Savepoint and named-constraint translation | Fixed |
| AUTH-03 invitation race 500 | Medium | Yes | Yes | Atomic conditional claim/revoke | Fixed |
| AUTH-04 proxy preprocessing | Medium | Yes | Yes | `--no-proxy-headers` in shipped entrypoints | Fixed for supported topology |
| AUTH-05 reset mailbox abuse | Medium | Yes | Yes | Canonical account budget plus bounded fallback | Fixed |
| AUTH-06 verification informational | Product decision | Yes | Yes | Central verified-user dependency on business/platform routes | Implemented |
| AUTH-07 plaintext pending secrets | Medium | Yes | Yes | AES-GCM sealed context, worker-only decryption | Hardened |
| AUTH-08 observability gap | Low | Yes | Yes | Bounded auth metric, events, alerts and runbook | Improved; receiver external |
| AUTH-09 logout access lifetime | Low/documentation | Yes | Yes | Contract retained, tested and documented | Documented |
| AUTH-10 silent/false-green tests | Medium | Yes | Yes | Security prerequisite failure and direct binding controls | Fixed |

## 4. Product Decisions Implemented

- Verification: authentication and recovery remain available, but workspace and platform business authority requires `email_verified_at`.
- Deletion: deletion is an irreversible tombstone, sets `is_active=false`, and increments `token_version` atomically.
- Email reuse: tombstoned emails and provider identities are not reusable.
- Logout: single logout revokes the supplied refresh token; an already-issued access token may live for at most its configured TTL. Logout-all, reset, disable, delete and refresh-replay teardown invalidate all tokens immediately.
- Google mailbox verification: Gmail and matching Google Workspace hosted-domain claims may satisfy Wasla verification. Other Google identities authenticate by stable issuer/subject but must complete Wasla email verification. This follows Google's documented distinction between Google-authoritative and third-party email domains.

## 5. AUTH-01 Remediation

Ordinary ID/email lookups exclude `deleted_at IS NOT NULL`; explicitly named administrative lookups include tombstones. Password login, access dependency, refresh, federated issuance, Google link/unlink/resume, verification, reset and invitation acceptance all fail closed for deleted identities. The platform delete operation changes `deleted_at`, `is_active` and `token_version` in one PostgreSQL statement and cannot target the caller. Enabling a tombstone returns not found.

Runtime regression: both intentionally inconsistent legacy states (`deleted=true, active=true` and `deleted=true, active=false`) returned 401 for login, access, refresh and workspace switch. Google login/link/unlink and invitation acceptance also refused tombstones without replacement identity creation.

## 6. AUTH-02 Remediation

Registration executes within a savepoint. Only PostgreSQL constraints `uq_users_email` and `uq_tenants_slug` are translated; unrelated integrity faults remain visible defects. Barrier results for exact, case-varied and outer-whitespace-varied same-email races were one 201 and one 409, with one user and one bootstrap. The same-slug race was one 201 and one 409.

## 7. AUTH-03 Remediation

Invitation use is a conditional `UPDATE ... WHERE status=pending AND expires_at>now RETURNING ...`. Concurrent calls cannot both claim. Existing- and new-account barrier cases each produced one success and one uniform invalid-token response, one accepted invitation and one membership. Revoke uses a same-tenant existence check for 404 non-disclosure, then an atomic pending-only update; accept-vs-revoke has exactly one final state winner.

## 8. AUTH-04 Remediation

Both shipped Uvicorn invocation modes explicitly use `--no-proxy-headers`; Wasla remains the sole interpreter of trusted forwarding headers. Real loopback socket evidence with rotating forged `X-Forwarded-For`: hardened server returned ten 401 responses followed by two 429 responses. A temporary negative-control server using Uvicorn proxy trust returned twelve 401 responses, demonstrating the bypass and the test's non-vacuity. Both processes were stopped and Redis DB 14 was cleared.

## 9. AUTH-05 Remediation

Password-reset requests spend a canonical-email budget (default three/hour) independent of the source-IP budget. Redis is the distributed source; a bounded local fallback retains protection during Redis failure. Suppressed requests retain the same generic 202 response. A source-rotation regression generated only the allowed two rows in its test policy; bypassing the account limiter generated four and failed the assertion.

## 10. AUTH-06 Verification Gate

`VerifiedUserDep` sits between authentication and `ActiveWorkspace`, and platform role guards also consume it. Route-graph tests classify all operations: `/auth/me`, verification, recovery, refresh/logout and Google identity onboarding remain reachable; workspace switching and all material workspace/platform routes require verification. Unverified business requests return 403 with `email_verification_required`. Existing fixtures that intentionally test post-onboarding business behavior now explicitly mark the account verified.

## 11. AUTH-07 Outbox Encryption

- Primitive: the existing `CredentialCipher` AES-GCM envelope implementation; no custom cryptography.
- Key source: `CREDENTIAL_ENCRYPTION_KEYS`, required when email delivery is enabled. First key encrypts; retained older keys decrypt for rotation.
- AAD: the immutable outbox idempotency key, namespaced as `email-outbox:<key>`.
- DB representation: sensitive templates store only `{ "sealed": "<versioned envelope>" }`; verification codes, reset tokens and invitation tokens do not appear in JSONB plaintext.
- Runtime: only the worker opens context immediately before rendering. Successful provider acceptance clears context.
- Failure: missing keys, malformed/invalid ciphertext and unexpected plaintext fail safely with a bounded permanent render error and no secret in logs/metrics.
- Backup classification: pending ciphertext and the key ring must be protected and retained together only for the intended rotation window.

Current key, rotated old key, wrong key, corrupted ciphertext, plaintext rejection, provider retry, permanent failure and clear-after-send are tested.

## 12. AUTH-08 Observability

`wasla_auth_security_events_total(event,outcome,reason)` is defined and emitted for login, access/lifecycle blocks, rate limits, reset suppression, verification failure/exhaustion, OAuth start/callback/collision, refresh replay, invitation invalid/replay, provider failure and webhook signature failure. Labels are fixed categories; email, IP, user/workspace IDs, tokens and provider subjects are absent. Tests exercise emission paths. PromQL alert recommendations and operator actions are documented. Production scraper, receiver and paging delivery are externally unverified.

## 13. AUTH-09 Logout Contract

The short-lived stateless access-token contract was retained. Tests prove logout makes its refresh token unusable while the access token can survive up to `ACCESS_TOKEN_TTL_SECONDS`; logout-all and the security/lifecycle events listed above invalidate current access and refresh credentials through `token_version`.

## 14. AUTH-10 Test Quality

With `WASLA_SECURITY_TESTS=1`, execution without an explicit `TEST_DATABASE_URL` fails with a direct prerequisite error rather than skipping. The security workflow supplies isolated PostgreSQL and Redis. Positive controls reach the browser-binding comparison for both Google login and authenticated link. Forcing the comparison true changed both expected 401 outcomes to 200, killing the mutation.

## 15. Database / Migrations

Migration `0048` backfills every deleted-but-active row to inactive and increments its token version. Downgrade intentionally does not resurrect identities. On task-owned isolated databases:

- fresh database `upgrade head` to 0048: pass;
- `alembic current`: 0048;
- `alembic check`: no new upgrade operations;
- downgrade to base, re-upgrade to head, second check: pass;
- synthetic 0047 row (`deleted_at` set, `is_active=true`, version 7) upgraded to `is_active=false`, version 8;
- downgrade to 0047 left it inactive at version 8, as required by no-resurrection policy.

No customer/production database or historical data was touched.

## 16. Concurrency Results

| Scenario | Expected/final result |
|---|---|
| Same email (exact/case/whitespace) | one 201, one 409; one identity/bootstrap |
| Same workspace slug | one 201, one 409; one tenant |
| Same invitation, new account | one winner; one controlled loser; one user/membership |
| Same invitation, existing account | one winner; one controlled loser; one membership |
| Invitation accept vs revoke | exactly one accepted/revoked state winner |
| Invitation expiry boundary | no claim at equality |
| Verification same code | existing barrier regression: one winner, one 422 |
| Reset same token | existing barrier regression: one winner, one 401 |
| Refresh same token | existing barrier regression: one winner, replay teardown invalidates the branch |
| Deleted lifecycle | sequential cross-path and atomic-delete tests pass; a separate delete-vs-login/refresh/Google barrier was not added |

All newly added committed-connection cases use independent PostgreSQL sessions and clean only their generated rows.

## 17. Mutation Results

| Mutation | Detecting test/result | Status |
|---|---|---|
| Remove deleted filters | lifecycle test changed expected 401 to 403; service lifecycle layer still blocked the attack | Killed; redundant defense survived |
| Remove registration race translation | three same-email cases raised unique violations instead of controlled loser | Killed |
| Restore invitation read-then-write | existing/new acceptance races raised user/membership unique violations | Killed |
| Bypass verification dependency | business test got generic entitlement denial rather than verification denial; entitlement layer still blocked action | Killed; redundant defense survived |
| Bypass reset account bucket | source-rotation test created four rows instead of two | Killed |
| Trust forged XFF | real-socket negative control returned twelve 401s instead of limiting after ten | Killed |
| Persist plaintext outbox context | at-rest/open-context regression failed safely | Killed |
| Force OAuth binding match | login and link mismatch controls changed from 401 to 200 | Killed (both) |

Every mutation was restored; redundant defenses were not weakened to manufacture a failure.

## 18. HTTP E2E

Real TCP/Uvicorn, PostgreSQL and Redis were exercised for the proxy/rate-limit sequence above. The repository's real-socket billing E2E also passed all four scenarios using a real registration/access token, verified-business gate, database commits and webhook routing (only the payment provider transport is mocked). The complete requested register→verify→reset→delete auth lifecycle was **not rerun as one real-socket scenario**; those stages passed through ASGI integration tests against real PostgreSQL/Redis. This distinction is a remaining verification item, not an implied pass.

## 19. Browser E2E

Not run. The repository has no production frontend for these flows, and no explicitly authorized test mailbox was supplied. A temporary browser harness was therefore not allowed to complete verification/reset email flows. Prior-audit browser evidence is historical and is not claimed as current.

## 20. Google OAuth E2E

No real Google authorization was rerun. Current unit/integration coverage validates issuer, audience, RS256/JWKS, nonce, state, PKCE, browser binding, stable subject, collision, hosted-domain verification and deleted-user refusal. Provider assumptions were checked against current official Google OpenID Connect documentation. Live redirect/client configuration and same-identity relogin remain deployment verification.

## 21. Resend E2E

Not run because no authorized recipient mailbox was supplied. Provider transport is covered by fakes, including encrypted context decryption, retry and acceptance clearing. No message was sent.

## 22. Resend Webhook / ngrok E2E

Not run because it depends on the authorized-mailbox delivery above. No tunnel or webhook was created and no API route was externally exposed. Signature, duplicate, unknown-message, stale/missing/invalid signature and malformed-body behavior remain covered locally.

## 23. Security Attack Matrix

| Attack | Result |
|---|---|
| Deleted/disabled credentials | Blocked across password, access, refresh, Google and invitations |
| JWT alg none/wrong key/issuer/audience/nbf/expiry/type | Blocked by focused suite |
| Refresh/reset/verification/OAuth state replay | Single winner; replay blocked |
| OAuth browser mismatch/account collision | Blocked; direct mutations killed |
| Open redirect | Allowlisted/local redirect rules pass |
| Cross-user/cross-tenant claims | Blocked; tenant isolation passes |
| Forward-header identity rotation | Blocked in shipped topology; negative control demonstrates bypass if misconfigured |
| Distributed reset abuse | Canonical account budget blocks source rotation |
| Duplicate invitation/registration | Controlled outcomes, no 500 |
| Oversized/malformed auth input | Schema/error-envelope tests pass |

## 24. Documentation Changes

Updated README, architecture, auth, authorization, security, email, verification, Google OAuth, backup, deployment, observability, runbook and API operation count. Historical ADRs were not rewritten.

## 25. Remaining Product Gaps

MFA/passkeys, per-device session management, self-service email change/deletion and breached-password checks remain intentionally out of scope. They are not regressions in AUTH-01 through AUTH-10.

## 26. Remaining External Verification

Before deployment:

1. Provision a strong `CREDENTIAL_ENCRYPTION_KEYS` key ring in the secret manager; retain old keys through pending-outbox/backup lifetime and rehearse rotation/recovery.
2. Verify the actual ingress forwards only from declared trusted proxies and launches Uvicorn with proxy preprocessing disabled.
3. Connect/scrape the auth metric and install/test the documented alerts and receiver.
4. Supply an explicitly authorized mailbox, then run browser verification/reset, real Resend delivery, and a route-restricted webhook tunnel with cleanup.
5. Rerun real Google login/relogin/deleted-user refusal against the deployment's exact OAuth client and redirect origins.
6. Run the complete auth lifecycle over a real TCP server, including the delete-race barriers omitted above.

## 27. Exact Test Results

| Check | Result |
|---|---|
| Auth/security focused pytest | 596 passed, 0 failed, 0 skipped |
| New committed concurrency module | 10 passed |
| Full pytest | 3,986 total: 3,874 passed, 73 skipped, 39 failed |
| Full-suite failure scope | 30 backup-shell tests and 9 readiness-shell tests; Windows cannot execute their Bash harnesses. One backup inventory test also enumerated task-owned pytest dump fixtures under the repository temp root. No application/auth/E2E test failed. |
| Real-socket proxy test | hardened: 10×401 then 2×429; vulnerable control: 12×401 |
| Real-socket application E2E | 4/4 billing lifecycle scenarios passed |
| Mutation tests | 8/8 mutations detected; two outcomes remained attack-safe due independent defenses |
| Ruff lint | pass |
| Ruff formatter | changed files formatted during remediation; repository-wide check reports 26 unrelated pre-existing files plus Black/Ruff disagreement on 3 changed files |
| Black | no pass claimed: Black reformatted the 3 reported changed files, but repeated `--check` processes hung on Windows and were terminated after bounded waits |
| MyPy | pass, 460 source files |
| Compileall | pass |
| Project-scoped pip-audit | pass: no known vulnerabilities |
| Alembic fresh/round-trip/drift/backfill | pass, as detailed in §15 |
| Browser/Google/Resend/webhook | not run, reasons in §§19–22 |

The 73 skips are platform/optional-integration cases already declared by the suite; the canonical auth selection ran with zero skips. Test JWT fixtures emit short-key warnings; deployment configuration validation requires real keys and these warnings are not production-key evidence.

## 28. Final Security Score

| Area | Score / 10 | Rationale |
|---|---:|---|
| Registration | 9.2 | deterministic unique races and verification gate |
| Password security | 9.0 | unchanged strong Argon2id/bounds/rehash core |
| Verification | 9.3 | centralized authorization gate and encrypted pending code |
| Password reset | 9.2 | canonical account budget and encrypted token |
| Access token security | 9.3 | deleted-state and version checks on every request |
| Refresh/session security | 9.2 | lifecycle checks plus existing atomic replay teardown |
| Google OAuth | 9.3 | tombstones, binding tests and authoritative-domain policy; live rerun pending |
| Account linking | 9.2 | live-account enforcement and no tombstone reuse |
| Abuse/rate limiting | 9.2 | reset account budget and coherent supported proxy path |
| Browser security | 8.0 | server controls sound; current browser/provider rerun pending |
| Tenant interaction | 9.3 | verified gate plus preserved non-disclosure/isolation |
| Failure safety | 9.3 | controlled races and fail-closed encrypted outbox |
| Observability | 8.3 | emitted bounded metric and runbook; deployed receiver unverified |
| Testing | 9.2 | 596 focused passes and all mutations detected; delete barriers/browser rerun pending |
| Operations | 7.8 | docs/config gates improved; deployment configuration remains unverified |

Overall code assessment: **9.0 / 10**. This is a code-quality/security assessment, not a production-readiness declaration.

## 29. Final Verdict

**AUTH READY WITH REQUIRED DEPLOYMENT CONFIGURATION**

AUTH-01 through AUTH-10 are fixed or implemented in the current code and the focused security suite is green. The application is **not production ready yet** because the exact deployment proxy/key/monitoring configuration and the current browser/Google/Resend/webhook flows have not been verified, and the complete auth lifecycle/delete races have not been rerun over a real TCP server. These items are explicit release gates, not assumed passes.
