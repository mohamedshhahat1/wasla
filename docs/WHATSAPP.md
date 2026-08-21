# WhatsApp Integration

**Status: In Progress** — inbound webhook verification, signature checking, parsing, tenant resolution and idempotent event storage are Implemented. The outbound client, media handling and templates are Planned. See [../TASKS.md](../TASKS.md) phase 3.

## Endpoints

| Endpoint | Purpose |
| --- | --- |
| `GET /api/v1/webhooks/whatsapp` | Subscription verification challenge |
| `POST /api/v1/webhooks/whatsapp` | Inbound messages and delivery statuses |

Both sit under the versioned prefix, so the callback URL configured in the Meta app dashboard is `https://<host>/api/v1/webhooks/whatsapp`.

## Configuration

| Setting | Purpose |
| --- | --- |
| `META_APP_SECRET` | Verifies the `X-Hub-Signature-256` payload signature |
| `META_VERIFY_TOKEN` | Shared secret for the subscription challenge |
| `META_ACCESS_TOKEN` | Platform credential for outbound calls (phase 3, outbound) |
| `META_APP_ID`, `META_API_VERSION` | Graph API target |

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

Meta retries any non-2xx response and eventually disables a subscription that keeps failing. The endpoint therefore answers `200` for everything it cannot act on — unparseable bodies, unknown phone numbers, disabled accounts, shapes not yet modelled — and counts and logs each case instead. Only an invalid signature answers `403`, because that request did not come from Meta.

## Tenant resolution

The workspace is resolved from `metadata.phone_number_id` and never from the customer's phone number, which the sender controls. `phone_number_id` is unique platform-wide, so a number can never map to two workspaces. The lookup is the one deliberately unscoped query in this subsystem — the workspace is what is being discovered — and is isolated in `WhatsAppAccountDirectory`, which holds that single method.

An account row carries no Meta credential. A per-workspace access token in a plain column would put a live sending capability in every database dump; until there is encryption at rest, outbound calls use the platform credential.

## Parsing

The parser never raises. Meta adds fields and message types continuously, so entries that cannot be understood are counted (`ignored`) rather than rejected, and the raw payload of every stored event is kept whole so it can be reinterpreted after new support ships.

## Idempotency

Events are stored in `whatsapp_events` under `UNIQUE(tenant_id, event_id)`, so a redelivery is a no-op instead of a duplicate reply to a customer.

Status events compose their key as `{message_id}:{status}`, because Meta reports `sent`, `delivered` and `read` for the same message under the same id; keying on the id alone would keep the first status and discard the rest.

The uniqueness constraint, not the preceding read, is the guarantee. Two simultaneous deliveries of one event both miss the read; the database rejects the loser, Meta retries, and the retry finds the row.

## What the webhook does not do

No AI processing, media downloading, or outbound calls. The request resolves the workspace, stores the event, and returns. Queueing to Redis and message/conversation projection arrive with phases 4 and 5.

## Planned: outbound

A client for text, media, location, buttons, lists and templates, with retries and error mapping; the 24-hour service window and template rules enforced before sending; delivery statuses and read receipts projected onto message rows once those exist (phase 4).
