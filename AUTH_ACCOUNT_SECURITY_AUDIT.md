# Wasla — Independent Authentication & Account Security Audit

**Audit date:** 2026-09-10
**Auditor:** independent adversarial review, runtime- and browser-backed
**Scope:** the whole authentication and account-security lifecycle

---

## 1. Executive Summary

Wasla's authentication *design* is unusually strong. Against a live instance backed by real
PostgreSQL, real Redis, real Google and real Resend, the token layer, the credential layer,
the OAuth layer and the abuse-resistance layer all held under adversarial testing:

- **34 forged-token attacks** against protected HTTP endpoints — all refused, including
  `alg=none`, RS256 algorithm substitution, audience arrays, missing/expired/stale claims and
  both directions of access/refresh token confusion.
- **Refresh-token replay** is detected and answered by tearing the whole session estate down.
- **Real Google OAuth sign-in through a real browser** succeeded end to end, with PKCE S256,
  a nonce, a single-use server-side state, and a browser-binding cookie all genuinely enforced.
- **Real email through Resend**, delivered to a real mailbox, with the delivery webhook
  returning through ngrok and passing signature verification.
- **Concurrency** — duplicate registration, refresh races, invitation races and password-reset
  races all produce exactly one winner against real committed transactions.
- **Redis outage** behaves exactly as documented: replay controls fail closed, password login
  survives on a process-local limiter.

Against that, the audit found **one critical defect that is a production blocker**, and it is
not subtle: **four `AuditAction` values exist in Python but were never added to the PostgreSQL
`audit_action` enum by any migration.** Every code path that writes one aborts its transaction
and returns HTTP 500. The blast radius is the entire account-lifecycle and credential-change
surface:

| Endpoint | Result | Security effect |
|---|---|---|
| `POST /auth/logout-all` | 500 | **sessions are not revoked** |
| `POST /auth/password` | 500 | **password is not changed** |
| `POST /auth/password/set` | 500 | Google-only accounts cannot gain a password |
| `POST /platform/users/{id}/disable` | 500 | **account is not disabled** |
| `POST /platform/users/{id}/enable` | 500 | account cannot be restored |
| `DELETE /platform/users/{id}` | 500 | **account is not deleted** |

"A refresh token leaked, kill it now" is the scenario `logout-all` and `token_version` were
built for, and it is precisely the scenario that does not work. The enforcement side is
correct — setting `is_active`, `deleted_at` or `token_version` directly in the database
immediately kills sessions, as designed — so this is a broken *write* path, not a broken
design. The fix is a migration adding four enum labels.

This defect was invisible to a 3 913-test green suite because **the database-backed tests build
their schema with `Base.metadata.create_all` rather than by running the migrations**, which
creates every PostgreSQL enum from the Python enum and so can never observe drift. Running the
identical `test_account_lifecycle.py` against a migration-built schema turns 23 passes into
**10 failures**.

**Verdict: AUTH NOT READY.** One migration and one CI guard away from ready.

---

## 2. Repository Verification

| Item | Expected | Actual |
|---|---|---|
| Repository | `mohamedshhahat1/wasla` | confirmed (`origin`) |
| Branch | `worktree-billing-google-auth` | **matches** |
| HEAD | `79e9a15e73ee...` | **`f148a26a9fb4cbbc5ad5a0874ecb5a2807ff4c4a`** — 5 commits ahead |
| Working tree | — | clean at start **and at end** |
| Migration head | — | `0048` (`alembic upgrade head` clean; `alembic check` reports no drift) |

HEAD differs from the value in the brief. The five newer commits are `d7ae0c2`, `4f837ab`,
`585e280 fix(auth): close account security lifecycle gaps`, `71c5d73 test(auth): …`,
`f148a26 docs(auth): record remediated security contracts`. Per instruction these prior
remediation and documentation claims were treated as **untrusted** and re-verified from the
current code. Nothing was reset, discarded or rewritten; the audit made no production changes.

> Note on `alembic check`: it reports "No new upgrade operations detected" **while the
> `audit_action` enum is missing four labels**. Alembic's autogenerate does not compare native
> enum labels. `alembic check` passing is not evidence of model/migration parity.

---

## 3. Audit Methodology

**Code read.** Every authentication-relevant human-authored module was read in full (inventory
in §3.1).

**Runtime.** Four independent instances of the real API were run on real TCP sockets against
real PostgreSQL 16 (pgvector) and real Redis 7, plus a real worker:

| Port | Configuration | Purpose |
|---|---|---|
| 8000 | rate limits **on** (shipped defaults) | abuse testing, browser E2E, ngrok/Resend |
| 8001 | rate limits off | protocol/flow testing without throttle interference |
| 8002 | `TRUSTED_PROXY_IPS=127.0.0.1` | proxy-header spoofing |
| 8003 | Redis pointed at a closed port | dependency-outage behaviour |

**External providers, genuinely exercised.**
- **Google**: real OAuth client (`wasla-507915`), real browser sign-in by the account owner,
  real JWKS fetch, real ID-token verification.
- **Resend**: verified domain `alkayan.studio`, real send to a real Gmail mailbox, real
  delivery webhook returned through an **ngrok** HTTPS tunnel to the local API.

**Browser.** A temporary harness (plain HTML/JS on `localhost:3000`) driven by Playwright
Chromium. It exists only for this audit and is **not production UI**; it was removed afterwards.

**Test-suite quality.** Tests were read, not just run; high-risk guarantees were challenged with
**21 mutations** across two rounds, each reverted and the repository verified clean.

**Limitations — stated plainly.**
- Google **Workspace/hosted-domain** (`hd`) accounts were not exercised; only a `gmail.com`
  account was available. The hosted-domain branch of `_google_is_authoritative_for_email` is
  **verified by code and unit test only**.
- Google account-linking (`/auth/identities/google/link`) and unlinking were exercised at the
  API/security level but not through a second real Google account.
- Inbox rendering of the delivered mail was confirmed via Resend's delivery event and the
  provider record, not by reading the recipient's inbox directly.
- No load or sustained brute-force testing; limits were verified functionally.

### 3.1 File inventory

```
Relevant files discovered: 63
Fully read:                63
Partially read:             0
Search-only:                0
Unreviewed:                 0
```

<details><summary>The 63 files</summary>

Core: `core/security.py`, `core/token_store.py`, `core/oauth_flow.py`, `core/oauth_binding.py`,
`core/rate_limit.py`, `core/proxy.py`, `core/config.py`, `core/dependencies.py`,
`core/exceptions.py`, `core/middleware.py`, `core/logging.py`, `core/limits.py`, `core/redis.py`,
`core/crypto.py`, `core/telemetry.py`, `core/net.py`, `main.py`.
API: `api/dependencies.py`, `api/rate_limits.py`, `api/route.py`, `api/v1/auth.py`,
`api/v1/google_oauth.py`, `api/v1/email_verification.py`, `api/v1/invitations.py`,
`api/v1/members.py`, `api/v1/email_webhooks.py`, `api/v1/platform.py`, `api/v1/__init__.py`.
Services: `auth_service.py`, `google_auth_service.py`, `account_service.py`,
`email_verification_service.py`, `password_reset_service.py`, `invitation_service.py`,
`membership_service.py`, `audit_service.py`, `email_service.py`, `email_templates.py`,
`email_event_service.py`, `credential_service.py`.
Integrations: `google/client.py`, `google/oidc.py`, `email/resend.py`, `email/signature.py`,
`email/base.py`, `email/fake.py`.
Repositories: `user_repository.py`, `membership_repository.py`, `invitation_repository.py`,
`password_reset_repository.py`, `email_verification_repository.py`, `identity_repository.py`,
`audit_repository.py`, `email_repository.py`.
Models: `user.py`, `membership.py`, `invitation.py`, `password_reset.py`,
`email_verification.py`, `identity.py`, `audit.py`, `email.py`.
Schemas: `auth.py`, `google_oauth.py`, `password_reset.py`, `invitation.py`.
Plus: `platform/roles.py`, `platform/access_audit.py`, `workers/email_worker.py`,
`alembic/versions/*` (auth-relevant), `.github/workflows/*`, `nginx/nginx.conf`,
`docker-compose*.yml`, `.env.example`, `docs/{AUTH,AUTHORIZATION,SECURITY,EMAIL,
EMAIL_VERIFICATION,GOOGLE_OAUTH,API,RUNBOOK,OBSERVABILITY}.md`, and the auth test modules.

</details>

---

## 4. Authentication Architecture

Wasla is a **stateless bearer-token API with one exception**: refresh tokens are individually
revocable through a Redis denylist, and every token carries a `ver` claim compared against
`users.token_version` on every request. There are no session cookies. The only cookie the API
sets is the OAuth browser-binding cookie.

**Registration** — `POST /api/v1/auth/register` → `RegistrationRequest` (`schemas/auth.py`,
`extra="forbid"`, `EmailStr`, 12–128-char password) → `AuthService.register`
(`services/auth_service.py`) → `begin_nested()` savepoint → `UserRepository.create`
(`normalise_email` = `.strip().lower()`; tombstone check) → `hash_password` (Argon2id,
`core/security.py`) → `TenantRepository.create` → `MembershipRepository.add_member`
(`TENANT_OWNER`) → `SubscriptionService.start` (contained failure) →
`EmailVerificationService.request` (outbox row, same transaction) → `_issue` → 201 with a token
pair. `IntegrityError` on `uq_users_email` / `uq_tenants_slug` becomes a 409.

**Password login** — `POST /auth/login` → `AuthService.login` → **per-account rate limit first**
(`account_identity(email)` = SHA-256 of the lower-cased address) → `UserRepository.get_by_email`
→ on miss, `spend_verification_time` (equal-cost Argon2) → `verify_password` → *then*
`deleted_at` / `is_active` → optional `password_needs_rehash` upgrade → `_resolve_workspace` →
`_issue`.

**Token issuance** — `_create_token` (`core/security.py`) is the single minting site. Claims:
`iss=wasla`, `aud` (`wasla-api` for access, `wasla-auth` for refresh), `sub`, `typ`, `jti`,
`iat`, `exp`, optional `tid`, `ver`. HS256 (config-constrained to `HS256|HS384|HS512`).

**Request authentication** — `get_current_user` (`api/dependencies.py`) → `decode_token`
(PyJWT verifies signature, issuer, audience and a `require` list; then explicit `aud` non-array
and `typ` checks) → load user (`deleted_at IS NULL`) → `is_active` → `ver == token_version`.
`require_verified_user` gates on `email_verified_at`; `get_active_workspace` re-reads the
**active** membership from the database on every request.

**Refresh** — `AuthService.refresh` → decode → **`RefreshTokenStore.spend` (single Redis
`SET NX`) before anything else** → on loss, `_tear_down_after_reuse` (atomic
`UPDATE … token_version+1 RETURNING`, audit row, **explicit commit**) → then account state →
then `ver` → rotate.

**Google OAuth** — `POST /auth/google/authorize` (`api/v1/google_oauth.py`) → binding secret
(`core/oauth_binding.ensure`) → `OAuthFlowStore.start` (Redis, `SET NX`, 600 s; stores nonce,
PKCE verifier, SHA-256 of the binding secret, flow kind, optional user id) →
`GoogleOAuthClient.authorization_url` (PKCE S256, `access_type=online`, `prompt=select_account`,
**fixed** `redirect_uri`). Browser → Google → frontend route → `POST /auth/google/callback` →
`GoogleAuthService._redeem`: **spend state → prove browser binding → exchange code → verify ID
token** (`GoogleIdTokenVerifier`, RS256 literal, live JWKS, issuer set, audience = client id,
nonce compared with `compare_digest`) → identity resolution by **`sub` only** →
`authenticate_federated` → the same session shape as password login.

---

## 5. Account Data Model

`users` — global identity, deliberately no `tenant_id`. `UNIQUE(email)` (`uq_users_email`);
addresses stored lower-cased so the constraint is case-insensitive. `hashed_password` nullable
(Google-first accounts). `email_verified_at` timestamp (NULL = unverified). `token_version`
integer, default 1, server-default `'1'`. `platform_role` nullable. Soft delete via `deleted_at`.

`memberships` — `UNIQUE(user_id, tenant_id)`, role and status per membership. `user_identities` —
`UNIQUE(provider, provider_subject)` **and** `UNIQUE(user_id, provider)`, FK to `users`
`ON DELETE CASCADE`. `tenant_invitations`, `password_reset_tokens`,
`email_verification_challenges` — all store only hashes of their secrets.

Verified at runtime against the migrated database: the Google account created by the real
browser E2E had exactly one `users` row, one `user_identities` row, a 21-digit numeric Google
`sub`, no password hash, `email_verified_at` stamped, avatar stored.

---

## 6. Registration

| Check | Result |
|---|---|
| Email normalisation (`.strip().lower()`, one helper, all paths) | PASS |
| Case-variant duplicate (`X@Y.com` vs `x@y.com`) | 409 — PASS |
| Whitespace-padded duplicate | 409 — PASS |
| **Concurrent** duplicate registration (barrier, 2 real connections) | `[409, 201]`, **1 DB row** — PASS |
| Concurrent duplicate, case-variant | 1 row — PASS |
| DB unique constraint as backstop | `uq_users_email` present — PASS |
| Transaction rollback on conflict (`begin_nested` savepoint) | PASS |
| Malformed email | 422 (`EmailStr`) — PASS |
| Unknown fields | 422 (`extra="forbid"`) — PASS |
| Password bounds enforced at the edge and in the domain | 12–128, both — PASS |
| Oversized body | refused by `core/limits.py` request-size middleware — PASS |
| Partially-created accounts after failure | none — savepoint + single transaction |
| Workspace + owner membership + subscription bootstrap | PASS |
| Account created **unverified** | PASS (by design) |

**Accepted enumeration surface:** registration answers 409 for an address that already exists.
This is inherent to self-service signup, is documented in `docs/AUTHORIZATION.md`, and is pinned
by `tests/integration/test_account_enumeration.py` to a bare 409 with no extra detail.

---

## 7. Password Security

**Argon2id** via `argon2-cffi` `PasswordHasher()` defaults (t=3, m=64 MiB, p=4, 16-byte salt,
32-byte hash) — at or above OWASP guidance. Salt, algorithm and parameters live inside the
stored string. `password_needs_rehash` upgrades on next successful login.

- Policy is **length only** (≥12, ≤128) — a defensible, well-argued choice.
- The 128-char ceiling is enforced **both** at the schema edge (`LoginRequest`,
  `PasswordChangeRequest`, `PasswordSetRequest`, `RegistrationRequest`) and in
  `validate_password_strength`, so no unbounded string reaches Argon2. **No DoS vector found.**
- Comparison is Argon2's own; there is no hand-rolled equality.
- `spend_verification_time` / `spend_code_verification_time` burn equivalent work on the miss
  paths, so timing does not distinguish "no such account" from "wrong password".

**Credential leakage — searched and not found.** No plaintext password reaches logs, audit rows,
metrics, database columns, API responses or emails. Audit entries carry a `token_version`
integer and never token material. Log lines carry event names, ids and reasons — never secrets.
The email outbox **encrypts** credential-bearing template context (AES-GCM via
`CredentialCipher`, AAD bound to the idempotency key), so a database reader cannot recover a
reset token or verification code.

---

## 8. Login

| Scenario | Status | Notes |
|---|---|---|
| Correct credentials | 200 | |
| Wrong password | 401 | |
| Unknown email | 401 | **byte-identical body** to wrong-password (only `request_id` differs) |
| Unverified account | 200 | authenticates; workspace routes refused (§16) |
| Disabled account (`is_active=false`) | 403 | *after* the password is proven |
| Deleted account (`deleted_at`) | 401 | |
| Google-only account (no password hash) | 401 | indistinguishable from unknown |
| Malformed input | 422 | |
| Repeated failures | 429 at attempt 6 | per-account limit, default 5/min |
| 5 concurrent logins, same account | 5×200, **5 distinct `jti`** | independent sessions |
| Login after password reset | old 401 / new 200 | |

**Enumeration: not present** on login. Status, body and timing all match; the per-account rate
limit is keyed on the address whether or not it exists, and a 429 is returned identically for a
known and an unknown address (verified: both 429).

---

## 9. Email Verification

Six digits, generated with `secrets.randbelow` and formatted to width (leading zeros preserved),
**Argon2-hashed** at rest — a deliberate and correct departure from the SHA-256 used for
256-bit tokens, because a 20-bit secret needs a slow verifier. Located by account, never by code.

| Check | Result |
|---|---|
| Code issued at registration, in the same transaction | PASS |
| Cryptographically strong, unpredictable | PASS |
| Correct code accepted | 200 `{"verified_at": …}` |
| Wrong code refused | 422, generic |
| **Replay of a used code** | 422 — PASS |
| Resend issues a new code | PASS |
| **Superseded code refused after resend** | 422 — PASS |
| **Cross-user code** (A's code presented by B) | 422 — PASS |
| Attempt cap durable across the refusal | PASS — `_reject` **commits** the increment |
| Atomic single-use | PASS — one conditional `UPDATE` re-checks everything |
| Address-change binding | challenge stores the address it was issued for |
| Codes never logged / returned / in URLs | PASS |
| Codes encrypted in the outbox | PASS |
| Verification unlocks workspace routes | PASS |

Delivery logic verified **and** provider delivery verified — see §32.

---

## 10. Password Reset

| Check | Result |
|---|---|
| Request answers a constant 202 for known / unknown / disabled / passwordless | PASS |
| Token: 256-bit `secrets.token_urlsafe`, SHA-256 at rest | PASS |
| New request supersedes outstanding tokens | PASS |
| **Old (superseded) token refused** | 401 — PASS |
| Current token accepted | 200 |
| **Replay of a consumed token** | 401 — PASS |
| Garbage token | 401, identical message |
| Old password dead / new password works | PASS |
| **Pre-reset refresh token revoked** | 401 — PASS |
| **Pre-reset access token revoked** | 401 — PASS |
| Passwordless (Google) account | 202 constant, **0 emails queued** — PASS |
| Cross-account aiming | impossible: payload is `{token, new_password}` only |
| **Concurrent confirmation ×2, 5 trials** | exactly one winner every time — PASS |

**Session-revocation policy:** a successful reset increments `token_version`, ending every
access and refresh token the account holds. Correct and clearly justified.

---

## 11. JWT / Access Tokens

Claims minted: `iss`, `aud`, `sub`, `typ`, `jti`, `iat`, `exp`, `tid?`, `ver?`. No `nbf`
(so nothing depends on it). Validation asks PyJWT to check signature, issuer, audience and
required-claim presence **in the same call**, then adds two explicit checks the library will
not make: `aud` must not be an array, and `typ` must match.

**Every one of the following was presented to a live protected endpoint over HTTP. All refused:**

| Attack | Result |
|---|---|
| genuine token (control) | 200 |
| `alg=none` | 401 |
| wrong HMAC secret | 401 |
| signature stripped / truncated | 401 |
| payload byte flipped | 401 |
| **RS256 attacker-signed (algorithm substitution)** | 401 |
| malformed / non-JWT | 401 |
| expired | 401 |
| future `nbf` | 401 |
| far-future `iat` (10-year skew) | 401 |
| wrong issuer | 401 |
| wrong audience (`wasla-auth` at the API) | 401 |
| **`aud` as an array containing `wasla-api`** | 401 |
| `aud` / `iss` / `typ` / `jti` absent | 401 |
| `ver` absent (pre-ADR-036 token) | 401 |
| `ver` stale (revoked) | 401 |
| `typ=refresh` at an API route | 401 |
| `sub` unknown / non-UUID / null | 401 |
| **refresh token used as access token** | 401 |
| **access token used at `/auth/refresh`** | 401 |
| **valid `sub` + forged foreign `tid`** | 403 |

The algorithm allowlist is enforced twice: `Settings` refuses anything outside
`{HS256,HS384,HS512}` (so `none` and RS256 cannot be configured), and `decode_token` passes a
single-element `algorithms` list. Google's verifier is a **separate module with no shared code
or key material** and an `RS256` literal — the correct defence against cross-family confusion.

---

## 12. Refresh Tokens / Sessions

**Model:** stateless refresh JWTs + a Redis denylist of spent `jti`s + a per-user
`token_version` bulk lever. No per-device session rows.

| Check | Result |
|---|---|
| Refresh succeeds once, rotates | PASS |
| Old refresh token after rotation | 401 |
| **Replay detected** | PASS |
| **Replay tears the estate down** (`token_version+1`) | PASS — the rotated descendant is dead too (401) |
| Access token dies after teardown | 401 |
| Teardown survives the failing request | PASS — `_tear_down_after_reuse` commits explicitly |
| Multiple legitimate devices | 5 concurrent logins → 5 independent sessions |
| **Concurrent refresh, same token, barrier** | `[401, 200]` — exactly one branch; the survivor's descendant is then dead (teardown fired) |
| Password reset ends sessions | PASS |
| Account disable / delete ends sessions | PASS *when the state is set* (§16) |
| Redis outage during refresh | **503, fails closed** — never mints on an unverifiable token |

`spend` is a single `SET NX`, so of two concurrent presentations exactly one can win regardless
of interleaving, and losing *is* the detection. `bump_token_version` is one
`UPDATE … RETURNING`, so simultaneous teardowns compose rather than cancel.

**Gap:** there is no per-device session or device-management surface. Signing out one device
without the others is not possible. Documented as a deliberate trade-off (`db/models/user.py`).

---

## 13. Logout / Revocation

**The exact contract, measured:**

- `POST /auth/logout` revokes **the presented refresh token only** (Redis denylist entry with a
  TTL matching the token). Verified: refresh afterwards → 401.
- It does **not** invalidate the access token, which stays usable for up to its 15-minute
  lifetime. This is documented in `docs/AUTH.md` and is a deliberate stateless-verification
  trade-off; clients must discard both tokens.
- Logging out twice, or with an expired/garbage token, is 204 — not an oracle.
- The endpoint is unauthenticated by design (an expired access token is exactly when people
  sign out) and is rate-limited by client address.
- During a Redis outage logout returns **503 rather than a false 204** — it does not claim a
  security action it did not perform.

**`POST /auth/logout-all` — the bulk revocation lever — returns 500 and revokes nothing.**
See AUTH-01.

---

## 14. Google OAuth / OIDC

Construction, verified against the live endpoint:

```
https://accounts.google.com/o/oauth2/v2/auth
  response_type=code   scope="openid email profile"
  code_challenge_method=S256   code_challenge=<43 chars>
  state=<43 chars>     nonce=<43 chars>
  access_type=online   prompt=select_account
  redirect_uri=<fixed configuration>
```

| Property | Result |
|---|---|
| `state` 256-bit, server-side, single-use, 600 s TTL | PASS |
| State spend is atomic (`GET`+`DEL` in `MULTI`) | PASS |
| Invented state | 401 |
| **Replayed state** | 401 |
| Malformed state shapes (empty, short, 500 chars, spaces, path traversal) | 401/422 — never a Redis key |
| Cross-flow use (login state at the link endpoint) | refused |
| **PKCE S256** required, verifier never leaves the server | PASS |
| **Nonce** compared with `compare_digest` | PASS |
| **Browser binding cookie** required at the callback | PASS — real state **without** the cookie → 401, before any call to Google |
| Cookie attributes | `HttpOnly`, `Path=/`, `SameSite=Lax`, `Max-Age=600`, no `Domain`; `Secure` + `__Host-` prefix in staging/production |
| `redirect_uri` from configuration only | PASS — `?next=`/body fields ignored |
| Authorization-code reuse | code is spent by Google; state single-use prevents a second exchange |
| Oversized code (100 KB) | 422 — refused without relaying to Google |
| ID token: RS256 literal, live JWKS, `kid` lookup only | PASS |
| Issuer (both Google spellings), audience = client id, `exp` with 30 s leeway | PASS |
| `email_verified` compared `is True` (not truthiness) | PASS |
| Claim length bounds: `sub`/`email` **refused**, name/picture **shortened** | PASS — correct asymmetry |
| `picture` scheme allowlisted to `https` | PASS — blocks `javascript:`/`data:` |
| Google access/refresh tokens | **never stored**; `access_type=online` means none is issued |
| Provider tokens / codes / error bodies logged | never |
| Google unreachable | 503, not a rejected login |
| Own rate-limit bucket | PASS — 429 after 10/min |

**Real browser E2E: PASS.** See §31.

---

## 15. Account Linking / Identity Collisions

The governing rule, and it is the right one: **identity is the Google `sub`, never the email
address.** Once a `user_identities` row exists the email claim is never consulted again, and
`_refresh_profile` deliberately updates only name and avatar — never `user.email`.

| Collision | Behaviour | Verdict |
|---|---|---|
| First login, address unused | account created; `email_verified_at` stamped **only** if Google is authoritative (gmail.com or matching `hd`) | correct |
| **First Google login onto an address that already has a Wasla account** | **409 `ADDRESS_IN_USE`** — never signs in, never links, never touches the password | correct — mailbox control is not account ownership |
| Google `sub` same, email changed | resumes the same account | correct |
| Google email same, `sub` different | treated as a new identity; collides on the address → 409 | correct |
| Address case variants | normalised → same identity (409) | correct |
| Deleted local account | tombstone included in the lookup → 409, no resurrection | correct |
| Disabled local account | 403, audited | correct |
| Linking a Google account already linked elsewhere | 409, identity **not moved** | correct |
| Link flow finished by a different session | 401 — `flow.user_id` is server-side | correct |
| Unlink leaving no way in | 403 with guidance | correct |
| Unverified Google email at first login | refused **before any account lookup** | correct — keeps the 409 from being a directory |

Runtime-confirmed against the real Google account: password login → 401; registering the same
address → 409; uppercase variant → 409; reset request → constant 202 with **zero** emails queued.

**One accepted consequence (ADR-047):** a Google-first account gets a valid session but **no
workspace** (`active_workspace: null`, `workspaces: []`, 0 memberships — confirmed). It can do
nothing until invited somewhere. This is disclosed, not hidden, but it is a real onboarding
dead end (AUTH-05).

---

## 16. Account Lifecycle

States actually modelled: **unverified** (`email_verified_at IS NULL`), **active**,
**disabled** (`is_active=false`), **deleted** (`deleted_at` tombstone, `is_active=false`,
`token_version+1`). Invitations are a separate table, not a user state. There is no "locked"
state — lockout is expressed as rate limiting.

| State | Login | Refresh | Reset | OAuth login | Verify | Create WS | Access WS | Be invited |
|---|---|---|---|---|---|---|---|---|
| unverified | ✅ 200 | ✅ | ✅ | ✅ | ✅ | ❌ 403 | ❌ 403 | ✅ |
| active | ✅ | ✅ | ✅ | ✅ | n/a | ✅ | ✅ | ✅ |
| disabled | ❌ 403 | ❌ 401 | 202 const., no mail | ❌ 403 | ❌ | ❌ | ❌ | ❌ |
| deleted | ❌ 401 | ❌ 401 | 202 const., no mail | ❌ 401 | ❌ | ❌ | ❌ | ❌ |

**The enforcement side is entirely correct.** Setting `is_active=false`, `deleted_at`, or
incrementing `token_version` directly in the database immediately produces 401/403 on access
tokens, refresh and login, and a deleted address cannot be re-registered (409, tombstone
honoured). Re-enabling also bumps the version so pre-suspension tokens do not resurrect.

**The transition side does not work.** `disable`, `enable`, `delete` and `logout-all` all 500
(AUTH-01), so no state transition can actually be performed through the API. The unverified →
active transition works (email verification), and so does the reset-driven credential change.

**Dead ends:** a Google-first account with no membership (§15). **Unreachable states:** none.

---

## 17. Invitations

| Check | Result |
|---|---|
| Token: 256-bit `secrets.token_urlsafe`, SHA-256 at rest | PASS |
| Token returned in the API response | **No** — email only |
| 7-day expiry | PASS |
| **Recipient binding** | acceptance always uses `invitation.email`; there is no caller-supplied address | PASS |
| Email normalised on issue and lookup | PASS |
| Invented token | 401, generic |
| **Replay of an accepted token** | 401 — atomic `claim()` on `status=PENDING AND expires_at > now` |
| Revoked invitation | refused |
| **Concurrent acceptance ×2** | exactly one winner; 1 user row, 1 membership row |
| Acceptance mints a session | **No** — a leaked link alone cannot open a session |
| Existing account: password argument **ignored** | PASS — closes the ADR-057 takeover (an inviter could otherwise write a password onto a stranger's Google-only account) |
| Deleted identity cannot be revived by invitation | PASS |
| Readmission reuses the membership row; invitation's role wins | PASS |
| Only an owner may invite an owner | PASS |
| Role escalation via acceptance | not possible — role comes from the invitation row |

**Finding (AUTH-04):** accepting an emailed invitation — which *is* proof of mailbox control —
does **not** set `email_verified_at`. The invited person is created unverified and is then
refused by `require_verified_user` on every workspace route of the very workspace they were
invited to, until they separately complete a verification code. Security-conservative, but it
wastes a proof already performed and is a poor first experience.

---

## 18. Browser / CORS / Cookie / CSRF Security

**Bearer tokens, not cookies**, for the session. The frontend therefore holds tokens in JS-
reachable storage; the security expectation on the frontend is that **any XSS is a full session
compromise**. That is the standard consequence of this architecture and should be stated in the
frontend brief; the API-side mitigation is a short 15-minute access-token life plus rotation.

- **CORS:** `access-control-allow-origin` echoed only for configured origins. A disallowed
  origin gets **400 with no allow-origin header** — verified from inside a real browser page:
  `BLOCKED: Failed to fetch`. `allow_credentials=true`, and `Settings` **refuses to start** in
  production with `CORS_ORIGINS=*`.
- **Security headers** on every response: `X-Content-Type-Options: nosniff`,
  `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, and a restrictive CSP
  (`default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'`).
  HSTS is emitted only when the request is known to have arrived over HTTPS through a trusted
  proxy — correct.
- **CSRF:** not applicable to the bearer-token routes (no ambient credential). The one cookie
  (`wasla_oauth`) authorises nothing on its own, is `HttpOnly`, host-only, `SameSite=Lax`, and
  is ignored by every other route — so it does not undermine the no-CSRF-token reasoning.
- **Open redirects:** none found. `redirect_uri` is configuration; no `next`/`redirect`/
  `return_url`/`callback_url` parameter influences any redirect. Attempts with
  `https://evil.example`, `//evil.example`, encoded and doubly-encoded forms were all inert —
  there is no code path that reads a redirect target from a caller.

---

## 19. Rate Limiting / Abuse Resistance

Fixed window, Redis `INCR`+`EXPIRE`, with a **process-local fallback for credential-facing
policies** so a Redis outage degrades rather than disables the control.

| Route | Key | Default |
|---|---|---|
| register, login, refresh, logout, password, password/set, reset request/confirm, invitation accept | client address | 10/min |
| **login (additionally)** | **account** (SHA-256 of lower-cased email) | **5/min** |
| password-reset request (additionally) | account | per hour |
| Google authorize/callback | client address, **own bucket** | 10/min |
| email verification send/verify | account id | per policy |
| WhatsApp webhook | **none, deliberately** | — |

**Bypass attempts — all failed:**

| Attempt | Result |
|---|---|
| Change email case | still limited (identity is lower-cased before hashing) |
| Whitespace padding | still limited |
| Spoof `X-Forwarded-For` from an untrusted peer | **ignored** — socket address used |
| Spoof `X-Real-IP` from an untrusted peer | **ignored** |
| Prepend forged XFF entries *with the peer trusted* | still limited — the walk is **from the right** |
| IPv6 spelling variants (`::1` vs `0:0:0:0:0:0:0:1`) | same bucket — canonicalised |
| `unknown` / unparseable XFF entries | skipped, not returned |

Proxy trust is compared on **parsed addresses/networks**, hostnames are refused at startup
(`TRUSTED_PROXY_IPS=nginx` → refuses to boot), and the shipped nginx sets
`X-Real-IP $remote_addr`, overwriting anything a client sends. `.env.example` defaults to empty
(trust nothing). Refusals carry `Retry-After` and `X-RateLimit-*`. A 429 is returned identically
for known and unknown accounts, so the limiter is not an enumeration oracle.

---

## 20. Secret Management

Every unsafe configuration is **refused at startup** (verified by construction):

| Configuration | Result |
|---|---|
| placeholder or <32-char `JWT_SECRET` in production / staging / **local** | REFUSED |
| `JWT_ALGORITHM=none` or `RS256` | REFUSED |
| production + `CORS_ORIGINS=*` | REFUSED |
| production + `DEBUG` | REFUSED |
| production + docs enabled | REFUSED |
| production without `META_APP_SECRET` | REFUSED |
| `TRUSTED_PROXY_IPS` containing a hostname | REFUSED |
| `EMAIL_ENABLED` without `EMAIL_FROM` / `APP_PUBLIC_URL` | REFUSED |

Notably the signing-key rule applies to **every environment except `test`**, closing the
"defaults to the unprotected side" hole. `.env.example` carries **no** real values for any of
the six sensitive keys. `.env` and `.env.local` are gitignored and untracked. Secrets never
appear in `/health` or `/metrics`. Google's client secret is used in exactly one place
(the server-side token exchange) and is never logged, never in a response, never in the
frontend bundle. Resend and webhook secrets are read only by the worker and webhook route.

Two minor items: **AUTH-07** (a pydantic startup `ValidationError` echoes a truncated fragment
of the input settings dict, which can include the tail of one secret) and the absence of a
documented **key-rotation** procedure for `JWT_SECRET` (rotating it signs everyone out; the
`CredentialCipher` already supports multiple keys, `JWT_SECRET` does not).

---

## 21. Database Integrity

- `uq_users_email`, `uq_tenants_slug`, `UNIQUE(user_id, tenant_id)`,
  `uq_user_identities_provider_subject`, `uq_user_identities_user_id_provider` — all present in
  the migrated database.
- FKs present, `user_identities → users ON DELETE CASCADE`.
- Deleted identities retain their address (tombstone), and every creation path checks tombstones
  before inserting — so an address cannot silently attach a new person to historical records.
- Reset tokens, invitations and verification challenges all use **conditional `UPDATE …
  RETURNING`** rather than read-then-write, so races resolve in the database.
- `alembic upgrade head` clean; `alembic check` reports no drift — **but see AUTH-01: it does
  not compare enum labels.** A full sweep of all **45** enum types found drift in exactly one
  (`audit_action`, 4 missing labels); the other 44 match.

Expired-token buildup: `password_reset_tokens` and `email_verification_challenges` are
superseded/consumed but not pruned. Not a security issue; a housekeeping note.

---

## 22. Concurrency

All with `asyncio.Barrier` so operations genuinely overlap, over real HTTP against real
committed PostgreSQL transactions — not sequential calls relabelled.

| Race | Result |
|---|---|
| Same-email registration ×2 | `[409, 201]`, 1 DB row — PASS |
| Same-email registration, case-variant ×2 | 1 row — PASS |
| Same refresh token ×2 | `[401, 200]`, 1 winner; descendant then dead — PASS |
| 5 concurrent logins | 5 independent sessions, 5 distinct `jti` — PASS |
| Same invitation ×2 | 1 winner, 1 user, 1 membership — PASS |
| Same reset token ×2 (**5 trials**) | 1 winner every trial, 1 consumed row — PASS |
| Same verification code ×2 | atomic conditional `UPDATE` — PASS |
| `logout-all` racing an in-flight refresh | `logout-all` **500s** (AUTH-01); the refreshed session survives |
| Google callback state replay ×2 | 1 winner (`GET`+`DEL` in `MULTI`) — PASS |

---

## 23. Crash / Failure Safety

- **User committed but verification email fails** — the outbox row is written in the *same*
  transaction, so it cannot be lost; `_request_email_verification` additionally contains
  `ValidationError` so a misconfiguration cannot 500 a signup.
- **Provider timeout after the challenge is written** — the outbox retries with exponential
  backoff and jitter, capped at `EMAIL_MAX_ATTEMPTS`; delivery is **at-least-once and says so**.
- **Google token exchange succeeds but the DB commit fails** — `CommittingRoute` commits only on
  success; no identity row is left behind, and the user simply retries.
- **Account created but workspace bootstrap fails** — subscription failure is contained and
  logged; the account is still usable.
- **Refresh rotation interrupted** — the Redis spend happens first and is atomic, so a crash
  after it leaves the token spent (fail-safe: the holder must sign in again) rather than
  replayable.
- **Reset committed while revocation fails** — impossible to split: the password write and the
  `token_version` bump are the same transaction.
- **Redis unreachable** (measured on a dedicated instance): login 200 (degraded limiter, which
  *did* engage — 429 after 4 attempts), protected routes 200 (stateless), refresh **503**,
  logout **503**, Google authorize/callback **503**, liveness 200, readiness 503. No response
  leaked internals; each named only `{"dependency": "redis"}`.

Retries are idempotent: the outbox is keyed by idempotency key, and every spend is conditional.

---

## 24. Multi-Tenancy Interaction

The tenant a request acts on comes **only** from the signed `tid` claim; no route reads a tenant
id from path, query or body. `get_active_workspace` re-reads an **active** membership from the
database on every request, so revoking membership takes effect immediately without touching the
token estate.

- Forged foreign `tid` with a valid `sub` → **403** (membership lookup fails). Verified.
- Authentication never implies authorization: `require_verified_user` → `get_active_workspace`
  → `require_tenant_roles` are separate, declarative dependencies.
- Platform roles are a property of the user row, entirely separate from tenant roles; a
  workspace owner gets nothing across the platform.
- Invitation acceptance cannot grant a role other than the one on the invitation row, and
  cannot target another account (the address comes from the row).
- A Google identity cannot be linked to the wrong local account: `flow.user_id` is written
  server-side at initiation, and the browser binding must also match.

---

## 25. Security Logging / Audit

Recorded in `audit_logs` (actor, actor kind, target, label, metadata, timestamp):
member invited, member removed/reinstated/left, email verified / verification failed /
requested, password reset completed, Google login succeeded/failed, Google identity
linked/unlinked/link failed, refresh token reused, platform role granted/revoked, platform reads.

**Not recorded because the enum label is missing (AUTH-01):** password changed, sessions
revoked, user disabled, user enabled — these do not merely fail to log, they **abort the
operation**.

Quality is good: no credentials, no tokens, no codes, no provider subjects, no OAuth state or
nonce anywhere in the trail. Refusal paths that must outlive a failing request
(`_tear_down_after_reuse`, `_reject`, rate-limit refusals) **commit explicitly** — a genuinely
easy thing to get wrong and it is right here. Platform staff are not exempt from the trail.

---

## 26. Metrics / Alerts / Operations

| Layer | State |
|---|---|
| Metric exists | ✅ `wasla_auth_security_events_total{event,outcome,reason}` with bounded labels |
| Metric emitted | ✅ verified with real values from this audit's traffic (login success/failure, rate-limit blocked by account and by client, email webhook invalid signature, oauth callback success, business access blocked) |
| 5xx visible per route | ✅ `wasla_http_requests_total{route,status="5xx"}` — the broken endpoints **are** visible (`/auth/logout-all` 5xx = 4, `/auth/password` 5xx = 1, …), plus `wasla_unhandled_errors_total` |
| Metric scraped | ❌ no scraper in the stack |
| Alert configured | ❌ **none** — no Alertmanager |
| Human receiver | ❌ none |

`docs/RUNBOOK.md` is candid about this: *"There is no configured alerting … no monitoring
vendor, nobody paged."* That honesty is worth noting — but it means AUTH-01 shipped, produced
9 unhandled errors across 6 security endpoints, and **nothing would have told anyone.**

---

## 27. Test-Suite Quality

**The good.** 523 auth-specific tests, zero skips with a real database. Real barrier-based
concurrency tests that deliberately avoid the transactional fixture and commit independently
(`test_auth_remediation_concurrency.py` — a genuinely good file). Enumeration tests that assert
byte-identical bodies *and* equal Argon2 work. A test that discovers HTTP clients by parsing
`app/` rather than listing them by hand. CI has a dedicated `auth-regressions` job with
`WASLA_SECURITY_TESTS=1`, which converts "no database configured" from a silent skip into a
hard failure — exactly the right instinct.

**The systemic defect (AUTH-02).** `tests/integration/conftest.py` builds the schema with
`Base.metadata.create_all`. That creates every PostgreSQL `ENUM` **from the Python enum**, so
model/migration enum drift is *structurally invisible* to every database-backed test. This is
not theoretical:

```
tests/integration/test_account_lifecycle.py

  against Base.metadata.create_all  ->  23 passed
  against `alembic upgrade head`    ->  10 FAILED
```

Ten tests whose names assert exactly the guarantees that are broken in production —
`test_a_leaked_refresh_token_dies_when_the_user_revokes_sessions`,
`test_changing_a_password_ends_every_session`,
`test_a_token_issued_before_a_disable_stops_working` — pass against a schema no deployment has.
The conftest's own comment says migrations "have their own gates (upgrade/downgrade/upgrade,
then `alembic check`)", but `alembic check` does not compare enum labels, so nothing in the
project closes this loop.

**Other gaps found:** no test drives password-reset single-use *under concurrency* (the
atomic predicate in `consume()` survives mutation — though the guarantee itself holds at
runtime, §28); no test asserts model/migration enum parity.

---

## 28. Mutation / Non-Vacuity Results

21 mutations across two rounds. Every file restored; `git status` verified clean afterwards.

**Round 1 — 13/15 killed.**

| Mutation | Expected detector | Result |
|---|---|---|
| password verification always succeeds | auth endpoints, security unit | **KILLED** |
| access-token expiry not enforced | token forgery | **KILLED** |
| token audience not checked | token audience | **KILLED** |
| `typ` confusion allowed | token audience/forgery | **KILLED** |
| refresh replay protection disabled | refresh reuse | **KILLED** |
| `token_version` revocation ignored | account lifecycle | **KILLED** |
| email-verification gate removed | route authorization | **KILLED** |
| OAuth browser binding not compared | oauth browser binding | **KILLED** |
| unverified Google email accepted | google endpoints | **KILLED** |
| Google nonce not checked | google oidc | **KILLED** |
| Google issuer not checked | google oidc | **KILLED** |
| login accepts a disabled account | account lifecycle | **KILLED** |
| reset-token usability ignored | password reset | **KILLED** |
| invitation `is_open` check removed | invitation tests | *SURVIVED* |
| OAuth state `deleted` flag ignored | oauth flow | *SURVIVED* |
| verification-code usability | — | skipped (anchor not unique) |

**Round 2 — mutating what the two survivors actually relied on — 4/5 killed.**

| Mutation | Result |
|---|---|
| invitation `claim()` drops `status = PENDING` (true replay) | **KILLED** |
| invitation `claim()` drops the expiry predicate | **KILLED** |
| OAuth flow state never deleted (true replay) | **KILLED** |
| refresh `spend()` drops `nx=True` (two winners) | **KILLED** |
| password-reset `consume()` drops `consumed_at IS NULL` | *SURVIVED* |

**Interpretation.** Both round-1 survivors were **redundant early-exit checks**; the real
enforcement lives in the atomic database/Redis operation, and mutating *that* is caught. This is
defence in depth working as intended, not vacuous testing.

The one genuine survivor is password-reset single-use under concurrency. I then tested the
guarantee directly at runtime: **5/5 trials produced exactly one winner** and exactly one
consumed row. So the product is correct and the *test coverage* is missing — recorded as
AUTH-06 (Low), not as a defect.

**No false-green was found in the mutation dimension.** The false-green in this codebase is
environmental (AUTH-02), not assertional.

---

## 29. Real HTTP E2E Results

Real sockets, real Postgres, real Redis. `register → verify → login → refresh → protected →
logout → forgot → reset → login-with-new-password` all pass.

```
PASS  register                                   201
PASS  duplicate registration (case-insensitive)  409
PASS  unverified blocked from workspace route    403 email_verification_required
PASS  login enumeration (status + identical body)
PASS  login success                              200
PASS  refresh first use                          200
PASS  refresh replay refused                     401
PASS  refresh replay tears down descendant       401
PASS  access token dead after teardown           401
PASS  logout                                     204
PASS  logout kills refresh token                 401
PASS  password reset request enumeration         202 == 202, identical body
```

Plus the 18 reset/verification lifecycle assertions in §9–§10, the 34 token attacks in §11, the
10 webhook cases in §32, and the outage matrix in §23 — all over real HTTP.

---

## 30. Browser E2E Results

**Environment:** Chromium (Playwright), temporary harness at `http://localhost:3000`, real API
at `http://localhost:8000` (rate limits on), real Postgres/Redis. Cross-origin, so every call
was a real CORS preflight. **17/17 passed.** Screenshot: `browser_e2e.png` (temporary).

| Flow | Expected | Actual | Verdict |
|---|---|---|---|
| Register | 201, tokens reach the page | HTTP 201 | **PASS** |
| Token storage | token present in `localStorage` | present | **PASS** |
| Protected "Who am I?" | 200 with the caller's email | 200, email echoed | **PASS** |
| Unverified → workspace route | 403 `email_verification_required` | 403 | **PASS** |
| Duplicate registration | 409 shown in the UI | 409 | **PASS** |
| Wrong password | 401, generic message | 401 | **PASS** |
| Email verification | 200 `verified_at` | 200 | **PASS** |
| Verified → workspace route | 200 | 200 | **PASS** |
| Session persists across reload | token survives | survives | **PASS** |
| Refresh | 200, rotated token stored | 200 | **PASS** |
| Logout | client session cleared | cleared | **PASS** |
| Protected call after logout | 401 | 401 | **PASS** |
| Forgot password | 202 constant | 202 | **PASS** |
| Reset password | 200 | 200 | **PASS** |
| Login with new password | 200 | 200 | **PASS** |
| Login with old password | 401 | 401 | **PASS** |
| CORS from a disallowed origin | browser blocks | `BLOCKED: Failed to fetch` | **PASS** |

---

## 31. Real Google OAuth Verification

**REAL GOOGLE BROWSER E2E: PASS.**

Environment: real OAuth client in project `wasla-507915`; authorized JS origin
`http://localhost:3000`; redirect URI `http://localhost:3000/auth/google/callback`; test user
`elnoomezo@gmail.com`. No dashboard changes were needed — the existing configuration already
matched. The account owner completed the Google authentication step in the browser (the only
human-only step; no password, MFA code or recovery code was ever entered by the audit).

Path exercised: harness → *Continue with Google* → Google consent (*"to continue to Wasla"*) →
Google redirect to the frontend callback → `POST /auth/google/callback` with `code`, `state` and
the binding cookie → **HTTP 200** with a Wasla token pair → protected `/auth/me` → 200.

Verified from the response and the database:

- Wasla session established; token pair identical in shape to a password session.
- Account auto-created: one `users` row, one `user_identities` row.
- `provider_subject` = the 21-digit numeric Google `sub`; `last_login_at` stamped.
- `hashed_password` **NULL** → password login for that address returns 401.
- `email_verified_at` **stamped** (correct: `gmail.com` ⇒ Google is authoritative).
- `full_name` and `avatar_url` synced from Google; avatar is an `https` `lh3.googleusercontent.com` URL.
- `active_workspace: null`, `workspaces: []`, 0 memberships — the ADR-047 consequence (AUTH-05).

This exercise transitively proves, against the real provider: live JWKS retrieval, RS256
signature verification, issuer and audience validation, nonce matching, PKCE verifier redemption,
single-use state, and browser-binding cookie enforcement.

**Not covered:** a Google Workspace (`hd`) account, and linking/unlinking with a second real
Google account. See §40.

---

## 32. Real Email Verification

**REAL EMAIL + REAL WEBHOOK E2E: PASS.**

Domain `alkayan.studio` — status **verified**, sending enabled. A registration for a real
mailbox (`elnoomezo+waslaaudit1@gmail.com`) produced a real Resend send, and the outbox row
reached `status = delivered` — which only happens when the **delivery webhook returns and
passes signature verification**. ngrok tunnelled
`https://recycler-gondola-numeral.ngrok-free.dev → localhost:8000`; the Resend webhook endpoint
was already configured at exactly that URL (no changes made).

Resend-side confirmation: `email.sent` → **success**, `email.delivered` → **success**.

Webhook failure cases, all through the real tunnel:

| Case | Result |
|---|---|
| valid signature, real event | 200 |
| **duplicate delivery of the same event** | 200, idempotent, no state corruption |
| missing signature headers | **403** |
| wrong secret (attacker-signed) | **403** |
| body tampered after signing | **403** |
| stale timestamp (outside the 300 s window) | **403** |
| `svix-id` swapped after signing | **403** |
| unknown email id, correctly signed | 200 (no oracle, no retry storm) |
| malformed JSON, correctly signed | 200 (does not make Resend retry forever) |
| bounce after delivered | accepted; status correctly **not** regressed (documented monotonic rule) |

Signature verification is Svix-correct: HMAC over `{id}.{timestamp}.{raw body}`, base64-decoded
`whsec_` key, timestamp freshness checked in **both** directions, multiple signature entries
compared without early return.

*Environment note:* an early batch of 24 outbox rows failed to render because I had started the
worker with a malformed encryption key before fixing my own audit `.env`. That was **my
misconfiguration, not a Wasla defect** — after correcting it, rendering and delivery worked
first time. Recorded here so the log excerpt is not misread.

---

## 33. Official Google Documentation Review

| Contract relied upon | Source | Accessed | Wasla behaviour | Status |
|---|---|---|---|---|
| ID tokens are RS256, signed by keys at `https://www.googleapis.com/oauth2/v3/certs` | Google Identity — *OpenID Connect*, `openid-connect` guide | 2026-09-10 | RS256 literal; fixed JWKS URL, not discovered | **Verified against the real provider** (live sign-in verified a real token) |
| `iss` is `https://accounts.google.com` **or** `accounts.google.com` | same | 2026-09-10 | both accepted, as a frozenset | **Verified against provider docs**; the set is correct and PyJWT's single-string `issuer=` is deliberately not used |
| `aud` must equal the client id | same | 2026-09-10 | `audience=self._client_id` in `jwt.decode` | **Verified by runtime** |
| `nonce` must be checked to prevent replay | same | 2026-09-10 | `compare_digest` against the per-flow nonce | **Verified by runtime + mutation** |
| `email_verified` may be `true` for third-party addresses only at Google-account creation; Google is authoritative for Gmail and hosted domains | same, *"email_verified" claim note* | 2026-09-10 | `_google_is_authoritative_for_email` restricts auto-verification to `gmail.com` or a matching `hd` | **Verified by code + runtime** for gmail; `hd` branch **unverified externally** |
| PKCE S256 recommended for all clients | OAuth 2.0 for Web Server Applications / RFC 7636 | 2026-09-10 | S256 only; `plain` not implemented | **Verified against provider** |
| `access_type=online` ⇒ no refresh token issued | OAuth 2.0 for Web Server Applications | 2026-09-10 | `access_type=online`; the access token is discarded, only the ID token is read | **Verified by code**; consistent with the real exchange |
| `redirect_uri` is exact-matched by Google | same | 2026-09-10 | fixed configuration, never request input | **Verified against provider** (the real flow succeeded only with the registered URI) |

One nuance worth recording: `email_verified` is compared with `is True`, so a provider sending
the **string** `"true"` would be read as unverified. Google sends a JSON boolean, and the real
E2E confirms the strict check does not reject genuine Google tokens — so this is correct, and
correctly strict.

---

## 34. Documentation Drift

| Document | Claim | Reality |
|---|---|---|
| `docs/API.md:59` | `POST /auth/logout-all` — "End **every** session this account holds" | **500; revokes nothing** |
| `docs/API.md:72` | "`logout-all` revokes the whole estate by raising `token_version`" | never raised |
| `docs/AUTH.md:38` | "`logout-all`, password reset, disable, delete … increment `token_version`, so their access tokens stop on the next request" | only **password reset** and refresh-replay teardown work |
| `docs/SECURITY.md:108-110` | `logout-all`, `POST /auth/password`, `POST /auth/password/set` described as working controls | all three 500 |
| `docs/AUTHORIZATION.md:118` | `/auth/logout-all` listed as an authorized, working route | 500 |
| `docs/GOOGLE_OAUTH.md:209,549` | "The route out is `POST /auth/password/set`" for Google-only accounts | 500 — **there is no route out** |
| `app/services/account_service.py:~128` | comment: *"The existing enum stays stable"* | the label is absent from the database enum — the precise false assumption behind AUTH-01 |
| `dac74c9` commit message | *"alembic check reporting no drift"* | true, and **not evidence** — `alembic check` ignores enum labels |

Everything else checked out: `docs/EMAIL.md`, `docs/EMAIL_VERIFICATION.md`,
`docs/AUTHORIZATION.md` (rate limits, enumeration), `docs/RUNBOOK.md` (candid about absent
alerting), `.env.example` and `docs/DEPLOYMENT.md` all match the code. ADRs were read as
historical records and are not counted as drift.

---

## 35. Missing Account-Security Capabilities

| Capability | Present? | Classification |
|---|---|---|
| Password change (authenticated) | route exists, **broken** | **REQUIRED BEFORE PRODUCTION** (AUTH-01) |
| Revoke all sessions | route exists, **broken** | **REQUIRED BEFORE PRODUCTION** (AUTH-01) |
| Admin disable / enable / delete account | routes exist, **broken** | **REQUIRED BEFORE PRODUCTION** (AUTH-01) |
| Password reset | ✅ working | — |
| Email verification | ✅ working | — |
| OAuth unlinking | ✅ working | — |
| MFA / TOTP | ❌ | EARLY IMPROVEMENT — the highest-value addition once AUTH-01 is fixed |
| Session / device management (per-device revoke) | ❌ (coarse lever only) | EARLY IMPROVEMENT |
| Login history visible to the user | ❌ (audit trail is staff-facing) | LATER PRODUCT |
| Suspicious-login notification | ❌ | LATER PRODUCT |
| Self-service account deletion | ❌ (platform-only) | LATER PRODUCT — likely a GDPR/DSR obligation |
| Email change flow | ❌ | LATER PRODUCT — the model is already prepared (`email_verified_at` reset documented) |
| Passkeys / WebAuthn | ❌ | ENTERPRISE FEATURE |
| Multiple OAuth providers | ❌ (Google only; model is provider-generic) | ENTERPRISE FEATURE |
| Enterprise SSO / SAML | ❌ | ENTERPRISE FEATURE |
| Account export | ❌ | ENTERPRISE FEATURE |
| Explicit user lockout state | ❌ | NOT NEEDED — rate limiting covers it without a lockout-DoS |
| Security-event dashboard | ❌ (metrics exist) | LATER PRODUCT |

---

## 36. Findings Ledger

### AUTH-01 — Four `AuditAction` values missing from the PostgreSQL enum break six account-security endpoints

- **Severity:** **CRITICAL** · **Confidence:** CONFIRMED BY RUNTIME + CONFIRMED BY TEST
- **Area:** account lifecycle, credential change, session revocation, platform administration
- **Affected files:** `app/db/models/audit.py` (105–121); `app/services/account_service.py`
  (124, 190, 221, 250, 305, 367); `alembic/versions/20260823_0021_add_user_token_version.py`
  (the migration that should have carried the `ALTER TYPE`); introduced in `dac74c9`.
- **Problem.** `AuditAction` declares 46 members; the migrated `audit_action` type has 42.
  Missing: `password_changed`, `user_disabled`, `user_enabled`, `user_sessions_revoked`.
  Every write of one raises `InvalidTextRepresentationError`, which aborts the transaction, so
  the *entire operation* is rolled back and the endpoint returns 500.
- **Reproduction.**
  ```bash
  createdb wasla_audit && DATABASE_URL=... alembic upgrade head
  # start the API against that database, register a user, then:
  curl -X POST $API/auth/logout-all -H "Authorization: Bearer $ACCESS"   # 500
  curl -X POST $API/auth/password   -H "Authorization: Bearer $ACCESS" \
       -d '{"current_password":"...","new_password":"..."}'              # 500
  ```
- **Evidence (measured).**
  | Endpoint | HTTP | Security effect |
  |---|---|---|
  | `POST /auth/logout-all` | 500 | refresh **and** access token still work afterwards |
  | `POST /auth/password` | 500 | old password still works, new one does not |
  | `POST /auth/password/set` | 500 | passwordless account cannot gain a password |
  | `POST /platform/users/{id}/disable` | 500 | `is_active` unchanged; sessions live; login works |
  | `POST /platform/users/{id}/enable` | 500 | — |
  | `DELETE /platform/users/{id}` | 500 | `deleted_at` NULL; sessions live; login works |

  Enum sweep across all 45 enum types: drift in `audit_action` only (4 labels).
- **Impact.** The documented answer to a compromised session ("sign out everywhere", "change
  your password") does not work. An operator cannot disable or delete a user account —
  including in response to abuse, a compromised account, or a deletion request. A user who
  learns their password is compromised **cannot change it**. Google-only users can never obtain
  a password.
- **Production blocker:** **YES.**
- **Remediation direction.** One migration issuing `ALTER TYPE audit_action ADD VALUE IF NOT
  EXISTS …` for the four labels (note: `ADD VALUE` cannot run inside a transaction block on
  older PostgreSQL — use `op.execute` with an autocommit block or the `IF NOT EXISTS` form on
  PG 12+). Then add the parity guard from AUTH-02 so this class cannot recur.
- **Required tests.** (a) a test asserting every `AuditAction` member exists in the database
  enum after `alembic upgrade head`; (b) the existing `test_account_lifecycle.py` run against a
  migration-built schema in CI.
- **External verification required:** none.

### AUTH-02 — Database-backed tests build the schema from models, so migration drift is structurally invisible

- **Severity:** **HIGH** · **Confidence:** CONFIRMED BY TEST
- **Area:** test infrastructure / CI
- **Affected files:** `tests/integration/conftest.py` (`_build_schema`, line ~92);
  `.github/workflows/security.yml`, `ci.yml`
- **Problem.** The schema is created with `Base.metadata.create_all`, which generates every
  PostgreSQL `ENUM` from the Python enum. Any label present in Python but never added by a
  migration therefore exists in the test database and only in the test database. The project
  relies on `alembic check` to close this, but `alembic check` does not compare enum labels.
- **Reproduction / Evidence.** Patching `_build_schema` to run `alembic upgrade head` instead,
  with no other change:
  ```
  tests/integration/test_account_lifecycle.py
    Base.metadata.create_all -> 23 passed
    alembic upgrade head     -> 10 FAILED
  ```
  The ten failures are precisely the tests named after the guarantees AUTH-01 breaks.
- **Impact.** A green suite of 3 913 tests certified six broken security endpoints. Any future
  enum addition without a migration will be equally invisible.
- **Production blocker:** **YES** (it is what allowed AUTH-01 to ship and would allow the next one).
- **Remediation direction.** Either build the integration schema by running migrations, or add
  a fast explicit parity test comparing `pg_enum` labels against every SQLAlchemy `Enum` in
  `Base.metadata` after `alembic upgrade head`. The latter is cheap and precise.
- **Required tests.** The parity test itself, run in CI after `alembic upgrade head`.
- **External verification required:** none.

### AUTH-03 — `POST /auth/logout` does not invalidate the access token

- **Severity:** LOW · **Confidence:** CORRECT BY DESIGN (documented)
- **Area:** session revocation
- **Affected files:** `app/services/auth_service.py::logout`, `app/core/token_store.py`
- **Problem/Behaviour.** Logout revokes only the presented refresh token. The access token
  remains valid for up to 15 minutes. Measured: after logout, refresh → 401, access → 200.
- **Impact.** On a shared machine, a signed-out session retains API access for up to 15 minutes.
- **Production blocker:** no — this is the documented, deliberate stateless trade-off
  (`docs/AUTH.md:38`), the window is short, and the correct remedy (`logout-all`) exists in
  design. It becomes materially worse only while AUTH-01 is unfixed.
- **Remediation direction.** None required. Optionally shorten the access-token TTL, or have
  the client call `logout-all` when the user asks to sign out of a shared device.
- **Required tests.** Already covered.

### AUTH-04 — Accepting an emailed invitation does not count as proving the mailbox

- **Severity:** MEDIUM · **Confidence:** CONFIRMED BY CODE + RUNTIME
- **Area:** invitations × email verification
- **Affected files:** `app/services/invitation_service.py::accept`;
  `app/api/dependencies.py::require_verified_user`
- **Problem.** Redeeming an invitation token proves control of the invited mailbox — that is the
  entire security basis of the invitation. Acceptance nonetheless leaves `email_verified_at`
  NULL, so `require_verified_user` refuses the new member every workspace route of the
  workspace they were just invited to until they complete a separate verification code.
- **Reproduction.** Invite → accept → log in → `GET /agents` → 403 `email_verification_required`;
  `/auth/me` shows `email_verified_at: null`.
- **Impact.** Team onboarding stalls at exactly the moment it should succeed; a second email is
  sent for a proof already performed. Security-conservative, so no exposure — a usability and
  trust cost.
- **Production blocker:** no.
- **Remediation direction.** Stamp `email_verified_at` on acceptance when the account is created
  by that invitation and the address matches the invitation row. Deliberately do **not** stamp
  it for a pre-existing account whose address differs.
- **Required tests.** Invitation acceptance sets verification for a newly created account;
  does not alter it for a pre-existing one.

### AUTH-05 — A Google-first account has a valid session but no workspace and no way to make one

- **Severity:** MEDIUM · **Confidence:** CONFIRMED BY RUNTIME (real Google account)
- **Area:** OAuth onboarding
- **Affected files:** `app/services/google_auth_service.py::_enrol` (ADR-047)
- **Problem.** `_enrol` creates no workspace (Google supplies no name/slug, and inventing a slug
  from a display name is refused because `SLUG_PATTERN` is ASCII-only). There is no
  "create workspace" endpoint reachable by a user with no membership — `get_active_workspace`
  requires a `tid`, and workspace creation only happens inside `register`.
- **Evidence.** The real Google sign-in produced `active_workspace: null`, `workspaces: []`,
  0 membership rows, and a session that can reach `/auth/me` and nothing else.
- **Impact.** "Sign up with Google" is a dead end unless the person is separately invited. For a
  self-service SaaS this is a conversion cliff, and it will read to users as a broken product.
- **Production blocker:** no, but it makes the Google sign-up button misleading if shipped.
- **Remediation direction.** Either a post-Google onboarding step that collects a workspace name
  and slug, or restrict the Google button to sign-*in* until such a step exists.
- **Required tests.** A Google-first account can complete workspace creation and reach a
  workspace-scoped route.

### AUTH-06 — No test proves password-reset single-use under concurrency

- **Severity:** LOW · **Confidence:** CONFIRMED BY TEST (mutation survived); guarantee holds at runtime
- **Area:** test coverage
- **Affected files:** `app/repositories/password_reset_repository.py::consume`;
  `tests/integration/test_password_reset.py`
- **Problem.** Removing `consumed_at IS NULL` / `superseded_at IS NULL` from the conditional
  `UPDATE` leaves the whole reset suite green, because the sequential path is caught by the
  earlier `is_usable` read. The atomic predicate is the *concurrency* control and nothing
  exercises it.
- **Evidence.** Mutation SURVIVED. Direct runtime test of the unmutated code: 5/5 barrier trials
  produced exactly one winner and one consumed row — **the guarantee itself is intact.**
- **Impact.** None today. A future refactor could remove the predicate with a green suite.
- **Production blocker:** no.
- **Remediation direction.** Add a barrier-based concurrent-confirmation test alongside the
  existing ones in `test_auth_remediation_concurrency.py`.

### AUTH-07 — A startup configuration error echoes a fragment of a settings value into logs

- **Severity:** LOW · **Confidence:** CONFIRMED BY RUNTIME
- **Area:** secret handling
- **Affected files:** `app/core/config.py` (`_validate_hardening` raising through pydantic)
- **Problem.** Pydantic's `ValidationError` includes `input_value={…}`, truncated head-and-tail.
  With settings supplied as a dict this can expose the tail of whichever value sits at the end —
  observed: `…OGLE-CLIENT-SECRET-zzz`. The message goes to container logs on a failed boot.
- **Evidence.** Canary test: full values were **not** present (pydantic truncates), but a
  recognisable fragment of one canary was.
- **Impact.** Partial disclosure of one secret to anyone who can read startup logs, only on a
  misconfigured boot. Low, but avoidable.
- **Production blocker:** no.
- **Remediation direction.** Raise the hardening problems as a plain `RuntimeError` after model
  construction, or mark sensitive fields so they are excluded from error rendering.

### AUTH-08 — No `JWT_SECRET` rotation path

- **Severity:** LOW · **Confidence:** CONFIRMED BY CODE
- **Area:** key management
- **Affected files:** `app/core/config.py`, `app/core/security.py`
- **Problem.** `jwt_secret` is a single value. Rotating it invalidates every token for every
  user in every tenant simultaneously — an outage, and the codebase says so. By contrast
  `CREDENTIAL_ENCRYPTION_KEYS` already accepts a list with a primary and decrypt-only keys.
- **Impact.** Signing-key compromise or routine rotation cannot be done without signing out the
  entire estate.
- **Production blocker:** no.
- **Remediation direction.** Accept a list: sign with the primary, accept any listed key during
  verification, mirroring the credential cipher.

---

## 37. Production Blockers

1. **AUTH-01** — six account-security endpoints return 500 and perform nothing:
   `logout-all`, `password`, `password/set`, platform `disable`, `enable`, `delete`.
   *Fix: one migration adding four enum labels.*
2. **AUTH-02** — the test suite cannot see model/migration enum drift, which is what let
   AUTH-01 ship green. *Fix: a parity test, or build the test schema from migrations.*

Nothing else in this audit blocks production.

---

## 38. Things That Can Be Fixed Later

- **AUTH-04** — stamp verification on invitation acceptance.
- **AUTH-06** — add the concurrent password-reset test.
- **AUTH-07** — stop pydantic rendering settings values on a failed boot.
- **AUTH-08** — multi-key `JWT_SECRET` for rotation.
- **AUTH-03** — no action required; optionally shorten the access-token TTL.
- Housekeeping: prune consumed/superseded reset tokens and verification challenges.
- Update the six documentation locations listed in §34 once AUTH-01 is fixed.
- Configure an actual alert receiver — the metrics are already there and already correct.

---

## 39. Product Decisions Required

1. **AUTH-05** — should "Continue with Google" create a workspace (needs an onboarding step to
   collect a name and slug), or should Google be sign-in only until that step exists?
2. **MFA/TOTP** — not a bug, but the most valuable next security capability for a platform
   holding customers' WhatsApp business credentials.
3. **Self-service account deletion and email change** — likely required for data-protection
   obligations; both are absent by design today.
4. **Per-device session management** — currently only the coarse `token_version` lever. Fine for
   launch; a visible "your sessions" list is a common expectation.
5. **Access-token lifetime** (15 min) versus the logout window in AUTH-03.

---

## 40. External Verification Still Required

| Item | Why it is not verified | Exact remaining test |
|---|---|---|
| Google **Workspace / hosted-domain** accounts | only a `gmail.com` test user was available | Sign in with a Google Workspace account whose `hd` matches the address domain; assert `email_verified_at` is stamped, and that an `hd` **not** matching the address domain leaves it NULL. |
| Google identity **linking / unlinking** with a real second account | one Google account available | With a password account signed in, run `/auth/identities/google/authorize` → consent → `/auth/identities/google/link`; assert 200 and one identity row. Then attempt the same Google account from a second Wasla account; assert 409 and that the identity does **not** move. |
| Google account whose **email changes** after linking | cannot change a Google account's address on demand | Change the Google account address, sign in again; assert the same Wasla account resumes and `users.email` is unchanged. |
| Resend **retry after API downtime** | not induced | Stop the API, trigger a send, let Resend retry, restart; assert the delivery event is eventually recorded exactly once. |
| Production **HSTS / `__Host-` cookie** behaviour | requires TLS + `ENVIRONMENT=staging|production` | Deploy behind the shipped nginx with TLS; assert `Strict-Transport-Security` is emitted and the binding cookie is named `__Host-wasla_oauth` with `Secure`. |
| Alert delivery | no Alertmanager in the stack | Point a scraper at `/metrics`, configure the expressions in `docs/OBSERVABILITY.md`, and prove a page reaches a human. |

---

## 41. Test Results

| Suite | Result |
|---|---|
| **Full pytest** (real PostgreSQL) | **3 913 passed, 73 skipped, 0 failed** |
| Skips | object store (`TEST_S3_ENDPOINT_URL`), real-provider OpenAI, POSIX-shell scripts on Windows, Redis-observing tests — **no auth tests skipped** |
| **Auth-specific subset** (23 modules, `WASLA_SECURITY_TESTS=1`) | **523 passed, 0 skipped, 0 failed** |
| Unit (auth) | included above; `test_security`, `test_token_forgery`, `test_token_audience`, `test_google_oidc`, `test_oauth_flow`, `test_oauth_binding`, `test_verification_codes`, `test_token_store`, `test_rate_limit` all pass |
| Integration (auth) | all pass **against `create_all`**; **10 fail against `alembic upgrade head`** (AUTH-02) |
| Concurrency | 9 races, real barriers, real commits — all correct (§22) |
| Failure/crash | Redis-outage matrix — all as designed (§23) |
| **E2E HTTP** | 12 core + 18 lifecycle + 34 token attacks + 10 webhook + 8 rate-limit — all pass |
| **Browser E2E** | **17/17 pass** |
| **Google OAuth (real browser)** | **PASS** |
| **Email (real Resend + ngrok webhook)** | **PASS** — `email.sent` and `email.delivered` both success |
| **Mutation** | round 1: 13/15 killed, 1 skipped; round 2: 4/5 killed. Sole genuine survivor → AUTH-06 |
| **Lint** (`ruff check .`) | **All checks passed** |
| **Format** (`black --check .`) | **509 files unchanged** |
| **Typing** (`mypy app`) | **Success: no issues in 238 source files** |
| **Migrations** | `alembic upgrade head` clean; `alembic check` "no new upgrade operations" — **but blind to enum labels (AUTH-01/02)**; head `0048` |

---

## 42. Authentication Security Score

| Area | Score | Basis |
|---|---:|---|
| Registration | 9/10 | atomic, normalised, race-proof; inherent 409 enumeration is documented and minimal |
| Password security | 9/10 | Argon2id, bounded both ends, equal-cost misses, rehash-on-login, no leakage anywhere |
| Verification | 9/10 | Argon2-hashed codes, durable attempt cap, atomic single-use, cross-user safe |
| Password reset | 9/10 | hash-at-rest, supersession, atomic consume, full session revocation; missing one concurrency test |
| Access-token security | 10/10 | 34 forgery attacks refused; double-checked audience and type; algorithm locked at config *and* decode |
| Refresh/session security | 8/10 | atomic spend, replay teardown, fail-closed on outage — but the bulk revocation lever is **broken** (AUTH-01) |
| Google OAuth | 9/10 | PKCE + nonce + single-use state + browser binding, proven against the real provider; `hd` path unverified |
| Account linking | 10/10 | identity is `sub`, never email; every collision refused correctly; ADR-057 takeover closed |
| Abuse / rate limiting | 9/10 | dual-key limiting, correct proxy trust, no bypass found, degrades rather than disappears |
| Browser security | 9/10 | CORS proven in-browser, strong headers, no open redirect; bearer-token XSS exposure is inherent |
| Tenant interaction | 10/10 | tenant from signed claim only; membership re-read per request; forged `tid` → 403 |
| Failure safety | 9/10 | fail-closed replay controls, transactional outbox, explicit commits where they matter |
| Observability | 5/10 | correct metrics genuinely emitted, 5xx visible per route — **but nothing scraped, nothing alerting, nobody paged** |
| Testing | 4/10 | excellent breadth and real concurrency, undone by a schema source that made six broken endpoints invisible |
| Operations | 3/10 | six documented security controls are non-functional in any migrated database, and the docs assert they work |

**Overall Authentication & Account Security: 6.5 / 10**

The cryptographic and protocol layers are 9–10 work. The score is dragged down by one defect
class — and by the fact that the test and operations layers were unable to notice it.

---

## 43. Final Verdict

```
AUTH NOT READY
```

**Why.** Six account-security endpoints — including "sign out everywhere", "change my password",
and every administrative disable/delete — return HTTP 500 and perform no part of their function
against any database built by this repository's own migrations. These are not peripheral
features; they are the controls a platform reaches for when an account is compromised, and they
are the controls this system's own documentation tells operators and users to rely on. A product
that cannot revoke a session or change a password should not hold real customer credentials.

**Why the verdict is nonetheless narrow.** The defect is a missing `ALTER TYPE`, not a design
flaw. The enforcement machinery behind all six endpoints is correct and was proven correct:
setting `is_active`, `deleted_at` or `token_version` directly makes every session die exactly as
designed. Everything else in this audit — token security, OAuth, linking, concurrency, rate
limiting, outage behaviour, browser behaviour, real Google, real email — passed adversarial
testing, much of it more convincingly than most production systems would.

**The path to READY** is short and specific:

1. Add the migration for the four `audit_action` labels (**AUTH-01**).
2. Add the model/migration enum-parity test to CI (**AUTH-02**).
3. Re-run `test_account_lifecycle.py` against a migration-built schema and confirm 23/23.
4. Correct the six documentation locations in §34.
5. Decide **AUTH-05** before exposing "Continue with Google" as a sign-**up** path.

With items 1–3 done, this codebase would be a straightforward **AUTH READY WITH REQUIRED
EXTERNAL VERIFICATION** (the remaining Google Workspace and TLS items in §40).

---

## Appendix — Answers to the 50 Questions

1. **Can a new user register safely?** Yes — atomically, with a savepoint, normalised address and a DB unique constraint as backstop.
2. **Can two concurrent registrations create duplicate identities?** No. Barrier-tested: `[409, 201]`, exactly one row, including case variants.
3. **Are emails normalized safely?** Yes — `.strip().lower()` in one helper used by every read and write path; the rate limiter uses the same form.
4. **Are passwords stored and verified safely?** Yes — Argon2id at library defaults, bounded 12–128 at edge and domain, rehash-on-login, no plaintext anywhere.
5. **Can login reveal whether an account exists?** No — identical status, byte-identical body, equal Argon2 work, and identical 429 behaviour.
6. **Are brute-force protections effective?** Yes — 5/min per account (survives botnets) plus 10/min per address; no bypass found via case, whitespace, or forged proxy headers.
7. **Can an unverified account authenticate?** Yes, deliberately — it gets a limited onboarding session and is refused every workspace and platform route.
8. **Can verification tokens be replayed?** No — atomic conditional `UPDATE`; replay, supersession and cross-user use all refused.
9. **Can reset tokens be replayed?** No — atomic `consume()`; replayed, superseded and expired tokens all get the same 401.
10. **Does password reset invalidate old credentials appropriately?** Yes — `token_version+1` kills every access and refresh token; verified 401 on both.
11. **Are JWT claims validated strictly?** Yes — signature, issuer, audience and required-claim presence inside `jwt.decode`, plus explicit non-array `aud` and `typ` checks.
12. **Can token types be confused?** No — refused in both directions, by two independent mechanisms (`aud` and `typ`).
13. **Can access tokens be forged or algorithm-confused?** No — `alg=none`, RS256 substitution, wrong secret, stripped and truncated signatures all 401; the algorithm is locked in config *and* at decode.
14. **Are refresh tokens rotated safely?** Yes — spent atomically before anything else, then reissued.
15. **Is refresh-token replay detected?** Yes — and answered by tearing down the entire session estate.
16. **Can concurrent refresh create two valid token branches?** No — barrier-tested `[401, 200]`; the single survivor's descendant is then dead because the teardown fired.
17. **What exactly does logout invalidate?** The presented refresh token only. The access token survives up to 15 minutes. Documented; `logout-all` is the intended full remedy — and it is broken (AUTH-01).
18. **Can a disabled/deleted account continue through existing sessions?** No — *when the state is set*, access and refresh both 401 immediately. But the API cannot set that state today (AUTH-01).
19. **Is Google OAuth protected by state?** Yes — 256-bit, server-side, single-use via `GET`+`DEL` in `MULTI`, 600 s TTL, shape-validated before use.
20. **Is PKCE/nonce required and correctly used?** Yes — S256 only (`plain` not implemented), and the nonce is compared with `compare_digest`. Both mutation-killed.
21. **Are Google ID tokens validated correctly?** Yes — RS256 literal, live JWKS with a bounded three-age cache, both issuer spellings, audience = client id, expiry with 30 s leeway. Proven against a real Google token.
22. **Is `email_verified` respected?** Yes — compared `is True`, checked *before* any account lookup, and auto-verification is further restricted to domains Google is authoritative for.
23. **Can Google login create duplicate local accounts?** No — unique `(provider, subject)`, tombstone-aware address lookup, and an `IntegrityError` backstop for simultaneous first logins.
24. **Can Google login hijack an existing password account?** No — a first Google login onto an existing address is refused with 409; it never signs in, links, or touches the password.
25. **Is provider `sub` treated safely?** Yes — it is the only identity key, stored whole, and a `sub` too long to store is **refused rather than truncated**.
26. **Can OAuth authorization codes be replayed?** No — the state is spent before the exchange, so a second attempt never reaches Google.
27. **Are OAuth redirects safe?** Yes — `redirect_uri` is configuration only; no request field influences any redirect. No open redirect exists anywhere in the auth surface.
28. **Can invitation tokens be stolen/replayed?** Replay: no (atomic `claim()`). Theft of the emailed link grants a membership but **no session**, and cannot set a password on an existing account.
29. **Can invitations grant the wrong role/account?** No — the address and role come from the invitation row; only an owner may invite an owner.
30. **Can auth identity cross tenant boundaries?** No — tenant comes from the signed claim, membership is re-read per request, and a forged `tid` yields 403.
31. **Are cookies/CORS/CSRF safe for the intended architecture?** Yes — bearer tokens (no ambient credential), CORS proven to block disallowed origins in a real browser, and the single OAuth cookie authorises nothing.
32. **Are auth endpoints adequately rate-limited?** Yes — every credential-facing route, with a per-account key in front of login and reset.
33. **Can proxy headers bypass rate limits?** No — headers ignored from untrusted peers; from a trusted peer the XFF walk is right-to-left; nginx overwrites `X-Real-IP`; hostnames are refused at startup.
34. **Are secrets protected?** Yes — every unsafe configuration refused at boot, `.env.example` clean, nothing in `/health` or `/metrics`. One minor fragment leak on a failed boot (AUTH-07).
35. **Are external-provider failures handled safely?** Yes — Google unavailable → 503 not 401; Redis unavailable → replay controls fail closed; email retries with backoff.
36. **Is auth DB state transactionally consistent?** Yes — conditional updates everywhere, and the deliberate explicit commits for consequences that must outlive a failing request.
37. **Are security events observable?** The events and metrics exist and are genuinely emitted — but nothing scrapes them and no alert reaches a human.
38. **Are existing tests actually proving the claims their names make?** Mostly yes — but ten tests in `test_account_lifecycle.py` prove their claims *only against a schema no deployment has*.
39. **Did mutation tests expose false-green auth tests?** They exposed one real coverage gap (AUTH-06) and confirmed the other two survivors were redundant checks. The decisive false-green was environmental, found by re-running the suite against migrations.
40. **Does register→verify→login→refresh→logout work over real HTTP?** Yes — end to end, on real sockets.
41. **Does it work through a real browser?** Yes — 17/17, including CORS, reload persistence and logout.
42. **Does password reset work through a real browser?** Yes — request → emailed token → confirm → new password works → old password refused.
43. **Does Google login work through a real browser?** **Yes** — real consent screen, real callback, real session, account created and profile synced.
44. **If Google browser E2E is blocked, what configuration is missing?** It was not blocked. Remaining Google items are a Workspace (`hd`) account and a second account for linking (§40).
45. **Are there paths where authentication succeeds but bootstrap is incomplete?** Yes — a Google-first account holds a valid session with no workspace and no way to create one (AUTH-05).
46. **Are account lifecycle states recoverable?** By design yes (disable ↔ enable; delete is deliberately terminal). In practice **no transition is currently performable** (AUTH-01).
47. **Which findings block production?** AUTH-01 and AUTH-02.
48. **Which improvements can wait?** AUTH-04, AUTH-06, AUTH-07, AUTH-08, token pruning, alert wiring, doc corrections.
49. **Which missing features are product decisions rather than bugs?** MFA, per-device sessions, login history, self-service deletion, email change, passkeys, additional providers, SSO — and the AUTH-05 Google onboarding question.
50. **Would you permit real customer authentication on this codebase today?** **No — not today, and not for long.** The moment a customer's session is compromised, this system cannot revoke it and cannot let them change their password, while telling them it can. Fix AUTH-01 (one migration), add the AUTH-02 parity guard, and I would be comfortable: the underlying authentication is better than most systems I would sign off.

---

*Prepared by independent audit. Temporary harnesses, databases, tunnels and test accounts
created for this audit were removed afterwards; the repository was verified clean at
`f148a26a9fb4cbbc5ad5a0874ecb5a2807ff4c4a` with no production code modified.*
