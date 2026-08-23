# WhatsApp Integration

**Status: Implemented** — inbound webhook (verification, signature checking, parsing, tenant resolution, idempotent storage), the account connection API, the outbound client, media in both directions ([MEDIA.md](MEDIA.md)), and the approved-template registry synced from Meta ([CAMPAIGNS.md](CAMPAIGNS.md)). See [../TASKS.md](../TASKS.md) phase 3.

## Endpoints

| Endpoint | Auth | Purpose |
| --- | --- | --- |
| `GET /api/v1/webhooks/whatsapp` | Meta verify token | Subscription verification challenge |
| `POST /api/v1/webhooks/whatsapp` | Meta signature | Inbound messages and delivery statuses |
| `POST /api/v1/whatsapp/accounts` | Owner, admin | Connect a WhatsApp Business number |
| `GET /api/v1/whatsapp/accounts` | Any member | List connected numbers |
| `POST /api/v1/whatsapp/accounts/{id}/disable` | Owner, admin | Stop accepting and sending traffic |
| `POST /api/v1/whatsapp/accounts/{id}/enable` | Owner, admin | Resume traffic |
| `POST /api/v1/whatsapp/accounts/{id}/verify` | Owner, admin | Prove control of a number already held |
| `POST /api/v1/whatsapp/accounts/{id}/release` | Owner, admin | Give the number up so another workspace can claim it |

The webhook sits under the versioned prefix too, so the callback URL configured in the Meta app dashboard is `https://<host>/api/v1/webhooks/whatsapp`.

## Configuration

| Setting | Purpose |
| --- | --- |
| `META_APP_SECRET` | Verifies the `X-Hub-Signature-256` payload signature |
| `META_VERIFY_TOKEN` | Shared secret for the subscription challenge |
| `META_ACCESS_TOKEN` | Platform credential for outbound calls |
| `META_APP_ID`, `META_API_VERSION` | Graph API target |

## Connecting a number

**A number is claimed by proving control of it, not by naming it** (ADR-037). The request carries a Meta access token, and before anything is written the platform reads `GET /{phone_number_id}` from the Graph API with *that* token and requires the node that comes back to be the node that was asked for. A token that can read a phone number node is a token the owning business issued; nothing else can read it.

The reason this exists: `phone_number_id` is not secret. It appears in every webhook payload, in Meta's dashboard, and in support threads. Platform-wide uniqueness decides who claimed a number *first*, not who is entitled to it — so before ownership proof, a workspace that knew a competitor's number could claim it and become the tenant every inbound message for that number resolved to.

| Field | Required | What happens to it |
| --- | --- | --- |
| `phone_number_id` | yes | Stripped, then verified. A trailing space copied from the dashboard would silently break webhook resolution for every inbound message |
| `access_token` | yes | The proof. Encrypted and stored if this deployment has a credential key (ADR-034), discarded otherwise. Never returned by any response model, never logged |
| `waba_id` | no | An assertion to **check**, not a value to store. Meta names the owning business account; a mismatch is refused rather than quietly corrected |
| `display_name` | no | The workspace's own label — "Support", "Sales". Cosmetic and local, which is why it is still an input |

`display_phone_number` and `verified_name` are no longer accepted at all: they come back from Meta during verification, so there is nothing for a caller to get wrong and nothing to spoof.

**The platform credential is deliberately not a route to this.** `META_ACCESS_TOKEN` can read every number the platform is connected to, so a claim proven with it would succeed for every workspace and prove nothing about any of them. The connect service is built with a verifier that holds no credential and has no access to settings, so the bypass is closed structurally rather than by a condition somebody could invert.

**Storage is a separate question from proof.** Verification needs the plaintext for the length of one call; storing it needs `CREDENTIAL_ENCRYPTION_KEYS`. A deployment without a key still connects numbers — proof happens, the token is discarded, and sending falls back to the platform credential as before.

### What a failure looks like

| Situation | Answer |
| --- | --- |
| Wrong number, revoked token, no permission, Graph outage, timeout, malformed reply | `422 whatsapp_ownership_unverified` — **one** message for all of them. Distinguishing them would turn the endpoint into an oracle for mapping other businesses' numbers |
| The number is already held by somebody | `409`, saying only that it is connected. Naming the workspace that holds it would be a disclosure |
| Two claims arriving at once | One `201` and one `409`, in either order. The read check gives the clean answer in the ordinary case; the partial unique index is the guarantee, and its violation is translated rather than surfacing as a `500` |
| This deployment cannot verify | `503`. Our misconfiguration, not the caller's mistake — and it refuses rather than accepting an unproven claim |
| Another workspace's account id | `404`, through the same scoped lookup that protects every other row |

Meta's own error text is logged with its numeric code and never returned: provider error strings quote the request back, and this request carries a live credential.

### Disable, enable, release

Disable and enable are named transitions rather than a general `PATCH` on status, because status is the only field with an operational meaning and the named transition keeps the audit trail readable.

**Release is a different act and is kept separate.** Disabling pauses traffic while the workspace keeps the claim; releasing hands the number back so another workspace can prove and claim it. A support request to "turn it off for a week" and one to "hand it back" have opposite consequences for everyone else on the platform.

Releasing is not a delete. The account row carries the workspace's conversations and messages by foreign key, and destroying a customer's history is not an acceptable price for moving a phone number. Setting `released_at` takes the row out of the partial uniqueness index instead — the claim ends, the history stays — and the same column removes it from inbound resolution, from `is_active`, and from the plan's number count. The stored credential is dropped on the way out, because a credential for a number the workspace no longer holds is a live sending capability retained past any authority to use it.

It is not reversible from here. Taking the number back means proving control of it again, at the bar anybody else has to clear — otherwise "release" becomes a way to hold a number in reserve without holding it.

### Numbers claimed before this existed

`ownership_verified_at` is null on them and `ownership_verified` reads `false`, and they are left that way rather than back-dated: that null is exactly the list an operator needs in order to re-verify, and inventing a timestamp would erase it. They are not refused at send time — breaking every existing deployment's traffic to close a claim-time hole would be the worse outage.

**`POST /whatsapp/accounts/{id}/verify` is how such a number is established** (ADR-041). Before it existed there was no way at all: `connect` refuses a number that is already claimed, so the only route was to release the number and claim it again — which frees it to the entire platform in between and hands anybody watching a race worth running. The safe-looking action was the dangerous one.

The number is **not** a parameter. It is read from the row, so proving control of a number you hold can never move a claim the way connecting grants one. Everything Meta returns overwrites what is stored, exactly as at claim time — for a legacy row the business account was typed in when nothing checked it, so Meta's reply is the first trustworthy value that row has ever had, and the audit entry records what changed.

It is not only a migration tool. Re-proving is how an operator establishes that a number they still hold is still theirs at Meta, and it is the only path by which a legacy number can acquire a stored credential: there is no update-credential endpoint, and `connect` refuses an already-claimed number.

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

`WhatsAppClient` covers text, media (by uploaded id — Wasla never sends by link), location, reply buttons, lists, templates, read receipts, and the two-step media fetch for inbound files. Reads take the opposite retry policy to sends: fetching a file twice costs a request and changes nothing anyone can see, so timeouts and 5xx are retried there where a send must never retry them. An outbound template is stored as a `template` message carrying the name and language it was sent with and no body, since Meta renders the wording from its approved copy and Wasla never sees it. The HTTP client, sleep function and attempt budget are injected, so retry behaviour is tested against `httpx.MockTransport` with no network and no real waiting.

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

No AI processing, media downloading, or outbound calls. The request resolves the workspace, stores the event, projects it, enqueues and returns. An attachment is noted and its bytes are left where they are: a file arrives as a handle rather than as data, and fetching it takes two round trips to Meta, which is a worker's job ([MEDIA.md](MEDIA.md)).

Two things *are* done here rather than deferred, and both for the same reason — a worker running later would leave a window in which the wrong message goes out. A customer's reply cancels any follow-up waiting on the conversation, and a message that is entirely a stop word opts them out of campaigns ([CAMPAIGNS.md](CAMPAIGNS.md)). Each costs one comparison against work the request is already doing.

## Planned

- Per-workspace access tokens, once there is encryption at rest (phase 14). Until then every outbound call uses the platform credential from configuration ([ADR-009](../DECISIONS.md)).
