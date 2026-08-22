# Security

**Status: In Progress** — configuration hygiene, safe error handling, logging redaction, authentication, RBAC, tenant isolation, Meta signature verification, rate limiting and audit logging are Implemented. Per-workspace credential encryption is Planned (phase 14).

Scope: the security model and its controls. Permission mechanics are in [AUTH.md](AUTH.md).

## Secrets and configuration

All secrets come from the environment. `.env` is never committed; `.env.example` documents required variables without values. No secrets in code, images, or CI logs.

**WhatsApp credentials are currently a single global token** (`META_ACCESS_TOKEN`), not per workspace. Per-workspace credentials with encryption at rest are Planned in phase 14; until then a deployment serves every workspace with one Meta token, which is a real limitation of the multi-tenancy rather than a detail.

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
