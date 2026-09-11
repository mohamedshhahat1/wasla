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

The workspace is resolved from `metadata.phone_number_id` and never from the customer's phone number, which the sender controls. `phone_number_id` is unique among *live* claims, so a number can never map to two workspaces at once. The lookup is the one deliberately unscoped query in this subsystem — the workspace is what is being discovered — and is isolated in `WhatsAppAccountDirectory`.

**The question is who held the number when the event happened, not who holds it now** ([ADR-101](../DECISIONS.md)). Meta retries an undelivered webhook for up to seven days, so for a week after a number changes hands a delivery can arrive carrying a message the *previous* owner's customer sent. Resolving to the current holder put that message, the customer's phone number and their profile name into a stranger's inbox — with both businesses having proved ownership of the number to Meta, so neither did anything wrong.

`whatsapp_accounts` therefore carries a tenure interval: `ownership_started_at` (set at claim time) and `released_at` (set when the workspace gives the number up). `owner_at` answers the routing question against it:

| Event timestamp | Resolved to |
| --- | --- |
| Inside a claim's `[ownership_started_at, released_at)` | That workspace |
| Outside every claim, and one workspace has ever held the number | That workspace |
| Outside every claim, and more than one workspace has held it | **Nobody.** Dropped and counted as `unowned` |
| Inside two claims at once | **Nobody.** A data-integrity error, logged and counted |

The second row is the one worth reading twice. **The property being protected is cross-workspace disclosure, not chronology.** A provider timestamp can legitimately precede the claim it belongs to — clocks drift, and Meta can hold a message sent moments before a claim committed — so refusing those would drop real customer messages on a number nobody has ever handed over. Once a second workspace appears in the number's history that reasoning is gone, and an unattributable event is dropped rather than guessed at: losing a stray message costs one conversation, and handing it to a stranger cannot be undone.

The live claim is still the fast path and, for a number that has never moved, the only query made. A **delivery status** is resolved differently again — see *Status reconciliation* below.

An event on a number the workspace has since **released** is still recorded, in that workspace, so its conversation history stays intact. It is not answered: the workspace cannot send through a claim it no longer holds, so an agent turn could only end in a refusal after paying for an inference.

An account row carries the workspace's own Meta token, encrypted (ADR-034, superseding ADR-009). A token in a plain column would put a live sending capability in every database dump, so the column is AES-256-GCM with the tenant id as additional authenticated data — a ciphertext lifted into another workspace's row will not decrypt. A workspace that has supplied one sends as itself; one that has not sends through the platform credential from configuration.

## Parsing

The parser never raises. Meta adds fields and message types continuously, so entries that cannot be understood are counted (`ignored`) rather than rejected, and the raw payload of every stored event is kept whole so it can be reinterpreted after new support ships.

## Idempotency

Events are stored in `whatsapp_events` under `UNIQUE(tenant_id, event_id)`, so a redelivery is a no-op instead of a duplicate reply to a customer.

Status events compose their key as `{message_id}:{status}`, because Meta reports `sent`, `delivered` and `read` for the same message under the same id; keying on the id alone would keep the first status and discard the rest.

The uniqueness constraint, not the preceding read, is the guarantee — and the insert says so: `ON CONFLICT DO NOTHING` makes a delivery that loses the race read back the winner rather than raise. Before that, the loser turned the constraint into a `500`, which is an internal error for a situation that is neither internal nor an error, on an endpoint whose failure rate Meta watches. The same savepoint-and-re-read pattern covers the contact and conversation identity races, so a burst of a customer's first messages converges without a single non-2xx.

### Event state, and what "accepted" guarantees

A `200` guarantees the event is **stored**. Whether it has been **processed** is a separate fact, and `whatsapp_events.state` is where it lives ([ADR-102](../DECISIONS.md)):

| State | Meaning |
| --- | --- |
| `received` | Stored, and something it needed has not happened yet. |
| `processed` | Projected, *and* every handoff it needed was accepted by a queue. |
| `failed` | Permanently unprocessable. An operator's problem, not a sweeper's. |

`processed` deliberately does not mean "a row exists". When Redis is unavailable the webhook stores the message, swallows the enqueue failure and still answers `200` — which is right, because a non-2xx would make Meta retry the whole delivery and eventually disable the subscription. The event then stays `received` carrying a bounded reason (`agent_enqueue_failed`, `media_enqueue_failed`), and `InboundRecoveryWorker` finishes it later.

**A redelivery is not a recovery mechanism.** Meta's retry of an event that is already stored stops at the duplicate check whatever state that event is in. Recovery has a single owner — the sweeper — because two paths racing to queue one agent turn is two replies to one customer message.

Events that owe nothing are `processed` immediately: a delivery status, a message type Wasla cannot read, and a message on a number the workspace has released.

To see the backlog:

```
python -m app.workers.queues unprocessed-inbound
```

and `wasla_unprocessed_inbound_events` alerts on it.

## Outbound client

`WhatsAppClient` has methods for text, media (by uploaded id — Wasla never sends by link), templates, read receipts, location, reply buttons, lists, and the two-step media fetch for inbound files.

**Client support is not product support**, and the difference matters when reading that list. Text, media and templates are shipped: there are routes, services and agent paths that reach them. `send_location`, `send_buttons` and `send_list` are called by nothing — no route, no service, no agent tool — so interactive messaging is not a capability this product has, and the inbound side matches: a button or list reply is stored as an `interactive` message whose reply id is kept only in the raw event. Building it out means carrying `interactive.*_reply.id` — never the title, which is display text a customer never chose — into a column of its own. See *Not supported* below. Reads take the opposite retry policy to sends: fetching a file twice costs a request and changes nothing anyone can see, so timeouts and 5xx are retried there where a send must never retry them. An outbound template is stored as a `template` message carrying the name and language it was sent with and no body, since Meta renders the wording from its approved copy and Wasla never sees it. The HTTP client, sleep function and attempt budget are injected, so retry behaviour is tested against `httpx.MockTransport` with no network and no real waiting.

### Retry policy

The Cloud API send endpoint accepts **no idempotency key**, so a retry can duplicate a customer-visible message. Only failures that definitely did not send are retried, and the exception type says which kind of failure it was so a caller does not have to read the message text:

| Failure | Retried in the client | Raises | May a caller send again? |
| --- | --- | --- | --- |
| `429` | Yes, with backoff | `RateLimitedError` | Yes — rejected outright, nothing was sent |
| Connection error | Yes, with backoff | `SendNotAttemptedError` | Yes — no connection, so no request arrived |
| `401`/`403`/Meta `code 190` | No | `ProviderAuthError` | Yes for this message — but the credential is refused for the whole number |
| Meta template codes | No | `TemplateWithdrawnError` | Yes for this message — the template is marked `paused` locally |
| Other `4xx` | No | `SendNotAttemptedError` | Yes — Meta read it and declined; nothing was delivered |
| `5xx` | No | `UncertainDeliveryError` | **No** — may have been accepted |
| Read timeout | No | `UncertainDeliveryError` | **No** — the request may have landed |
| Transport failure (reset, protocol) | No | `UncertainDeliveryError` | **No** — the request left; no answer came back |
| `2xx` with no usable message id | No | `UncertainDeliveryError` | **No** — a `2xx` is Meta saying it accepted the message |

The last row is the one most easily got backwards, and getting it backwards costs a customer a second copy of a message. A `2xx` whose body carries no message id used to be recorded as a definite failure — the one classification that licenses a new send. It is now an unknown. The id is still needed (delivery statuses arrive keyed on it), so such a message cannot be tracked; but "we cannot track it" and "it was not delivered" are different statements and only the first is true.

`ProviderAuthError` and `TemplateWithdrawnError` are both subclasses of `SendNotAttemptedError`, because the truth about *this message* is that nothing was delivered. They are separate types because the truth about the *workspace* is different, and a sweep needs to act on it: a campaign stops on the first refused credential rather than burning one attempt budget per recipient, and a refused template is written back to the registry so the next send is refused locally.

`429` honours `Retry-After` when Meta sends it, clamped so a provider header cannot park a worker indefinitely, and every backoff is jittered — additively, because a retry landing *earlier* than the backoff intended is the one thing backoff must not do.

Meta's error `code`, `type` and `error_subcode` are logged; the message raised to callers is our own, because provider error text can echo fragments of a request and this client holds a live platform credential.

## Delivery protocol

An outbound send commits before Meta can deliver anything ([ADR-093](../DECISIONS.md)):

```
TX1   the send intent, state claimed                  -> COMMIT
--    (media only) upload the file. Delivers nothing.
TX2   state requested                                 -> COMMIT
--    ask Meta to deliver. No transaction, no pooled connection held.
TX3   record what came back
```

The row used to be flushed before the call and committed after it, which is not the same thing: a process that stopped in between left a customer holding a message this system had no record of, and held a pooled connection for the length of a Graph API round trip while it did.

`messages.delivery_state` is what the protocol reads, and it is a different question from `messages.status`. `status` answers *what happened to it* and is advanced by Meta's status webhooks; `delivery_state` answers *may this be sent now, and might Meta already have it*:

| State | Meaning |
| --- | --- |
| `claimed` | Committed, and Meta has not been asked. Provably nothing delivered. |
| `requested` | Meta may have accepted it. **Nothing may send this message again.** |
| `sent` | Meta accepted it and named it. Delivery statuses take over. |
| `undelivered` | Nothing was delivered and that is known — Meta declined, or the request never left. |

It is NULL on every inbound message.

**There is no reconciler for `requested`, and that is a fact about Meta.** The send endpoint takes no idempotency key and offers no lookup keyed on anything Wasla holds before Meta answers, so an unanswered request cannot be asked about. A `requested` row is shown to a person rather than resolved by a sweep, and these are the three things that show it to them:

```
python -m app.workers.queues unresolved-sends
```

`wasla_unresolved_outbound_messages` counts them and `wasla_oldest_unresolved_outbound_age_seconds` says whether the oldest is a send in flight or one that broke an hour ago; `UnresolvedOutboundSends` alerts on the second. `docs/RUNBOOK.md` has the procedure. On a healthy deployment all three read zero, and none of them resends anything — a design that refuses to guess is only honest if somebody is told there is something to decide.

A follow-up or a campaign recipient whose send ended `requested` is recorded as failed with "WhatsApp did not confirm this message, so it was not sent again", and is never retried. An explicit rejection is different: nothing was delivered, so the existing retry policy applies unchanged.

## Status reconciliation

A delivery status identifies itself by the **provider message id**, not by the number it arrived on — and that difference is the whole design. Resolving the workspace from the number and *then* looking the message up inside it finds nothing once the number has changed hands, so every message the previous owner had in flight at release time stayed `sent` with a null `delivered_at` for ever.

So a status is resolved by `wa_message_id` first, across the workspaces that have ever held that number, and the workspace comes from the message that matched. The lookup is fenced three ways: the candidate set comes from `holders_of`, so an id Meta invented reaches nothing; an empty candidate set matches nothing rather than searching the platform; and no API route can reach the class — it is constructed by the ingestion service and the inbound sweeper, both of which are answering Meta rather than a person.

A status naming no message Wasla sent is ordinary traffic — a template sent from Meta's own console, or a number's history before it was connected — and is acknowledged with no placeholder row.

### Monotonicity

The projection never moves a message backwards, and `failed` is ranked rather than absolute:

```
pending  <  sent  <  failed  <  delivered  <  read
```

`failed` beats `sent`, because a send Meta accepted and then could not deliver did fail. It loses to `delivered` and `read`, because those are reports that the message *arrived* — and the two contradictions the old unconditional `failed` made reachable were a message the customer demonstrably read being downgraded, and a row carrying `failed` beside a non-null `delivered_at`. The report is not discarded either way: `failure_reason` records that Meta said it failed, so the disagreement is visible on the row.

Every timestamp is written once, so a redelivered status moves nothing.

## Manual send idempotency

`POST /conversations/{id}/messages`, `…/messages/template` and `…/messages/media` accept an `Idempotency-Key` header. Repeating a request with the same key returns the original message and does not ask Meta a second time.

**Explicit, and never inferred.** Sending the same words twice is something people legitimately do — "are you there?" twice is two messages — so suppressing a duplicate body within a time window would silently swallow real intent, and a swallowed message is worse than a visible duplicate because nobody can see it happen. The key says which requests are the same request, and nothing else does.

Scoped to the workspace (`UNIQUE(tenant_id, idempotency_key)`), because keys are generated by clients and a global constraint would let one workspace's chosen key suppress another's message. Reusing a key for *different* content answers `409` rather than returning the earlier message, which would tell a caller their new message was sent when it was not.

Nothing else carries a key. A campaign recipient and a follow-up are protected by their own `link`-and-abandon invariant ([ADR-093](../DECISIONS.md)), and an agent turn by the queue's engagement barrier ([ADR-074](../DECISIONS.md)).

## Message origin

`messages.origin` records what produced each line of the transcript: `customer`, `human`, `agent`, `campaign`, `follow_up`, `system`. It exists because inferring it from `sent_by_id` is wrong twice — a campaign carries its creator, so every broadcast read as that person's reply, and a follow-up carries nobody, so every nudge read as an AI reply. Both were recoverable by joining `campaign_recipients` or `follow_ups`, and neither was recoverable by reading the transcript, which is what an auditor, an analytics query and a colleague all actually do.

Set explicitly at every creation site rather than defaulted: a default is what an unlabelled send would silently inherit, and inheriting the wrong attribution is the defect the column removes.

The set is open at the end on purpose. WhatsApp Coexistence lets the Business App originate messages on a number Wasla also holds, and those are neither `human` nor `agent`; a `business_app` member slots in beside these when that work happens. Having the column at all is the part worth doing now — adding a label later is an `ALTER TYPE`.

## Not supported

Stated here so a reader does not infer a capability from a client method:

- **Interactive messaging.** `send_buttons`, `send_list` and `send_location` exist on the client and are called by nothing. Inbound button and list replies are stored as `interactive` messages and their reply ids survive only in the raw event.
- **Outbound chunking.** One logical message is one provider message. A reply over WhatsApp's 4096-character body limit is refused by `MessagingService.send_text` rather than truncated (which puts words in a business's mouth) or split (which reintroduces chunk ordering, partial failure and duplicate chunks). Agents are told the limit in their instructions, which reduces how often the refusal is reached without pretending a token budget can bound a character count.
- **Message edit, delete and revoke.**
- **Reactions as first-class.** A reaction is stored as an `unsupported` message and deliberately does not trigger an agent turn: it has no content to answer, and doing so cost one billed inference and possibly one reply to nothing.
- **Coexistence.** See *Message origin* for the one piece of groundwork that is in place.

## Graph API version

`META_API_VERSION` is `v21.0`, which Meta retires on **2027-01-21** (Graph API changelog, read 2026-09-11). Start-up warns inside the final ninety days, from a table this repository maintains by hand in `app/integrations/whatsapp/versions.py`.

**Maintaining it by hand is the design.** Fetching the changelog at boot would make starting the application depend on a third party's website being up and parseable, which is a worse failure than the one being prevented. So: when a version is added or retired, update `META_API_SUNSETS`. A version the table has never heard of warns too, because silence would make a typo look like an endorsement.

Moving version is planned work rather than a config change: re-verify the send payloads, the media fetch and the error envelope against the new version before deploying it.

## What the webhook does not do

No AI processing, media downloading, or outbound calls. The request resolves the workspace, stores the event, projects it, enqueues and returns. An attachment is noted and its bytes are left where they are: a file arrives as a handle rather than as data, and fetching it takes two round trips to Meta, which is a worker's job ([MEDIA.md](MEDIA.md)).

Two things *are* done here rather than deferred, and both for the same reason — a worker running later would leave a window in which the wrong message goes out. A customer's reply cancels any follow-up waiting on the conversation, and a message that is entirely a stop word opts them out of campaigns ([CAMPAIGNS.md](CAMPAIGNS.md)). Each costs one comparison against work the request is already doing.

## Planned

- Per-workspace access tokens, once there is encryption at rest (phase 14). Until then every outbound call uses the platform credential from configuration ([ADR-009](../DECISIONS.md)).
