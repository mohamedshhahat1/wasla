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

## Password reset — deliberately absent

**There is no password reset flow, and one must not be added until this
repository can send email.**

A reset serves somebody who *cannot* sign in. Its one-time token therefore has
to reach an address that person controls, and delivery is the security control —
without it there is no proof of ownership at all. This repository has no email
capability: no SMTP, no provider client, no queue for it. The only mail-adjacent
dependency is `email-validator`, which validates the shape of an address and
sends nothing.

The tempting shortcut is the one the invitation flow already takes — return the
token in the API response. That is sound for an invitation, because the caller
is an authenticated administrator who is already trusted to hold it and pass it
on. It is catastrophic for a reset: the request is unauthenticated by necessity,
so anyone could ask for a token for any address and read it straight out of the
response. That is account takeover with extra steps, and it would be worse than
having no reset at all, because it would look like a feature.

**Shipped instead: `POST /auth/password`**, an authenticated password *change*.
The current password is the proof, so nothing needs to be delivered anywhere. It
ends every session on success, which covers the common case — "I think something
was taken, rotate my credential and kill what is out there."

What a reset will need when email exists, recorded now so it is not redesigned
under pressure: a one-time token stored only as a hash, a short expiry,
single-use invalidation, an identical response whether or not the address is
registered, a rate limit on requests, a session bump on success, the token never
logged and never returned through the API, and tests covering replay and
expiry. That is a separate capability with an infrastructure dependency, not an
oversight in this one.

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
- **No password reset.** See above.
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

## Still open

- **GitHub Actions are pinned by mutable tag**, not by SHA (`actions/checkout@v4`,
  `aquasecurity/trivy-action@v0.36.0`). A retagged or compromised action would
  run with the workflow's token. The token is scoped to `contents: read` on CI,
  which bounds it. Pinning by digest is the fix and is deferred rather than done.
- **A workspace still cannot remove one member** (W-03a) — the largest remaining
  access-control gap; see `AUTHORIZATION.md`.
- **Redis is unauthenticated** (W-05). Job payloads carry tenant authority, and
  the application validates them, but nothing authenticates the transport.
- **A WhatsApp number can be claimed without proof of ownership** (W-02) — the
  only finding in these audits that crosses a tenant boundary through ordinary
  use.
- **No password reset** — blocked on email delivery, above.
- **Media download is bounded after the fact**, not while streaming: the size cap
  is checked once the body is in memory. The declared size is checked first, but
  a lying server is only caught afterwards.
- **DNS rebinding**, above.
