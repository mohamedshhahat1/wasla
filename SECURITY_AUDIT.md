# Wasla — Complete Security Audit

**Audit date:** 2026-08-23
**Auditor role:** Senior Application Security Engineer / Cloud Security Engineer / Backend Security Architect
**Audited tree:** `b0f922d` (`origin/main`, Phase 15)
**Method:** Read-only inspection of the entire repository, plus dynamic verification of specific claims against a running ASGI instance. **No application, configuration, or test file was modified by this audit** — this report is the only file added.

---

## 0. Scope, and an important correction about what was audited

The audit brief stated that phases through Phase 15 are complete. That is true of the **remote** branch, not of the local working copy:

| Ref | Commit | State |
|---|---|---|
| local `main` | `62d33e4` | **Phase 11** (campaigns). Stale. |
| `origin/main` | `b0f922d` | **Phase 15**. Merge of PR #1. |
| `worktree-phase12-analytics` | `5a2e57f` | Tree-identical to `origin/main`. |

`git diff origin/main worktree-phase12-analytics` is empty, so that branch **is** the Phase-15 tree. **This audit assesses Phase 15** (28,587 lines of application code, 78 routes, 101 test files, 20 migrations).

> **Finding W-29 (Informational), stated here because it shaped the audit:** the local `main` checkout is 21 commits behind `origin/main`. Everything in phases 12–15 — billing, usage, analytics, audit logging, rate limiting, request limits, credential encryption, the delivery pipeline — is absent from it. Any work done in the local checkout without pulling first will be built against a codebase that does not have the security controls this report credits. **Pull before doing anything else.**

Two of my own intermediate findings were **retracted during the audit** after dynamic testing revealed a methodology error (Python was resolving the `app` package from the stale checkout via an editable-install `.pth` file rather than from the Phase-15 tree). Those retractions are recorded in §K so the reasoning is auditable rather than silently dropped.

### What "verified" means in this report

Findings are labelled:

- **[CONFIRMED — dynamic]** — reproduced against a running instance of the Phase-15 application.
- **[CONFIRMED — static]** — the code path is unambiguous on reading; no runtime ambiguity exists.
- **[ASSESSED]** — a design/coverage judgement, not a reproducible exploit.

I did not take documentation, ADRs, `TASKS.md`, `README.md`, or comments as evidence that a control exists. Where a document claimed a control, I read the code; where the code was ambiguous, I executed it.

---

## A. Executive security summary

Wasla is, structurally, a **well-built multi-tenant application**. The core isolation model is not merely present but correctly designed: the acting tenant is read from a signed access token and never from request input; every tenant-owned repository inherits a mandatory abstract `_tenant_filter()`, so a subclass that forgets isolation cannot be instantiated; cross-tenant misses return 404 rather than 403 so error codes cannot be used to probe for another workspace's rows; and the pgvector similarity search — the single highest-consequence query in the product — filters tenant on *both* the chunk and the joined document. Credential encryption is genuinely correct: AES-256-GCM, random nonce, key ring with digest-derived key IDs, and the tenant id bound as additional authenticated data so a ciphertext copied between rows fails to decrypt. Password handling (Argon2id, rehash-on-login, equalised timing for unknown users), webhook HMAC verification over raw bytes, path-traversal defence in the file store, and the deployment pipeline (digest-pinned images, pinned SSH host keys, Trivy gate, SLSA provenance) are all above the standard I typically encounter at this stage.

That quality makes the gaps unusually legible, and they cluster in three places.

**First, anti-automation on authentication is effectively absent.** The rate limiter works and is correctly wired — I verified that it refuses requests — but it derives the caller's identity from the *first* entry of the `X-Forwarded-For` header, which the shipped nginx configuration populates with `$proxy_add_x_forwarded_for` (a value that *appends* the real peer to whatever the client sent). The client therefore controls the rate-limit bucket. I reproduced 20 consecutive failed logins with a rotating header value: zero refusals, 21 distinct Redis buckets. There is no account lockout, no per-account limiting, and no MFA, so this is the only anti-automation control on the login endpoint and one HTTP header removes it.

**Second, there is no way to revoke a person's access to a workspace.** The `memberships` table has no `status` column, `MembershipRepository` has no remove or role-change method, and no `/members` route exists. Access can be granted (invitations work) but never withdrawn. An ex-employee, a compromised account, or a mis-addressed invitation retains workspace access permanently unless someone edits the database by hand. `claude.md` §7 explicitly requires suspending and removing memberships; this is unbuilt rather than broken.

**Third, several controls fail open outside production, and the test suite contains at least one fixture that masks a real defect.** WhatsApp webhook signature verification is skipped entirely — with a log warning — in any environment that is not literally `production`, which includes `staging`, where real customer data typically lives. And `POST /invitations/accept`, documented and tested as "unauthenticated on purpose", now returns **401** in production: the Phase-12 rate-limit wiring attached a router-level dependency that resolves the full authentication chain. The test that asserts otherwise passes only because its fixture chain overrides `get_active_workspace`. Invitation acceptance — the entire team-onboarding path — is broken, and the suite reports it as working.

The unifying theme is that Wasla's **explicitly designed** controls are strong and its **emergent** properties are where risk sits: what happens when a header is attacker-controlled, when an environment is not `production`, when a router-level dependency is added to a router containing an unauthenticated route, or when a capability (revocation) is simply never built. The remediation is correspondingly cheap — most Critical and High items below are a few lines each — but they should be treated as blocking for any deployment that handles real customer conversations.

**Headline counts:** 1 Critical, 6 High, 13 Medium, 10 Low/Informational.

---

## B. Critical findings

---

### W-01 — Authentication rate limiting is fully bypassable with a client-supplied header

**[CONFIRMED — dynamic]**

| | |
|---|---|
| **Severity** | **Critical** |
| **Location** | `app/api/rate_limits.py:45-60` (`client_identity`), with `nginx/nginx.conf:107` |
| **Code change** | Yes |
| **Migration** | No |
| **Tests missing** | Yes |

**What the vulnerability is.** `client_identity()` derives the rate-limit bucket from the first comma-separated entry of `X-Forwarded-For`:

```python
forwarded = request.headers.get("X-Forwarded-For")
if forwarded:
    first = forwarded.split(",")[0].strip()
    if first:
        return first
```

The shipped reverse proxy sets `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;`. That nginx variable **appends** the real peer address to whatever the client already sent. A request arriving with `X-Forwarded-For: 10.0.0.7` reaches the application as `X-Forwarded-For: 10.0.0.7, <real-peer>` — so `[0]` is the **attacker's** value, and the genuine address is discarded. Behind no proxy at all, the header is trivially attacker-controlled with no dilution.

**Why it matters.** This is the only anti-automation control protecting `/auth/login`, `/auth/register` and `/auth/refresh`. There is no account lockout, no failed-attempt counter on the user row, no CAPTCHA, no MFA, and no per-account limiting anywhere in the codebase. Removing this control leaves password guessing entirely unbounded. It also converts every forged header value into a fresh Redis key with a 60-second TTL, giving a secondary memory-pressure vector against the Redis instance that also holds the refresh-token revocation list.

**Attack scenario.** An attacker obtains a credential-stuffing list. They POST to `/api/v1/auth/login` with `X-Forwarded-For` set to a fresh random RFC-1918 address on every request. Each request lands in its own bucket, so the configured limit of 10/minute never engages. Argon2 verification is the only brake. Successful pairs yield a token pair; because `login` accepts an optional `workspace_slug` and `/auth/me` enumerates every workspace the account can open, one hit yields the full workspace inventory for that identity.

**Verification.** Against the Phase-15 application with `rate_limit_auth_per_minute=3`:

```
no XFF, 6 attempts        : [401, 401, 401, 429, 429, 429]
rotating XFF, 20 attempts : [401 x 20]
429s while rotating XFF   : 0
distinct redis buckets    : 21
```

**Current mitigation.** The module docstring acknowledges the weakness ("Behind no proxy the header is attacker-controlled and this is weaker than it looks — which is why it limits *authentication attempts* rather than authorising anything").

**Is the mitigation effective?** **No.** The docstring reasons about the no-proxy case and concludes the risk is acceptable because the value only gates rate limiting. But rate limiting *is* the control, and the reasoning misses that the header is attacker-controlled **even with the proxy in place**, because `$proxy_add_x_forwarded_for` appends rather than replaces. nginx already sets a trustworthy `X-Real-IP: $remote_addr` on line 104, and the application ignores it.

**Recommended fix.**
1. Prefer `X-Real-IP` (proxy-set, not client-influenced), falling back to `request.client.host`. Read `X-Forwarded-For` only when a `TRUSTED_PROXY_IPS` setting is configured, and then take the **last** entry not in the trusted set, never the first.
2. Add a **per-account** limiter on `/auth/login` keyed by the submitted email, independent of network origin — this is what actually stops distributed credential stuffing, which no IP-based limit can.
3. Add progressive delay or temporary lockout after N consecutive failures for one account.

**Missing tests.** No test forges `X-Forwarded-For` against a limited endpoint. Add: (a) rotating-XFF must still be refused; (b) per-account limiting engages regardless of source address.

---

## C. High findings

---

### W-02 — A WhatsApp number can be claimed with no proof of ownership

**[CONFIRMED — static]**

| | |
|---|---|
| **Severity** | **High** |
| **Location** | `app/api/v1/whatsapp.py:35-54`, `app/services/whatsapp_account_service.py:45-104`, `app/repositories/whatsapp_repository.py:57-84` |
| **Code change** | Yes |
| **Migration** | Possibly (a verification-state column) |
| **Tests missing** | Yes |

**What the vulnerability is.** `POST /api/v1/whatsapp/accounts` accepts `phone_number_id`, `waba_id` and `display_phone_number` as free-form strings from any `TENANT_ADMIN`, and stores them after a uniqueness check only. Nothing calls Meta to confirm that the calling workspace actually controls that number or that WhatsApp Business Account. Inbound tenant resolution then trusts this row completely — `WhatsAppAccountDirectory.get_by_phone_number_id()` is the deliberately unscoped lookup that maps an incoming webhook to a tenant.

**Why it matters.** The `whatsapp_accounts.phone_number_id` row *is* the tenant-resolution authority for every inbound customer message. A row written without verification is an unverified claim over another business's message stream.

**Attack scenario.** An attacker registers a free Wasla workspace (registration is open and self-service). They learn a target business's `phone_number_id` — this identifier is not a secret; it is visible to anyone who has integrated with that business and is frequently exposed in client-side code and support threads. They `POST` it as their own. If the platform's Meta app is subscribed to that number's WABA and the target has not yet connected it, every inbound webhook for that number now resolves to the attacker's `tenant_id`: the attacker reads the target's customer conversations, and the attacker's configured AI agent replies to the target's customers under the target's business identity. Even where the preconditions do not hold, two lesser attacks remain: **squatting** (pre-claiming numbers permanently blocks the legitimate business, because `uq_whatsapp_accounts_phone_number_id` is platform-wide) and **enumeration** (the `ConflictError` reliably discloses whether a given number is already onboarded to Wasla).

**Current mitigation.** A platform-wide unique constraint on `phone_number_id`, and the conflict message deliberately does not name the holding workspace.

**Is the mitigation effective?** **Partially.** The unique constraint does close the worst case — two tenants can never hold the same number, so inbound traffic is never ambiguous, and that is a genuinely important property. But uniqueness answers "who got here first", not "who owns this". First-claim-wins is precisely the attacker's advantage. The message-hiding mitigates the enumeration oracle's *detail* but the status code still discloses the fact.

**Recommended fix.** Verify the claim against Meta before persisting: call `GET /{phone_number_id}` with the supplied or platform token and require that the returned `id` matches and that the number belongs to the declared `waba_id`. Preferably, only create accounts through Meta Embedded Signup, where the token exchange itself proves control. Failing that, add a `verification_status` column and refuse to route inbound traffic to an unverified account. Rate-limit and audit connection attempts.

**Missing tests.** No test asserts that connecting a number the workspace cannot prove it owns is refused.

---

### W-03 — Workspace membership can never be revoked; the invitation flow is broken in production

This is two defects with one root: the membership lifecycle is incomplete, and the one endpoint that writes memberships no longer works.

#### W-03a — No member removal, suspension, or role change exists

**[CONFIRMED — static]**

| | |
|---|---|
| **Severity** | **High** |
| **Location** | `app/db/models/membership.py:19-46`, `app/repositories/membership_repository.py:13-37`, `app/api/v1/` (absence) |
| **Code change** | Yes |
| **Migration** | Yes (add `memberships.status`) |
| **Tests missing** | Yes |

**What the vulnerability is.** Access can be granted but not withdrawn:

- `Membership` has `user_id`, `tenant_id`, `role` — and **no `status` column**.
- `MembershipRepository` exposes `get_for_user`, `require_for_user`, `list_members`, `add_member` — and **no remove, no suspend, no role update**.
- **No `/members` or `/users` router exists.** `list_members()` is not reachable from any endpoint.

**Why it matters.** Offboarding is a fundamental access-control operation. `get_active_workspace` correctly re-reads the membership from the database on every request specifically so "withdrawing someone's access takes effect at once instead of whenever their access token happens to expire" — but nothing can perform the withdrawal. The dependency is a correctly-built door with no handle. `claude.md` §7 requires "Removing memberships" and "Suspending memberships"; `TASKS.md` marks Phase 2 complete without them.

**Attack scenario.** An employee with `TENANT_ADMIN` in a workspace leaves the company, or their laptop is stolen, or a phishing attack yields their password. There is no product action that removes their access. The only available responses are (a) direct SQL against production, or (b) setting `users.is_active = false`, which is also not exposed by any API and which evicts that person from **every** workspace they belong to — unacceptable for a contractor or agency who legitimately works across several tenants. A tenant owner cannot even list who currently has access.

**Current mitigation.** None. `users.is_active` is checked at authentication and `tenants.is_active` at workspace resolution, but neither is a per-membership control and neither has an API.

**Recommended fix.** Add `memberships.status` (`active` / `suspended`) with a migration; filter it in `MembershipRepository._select()` so suspended memberships stop resolving everywhere at once; add `GET/PATCH/DELETE /api/v1/members` behind `TenantAdminDep`; forbid removing or demoting the last `TENANT_OWNER`; record every change on the audit trail.

**Missing tests.** No test asserts that a removed or suspended member is refused; no test asserts the last owner cannot be removed.

#### W-03b — `POST /invitations/accept` returns 401, and its test masks it

**[CONFIRMED — dynamic]**

| | |
|---|---|
| **Severity** | **High** (availability + false test assurance) |
| **Location** | `app/api/v1/__init__.py:49-82`, `app/api/v1/invitations.py:92-119`, `tests/integration/test_invitation_endpoints.py:170` |
| **Code change** | Yes |
| **Migration** | No |
| **Tests missing** | Yes — the existing test is neutralised |

**What the vulnerability is.** `invitations.router` is listed in `WORKSPACE_ROUTERS` and included with `dependencies=[_WORKSPACE_LIMIT]`. That guard's signature begins `workspace: ActiveWorkspaceDep`, so FastAPI resolves the entire authentication chain — `get_active_workspace` → `get_current_user` → `HTTPBearer` — for **every** route on that router, including `/accept`, which is documented "Unauthenticated on purpose: the invited person may have no account yet." The `rate_limit_enabled` short-circuit lives inside the guard body and therefore runs *after* dependency resolution, so disabling rate limiting does not disable the auth requirement.

**Verification.** Against Phase 15 with `ENVIRONMENT=production`:

```
POST /api/v1/invitations/accept (no auth) -> 401
{"error":{"code":"unauthenticated","message":"Authentication is required."}}
```

The message is `get_current_user`'s, not the invitation service's — confirming the request never reaches the handler. Under Phase 11 (before the ADR-032 wiring) the same request reached the handler. **This is a regression introduced in Phase 12–15.**

**Why it matters.** Team onboarding is completely broken: an invited person without an account cannot accept, and the whole multi-workspace model in `claude.md` §7 depends on this path. Combined with W-03a, the workspace membership lifecycle has neither a working entrance for new people nor any exit.

**Why the tests do not catch it.** `test_accepting_needs_no_credentials(client, service)` requests the `service` fixture, which depends on the `owner` fixture, which executes `app.dependency_overrides[get_active_workspace] = lambda: workspace`. The router-level guard therefore resolves successfully and the test passes. The neighbouring `test_inviting_requires_authentication` deliberately omits the override and correctly observes 401 — so the suite demonstrates it *can* test the real chain, and simply does not here.

**Current mitigation.** None.

**Recommended fix.** Move `invitations.router` out of `WORKSPACE_ROUTERS`, applying `Depends(workspace_rate_limit)` to the three authenticated routes individually and a client-address limiter to `/accept` (which, being unauthenticated and credential-bearing, needs one). Then fix the test to assert 401/200 **without** any `get_active_workspace` override. As a structural guard, add a test that asserts, for every route in the application, that the set of routes resolving `get_current_user` matches an explicit allow-list — so an unauthenticated route can never silently acquire an auth dependency again.

---

### W-04 — Webhook signature verification is skipped outside production

**[CONFIRMED — static]**

| | |
|---|---|
| **Severity** | **High** |
| **Location** | `app/api/v1/webhooks.py:55-72` |
| **Code change** | Yes |
| **Migration** | No |
| **Tests missing** | Yes |

**What the vulnerability is.**

```python
app_secret = settings.meta_app_secret
if not app_secret:
    if settings.is_production:
        raise DependencyUnavailableError("WhatsApp webhooks are not configured.")
    logger.warning("whatsapp.signature_verification_skipped")
    return
```

`is_production` is `environment == "production"` exactly. The permitted environments are `local`, `test`, `staging`, `production` — so a **staging** deployment with no `META_APP_SECRET` accepts **completely unsigned webhooks from anyone on the internet.**

**Why it matters.** Staging environments routinely hold production-shaped or production-copied data, are reachable from the internet (Meta must reach them to deliver webhooks at all), and are protected far less carefully than production. The webhook is the one unauthenticated write path into the system.

**Attack scenario.** An attacker finds the staging host (certificate transparency logs, DNS enumeration, a `staging.` subdomain). They POST a forged WhatsApp payload with any `phone_number_id` they can guess. With no signature check, the payload is accepted: it creates contacts, injects messages into conversations, and enqueues agent jobs. Because the payload dictates `phone_number_id`, the attacker chooses which tenant to write into. Injected message text is then read by the agent as customer input — a direct prompt-injection channel into another workspace's AI, with outbound WhatsApp sends as the effect.

**Current mitigation.** Production fails closed, which is correct and important. The signature implementation itself (`app/integrations/whatsapp/signature.py`) is textbook: HMAC-SHA256 over the raw bytes (not re-serialised JSON), `hmac.compare_digest`, and a missing header or secret returns `False` rather than passing.

**Is the mitigation effective?** **Only for production.** The fail-open branch covers staging, which is the environment where this actually bites. Additionally, `meta_app_secret` is absent from `_validate_production_hardening`, so a production deployment starts happily and then 503s every webhook — a silent integration outage rather than a startup failure.

**Recommended fix.** Restrict the fail-open branch to `local` and `test` only (`if settings.environment in ("local", "test")`). Add `meta_app_secret` and `meta_verify_token` to the production start-up validator so a misconfigured production deployment refuses to boot rather than silently dropping every customer message.

**Missing tests.** No test asserts that a staging-configured application refuses an unsigned delivery.

---

### W-05 — Redis is unauthenticated, and queue jobs carry tenant authority

**[ASSESSED — static]**

| | |
|---|---|
| **Severity** | **High** (given network access) |
| **Location** | `docker-compose.prod.yml:26-38`, `app/workers/queue.py:50-61`, `app/core/token_store.py` |
| **Code change** | Yes (config) |
| **Migration** | No |
| **Tests missing** | Yes |

**What the vulnerability is.** Redis runs as `redis-server --appendonly yes` with **no `--requirepass`, no ACL, and no TLS**; the health check `redis-cli ping` confirms unauthenticated access. That instance holds three security-relevant datasets:

1. **The refresh-token revocation list** (`auth:refresh:revoked:*`) — the *only* record that a refresh token has been spent or logged out.
2. **Rate-limit counters** (`ratelimit:*`).
3. **Job queues** whose payloads carry `tenant_id` and `conversation_id`, which the workers trust as authority (`AgentJob.decode`).

**Why it matters.** Each dataset is a control, and Redis is the enforcement point for all three. Anyone who reaches the port inherits them.

**Attack scenario.** An attacker who reaches the `internal` bridge network — via a compromised container, an SSRF primitive, a misconfigured `ports:` line, or a shared Docker host — can: `FLUSHDB` the revocation list, reinstating every stolen or logged-out refresh token; reset rate-limit counters at will; and, most seriously, `RPUSH` a forged job onto `agent:jobs:pending` naming an arbitrary `tenant_id` and `conversation_id`. The worker decodes it, resolves that tenant's default agent, and runs a full agent turn — reading that workspace's conversation history and knowledge base and sending a WhatsApp message as that business. That is cross-tenant read and write, driven entirely from Redis.

**Current mitigation.** The `internal` bridge network is not published to the host; only nginx exposes ports. Queue keys are fixed namespaces with no user input, so there is no key-injection vector. Job decoding is strict (`uuid.UUID` parsing, malformed jobs dead-lettered).

**Is the mitigation effective?** **As a boundary, yes; as a defence, it is single-layered.** Network isolation is the only thing standing between an in-cluster foothold and full cross-tenant compromise. There is no authentication as a second layer, which is exactly the defence-in-depth this codebase applies rigorously elsewhere (the pgvector search filters tenant twice; the storage layer checks the key pattern *and* path containment).

**Recommended fix.** Set `--requirepass` from a required environment variable and put the credential in `REDIS_URL`; prefer a Redis ACL user with only the commands the application uses. Enable TLS if Redis is ever off-host. Optionally sign job payloads (HMAC with a worker-shared key) so a forged queue entry is rejected on decode.

**Missing tests.** No test asserts a forged/unsigned job is rejected.

---

### W-06 — TLS is not enabled in the shipped production topology, and each deploy overwrites the operator's nginx changes

**[ASSESSED — static]**

| | |
|---|---|
| **Severity** | **High** |
| **Location** | `nginx/nginx.conf:54-97`, `docker-compose.prod.yml:170-195`, `.github/workflows/deploy.yml:191-198` |
| **Code change** | Yes (deploy pipeline) |
| **Migration** | No |
| **Tests missing** | N/A |

**What the vulnerability is.** Two compounding issues.

1. **As shipped, the production stack terminates no TLS.** The entire `listen 443 ssl` block is commented out. The active listener is `listen 8443` speaking **plain HTTP**, published to `127.0.0.1:8443`. Port 80 redirects to `https://$host` — an address nothing in the stack serves. HSTS is commented out with the TLS block, so it is never sent.
2. **The deploy pipeline overwrites `nginx/` on every release**: `scp -i ~/.ssh/deploy_key -r nginx "${DEPLOY_USER}@${DEPLOY_HOST}:${DEPLOY_PATH}/"`. An operator who follows `docs/DEPLOYMENT.md` and uncomments the TLS block **on the server** has that change silently reverted by the next deployment — and the stack falls back to plaintext without any signal.

**Why it matters.** Bearer access and refresh tokens, WhatsApp payloads, customer PII, and Meta credentials all cross this proxy. The repository's own nginx header says so. A configuration that is correct only until the next deploy is a control that will fail at the least observable moment.

**Attack scenario.** An operator enables TLS on the host, verifies HTTPS, and publishes `443:443`. Two weeks later a routine deploy runs, `scp -r nginx` restores the repository's version with `ssl_certificate` still commented out, and `docker compose up -d` restarts nginx. The listener on 443 is now plain HTTP or fails to start. Because HSTS was never successfully served, browsers happily downgrade, and every bearer token crosses the network in cleartext.

**Current mitigation.** The nginx file is candid that TLS is absent and explains why a self-signed certificate would be worse. Port 8443 is bound to loopback so the plaintext listener is not publicly exposed by default. `docs/DEPLOYMENT.md` documents the procedure.

**Is the mitigation effective?** **The documentation is honest; the mechanism is not safe.** Deliberately shipping without certificates is defensible. Shipping a pipeline that reverts the operator's fix is not — it converts a documented manual step into a recurring silent regression.

**Recommended fix.** Make TLS configuration a deployed artefact rather than a manual host edit: template `nginx.conf` from environment variables (`WASLA_DOMAIN`, cert paths) so the committed file is the one that runs, or exclude `nginx/` from the deploy `scp` and manage it as host state. Uncomment HSTS as soon as certificates renew reliably. Add a post-deploy assertion that the public endpoint answers over HTTPS.

---

### W-07 — The post-deployment readiness gate cannot fail

**[CONFIRMED — static]**

| | |
|---|---|
| **Severity** | **High** (verification integrity) |
| **Location** | `.github/workflows/deploy.yml:222-231`, `nginx/nginx.conf:49-51` |
| **Code change** | Yes |
| **Migration** | No |
| **Tests missing** | N/A |

**What the vulnerability is.** The final deployment gate is:

```bash
ssh ... "curl -fsS --max-time 10 http://127.0.0.1/health/ready"
```

Port 80 on the host is nginx's HTTP listener, whose only non-ACME rule is `location / { return 301 https://$host$request_uri; }`. So `/health/ready` returns **301**, never reaching the application. `curl -f` fails on 4xx/5xx only; a 301 without `-L` exits **0**. The step passes whenever nginx is running — regardless of whether the API is up, the database is reachable, or migrations applied.

**Why it matters.** The workflow's own comment states the intent precisely: "A readiness check here is what turns 'the command exited zero' into 'the release works'." It does not. A completely broken release — API crash-looping, database unreachable, wrong image — is reported as a successful deployment with a green tick, which is the specific failure mode the step was written to prevent. This also silently defeats rollback triggering.

**Current mitigation.** `docker compose up -d --wait` blocks on container health checks, which does catch a container that fails to start. But the API's health check is **liveness only** (`/health/live`), deliberately independent of PostgreSQL and Redis — so a running API with a dead database passes both gates.

**Is the mitigation effective?** **No.** The `--wait` gate and the readiness curl are meant to be complementary; the readiness half is inert, leaving only liveness, which by design proves nothing about dependencies.

**Recommended fix.** Query the application directly rather than through the redirect: `curl -fsS --max-time 10 http://127.0.0.1:8000/health/ready` from inside the compose network (e.g. `docker compose exec -T api curl ...`), or add `-L` plus a real HTTPS endpoint once W-06 is resolved. Assert on the response body (`"status":"ready"`), not merely the exit code.

---

## D. Medium findings

---

### W-08 — Validation errors echo the submitted value, including passwords and tokens, in production

**[CONFIRMED — dynamic]**

| | |
|---|---|
| **Severity** | **Medium** |
| **Location** | `app/core/exceptions.py:192-202` |
| **Code change** | Yes · **Migration** No · **Tests missing** Yes |

`handle_request_validation_error` returns `details={"errors": jsonable_encoder(error.errors())}` **in every environment**. Pydantic v2's `errors()` includes an `input` key holding the raw offending value.

**Verification** (`ENVIRONMENT=production`): a `POST /api/v1/auth/register` with an over-length password returned **422 with the submitted password in the response body**.

The same applies to the invitation token on `/invitations/accept` (a membership-granting credential) and to `access_token` on `/whatsapp/accounts` (a live Meta credential, `max_length=512`). The `handle_unexpected_error` sibling correctly suppresses detail in production; this handler was not given the same treatment, and it contradicts the project's own "never log secrets" principle in `app/core/logging.py`. Impact is propagation rather than direct theft: the value lands in reverse-proxy logs, APM/error-tracking payloads, browser HAR captures and client-side error reporters.

**Fix.** Strip `input` (and `url`) from every serialised error; in production, return only `loc`, `type` and a generic message. Add a test asserting no submitted secret appears in a 422 body.

---

### W-09 — Tool grants are offered to the model but not enforced when a tool runs

**[CONFIRMED — static]**

| | |
|---|---|
| **Severity** | **Medium** |
| **Location** | `app/agents/registry.py:541-552` (`ToolRegistry.run`), `app/agents/orchestrator.py:279-290` (`_run`) |
| **Code change** | Yes · **Migration** No · **Tests missing** Yes |

The orchestrator computes the agent's grants and turns them into specs offered to the model:

```python
grants = await self._grants.list_for_agent(agent_id=resolved.id, enabled_only=True)
specs  = self._registry.specs(grant.name for grant in grants)
```

But execution consults only the global registry:

```python
definition = self._definitions.get(name)
if definition is None:
    raise ToolArgumentError(f"There is no tool named {name}.")
return await definition.handler(context, validate_arguments(definition, arguments))
```

The granted set is never re-checked. Any tool registered in `build_default_registry()` — `request_human_handoff`, `search_knowledge`, `record_lead_details`, `schedule_follow_up` — executes if the model names it, whether or not this agent was granted it.

**Why it matters.** `claude.md` §19 requires "Agents should only receive tools explicitly allowed for them" and "Never allow arbitrary tool execution from model output". Grants are a workspace-configured authorization boundary; enforcing them only by omission from the prompt means the boundary lives in the *provider's* behaviour rather than in Wasla. `schedule_follow_up` writes rows and causes an outbound WhatsApp message; a workspace that deliberately withheld it has no guarantee it stays withheld.

**Exploitability is low in practice** — OpenAI's Responses API normally emits calls only for supplied functions — which is why this is Medium rather than High. It is a trust-boundary and defence-in-depth gap, and the fix is three lines.

**What is done well here and should not be changed:** tenant identity is carried in `ToolContext` from the authenticated session and is **never** a model-supplied argument; `validate_arguments` rejects unexpected arguments, enforces types and enums, and truncates to column widths. That is the hard part, and it is right.

**Fix.** Pass the granted names into `_run` and refuse anything outside that set, returning a `ToolArgumentError` the model can read. Log the refusal as a security event.

---

### W-10 — Refresh-token reuse is detected but the token family is not revoked

**[CONFIRMED — static]**

| | |
|---|---|
| **Severity** | **Medium** |
| **Location** | `app/services/auth_service.py:193-229` |
| **Code change** | Yes · **Migration** No · **Tests missing** Yes |

Rotation is implemented correctly — the presented token is revoked as soon as it is exchanged. When a **spent** token is presented again, the service logs `auth.spent_refresh_token_presented` and refuses that request only. It does not revoke the descendant tokens.

Replay of a spent refresh token has essentially one cause: the token leaked. Standard guidance (RFC 6819 / OAuth 2.0 BCP) is to treat reuse as proof of compromise and revoke the whole family. Here, the attacker who refreshes *first* obtains a fresh valid pair and keeps it; the legitimate user is the one who gets refused, and their next login quietly issues a new token alongside the attacker's live session. The signal is generated and then discarded.

**Fix.** Add a family/session identifier to refresh tokens; on reuse detection revoke every token in that family and force re-authentication. Surface the event to alerting (see W-21).

---

### W-11 — Webhook idempotency is check-then-insert, and jobs are enqueued before commit

**[CONFIRMED — static]**

| | |
|---|---|
| **Severity** | **Medium** |
| **Location** | `app/repositories/whatsapp_repository.py:103-132`, `app/services/whatsapp_service.py:1-13, 183-187` |
| **Code change** | Yes · **Migration** No · **Tests missing** Yes |

`WhatsAppEventRepository.record()` reads, then inserts. The docstring is candid that the unique constraint is the real guarantee and the read is only a fast path. Two consequences:

1. **Concurrent duplicate delivery** (Meta retries aggressively, and replicas are `replicas: 2`) makes both requests miss, and the loser raises `IntegrityError` at flush → unhandled → **500**. Meta then retries the *whole batch*. Data integrity holds (the constraint does its job), but the endpoint answers 5xx under exactly the concurrency it is expected to face — and repeated non-2xx is precisely what ADR-032 says leads Meta to disable the subscription.
2. **Jobs are enqueued before the transaction commits** (a documented trade-off). If the transaction then rolls back — including because of case 1 — jobs already pushed to Redis are not rolled back. On Meta's retry the messages are stored and jobs are enqueued **again**. There is no idempotency key on `AgentJob` and no dedup in the worker, so the same conversation can receive two agent turns and the customer can receive **two replies**. `claude.md` §50 requires "Never send duplicate WhatsApp messages because a worker retried."

**Current mitigation.** `UNIQUE(tenant_id, event_id)` prevents duplicate *storage*, which is the important half, and also provides the only replay protection (Meta's signature carries no timestamp, so a captured body is replayable forever — the constraint is what stops it). Campaign sends are separately protected by `FOR UPDATE SKIP LOCKED`.

**Is the mitigation effective?** For storage and replay, yes. For duplicate *agent replies*, no — nothing deduplicates at the turn or send level.

**Fix.** Use `INSERT ... ON CONFLICT DO NOTHING RETURNING` and treat an empty return as the duplicate case, eliminating both the race and the 500. Catch `IntegrityError` around the flush as a backstop. Give `AgentJob` an idempotency key (e.g. the triggering message id) and have the worker skip a conversation whose latest inbound message already has an outbound reply after it.

---

### W-12 — Account and workspace enumeration on registration

**[CONFIRMED — static]**

| | |
|---|---|
| **Severity** | **Medium** |
| **Location** | `app/repositories/user_repository.py:46-48`, `app/repositories/tenant_repository.py:49-55` |
| **Code change** | Yes · **Migration** No · **Tests missing** Yes (an existing test asserts the current behaviour) |

`POST /auth/register` returns distinct 409s: *"An account with that email address already exists."* and *"That workspace address is already taken."* An unauthenticated caller can therefore enumerate registered email addresses and discover which companies use Wasla.

This directly contradicts the threat model applied elsewhere in the same service: `login` spends verification time on a missing user specifically so response timing cannot disclose registration, and `_resolve_workspace` deliberately returns one answer for "no such workspace" and "you are not in it" because "distinguishing them would let anyone map which workspaces exist." Registration undoes both. With W-01 removing the rate limit, enumeration is unbounded.

`tests/integration/test_authorization.py:207` (`test_a_second_registration_with_the_same_address_conflicts`) asserts the enumerating behaviour as correct, so the suite will resist the fix — this is a design decision to revisit, not merely a bug.

**Fix.** Return an identical generic response whether or not the address exists, and deliver the outcome by email. If the product requires a synchronous answer, treat the disclosure as accepted risk but fix W-01 first, and consider decoupling workspace-slug availability into an authenticated or separately-throttled endpoint.

---

### W-13 — Audit trail covers a minority of privileged actions

**[ASSESSED — static]**

| | |
|---|---|
| **Severity** | **Medium** |
| **Location** | `app/db/models/audit.py:70-89`, callers of `AuditTrail(` |
| **Code change** | Yes · **Migration** Yes (new enum values) · **Tests missing** Yes |

The audit subsystem is well designed — entries are staged in the caller's transaction (so an entry can never survive the rollback of the act it describes), nothing swallows exceptions (so an action whose audit write fails does not silently succeed), and labels are copied at write time so a later rename or deletion cannot render history unreadable. `PlatformAuditLogRepository` deliberately includes the platform's own actions.

But only six call sites exist: invitations, WhatsApp accounts, subscriptions, platform billing, and campaigns. **Not audited:**

- Authentication events — login success/failure, logout, refresh, **workspace switching**
- Registration (account and workspace creation)
- **Agent configuration and tool grants** — changing what the AI is permitted to do
- **Knowledge base and document changes** — the RAG corpus, i.e. retrieval-poisoning surface
- **Conversation mode (AI↔HUMAN), assignment, priority** — `claude.md` §30 requires knowing who took over
- Lead status, assignment and score changes
- **`clear_opt_out`** — re-enrolling someone who asked to stop, a compliance-sensitive act recorded only via `logger.info`
- Template sync

**Tamper resistance** is also absent: `audit_logs` is an ordinary table with no append-only enforcement, hash chaining, or separate credentials. The API exposes reads only, so tampering requires database access — acceptable for now, worth noting for a compliance posture.

**Fix.** Extend `AuditAction` and add `AuditTrail.record` calls at each site above, prioritising `clear_opt_out`, agent/tool grants, knowledge changes, and conversation handoff. Consider a periodic hash-chain checkpoint.

---

### W-14 — The rate limiter fails open on any Redis error

**[CONFIRMED — static]**

| | |
|---|---|
| **Severity** | **Medium** |
| **Location** | `app/core/rate_limit.py:116-127` |
| **Code change** | Judgement call · **Migration** No · **Tests missing** Partially |

Any `RedisError` results in `allowed=True` with a warning. The reasoning ("a limiter that fails closed converts a cache outage into a total outage") is sound for the general workspace limiter and is a legitimate availability trade-off. It is **not** sound for the authentication limiter: an attacker who can degrade Redis — including by flooding it with forged `X-Forwarded-For` buckets per W-01 — removes the only brute-force control, and the failure is invisible to anyone not watching for `ratelimit.unavailable`.

**Fix.** Keep fail-open for workspace and campaign policies. Fail **closed** for the auth policy, or fall back to a bounded in-process limiter so authentication is never entirely unthrottled. Alert on `ratelimit.unavailable`.

---

### W-15 — The unauthenticated webhook is unlimited, timeout-exempt, and buffers 32 MB

**[ASSESSED — static]**

| | |
|---|---|
| **Severity** | **Medium** |
| **Location** | `app/api/v1/webhooks.py:106-136`, `app/core/limits.py:42`, `app/api/v1/__init__.py:73-77` |
| **Code change** | Yes · **Migration** No · **Tests missing** Yes |

`POST /api/v1/webhooks/whatsapp` is deliberately (and correctly) exempt from rate limiting and from the request timeout. It is, however, still subject only to `max_request_bytes` (default **32 MB**), and the handler does `body = await request.body()` — fully buffering into memory — **before** the HMAC is computed, because the signature must be computed over the raw bytes.

An unauthenticated attacker who knows the URL can therefore stream unlimited 32 MB bodies; each one is buffered and HMAC'd before rejection, with no per-caller limit and no timeout. With `replicas: 2` at `memory: 1G`, a modest number of concurrent uploads exhausts memory. `_decode` then runs `json.loads` on up to 32 MB, where deeply nested input raises `RecursionError`, which is not caught (`except json.JSONDecodeError` only) and becomes a 500.

**Current mitigation.** nginx sets `client_max_body_size 10m`. But `app/core/limits.py` argues at length — correctly — that "nginx is one deployment topology, not a property of the software," and that reasoning applies here too.

**Fix.** Give the webhook path its own much smaller body cap (Meta payloads are kilobytes; 256 KB is generous). Catch `RecursionError` and `ValueError` in `_decode`. Consider a high-ceiling per-source-IP limiter that only engages far above Meta's plausible delivery rate, and a bounded timeout well above normal processing.

---

### W-16 — Interactive API documentation is exposed in production

**[CONFIRMED — dynamic]**

| | |
|---|---|
| **Severity** | **Medium** |
| **Location** | `app/core/config.py:41, 206-222`, `app/main.py:60-62`, `docker-compose.prod.yml:55-84` |
| **Code change** | Yes · **Migration** No · **Tests missing** Yes |

`docs_enabled` defaults to `True`, is **not** checked by `_validate_production_hardening` (which validates only `jwt_secret` and `debug`), and `DOCS_ENABLED` is **not set** in the production compose service. Verified against a production-configured instance: `/docs` → **200**, `/openapi.json` → **200**.

This publishes the complete API surface — every platform-administration route, every schema, every field — to anonymous callers, giving an attacker an exact map including endpoints they would otherwise have to guess. Not a vulnerability in itself, but it materially assists every other finding here.

**Fix.** Default `docs_enabled` to `False` when `is_production`, or add it to the production validator; set `DOCS_ENABLED: "false"` explicitly in `docker-compose.prod.yml`.

---

### W-17 — The application sets no security headers; HSTS and CSP are absent entirely

**[CONFIRMED — static]**

| | |
|---|---|
| **Severity** | **Medium** |
| **Location** | `app/main.py:72-86`, `nginx/nginx.conf:91-97` |
| **Code change** | Yes · **Migration** No · **Tests missing** Yes |

A repository-wide search finds **no** security headers set by the application; the sole exception is `X-Content-Type-Options` on the media download route. `X-Content-Type-Options`, `X-Frame-Options` and `Referrer-Policy` exist only on the nginx 8443 block. **HSTS is commented out** and never sent. **No CSP exists anywhere.**

This is the same "nginx is one deployment topology" argument the project itself makes to justify enforcing body-size and timeout limits in the application — applied to those two controls but not to headers. Any deployment that runs the container behind a different ingress, or reaches it in-cluster, loses all of them silently.

**Fix.** Add a small `SecurityHeadersMiddleware` setting `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `Permissions-Policy`, and (when `X-Forwarded-Proto` is https or the environment is production) `Strict-Transport-Security`. Add a restrictive CSP — `default-src 'none'; frame-ancestors 'none'` suits a JSON API. Uncomment HSTS in nginx once certificates renew reliably (W-06).

---

### W-18 — GitHub Actions and base images are pinned by mutable tags

**[ASSESSED — static]**

| | |
|---|---|
| **Severity** | **Medium** |
| **Location** | `.github/workflows/*.yml`, `Dockerfile:3`, `docker-compose.prod.yml:10, 27, 171` |
| **Code change** | Yes · **Migration** No · **Tests missing** N/A |

Every action is referenced by tag (`actions/checkout@v4`, `docker/build-push-action@v6`, `aquasecurity/trivy-action@v0.36.0`), and every base image by mutable tag (`python:3.12-slim`, `pgvector/pgvector:pg16`, `redis:7-alpine`, `nginx:1.27-alpine`). Git tags can be repointed; a compromised upstream action executes in a workflow holding `packages: write` and, in `deploy.yml`, the production SSH key and host. This is the documented mechanism behind several real-world Actions supply-chain incidents.

**What is already right:** `permissions:` is least-privilege at workflow level; there is no `pull_request_target`; no untrusted `github.event.*` value is interpolated into a `run:` block (no script-injection sink); the registry login uses the ephemeral `GITHUB_TOKEN` rather than a long-lived PAT; the published image is deployed **by digest**; `provenance: true` is set; the SSH host key is **pinned** via `DEPLOY_KNOWN_HOSTS`; and the `production` environment provides an approval gate. This is a well-built pipeline — pinning is the remaining gap.

**Fix.** Pin every action to a full commit SHA with a version comment. Pin base images by digest and update them deliberately (Dependabot handles both). Note also that the "Check the deployment target is configured" step validates `DEPLOY_HOST`, `DEPLOY_USER` and `DEPLOY_SSH_KEY` but **not** `DEPLOY_KNOWN_HOSTS`; an empty value produces an empty `known_hosts` and ssh then fails closed, but it should be validated explicitly alongside the others.

---

### W-19 — Media fetch follows redirects while carrying a bearer token, with no host allow-list

**[ASSESSED — static]**

| | |
|---|---|
| **Severity** | **Medium** (low exploitability) |
| **Location** | `app/integrations/whatsapp/client.py:394-410` |
| **Code change** | Optional · **Migration** No · **Tests missing** Yes |

`_get()` issues `GET` with `Authorization: Bearer <token>` and `follow_redirects=True`, against a URL taken from Meta's Graph response. There is no allow-list of permitted download hosts and no guard against redirects into private address space (link-local `169.254.169.254`, RFC-1918, loopback).

Exploitation requires influencing Meta's Graph response or DNS, so this is a hardening gap rather than a live SSRF. Two mitigations already apply: httpx strips the `Authorization` header on cross-origin redirects (so the token does not follow to the CDN), and `MediaService` enforces `media_max_bytes` **twice** — once against the declared size from `probe_media` and again against the bytes actually received (`app/services/media_service.py:113-140`), which correctly closes the TOCTOU where a small declared size precedes a huge body.

**Fix.** Restrict redirect targets to an allow-list of Meta/CDN hosts, cap redirect depth, and reject resolutions to private or link-local ranges. Stream to a size ceiling rather than reading `.content` whole.

---

### W-20 — CORS is configured permissively with credentials and no origin validation

**[ASSESSED — static]**

| | |
|---|---|
| **Severity** | **Medium** |
| **Location** | `app/main.py:78-86`, `app/core/config.py:49, 172-185` |
| **Code change** | Yes · **Migration** No · **Tests missing** Yes |

CORS is registered with `allow_credentials=True`, `allow_methods=["*"]` and `allow_headers=["*"]`. `cors_origins` accepts any string with no validation — nothing rejects `*`, a `null` origin, or a plain-HTTP origin, and the production compose passes `CORS_ORIGINS` straight through with an empty default.

Because authentication is `Authorization: Bearer` rather than cookies, the practical impact of a wildcard is lower than it appears (the browser will not attach ambient credentials). But `allow_headers=["*"]` explicitly permits `Authorization` on cross-origin requests, and a misconfigured or overly broad origin list lets any page in that set drive the API with a token it can reach.

**Fix.** Validate each origin as an absolute `https://` URL (permitting `http://localhost` outside production) and reject `*` when `allow_credentials` is true. Narrow `allow_methods` and `allow_headers` to what the frontend actually uses.

---

## E. Low and informational findings

---

**W-21 — No security monitoring, alerting, or incident-response path. [ASSESSED]**
Structured logging is genuinely good — request IDs, correlation context, key-name-based secret redaction, and a deliberate discipline of logging identifiers rather than customer message content (I checked; no message bodies, phone numbers or email addresses are logged). But nothing consumes these logs. There are no alerts on `auth.spent_refresh_token_presented` (token theft), `whatsapp.invalid_signature` (forged webhooks), `ratelimit.refused` / `ratelimit.unavailable`, `credential.decryption_failed`, or `repository.scoped_row_missing` (attempted cross-tenant access) — each of which is a high-quality security signal that is currently written and discarded. No runbook, no on-call path, no documented incident response. *Fix: ship logs to a searchable store and alert on those events.* **Severity: Medium-Low**, listed here because it is a gap in an otherwise-present control rather than a flaw.

**W-22 — `X-Request-ID` is client-controlled, unbounded, and reflected. [CONFIRMED — static]**
`app/core/middleware.py:30` takes the client's header verbatim, binds it into every log line, and reflects it in the response. In `console` log format it is unescaped, permitting log-line forgery; the value has no length limit. nginx overwrites it with `$request_id`, so this only bites when the container is reached directly. *Fix: validate against `^[A-Za-z0-9._-]{1,64}$` and otherwise generate one.*

**W-23 — `jwt_algorithm` is configurable and unvalidated. [ASSESSED]**
`app/core/config.py:54` allows any string, used directly in both `jwt.encode` and `jwt.decode`. PyJWT rejects `none` with a non-empty key and asymmetric algorithms fail against an HMAC secret, so this is not directly exploitable — but a security-critical primitive should not be free-form. *Fix: constrain to `Literal["HS256","HS384","HS512"]`.*

**W-24 — Access tokens cannot be revoked. [ASSESSED — documented trade-off]**
Deliberate and reasonable (15-minute TTL, stateless verification), and partially compensated because `get_active_workspace` re-reads the membership on every request. The residual exposure is a ≤15-minute window after logout or role change. Becomes materially worse in combination with W-03a, where nothing can trigger revocation at all.

**W-25 — Container runtime hardening is absent. [ASSESSED]**
The Dockerfile is strong — multi-stage, non-root `wasla` user (uid 1001), `apt upgrade` for base CVEs, pip deleted from the runtime image, OCI provenance labels, liveness-only health check, no secrets, no Docker socket. But `docker-compose.prod.yml` sets no `read_only: true`, no `cap_drop: [ALL]`, no `security_opt: [no-new-privileges:true]`, and no `tmpfs` for writable paths. *Fix: add all four; the media volume is the only path needing write access.*

**W-26 — Secrets are delivered as environment variables. [ASSESSED]**
Visible via `docker inspect`, `/proc/<pid>/environ`, and crash dumps. Common practice and acceptable, but Docker/Swarm secrets or a secrets manager would be better for `JWT_SECRET`, `CREDENTIAL_ENCRYPTION_KEYS` and `META_ACCESS_TOKEN`.

**W-27 — Key rotation is designed but never executed. [ASSESSED]**
`CredentialCipher.needs_rotation()` exists and is correct; the docstring admits "Nothing calls it yet." There is no re-encryption sweep, so a rotated-out key must be kept in the ring indefinitely or credentials become unreadable. *Fix: add a worker that re-encrypts rows whose `key_id` is not primary.*

**W-28 — PostgreSQL connections are not TLS-enforced. [ASSESSED]**
No `sslmode=require` in `DATABASE_URL` guidance or the compose file. Acceptable on a single-host bridge network; must change the moment the database is off-host. No documented backup strategy or backup encryption.

**W-29 — Local checkout is 21 commits behind `origin/main`.** See §0.

**W-30 — `.claude/` is untracked and not ignored. [ASSESSED]**
`git status` shows `?? .claude/`, which contains complete secondary checkouts under `.claude/worktrees/`. A careless `git add -A` would commit a nested working tree. *Fix: add `.claude/` to `.gitignore`.*

---

## F. Security controls already implemented (verified in code, not assumed)

These are genuinely present and correct. They should be protected during remediation.

| Control | Evidence |
|---|---|
| **Tenant isolation architecture** | `TenantScopedRepository` with an **abstract** `_tenant_filter()` — a subclass that omits it cannot be instantiated. Every read starts from `_select()`, which carries the predicate. `repositories/base.py:59-106` |
| **Tenant comes from the token, never from input** | `get_active_workspace` reads `tid` from the signed JWT; no route accepts a tenant parameter. `api/dependencies.py:148-172` |
| **Membership re-checked every request** | Not trusted from the token, so revocation takes effect immediately (once W-03a makes revocation possible). |
| **Cross-tenant miss returns 404, not 403** | `TenantIsolationError(NotFoundError)` — error codes cannot probe for another tenant's rows. `core/exceptions.py:78-83` |
| **Tenant-isolated vector search** | Filters `DocumentChunk.tenant_id` **and** `Document.tenant_id`, plus `status == READY` and non-null embedding. `repositories/knowledge_repository.py:252-290` |
| **Credential encryption at rest** | AES-256-GCM, random 96-bit nonce, key ring keyed by SHA-256 digest, **tenant id bound as AAD** so a ciphertext moved between rows fails to decrypt. Uniform error to avoid an oracle. `core/crypto.py` |
| **Credentials never returned or logged** | Write-only schema field, no response model exposes it, `ResolvedCredential.__repr__` overridden. Decryption failure refuses rather than silently downgrading to the platform token. `services/credential_service.py` |
| **Password hashing** | Argon2id, rehash-on-login, 12–128 char policy, `spend_verification_time` to equalise timing for unknown users. `core/security.py` |
| **Webhook HMAC verification** | HMAC-SHA256 over **raw bytes**, `hmac.compare_digest`, fails closed on missing header/secret. `integrations/whatsapp/signature.py` |
| **Webhook verify-token check** | Constant-time; the challenge is never echoed to a failed attempt. `api/v1/webhooks.py:80-98` |
| **Webhook idempotency** | `UNIQUE(tenant_id, event_id)`, scoped per workspace so one tenant cannot suppress another's traffic. |
| **Path-traversal defence** | Keys are server-generated; `SAFE_KEY` regex **plus** `resolve()`/`is_relative_to()` containment. `core/storage.py:165-181` |
| **Stored-media serving** | Authenticated, tenant-scoped, cross-conversation IDOR check, `Content-Disposition: attachment` + `nosniff`. `api/v1/conversations.py:208-241` |
| **No SQL/ORM injection** | Repository-only queries; `text()` appears solely in static index predicates; no f-string SQL. No `eval`, `exec`, `pickle`, `subprocess`, or `yaml.load` anywhere. |
| **Tool argument validation** | Strict types, rejects unexpected arguments, enum enforcement, truncation to column widths; **tenant id is never a model-supplied argument**. `agents/registry.py:135-185` |
| **Prompt-injection containment** | Retrieved passages enter as *tool output*, never concatenated into the system prompt; `MAX_CONTEXT_CHARACTERS` bounds them; empty retrieval returns an explicit refusal instruction rather than silence. |
| **Campaign abuse controls** | Approved-template check at compose **and** send; audience restricted to contacts who wrote first; **opt-out re-checked at send time** (`_deliver`, `campaign_service.py:545`) so opting out mid-campaign works; `FOR UPDATE SKIP LOCKED` prevents double sends; rate limit persisted on the row rather than slept. |
| **Inbound opt-out honoured synchronously** | Recorded on the webhook path, not deferred to a worker, closing the window where a sweep could message someone who just said stop. |
| **24-hour service window** | Enforced on free-form text and media; templates are the sanctioned exception. `services/messaging_service.py` |
| **Request body and timeout limits in the app** | Pure-ASGI body limit checked **before** the stream is read, plus streaming enforcement for a lying `Content-Length`. `core/limits.py` |
| **Production start-up guard** | Refuses to boot with a placeholder or short `JWT_SECRET`, or with `DEBUG` on. Verified: it fires. |
| **Production error suppression** | `handle_unexpected_error` returns no exception detail in production (but see W-08 for its sibling). |
| **Structured logging with redaction** | Key-name redaction for password/secret/token/api_key/authorization/credential/cookie/signature; identifiers logged, customer content deliberately not. |
| **Audit trail design** | Same-transaction staging, no swallowed exceptions, labels copied at write time, platform actions included. |
| **Reliable job queue** | `BLMOVE` to an in-flight list, dead-letter list, malformed jobs dead-lettered, fixed key namespaces (no key injection). |
| **Delivery pipeline** | Digest-pinned deploys, `provenance: true`, Trivy CRITICAL/HIGH gate, **pinned SSH host key**, `production` environment approval gate, migrations before serving, checkout of the CI-verified SHA rather than branch head. |
| **Secrets hygiene** | Nothing sensitive tracked; `.gitignore` correct; gitleaks scans **full history** with `--redact`; `pip-audit` on a weekly schedule. |
| **Test suite** | 101 files including explicit cross-tenant, RBAC, invitation-privilege and webhook-signature negative tests. |

---

## G. Security controls partially implemented

| Control | What exists | What is missing |
|---|---|---|
| **Rate limiting** | Correct limiter, sensible policies, correctly wired to routes (verified refusing) | Identity is attacker-controlled (**W-01**); fails open on the auth path (**W-14**); no per-account limit |
| **Membership lifecycle** | Invitations, roles, per-request re-check | No revoke, suspend, role change, or member listing (**W-03a**); accept is broken (**W-03b**) |
| **Webhook authenticity** | Correct HMAC, production fail-closed | Fail-open in `staging` (**W-04**); no replay window; secret absent from the start-up validator |
| **Agent tool authorization** | Grants modelled, stored, and offered per agent | Not enforced at execution (**W-09**) |
| **Audit logging** | Excellent design, 6 action families | ~14 privileged action families unaudited (**W-13**); no tamper resistance |
| **Production hardening validator** | Checks `jwt_secret`, `debug` | Ignores `docs_enabled` (**W-16**), `meta_app_secret`, `cors_origins`, `credential_encryption_keys` |
| **Security headers** | Three headers in nginx only | None from the app; no HSTS; no CSP (**W-17**) |
| **TLS** | Complete, correct, commented-out config | Not enabled; reverted on every deploy (**W-06**) |
| **Key rotation** | Key ring + `needs_rotation()` | No re-encryption sweep (**W-27**) |
| **Deployment verification** | `--wait` on container health | Readiness gate cannot fail (**W-07**) |
| **Supply chain** | pip-audit, gitleaks, Trivy, provenance | Actions and base images on mutable tags (**W-18**) |
| **Idempotency** | Strong for webhook storage and campaign sends | Absent for agent jobs / outbound replies (**W-11**) |

---

## H. Security controls completely missing

1. **Access revocation for workspace members** (W-03a) — no remove, suspend, or demote.
2. **Account lockout / per-account brute-force protection** — nothing counts failed attempts per identity.
3. **Multi-factor authentication** — no model, no flow, no recovery codes.
4. **Password reset / forgotten-password flow** — no endpoint exists; a user who forgets their password has no recovery path (also an availability gap).
5. **Session management** — no "log out everywhere", no session listing, no device/IP history.
6. **Refresh-token family revocation on reuse** (W-10).
7. **Security monitoring and alerting** (W-21) — good signals produced, nothing consumes them.
8. **Incident response** — no runbook, no on-call path, no documented breach procedure.
9. **Content Security Policy** — nowhere in app or nginx.
10. **HSTS** — commented out; never sent.
11. **GDPR/privacy operations** — no data export, no right-to-erasure flow, no retention policy. `whatsapp_events.payload` retains complete raw customer messages as JSONB **forever**, with no pruning; `SoftDeleteMixin` exists on `Tenant` and `User` but no deletion pipeline consumes it, and `UserRepository` does not filter `deleted_at`, so a soft-deleted user with `is_active=True` would still authenticate.
12. **Database backup and restore strategy** — no policy, no encryption, no tested restore, no documentation.
13. **Redis authentication** (W-05).
14. **Anti-automation on registration** beyond the bypassable IP limiter — no CAPTCHA or email verification, so unlimited free workspaces can be created (this is also the precondition for W-02).
15. **WhatsApp number ownership verification** (W-02).
16. **Per-tenant encryption key separation** — one key ring for all tenants; AAD binds the ciphertext to a tenant but does not isolate key compromise.
17. **Automated dependency updates** — pip-audit reports, but there is no Dependabot/Renovate to act.

---

## I. Recommended remediation order

**Phase A — before any real customer traffic (days)**

1. **W-01** Fix `client_identity` to use `X-Real-IP`/`request.client.host`; add a per-account login limiter. *(Critical, ~30 lines)*
2. **W-03b** Remove `invitations.router` from `WORKSPACE_ROUTERS`; apply limits per route; fix the neutralised test. *(High, ~15 lines — onboarding is currently broken)*
3. **W-04** Restrict the signature fail-open branch to `local`/`test`; add `meta_app_secret` to the production validator. *(High, ~5 lines)*
4. **W-08** Strip `input` from validation-error responses. *(Medium, ~5 lines — credentials are leaking today)*
5. **W-16** Disable docs in production. *(Medium, ~3 lines)*

**Phase B — before onboarding third-party businesses (1–2 weeks)**

6. **W-03a** `memberships.status` + migration + `/members` endpoints + audit. *(High)*
7. **W-02** Verify number ownership against Meta before persisting. *(High)*
8. **W-05** Redis `requirepass`/ACL. *(High, config)*
9. **W-06 / W-07** Template nginx TLS as a deployed artefact; fix the readiness gate. *(High)*
10. **W-17** `SecurityHeadersMiddleware` + CSP + HSTS. *(Medium)*
11. **W-14** Fail closed on the auth limiter. *(Medium)*

**Phase C — hardening (2–4 weeks)**

12. **W-09** Enforce tool grants at execution.
13. **W-11** `ON CONFLICT DO NOTHING` + agent-job idempotency key.
14. **W-10** Refresh-token family revocation.
15. **W-13** Extend audit coverage (start with `clear_opt_out`, tool grants, knowledge changes, handoff).
16. **W-15** Webhook-specific body cap; catch `RecursionError`.
17. **W-18** SHA-pin actions; digest-pin base images.
18. **W-12** Decide the registration-enumeration trade-off deliberately.
19. **W-20** Validate CORS origins.

**Phase D — programme-level (ongoing)**

20. Password reset, MFA, session management (§H 3–5).
21. Security monitoring and alerting (W-21); incident-response runbook.
22. GDPR: retention policy for `whatsapp_events.payload`, export and erasure flows, `deleted_at` filtering.
23. Backup strategy with tested, encrypted restores.
24. Key-rotation sweep (W-27); container hardening (W-25).

---

## J. Proposed security hardening phases

### Phase 16 — Authentication and access control hardening
**Closes:** W-01, W-03a, W-03b, W-10, W-12, §H 2–6
Trusted-proxy handling and per-account rate limiting; account lockout with progressive delay; `memberships.status` and the full member-management API; refresh-token families with reuse revocation; "log out everywhere"; password reset; enumeration-resistant registration.
*Migration: yes (`memberships.status`, token family id, failed-attempt counters).*
*Tests: XFF-forgery rejection; suspended/removed member refused; last-owner protection; unauthenticated `/accept` **without** dependency overrides; token-family revocation.*

### Phase 17 — Trust boundaries and provider hardening
**Closes:** W-02, W-04, W-05, W-09, W-11, W-15, W-19
Meta number-ownership verification; signature fail-open restricted to local/test; Redis authentication and optional job signing; execution-time tool-grant enforcement; `ON CONFLICT` idempotency plus agent-job idempotency keys; webhook-specific body cap; media redirect allow-list.
*Migration: yes (`whatsapp_accounts.verification_status`, agent-job idempotency).*
*Tests: unverified number refused; staging refuses unsigned webhooks; ungranted tool refused; concurrent duplicate webhook produces one row and no 500; forged job rejected.*

### Phase 18 — Edge, transport and deployment hardening
**Closes:** W-06, W-07, W-16, W-17, W-18, W-20, W-25
Templated nginx TLS as a deployed artefact with HSTS; a real post-deploy readiness gate; `SecurityHeadersMiddleware` and CSP; docs disabled in production and an expanded production validator; SHA-pinned actions and digest-pinned images; `cap_drop`/`read_only`/`no-new-privileges`; validated CORS origins.
*Migration: no.*
*Tests: header assertions on every response; production settings refuse unsafe configuration; a route-inventory test pinning which routes are unauthenticated.*

### Phase 19 — Observability, audit and data governance
**Closes:** W-13, W-21, W-27, W-28, §H 7–8, 11–12, 17
Audit coverage for every privileged action; log shipping and alerting on the security signals already emitted; incident-response runbook; retention and pruning for `whatsapp_events.payload`; GDPR export and erasure; `deleted_at` filtering in `UserRepository`; credential re-encryption sweep; encrypted, tested backups; Dependabot.
*Migration: yes (new audit actions, retention columns).*
*Tests: privileged actions produce audit entries; erasure removes what it claims; soft-deleted users cannot authenticate.*

---

## K. Retractions and audit-methodology notes

Recorded so the reasoning behind this report is auditable.

**Retraction 1 — "Rate limiting is entirely inert."** I initially found, by both dependency-graph introspection and behavioural testing, that 0 of 78 routes carried any rate-limit dependency and that no Redis key was ever touched. **This was wrong.** My probe scripts ran from a temporary directory, so the current directory was not on `sys.path`; an editable-install `.pth` file resolved `app` to `D:\Wasla\wasla` — the **stale Phase-11 checkout**, which genuinely has no rate limiting. Re-run with `PYTHONPATH` pinned to the Phase-15 tree, the limiter refuses correctly (`[401,401,401,429,429,429]`) and writes the expected Redis keys. Rate limiting **works**. The residual defect is W-01, which is about the *identity* the limiter counts, not whether it runs.

**Retraction 2 — "`/invitations/accept` works unauthenticated."** My first dynamic test returned 401 from the *invitation service* ("That invitation is not valid."), which I read as evidence the handler ran. That test also executed against the stale tree. Re-run against Phase 15, the response is 401 from the *auth dependency* ("Authentication is required.") — the handler is never reached. This confirmed the opposite of my interim conclusion and became **W-03b**; the Phase-11 behaviour establishes it as a Phase-12–15 regression.

**Method note.** Two findings (W-01, W-03b) were reproduced only because static reading was checked against execution. Two others (W-08, W-16) contradicted what the documentation implied. And one test in the suite (`test_accepting_needs_no_credentials`) asserts a behaviour the application does not have, because a fixture chain silently supplied the authentication the test claims is unnecessary. Where this report credits a control, it is because I read the code that implements it — or ran it.

---

*End of report. No application, configuration, or test file was modified by this audit.*

---

## Resolution status — updated 2026-08-23 (`worktree-security-audit`)

This report is a record of what was true when it was written and is not rewritten
in place. What has since changed:

| Finding | Status | Where |
|---|---|---|
| **W-02** — a WhatsApp number could be claimed with no proof of ownership | **Closed** | ADR-037. The connect request carries a Meta access token; the claim is verified against the Graph API for that exact `phone_number_id` before anything is written, and the business account, display number and verified name come from Meta's answer rather than the request. The platform credential is deliberately not a route to it. `tests/unit/test_number_ownership.py`, `tests/integration/test_whatsapp_ownership.py` |
| **W-03a** — no member removal, suspension or role change | **Closed for removal and readmission** | ADR-038. `memberships.status`, enforced in `get_active_workspace`. Role *change* on an existing membership is still only expressible through remove-and-reinstate. `tests/integration/test_membership_revocation.py` |
| **W-05** — Redis unauthenticated | **Closed for the production stack** | `REDIS_PASSWORD` is required by `docker-compose.prod.yml`, and the healthcheck authenticates. Job signing remains unbuilt and unneeded while the transport is authenticated |
| **W-10** — refresh-token reuse detected but the family not revoked | **Closed** | ADR-039. Spending is a single atomic `SET NX`; losing that race raises `users.token_version` and audits it, committed before the refusal is raised. `tests/integration/test_refresh_reuse.py` |
| GitHub Actions pinned by mutable tag | **Closed** | Every `uses:` is pinned to a commit SHA with the tag kept as a trailing comment |
| `JWT_ALGORITHM` unvalidated | **Closed** | Constrained to `{HS256, HS384, HS512}` at startup, so `none` and the asymmetric families cannot be configured. `tests/unit/test_config.py` |
| **W-12** — registration discloses whether an address or slug is taken | Open | Deliberate: the alternative is an unhelpful signup flow, and the disclosure is bounded by the client-address limit |
| **W-14** — the limiter fails open on a Redis error | Open, deliberate | ADR-032 |

Found while closing the above, and fixed here rather than deferred:

- **Revoked memberships and released numbers still consumed plan capacity.**
  `TEAM_MEMBERS` counted every membership row, so removing a colleague on a
  two-seat plan would have consumed the seat permanently. `WHATSAPP_NUMBERS`
  excluded `disabled` but not `released`.
- **`TemplateService.sync` read a workspace's templates with the platform
  credential**, against a `waba_id` the workspace had typed in. Both halves are
  fixed: the id is now Meta's own answer, and the sync resolves the credential
  the same way a send does.
- **The review's own dependency-graph walker read the wrong tree.** Run as a
  script file from a scratch directory, `import app` resolved to the installed
  package in the main checkout rather than to the worktree under review. It
  reported 98 operations against a tree containing 106, and every route added in
  this change was silently absent. Re-run with `PYTHONPATH` pinned and
  cross-checked against `app.openapi()`.

The full current position is in [docs/AUTHORIZATION.md](docs/AUTHORIZATION.md) §6
and §7.
