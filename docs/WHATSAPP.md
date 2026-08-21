# WhatsApp Integration

**Status: Implemented** — inbound webhook (verification, signature checking, parsing, tenant resolution, idempotent storage), the account connection API, and the outbound client. Media handling and template sync belong to later phases. See [../TASKS.md](../TASKS.md) phase 3.

## Endpoints

| Endpoint | Auth | Purpose |
| --- | --- | --- |
| `GET /api/v1/webhooks/whatsapp` | Meta verify token | Subscription verification challenge |
| `POST /api/v1/webhooks/whatsapp` | Meta signature | Inbound messages and delivery statuses |
| `POST /api/v1/whatsapp/accounts` | Owner, admin | Connect a WhatsApp Business number |
| `GET /api/v1/whatsapp/accounts` | Any member | List connected numbers |
| `POST /api/v1/whatsapp/accounts/{id}/disable` | Owner, admin | Stop accepting and sending traffic |
| `POST /api/v1/whatsapp/accounts/{id}/enable` | Owner, admin | Resume traffic |

The webhook sits under the versioned prefix too, so the callback URL configured in the Meta app dashboard is `https://<host>/api/v1/webhooks/whatsapp`.

## Configuration

| Setting | Purpose |
| --- | --- |
| `META_APP_SECRET` | Verifies the `X-Hub-Signature-256` payload signature |
| `META_VERIFY_TOKEN` | Shared secret for the subscription challenge |
| `META_ACCESS_TOKEN` | Platform credential for outbound calls |
| `META_APP_ID`, `META_API_VERSION` | Graph API target |

## Connecting a number

The identifiers are copied from the Meta app dashboard, so they are stripped on the way in: a trailing space in `phone_number_id` would silently break webhook resolution for every inbound message.

The workspace comes from the access token — these payloads carry no tenant field, and an unknown field is rejected with `422` rather than ignored. `phone_number_id` is unique platform-wide, so a duplicate answers `409` saying only that the number is already connected; naming the workspace that holds it would be a disclosure. Disabling an account owned by another workspace answers `404` through the same scoped lookup that protects every other row.

Disable and enable are named transitions rather than a general `PATCH` on status, because status is the only field with an operational meaning and the named transition keeps the audit trail readable.

## Subscription verification

Meta calls `GET` with `hub.mode`, `hub.verify_token` and `hub.challenge`. The challenge is echoed only when the mode is `subscribe` **and** the token matches, compared in constant time. A failed attempt gets `403` and never sees the challenge value. If no verify token is configured the endpoint answers `503` rather than accepting an unverifiable subscription.

## Signature verification

`POST` requests are verified as an HMAC-SHA256 of the **raw request body** using the app secret. The raw bytes matter: signing a re-serialised payload would verify our own serialisation rather than Meta's. Comparison is constant-time so timing cannot leak the expected signature.

When no app secret is configured the behaviour splits deliberately:

| Environment | Behaviour |
| --- | --- |
| production | `503` — refuses to serve rather than accept unverified traffic |
| local, test, staging | Warns and continues, so the flow works without Meta credentials |

## Status codes

Meta retries any non-2xx response and eventually disables a subscription that keeps failing. The webhook therefore answers `200` for everything it cannot act on — unparseable bodies, unknown phone numbers, disabled accounts, shapes not yet modelled — and counts and logs each case instead. Only an invalid signature answers `403`, because that request did not come from Meta.

## Tenant resolution

The workspace is resolved from `metadata.phone_number_id` and never from the customer's phone number, which the sender controls. `phone_number_id` is unique platform-wide, so a number can never map to two workspaces. The lookup is the one deliberately unscoped query in this subsystem — the workspace is what is being discovered — and is isolated in `WhatsAppAccountDirectory`, which holds that single method.

An account row carries no Meta credential. A per-workspace access token in a plain column would put a live sending capability in every database dump; until there is encryption at rest, outbound calls use the platform credential.

## Parsing

The parser never raises. Meta adds fields and message types continuously, so entries that cannot be understood are counted (`ignored`) rather than rejected, and the raw payload of every stored event is kept whole so it can be reinterpreted after new support ships.

## Idempotency

Events are stored in `whatsapp_events` under `UNIQUE(tenant_id, event_id)`, so a redelivery is a no-op instead of a duplicate reply to a customer.

Status events compose their key as `{message_id}:{status}`, because Meta reports `sent`, `delivered` and `read` for the same message under the same id; keying on the id alone would keep the first status and discard the rest.

The uniqueness constraint, not the preceding read, is the guarantee. Two simultaneous deliveries of one event both miss the read; the database rejects the loser, Meta retries, and the retry finds the row.

## Outbound client

`WhatsAppClient` covers text, media (link or uploaded id), location, reply buttons, lists, templates, and read receipts. The HTTP client, sleep function and attempt budget are injected, so retry behaviour is tested against `httpx.MockTransport` with no network and no real waiting.

### Retry policy

The Cloud API send endpoint accepts **no idempotency key**, so a retry can duplicate a customer-visible message. Only failures that definitely did not send are retried:

| Failure | Retried | Reason |
| --- | --- | --- |
| `429` | Yes, with backoff | Rejected outright; nothing was sent |
| Connection error | Yes, with backoff | No connection, so no request arrived |
| `5xx` | No | May have been accepted; a duplicate reply is worse than a failure |
| Read timeout | No | Same: the request may have landed |

Meta's error `code`, `type` and `error_subcode` are logged; the message raised to callers is our own, because provider error text can echo fragments of a request and this client holds a live platform credential.

A message accepted without an identifier is treated as an error: delivery statuses arrive keyed on that id, so a message that cannot be identified cannot be tracked.

## What the webhook does not do

No AI processing, media downloading, or outbound calls. The request resolves the workspace, stores the event, and returns. Queueing to Redis and message/conversation projection arrive with phases 4 and 5.

## Planned

- An outbound send API, which belongs with conversations in phase 4: there is a message to persist and a 24-hour service window to enforce, and a raw send endpoint now would invite bypassing both.
- Projection of delivery statuses and read receipts onto message rows, which needs the message model from phase 4.
- Media download and storage (phase 9), template sync and campaigns (phase 11).
- Per-workspace access tokens, once there is encryption at rest (phase 14).
