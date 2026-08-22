# Security

**Status: In Progress** — Phase 0 covers configuration hygiene, safe error handling, and logging redaction. Authentication, RBAC, signature verification, and audit logging are Planned.

Scope: the security model and its controls. Permission mechanics are in [AUTH.md](AUTH.md).

## Secrets and configuration

All secrets come from the environment. `.env` is never committed; `.env.example` documents required variables without values. No secrets in code, images, or CI logs. Tenant-specific WhatsApp credentials are stored with encryption at rest rather than as a single global token.

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
