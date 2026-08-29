# Security

**Status: Implemented** — configuration hygiene, safe error handling, logging redaction, authentication, RBAC, tenant isolation, Meta signature verification, rate limiting, request limits, audit logging and per-workspace credential encryption at rest all exist and are tested.

Scope: the security model and its controls. Permission mechanics are in [AUTH.md](AUTH.md).

## Secrets and configuration

All secrets come from the environment. `.env` is never committed; `.env.example` documents required variables without values. No secrets in code, images, or CI logs.

## Credentials at rest

**Status: Implemented** (ADR-034, superseding ADR-009). A workspace may supply its own Meta token when it connects a number. It is encrypted with AES-256-GCM under a key ring before it reaches the database, and the plaintext exists only inside the call that receives it or the call that sends with it.

- **The workspace is bound into the ciphertext** as additional authenticated data, so a credential copied from one account row to another fails to decrypt. That copy is the obvious attack once a column of tokens exists.
- **Authenticated encryption**, so tampering fails rather than decrypting into attacker-chosen bytes that would then be used as a bearer token.
- **A key ring, not a key.** The envelope names its key by digest, so a new key can be prepended and old credentials keep working; reordering configuration cannot orphan them.
- **Write-only.** No response model contains a token; a caller learns only whether a number has its own credential.
- **No key configured is supported; plaintext is not.** Such a deployment sends through `META_ACCESS_TOKEN` as before and refuses to store a workspace credential, because "in the clear for now" is how a plaintext token column comes to exist.
- **No silent fallback.** A stored credential that cannot be decrypted fails the send rather than quietly sending as the platform, which would be a different sender identity.

Keys come from configuration, which puts them in the environment of every API and worker container — the same exposure `JWT_SECRET` already has. Rotation is possible (prepend a key) but not yet automated: `needs_rotation` identifies stragglers and nothing sweeps them.

## Rate limiting

**Status: Implemented** (ADR-032). Authentication is counted per client address, everything a signed-in workspace does is counted per workspace, and campaigns and template syncs carry a second, smaller budget. A refusal answers `429` with `Retry-After`.

Two properties are deliberate and both are tested. The limiter **fails open**: if Redis is unreachable the request is allowed, because a limiter that fails closed turns a cache outage into a total outage. And **the WhatsApp webhook is never limited** — Meta retries a non-2xx and eventually disables the subscription, so a 429 there loses a customer's message and then the integration. The webhook is bounded instead by signature verification, idempotency on the event id, and doing no inference on the request path.

## Tenant isolation

Isolation is a security control, not a filter convenience. Every tenant-owned query is scoped by `tenant_id` in repositories and services; frontend filtering is never trusted. A client-provided tenant identifier is honoured only after membership verification. Cross-tenant access attempts are explicitly tested for conversations, contacts, leads, messages, documents, embeddings, WhatsApp accounts, agents, analytics, usage, and settings.

## Input and transport

Strong Pydantic validation on every request. Parameterised SQLAlchemy queries prevent injection. Request size limits, timeouts, and retry policies on outbound calls. CORS is configured explicitly per environment, with secure response headers at the proxy and application layers.

## Webhook security

Meta webhook signature verification against the raw request body, subscription challenge verification, and idempotency keyed on WhatsApp event IDs. Rate limiting must never cause loss of Meta webhook retries.

## Outbound abuse

A platform that can write to thousands of phones at once is a spam tool unless something structurally prevents it, and the prevention is an absence rather than a control: **there is no route that accepts a phone number to message.** A campaign audience is derived from contacts the workspace already has a conversation with on the sending number, so consent exists in the data rather than in a promise the platform cannot check ([ADR-025](../DECISIONS.md), [CAMPAIGNS.md](CAMPAIGNS.md)).

Three further limits sit on top of that. Only templates Meta has approved may be broadcast, checked when the campaign is composed and again before every batch. A contact's marketing opt-out lives in the base population of the audience query rather than as an optional filter, and is re-checked at send time. And every campaign is paced by a stored rate limit — well under Meta's own throughput ceiling, because the risk being managed is the number's reputation rather than the API's capacity.

An opt-out is honoured on the inbound path, in the same transaction that stores the message, so there is no window in which a sweep can write to somebody who has already refused.

## Logging and error handling

Structured logs with request correlation IDs. API keys, access tokens, passwords, and secrets are never logged, and full sensitive customer content is avoided unless necessary. Errors surface safe messages; stack traces and internal details are never returned in production responses. Exceptions are never silently swallowed.

## Auditing

Privileged and administrative actions are recorded in an audit log, including platform owner actions, which never bypass auditing.

## Supply chain

Controlled dependency versions, dependency vulnerability scanning, secret scanning, and container scanning where practical.

## Request limits

**Status: Implemented.** A body larger than `MAX_REQUEST_BYTES` is refused with `413` **before it is read** — a limit applied after buffering has already spent the memory it exists to protect — and a request that declares no length is counted as it streams. A handler that runs past `REQUEST_TIMEOUT_SECONDS` answers `504`, which bounds a pooled database connection being held rather than the client's patience.

Both are configured in nginx as well, and both are here anyway: nginx is one deployment topology, not a property of the software. Run the container directly or put a different proxy in front and every limit configured there disappears silently.

The WhatsApp webhook is exempt from the timeout, for the reason it is exempt from rate limiting: a timed-out delivery is a non-2xx that Meta retries until the subscription is disabled. It keeps the body cap, because that protects memory rather than shedding load.

## Audit trail

**Status: Implemented** (ADR-033). `audit_logs` records deliberate acts: who was let into a workspace, which numbers were connected or disabled, every change to what a workspace pays, and every campaign scheduled or cancelled. A workspace reads its own trail at `GET /api/v1/audit-logs` (owners and administrators); platform staff read every entry, including the platform's own, at `GET /api/v1/platform/audit-logs`.

Four properties, each chosen against a specific failure:

- **Append-only.** No route, repository method or service call updates or deletes an entry. The ability to rewrite the record of what you did does not exist to be misused.
- **Labels are copied, not joined.** An entry names `owner@example.com`, not a user id, and keeps naming them after the account is deleted — `actor_id` is `SET NULL`, never `CASCADE`. The interesting entries are always about things that have since been removed.
- **Staged in the same transaction as the act.** An entry cannot survive the rollback of the thing it describes, and an act cannot succeed while its entry fails.
- **The platform is not exempt.** A payment recorded or an invoice voided by platform staff is written to *that workspace's* trail, attributed to the staff member. The customer is entitled to see who marked their invoice paid.

Reads are deliberately not audited: they would bury the entries that matter, and they are the wrong tool for that question.

## Sessions and revocation

Tokens carry a `ver` claim holding the value of `users.token_version` when they
were minted, and both the access token and the refresh token are checked against
that column on use (ADR-036). Raising it by one ends every session that person
holds.

The access-token check rides the user row `get_current_user` already loads to
verify `is_active`, so it costs no extra query — which is why revocation is
immediate rather than waiting out the fifteen-minute access lifetime.

| Action | Who may do it | Effect |
|---|---|---|
| `POST /auth/logout-all` | The account holder | Ends every session, including the calling one. The account stays usable; sign in again. |
| `POST /auth/password` | The account holder, proving the current password | Replaces the password and ends every session. |
| `POST /platform/users/{id}/disable` | Platform staff | Suspends the account and ends every session. |
| `POST /platform/users/{id}/enable` | Platform staff | Restores the account **and bumps again**, so tokens from before the suspension stay dead. |
| `POST /auth/logout` | Anyone holding the token | Revokes that one refresh token via the Redis denylist. |

Three properties worth stating because they are easy to get wrong:

- **Re-enabling bumps the version.** Without it, a disable/enable cycle would
  hand pre-suspension tokens their authority back — resurrecting exactly the
  credentials the disable existed to kill.
- **A token with no `ver` claim is refused.** Tokens minted before migration
  `0021` stop working, so applying it signs out every open session once.
  Treating them as current would exempt precisely the tokens this mechanism
  exists to revoke.
- **Disabling an account is a platform action, not a workspace one.** An account
  is a global identity; a tenant administrator able to disable one could evict
  somebody from workspaces that administrator has nothing to do with.

The Redis refresh denylist still handles ordinary rotation within a session.
Neither replaces the other, and because the version check reads PostgreSQL, a
Redis outage no longer makes revocation impossible.

## Password reset — shipped, and built to the list written before it

**`POST /auth/password-reset/request` and `POST /auth/password-reset/confirm`
exist** (ADR-042). They were blocked until this repository could send email,
because a reset serves somebody who *cannot* sign in: its one-time token has to
reach an address that person controls, and delivery is the security control.

This section previously recorded what a reset would need before it was written,
so that it would not be redesigned under pressure. That list is now the
implementation, item for item:

- **A one-time token stored only as a hash.** 32 random bytes; only its SHA-256
  reaches `password_reset_tokens`. A stolen database yields nothing usable.
- **A short expiry.** 30 minutes.
- **Single-use invalidation.** `consumed_at` is written by an atomic `UPDATE`,
  so racing confirmations produce exactly one winner.
- **Superseding.** Issuing a new token, or completing a reset, marks every other
  outstanding token for the account `superseded_at` — asking repeatedly narrows
  the live surface to one link rather than accumulating them.
- **An identical response either way.** Registered, unknown, suspended and
  passwordless addresses all receive `202` with the same body, so the endpoint
  is not an oracle for which addresses have accounts.
- **A rate limit on requests.** Both routes carry the client-address credential
  limit, which degrades rather than disappears when Redis does (ADR-040).
- **A session bump on success.** `token_version` is raised, so every access and
  refresh token dies with the old password (ADR-036), and the account is told
  through the outbox in the same transaction.
- **The token never logged and never returned through the API.** It exists in
  the emailed link and in the outbox row carrying it, and that row's context is
  cleared the moment the message is sent or permanently fails.
- **Tests covering replay and expiry.** `tests/integration/test_password_reset.py`
  covers reuse, expiry, supersession, enumeration, the constant refusal, and
  that no token reaches the audit trail.

The shortcut deliberately *not* taken is the one the invitation flow takes —
returning the token in the API response. That is sound for an invitation, whose
caller is an authenticated administrator already trusted to hold it, and
catastrophic for a reset, whose request is unauthenticated by necessity.

**`POST /auth/password`** remains the authenticated password *change*: the
current password is the proof, so nothing has to be delivered anywhere. Both now
notify the account holder afterwards.

**Email verification is deliberately absent**, and that is a decision rather
than a gap — nothing in the authorization model reads a verified flag. See
ADR-042 for the reasoning and the residual account-squatting risk.

## Email verification — a six-digit code that proves an inbox

**`POST /auth/email/verification/send` and `.../verify` exist** (ADR-043).
Both require a session and act only on the calling account.

The controls, and why each differs from the reset flow where it does:

- **An Argon2 verifier, not SHA-256.** A reset token is 256 bits of randomness
  and there is nothing to brute-force. Six digits is a million candidates, so a
  fast hash over a stolen database would surrender every live code. The price is
  that a code cannot be a lookup key — challenges are found by account.
- **A short expiry.** Ten minutes by default, `EMAIL_VERIFICATION_TTL_SECONDS`,
  bounded to 60–3600 and refused outside that range in every environment rather
  than clamped.
- **A per-challenge attempt cap** the reset token does not need, because its
  token cannot be guessed. `EMAIL_VERIFICATION_MAX_ATTEMPTS`, bounded 1–10,
  counted by the database and counted *before* the code is compared.
- **Single use, and superseded on reissue** — the reset flow's two mechanisms,
  plus a partial unique index so the database itself refuses a second live
  challenge.
- **No enumeration surface rather than a mitigated one.** There is no
  unauthenticated send endpoint. Neither route accepts an address or an account
  id, so there is nothing to probe and no way to make the platform mail a
  stranger.
- **Bound to the address, not only the account.** Each challenge records the
  address it was issued for. There is no email-change flow yet; when there is,
  a code sent to the old mailbox already cannot verify the new one.
- **Rate limited per account, following ADR-040.** Both policies carry the
  process-local fallback. Keying by account is safe only because the routes
  require that account's session, so nobody can spend a stranger's budget —
  the limit is not an attacker-triggerable lockout.
- **Never logged, never returned, never in a URL.** Covered by a regression
  test that drives both endpoints with a known code and asserts it appears in
  no captured log record and in no audit row.

**It grants nothing.** No route reads the column. That is the property most
worth protecting here: a verified-email check added casually would lock out
every account created before the column existed.

## Google sign-in — a second issuer, and two rules about what it may change

The design, the five ADRs and the flow are in [GOOGLE_OAUTH.md](GOOGLE_OAUTH.md).
Two properties belong here because they are the ones a later change is most
likely to undo by accident.

**A verified Google address never claims an existing account.** A first login
onto an address that already has a Wasla account is refused with `409`, never
signed in and never linked (ADR-049). The caller has proven control of a
*mailbox*; that is not proof of anything about an account registered under it,
which may have been opened by whoever held the address before them. Linking is
authenticated and deliberate, and binds to the account recorded server-side when
the flow began — not to the address in the token.

**A later Google login never moves an account to a new address.** `users.email`
is written once, at enrolment, and never refreshed; once an identity row exists
the email claim is not consulted at all. If it were, control of a Google account
would become the power to move a Wasla account onto any address Google would
attest to, and every password reset thereafter would follow. The display claims
— name and picture — *are* refreshed every login, which is what makes the
address standing still a decision rather than the refresh failing to run;
`tests/unit/test_google_profile.py` and
`tests/integration/test_google_profile.py` assert exactly that pairing.

**The `picture` claim is validated at the boundary, not escaped at the edge.**
It is the one claim whose value is handed straight to a browser, and a signature
is not a safety guarantee: Google signs what the account says, so a
`javascript:` or `data:` URL arrives perfectly signed. Only `https`, with a
host, within the column length is stored; everything else becomes no picture,
never a refused login. Validating on the way in means the column cannot hold a
value that is dangerous for *any* consumer to render, including one written
later that forgets. The hostile shapes are enumerated in
`tests/unit/test_google_oidc.py`.

**No Google credential is stored** — no ID token, access token, authorization
code, and no refresh token, because `access_type=online` means Google never
issues one. "It is never issued" is a stronger guarantee than "do not store it".

## Account lifecycle — what is still missing

Stated rather than carried silently.

- **A workspace cannot remove or suspend a member.** `memberships` has no
  `status` column and there is no `/members` router. Platform staff can disable
  a person's whole account, and a person can end their own sessions, but a
  workspace owner cannot withdraw one colleague's access to one workspace. This
  is the tenant-scoped counterpart to what ADR-036 built, and it is the largest
  remaining gap in access control.
- **Refresh-token families are not revoked on reuse.** Presenting a spent token
  is detected and refused, but the chain a thief already established is not torn
  down by it. Bumping the version does tear it down, so the lever exists — it is
  simply not pulled automatically on reuse.
- **`POST /auth/logout` is unauthenticated and unlimited.** Revoking a token you
  hold is legitimate, but so is revoking one you stole.
- **No email verification.** A decision, not a gap; see ADR-042.
- **Revocation is per-user, not per-session.** Signing one device out while
  leaving another alone would need a session table (ADR-036 records why one was
  not built).

## Response surface

**Validation errors do not repeat what was sent.** Pydantic's `errors()` carries
an `input` key holding the offending value verbatim; it was serialised straight
into the 422 body in every environment, and a rejected over-length password came
back in full. A 422 travels — reverse-proxy access logs, APM payloads, browser
HAR captures, client-side reporters — so that was a credential in all of them.
`loc`, `type` and `msg` are kept; `input`, `url` and `ctx` are stripped.

**Security headers are set by the application, not only by nginx.** nginx is one
deployment topology: `docker-compose.prod.yml` runs the API as its own container
and the image can be run directly, and in both, a header configured only in the
proxy is absent.

| Header | Value | Why |
|---|---|---|
| `X-Content-Type-Options` | `nosniff` | Customer-uploaded media is served through the API; sniffing is the difference between download and execution on this origin |
| `X-Frame-Options` | `DENY` | With `frame-ancestors 'none'` |
| `Referrer-Policy` | `no-referrer` | URLs here carry conversation, lead and media identifiers |
| `Content-Security-Policy` | `default-src 'none'` + narrow allowances | A JSON API; the allowances exist only for the Swagger UI in non-production |
| `Cache-Control` | `no-store` | Responses carry workspace data and access tokens |
| `Strict-Transport-Security` | 1 year | **Only when the request arrived over HTTPS**, and the forwarded protocol is believed only from a peer in `TRUSTED_PROXY_IPS` |

## Outbound requests and SSRF

One URL is not constructed here: the WhatsApp media location, which arrives in a
provider response. It passes through `app/core/net.py` before it is fetched, and
so does every redirect hop.

Correcting an earlier audit note: it claimed the fetch carries a bearer token
across redirects. **It does not** — httpx strips `Authorization` when the origin
changes, and `tests/unit/test_outbound_url_safety.py` pins that against the
installed version. What is real is that the request happens at all: the worker
sits inside the deployment network where `169.254.169.254` answers with instance
credentials and `127.0.0.1:6379` is the Redis holding the refresh-token denylist,
and the fetched body is stored as media and readable back through the API.

Enforced: `https` only; every hop validated, not just the first; judged by
resolved **address** rather than hostname, so a name pointing at metadata does
not pass; IPv4-mapped IPv6 forms handled; at most three redirects.

**Not defeated: DNS rebinding.** A name resolving public here and private when
the socket opens would pass. Closing it needs the connection pinned to the
checked address, which means a custom transport. Deferred deliberately — the
caller is a provider URL rather than a user-supplied one.

## Request limits

The webhook has its own, tighter body cap (`WEBHOOK_MAX_REQUEST_BYTES`, 1 MB)
because it is the one endpoint an unauthenticated caller can reach and signature
verification happens *after* the body is read. The general 32 MB allowance exists
for media uploads by signed-in colleagues. The cap is applied as a `min`, so
lowering `MAX_REQUEST_BYTES` can never loosen the webhook — an existing test
caught that exact regression.

The webhook remains exempt from the request **timeout** and from rate limiting
(ADR-032): a non-2xx is retried by Meta and eventually costs the subscription.

## Production fails closed

`Settings` refuses to start when configuration would be unsafe. Verified in a
container, not only in tests:

| Condition | Environments | Result |
|---|---|---|
| `JWT_SECRET` missing, placeholder, or under 32 chars | all except `test` | refuses to start |
| `DEBUG` enabled | production | refuses to start |
| `DOCS_ENABLED` true | production | refuses to start |
| `META_APP_SECRET` missing | production | refuses to start |
| `CORS_ORIGINS` contains `*` | production | refuses to start |

The CORS rule exists because the middleware sets `allow_credentials=True`, and
Starlette answers wildcard-plus-credentials by echoing whatever `Origin` arrives.
Bearer-token authentication limits the damage, but "the other control saves us"
is not a reason to ship the combination.

## CSRF

**Not applicable, and this is a conclusion rather than an omission.**
Authentication is `Authorization: Bearer` only — there is no cookie anywhere in
the application, no session cookie, and no `Set-Cookie` on any response. A
browser does not attach an `Authorization` header to a cross-site request on its
own, so there is nothing for a forged request to ride. If a cookie transport is
ever added, CSRF protection has to be added with it.

## Container hardening

The image runs as an unprivileged user (`USER wasla`, uid 1001), verified in a
running container. The three application services in `docker-compose.prod.yml`
additionally carry `cap_drop: ALL` and `no-new-privileges:true` — the latter is
what makes the former durable rather than advisory. PostgreSQL, Redis and nginx
keep their defaults: the databases write their own data directories and nginx
binds 80/443.

## Payment callbacks — the one endpoint that decides money moved

`POST /api/v1/webhooks/paymob` is unauthenticated by necessity and settles
invoices, which makes it the highest-value target in the application: a
callback that can be forged is a way to get the product for free.

| Concern | Defence |
| --- | --- |
| Forged callback | HMAC-SHA512 over the twenty fields Paymob documents, compared with `hmac.compare_digest`. Pinned to the vendor's published worked example, so a wrong field order fails in CI rather than against a live account |
| Tampered amount | `amount_cents` is inside the signed set, so raising it breaks the digest — and the amount is checked against the invoice afterwards regardless |
| Replay / provider retry | `UNIQUE(provider, provider_event_id)` on `payment_events`; the insert is the claim, so two simultaneous retries cannot both proceed |
| Cross-tenant settlement | The payment is matched by a reference *we* generated, then loaded through a tenant-scoped repository — a leaked reference still reaches nothing from another workspace |
| Customer-chosen price | The checkout request carries a plan code only; the price is read from the database. `extra="forbid"` makes sending `amount` a 422 |
| Private plan self-selection | `is_public` is enforced on the checkout path as well as on plan selection |
| Card data | Never touches this infrastructure. Redirection means Paymob collects it; the callback carries the last four digits and they are not persisted |
| Secret disclosure | The API key appears only in one `Authorization` header; the HMAC secret only inside `compare_digest`. Neither the sent digest nor the expected one is logged — writing the expected value beside a rejected one turns a refusal into an oracle |
| Misconfiguration | `BILLING_PROVIDER=paymob` without every credential refuses to boot, in every environment. A callback with no provider configured is 503, never 200 |
| Mismatched key modes | A live secret key with a test public key creates a real intention behind a test payment page — nothing is collected and every callback is for money that does not exist. Both halves look valid alone, so the pair is checked at startup |
| Test keys in production | Refused at startup. Every payment would be pretend and every customer would get the product free, while the dashboard looked busy |
| Illegal state transitions | `PAYMENT_TRANSITIONS` and `INVOICE_TRANSITIONS`. A signed callback claiming a refunded payment succeeded is refused rather than believed, and a paid invoice cannot be settled twice |
| Callback progression lost | `provider_event_id` pairs the transaction with the state reported, so `pending` then `success` is two events rather than a duplicate — and a refund notification on the original transaction is not swallowed |
| Credential echoed in a provider error | Paymob quotes the request back in error bodies. Truncation bounds how much returns and not *what*, so this deployment's own secret and HMAC keys are removed from the text before it reaches an exception or a log |
| Refund of somebody else's payment | Tenant-scoped lookup answering not-found, and the amount is the payment's own balance rather than anything a caller sends |
| Stored provider payload | None. `payment_events.detail` is a sentence written by this application; the callback body carries a masked card number, billing details and a redirect URL containing a bearer token, and none of it is persisted |

**The customer's redirect settles nothing.** Paymob sends the browser back with
the transaction in the query string; anybody can visit a URL, and no endpoint
reads it. Only the server-to-server callback moves billing state.

## Verified controls

Confirmed behaviourally during this audit rather than by reading:

- **Credential encryption at rest** — AES-256-GCM, per-encryption random nonce,
  versioned envelope, key ring. A ciphertext moved to another workspace is
  refused (AAD binding), an unknown key is refused, corrupted ciphertext is
  refused, a malformed envelope is refused, and identical plaintext encrypts
  differently each time.
- **Logging carries no secrets.** Every logger call was traced: the hits on
  credential-shaped names are event *names* (`password_changed`) or identifiers
  (`jti`, `key_id`), never values. No schema exposes `hashed_password`,
  `token_hash` or `access_token_encrypted`. Audit metadata carries only a
  version counter.
- **CI permissions are minimal** — `contents: read`, with `packages: write` only
  on the deploy workflow.

## Closed since the last review

- **A WhatsApp number is claimed by proving control of it** (W-02 / M-01,
  ADR-037). The connect request carries a Meta access token; the claim is
  verified against the Graph API for that exact `phone_number_id` before
  anything is written, and the business account, display number and verified
  name come from Meta's answer rather than the request. The platform credential
  is deliberately not a route to this: it can read every number the platform is
  connected to, so a claim proven with it would prove nothing about the
  workspace making it. Every failure — wrong number, revoked token, Graph
  outage, malformed reply — is one refusal with one message, and Meta's error
  text is logged with its numeric code and never returned.
- **A workspace can withdraw one member's access** (W-03a, ADR-038).
  `memberships.status`, enforced in `get_active_workspace`, which every
  workspace-scoped route already resolves — so it takes effect on the next
  request without touching that person's account, their other workspaces, or
  their tokens.
- **A replayed refresh token tears the session estate down** (W-10, ADR-039).
  Spending is a single atomic `SET NX`; losing that race raises
  `users.token_version` and writes an audit entry, committed before the refusal
  is raised so the revocation survives the failed request.
- **GitHub Actions are pinned by commit SHA**, with the tag kept as a trailing
  comment. A retagged or compromised action can no longer run with the
  workflow's token — which matters most on the deploy workflow, where that token
  has `packages: write` and the job holds a deployment SSH key.
- **Redis requires a password in production** (W-05). The private network was the
  old argument for leaving it open, and it is not enough given what this Redis
  holds: the spent-refresh-token denylist. Anything that can write to it can
  delete a key and turn a revoked token back into a live one — undoing a logout,
  an ADR-036 sign-out-everywhere, and the ADR-039 teardown. It also holds the
  agent queue, so write access is enough to make a worker send messages of
  somebody's choosing. `REDIS_PASSWORD` is required by
  `docker-compose.prod.yml`, and the healthcheck authenticates too, so a
  misconfigured password fails the check rather than reporting a healthy server
  that refuses every real client.
- **A response is no longer sent before its write has committed.** The session
  committed in a `yield` dependency's teardown, which runs after the response
  has reached the client — a 25–75 ms window against a containerised
  PostgreSQL, during which a token from `POST /auth/register` was refused. The
  timing was the symptom; the defect was answering `201 Created` before the
  write was durable, so a commit failing afterwards left the caller holding a
  success for something that never happened. `CommittingRoute` now commits
  inside the handler chain. Only a real socket can observe this, so the
  regression test runs one.
- **`JWT_ALGORITHM` is validated to the HMAC family.** It was a free string
  passed straight into PyJWT's `algorithms=` allowlist, so `none` and the
  asymmetric families were both configurable — the latter meaning the
  application would verify with `jwt_secret` as a public key.

- **DNS rebinding is closed** (ADR-040). It was not theoretical: validating a
  hostname and then handing the *name* to httpx meant two independent
  resolutions, and a resolver answering public once and loopback afterwards got
  the body of a service on 127.0.0.1 back. `GuardedTransport` resolves once,
  judges every address, and connects to a literal — so there is no second
  resolution to poison. `Host` and the TLS server name keep the original
  hostname, because pinning the route must not weaken the identity check. Every
  outbound client is guarded, not only the media fetch.
- **A Redis outage no longer removes the login limit** (ADR-040). Measured
  across connection-refused, timeout and authentication failure. Capacity limits
  still fail open — refusing signed-in colleagues to protect uncontended
  capacity *is* the outage — while the credential limits fall back to a bounded
  process-local counter. Failing closed was rejected: it makes signing in
  impossible whenever the cache is down, and anyone able to degrade Redis could
  trigger it. Refresh-token spending was already fail-closed and stays that way.
- **`POST /auth/logout` is rate-limited** (ADR-040), and deliberately still
  unauthenticated: requiring an access token would break logout exactly when it
  is used, and adds nothing against somebody holding a victim's refresh token —
  they can exchange it, which is worse than revoking it.
- **A number claimed before ADR-037 can be proven in place** (ADR-041). It could
  previously only be established by releasing the number and claiming it again,
  which frees it to the whole platform in between — the safe-looking action was
  the dangerous one. `POST /whatsapp/accounts/{id}/verify` reads the number from
  the row, so it cannot move a claim, and `ownership_verified` is now on the API.

## Still open

- ~~No email verification~~ — **built** (ADR-043), which revisits the ADR-042
  clause deciding against it. It proves inbox control and grants nothing: no
  route, permission or entitlement reads `email_verified_at`, and a test
  asserts an unverified account can use the application. What it closes is
  account squatting being *undetectable*, not an authorization gap — there was
  never one. See `docs/EMAIL_VERIFICATION.md`.
- **Ownership is proven at a point in time, not continuously.** A number that
  moves at Meta after the fact is not noticed. Re-verification on a schedule is
  the obvious next step and is now cheap: ADR-041 built the mechanism, and only
  the trigger is missing. Rows claimed before ADR-037 keep working and report
  `ownership_verified: false` until an administrator proves them.
- **Registration discloses whether an address is taken** (W-12), accepted rather
  than fixed. The attacker chooses the workspace slug, so a unique slug makes a
  409 mean "that address exists" whatever the message says — merging the two
  conflict messages would be theatre that costs a real person the ability to
  tell which field was wrong. The only real fix is not creating the account
  synchronously and confirming through the address, which needs the delivery
  channel that password reset is also blocked on. Bounded by the client-address
  limit, and that bound now survives a Redis outage (ADR-040).
- **The credential rate-limit fallback is per process**, so the effective budget
  during a Redis outage scales with the number of API processes. Deliberate, and
  preferable to either alternative.
- **Session revocation is per-user, not per-session** (ADR-036). Membership
  revocation is per-membership and does not touch tokens; the two are different
  operations on different objects.
- **Media download is bounded after the fact**, not while streaming: the size cap
  is checked once the body is in memory. The declared size is checked first, but
  a lying server is only caught afterwards.
