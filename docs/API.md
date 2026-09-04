# API

**Status: Implemented** — health, authentication, invitations, WhatsApp accounts, the WhatsApp webhook, conversations, media, the knowledge base, leads, follow-ups, templates, campaigns, usage, analytics, billing and the platform surface. What is absent is absent on purpose and is listed where it would otherwise appear.

Scope: API conventions and the endpoint catalogue. The interactive schema is served by FastAPI's OpenAPI docs.

## Conventions

- Versioned base path `/api/v1`; REST resource naming.
- Request and response bodies are Pydantic schemas. Request schemas reject unknown fields rather than ignoring them.
- Domain exceptions map to stable status codes through one exception handler, so every failure returns the same envelope. Stack traces are never returned.
- The workspace a request acts on comes from the signed access token, never from a path, query or body field. There is no request field a caller could forge to reach another workspace's data.
- A resource belonging to another workspace answers `404`, not `403`: a permission error would confirm the resource exists.
- Collections take a `limit` bound (default 50, maximum 100). Conversations and messages are paged by cursor: the response is `{ "items": [...], "next_cursor": "..." }`, and passing that value back as `?cursor=` returns the rows that follow. A null `next_cursor` means the collection is exhausted — stop on that rather than on a short page. Cursors are opaque and should not be constructed by hand; a malformed one answers `422`. Other collections still return bare arrays.

## Error envelope

| Status | Raised by | Meaning |
| --- | --- | --- |
| 401 | `AuthenticationError` | Missing, malformed or no-longer-valid credentials |
| 403 | `PermissionDeniedError` | Authenticated, but not permitted in this workspace |
| 404 | `NotFoundError`, `TenantIsolationError` | Absent, or outside the caller's workspace |
| 409 | `ConflictError` | Uniqueness or state conflict |
| 422 | `ValidationError` | Rejected by a business rule |
| 429 | `RateLimitedError` | Upstream is rate limiting |
| 502 | `ExternalServiceError` | A provider failed |
| 503 | `DependencyUnavailableError` | A dependency or platform credential is unavailable |

## Health

| Method | Path | Purpose | Status |
| --- | --- | --- | --- |
| GET | `/health` | Aggregate summary, `ok` or `degraded` | Implemented |
| GET | `/health/live` | Liveness, process only, no dependency checks | Implemented |
| GET | `/health/ready` | Readiness, verifies PostgreSQL and Redis | Implemented |

Liveness deliberately does not touch PostgreSQL: a database outage must not make an orchestrator kill healthy processes.

## Authentication

| Method | Path | Purpose | Access |
| --- | --- | --- | --- |
| POST | `/api/v1/auth/register` | Create an account and its first workspace (`201`) | Public |
| POST | `/api/v1/auth/login` | Exchange credentials for tokens | Public |
| POST | `/api/v1/auth/refresh` | Rotate a refresh token | Public |
| POST | `/api/v1/auth/logout` | Revoke a refresh token (`204`) | Public |
| POST | `/api/v1/auth/workspace` | Switch the active workspace | Authenticated |
| GET | `/api/v1/auth/me` | The caller and their memberships | Authenticated |
| POST | `/api/v1/auth/logout-all` | End **every** session this account holds | Authenticated |
| POST | `/api/v1/auth/password` | Change the password, proving the current one, ending every session | Authenticated |
| POST | `/api/v1/auth/password/set` | Choose a **first** password for an account that has none, ending every session | Authenticated |
| POST | `/api/v1/auth/password-reset/request` | Ask for a reset link by email (`202`, always the same body) | Public |
| POST | `/api/v1/auth/password-reset/confirm` | Redeem the emailed token for a new password | Public |
| POST | `/api/v1/auth/email/verification/send` | Mail a six-digit code to **your own** address (`202`, always the same body) | Authenticated |
| POST | `/api/v1/auth/email/verification/verify` | Prove control of your own address | Authenticated |
| POST | `/api/v1/auth/google/authorize` | Begin signing in with Google | Public |
| POST | `/api/v1/auth/google/callback` | Finish signing in with Google | Public |
| POST | `/api/v1/auth/identities/google/authorize` | Begin connecting Google to this account | Authenticated |
| POST | `/api/v1/auth/identities/google/link` | Attach a verified Google account | Authenticated |
| DELETE | `/api/v1/auth/identities/google` | Disconnect Google (`204`) | Authenticated |

`logout` revokes one refresh token; `logout-all` revokes the whole estate by raising
`users.token_version`, which every token is checked against (ADR-036). The calling
session is ended too - exempting it would leave the one an attacker is most likely to
be holding. Both return the account's new state, never a token.

`password` is a *change*, not a reset: the current password is the proof, so nothing has
to be delivered anywhere. The reset beside it is for somebody who *cannot* sign in, and
was unblocked once the repository could send email (ADR-042). See
[SECURITY.md](SECURITY.md).

The two verification routes take **no address and no account id** (ADR-043). `send`
mails the address on the calling account; `verify` accepts a `code` and nothing else -
`{"code": "482731"}`, with spaces and hyphens tolerated. Any extra field is a `422`, so
an attempt to name somebody else is refused rather than ignored.

```
POST /api/v1/auth/email/verification/send    -> 202 {"message": "If that address still needs verifying, a code has been sent to it."}
POST /api/v1/auth/email/verification/verify  -> 200 {"verified_at": "2026-08-27T14:12:03Z"}
```

`send` answers the same sentence whether a code was queued or the address was already
verified. `verify` answers the same `422` for every rejection - wrong, expired,
exhausted, superseded, malformed or never issued - because an error that separates
"expired" from "wrong" tells somebody guessing whether to continue. The code is never
in a response, never in a URL and never in the subject line.

Neither route grants anything. `verify` returns a timestamp, not a credential, and
`GET /auth/me` reports `email_verified_at` so a client can decide whether to prompt.
No route in this document requires a verified address.

### Signing in with Google

Five routes, all answering `404` when `GOOGLE_ENABLED` is off - a feature nobody
configured does not exist in this deployment, which is a different statement from
`503`'s "it is temporarily unwell". See [GOOGLE_OAUTH.md](GOOGLE_OAUTH.md).

`authorize` is a `POST` rather than a `GET` because it writes server state - a
single-use flow record - and a `GET` that writes state is one a link preview will
happily fetch on its own. The callback is a `POST` **from the frontend**, not a
redirect target for Google: this API returns tokens in response bodies, so a `GET`
callback reached by top-level navigation would render a document containing a refresh
token. Google redirects the browser to `GOOGLE_REDIRECT_URI`, a frontend route, which
reads `code` and `state` from its own URL and posts them here.

```
POST /api/v1/auth/google/authorize  -> 200 {"authorization_url": "https://accounts.google.com/o/oauth2/v2/auth?...", "expires_in": 600}
POST /api/v1/auth/google/callback   -> 200 {"access_token": "...", "refresh_token": "...", ...}
```

`callback` answers exactly what `/auth/login` answers, built by the same helper: a
caller cannot tell from the response which method opened the session, and nothing
downstream can either. Errors are `401` for any failed authorization (bad, replayed or
expired state; refused code; forged token; wrong nonce), `403` for a disabled account,
`409` when the verified Google address already has a Wasla account - which must be
linked deliberately rather than claimed (ADR-049) - and `503` when Google or Redis
cannot be reached.

`identities/google/link` returns the connection, never a session:

```
POST /api/v1/auth/identities/google/link -> 200 {"provider": "google", "connected_at": "...", "last_login_at": null}
DELETE /api/v1/auth/identities/google    -> 204
```

Unlinking is `404` when nothing is connected and `403` when it would leave the account
with no way to sign in at all - no password and no other identity.

### The profile a Google login carries

`GET /auth/me` reports `full_name` and `avatar_url`. Both are refreshed from Google on
every Google login, so a renamed or re-photographed account is followed; `avatar_url`
is always an `https` URL or `null`, validated before storage, so a client may render it
directly. An account that has never signed in with Google simply has `null`.

The account's `email` is **not** refreshed - it is written once, at enrolment. See
[GOOGLE_OAUTH.md](GOOGLE_OAUTH.md#profile-data) for why that asymmetry is deliberate.

## Invitations

| Method | Path | Purpose | Access |
| --- | --- | --- | --- |
| POST | `/api/v1/invitations` | Invite someone to the workspace (`201`) | Tenant admin |
| GET | `/api/v1/invitations` | List open invitations | Tenant admin |
| DELETE | `/api/v1/invitations/{invitation_id}` | Revoke an invitation | Tenant admin |
| POST | `/api/v1/invitations/accept` | Redeem an invitation token | Public |
| GET | `/api/v1/workspace/members` | Who is in this workspace (`?include_revoked=true` for the full history) | Workspace member |
| DELETE | `/api/v1/workspace/members/{user_id}` | Withdraw access, or leave by naming yourself | Workspace member (see below) |
| POST | `/api/v1/workspace/members/{user_id}/reinstate` | Readmit somebody who was removed | Tenant admin |

Only the hash of an invitation token is stored, so a database disclosure does not yield usable invitations. **The raw token is not returned by `POST /api/v1/invitations`** and never has been readable from any other route (ADR-057): it travels only in the email queued to the invited address, so an invitation cannot be delivered by a deployment with `EMAIL_ENABLED=false`.

Accepting an invitation for an address that already has an account adds or reinstates the membership and changes nothing about the account — a `password` in the body is ignored. Only the branch that *creates* an account sets one. Somebody who signed up with Google and wants a password uses `POST /api/v1/auth/password/set`.

## WhatsApp accounts

| Method | Path | Purpose | Access |
| --- | --- | --- | --- |
| POST | `/api/v1/whatsapp/accounts` | Connect a WhatsApp number (`201`) | Tenant admin |
| GET | `/api/v1/whatsapp/accounts` | List connected numbers | Workspace member |
| POST | `/api/v1/whatsapp/accounts/{account_id}/disable` | Stop accepting traffic | Tenant admin |
| POST | `/api/v1/whatsapp/accounts/{account_id}/enable` | Resume accepting traffic | Tenant admin |
| POST | `/api/v1/whatsapp/accounts/{account_id}/verify` | Prove control of a number already held (ADR-041) | Tenant admin |
| POST | `/api/v1/whatsapp/accounts/{account_id}/release` | Give the number up, keeping its history | Tenant admin |

`phone_number_id` is unique across the platform, not per workspace: it is how an inbound webhook is attributed to a workspace, so two workspaces claiming one number would make attribution ambiguous.

## Billing checkout

| Method | Path | Purpose | Access |
| --- | --- | --- | --- |
| POST | `/api/v1/billing/subscription` | Choose a **free** plan for a workspace that has none (`201`); a priced plan answers `402` | Workspace **owner** |
| POST | `/api/v1/billing/subscription/plan` | Move to another **free** plan; a priced plan answers `402` | Workspace **owner** |
| POST | `/api/v1/billing/checkout` | Open a hosted payment page for a plan or an outstanding invoice (`201`) | Workspace **owner** |
| GET | `/api/v1/billing/payments/{id}` | Where one payment attempt has got to | Workspace **owner** |
| POST | `/api/v1/billing/payments/{id}/refund` | Give back what is left of a payment (`202`) | Workspace **owner** |
| POST | `/api/v1/webhooks/paymob` | Receive a payment provider callback | Public, HMAC-verified |

```
POST /api/v1/billing/checkout   {"plan_code": "pro"}
  -> 201 {"redirect_url": "https://eg.checkout.paymob.com/?publicKey=...&clientSecret=...",
          "payment_id": "...", "invoice_id": "...", "amount": "99.00", "currency": "EGP"}
```

The request names **either a `plan_code` or an `invoice_id`**, and exactly one
of them — naming a plan is choosing what to buy, naming an invoice is paying a
renewal the sweep already issued. Amount, currency and workspace come from the
database and the access token; the schema forbids extra fields, so sending
`amount` is a `422` rather than a value quietly ignored. Redirect the customer
to `redirect_url`.

An optional `idempotency_key` makes a retried request safe. A repeat is `409`
rather than a replay: the response carries a one-use URL that is deliberately
never stored, so read the payment's status instead of starting another.

**A customer returning to your site is not a payment.** Paymob redirects them
back with the transaction in the query string; use it to show a success,
pending or failure page and nothing else. Poll
`GET /billing/payments/{payment_id}` for the real state, which changes only
when the signed callback arrives. There is deliberately no endpoint that
accepts the redirect as proof.

```
GET /api/v1/billing/payments/{payment_id}
  -> 200 {"status": "pending" | "succeeded" | "failed" | "refunded",
          "amount": "99.00", "currency": "EGP", "invoice_id": "...",
          "refunded_amount": "0.00", "refund_pending": false,
          "failure_reason": null, "processed_at": null}
```

`pending` is an answer, not a missing one — 3-D Secure and several local
methods finish after the customer has already been sent back, so treating it
as failure tells people their payment did not work while it is still working.
Keep polling; the callback is what resolves it.

```
POST /api/v1/billing/payments/{payment_id}/refund   {"reason": "..."}
  -> 202 {"status": "succeeded", "refund_pending": true,
          "refunded_amount": "0.00", ...}
```

**202, and the status still says `succeeded`.** This records that the provider
accepted the reversal; the money moves later and is confirmed by a callback,
exactly as a payment is. There is no amount in the request — it is the
payment's own unreturned balance, so no client can ask for more back than was
paid. Render `refund_pending`, never "refunded", until `refunded_at` is set.

The webhook is unauthenticated by necessity and answers `200 {"status":
"received"}` to everything it verified — applied, duplicate, unmatched or
mismatched alike, because a reply that distinguished them would confirm which
payment references exist. An unverified request is `403`; a deployment with no
provider configured is `503`, so the provider retries rather than believing a
payment was recorded.

## Webhook

| Method | Path | Purpose | Access |
| --- | --- | --- | --- |
| GET | `/api/v1/webhooks/whatsapp` | Subscription verification challenge | Public |
| POST | `/api/v1/webhooks/whatsapp` | Message and status events | Signature |
| POST | `/api/v1/webhooks/email` | Resend delivery, bounce and complaint events | Signature |

The POST always answers `200` unless the signature check fails, which answers `403`. Meta retries anything else, and retrying a payload we could not understand would not help. See [WHATSAPP.md](WHATSAPP.md).

## Conversations

All conversation routes are available to any member of the workspace. Restricting them to admins would exclude the people who actually staff an inbox.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/v1/conversations` | Open conversations, most recently active first; `?priority=` narrows |
| GET | `/api/v1/conversations/{conversation_id}` | One conversation |
| GET | `/api/v1/conversations/{conversation_id}/messages` | Messages, most recent first |
| POST | `/api/v1/conversations/{conversation_id}/messages` | Send free text (`201`) |
| POST | `/api/v1/conversations/{conversation_id}/messages/template` | Send an approved template (`201`) |
| POST | `/api/v1/conversations/{conversation_id}/messages/media` | Send an attachment, multipart (`201`) |
| GET | `/api/v1/conversations/{conversation_id}/media/{media_id}` | Download a stored attachment |
| POST | `/api/v1/conversations/{conversation_id}/mode` | Switch between AI and human handling |
| POST | `/api/v1/conversations/{conversation_id}/priority` | Set priority by hand |
| POST | `/api/v1/conversations/{conversation_id}/assignment` | Assign, or clear the assignment |
| POST | `/api/v1/conversations/{conversation_id}/close` | Close the conversation |
| POST | `/api/v1/conversations/{conversation_id}/reopen` | Reopen it |

Four behaviours worth knowing before integrating:

- **Free text is only accepted inside the 24-hour service window.** Outside it, Meta accepts approved templates only, so the free-text route answers `422` and the template route still works. Every conversation read includes `service_window_open`, so a client can disable its composer instead of discovering the rule by failing a send.
- **A rejected send answers `201`, with the message in `failed` state.** The message row is written before Meta is called, and a rejection is recorded on that row. Raising instead would roll the request back and destroy the only evidence the attempt was made. A missing platform credential does raise `503`, because nothing was attempted. Callers should read `status` rather than relying on the response code.

- **A template message has no `body`.** It carries `template_name` and `template_language` instead, and `kind` is `template`. Meta renders the wording from its own approved copy, so Wasla has no text to return; a client should render the template it identifies rather than expecting the words the customer saw. Both fields are null on every other kind.

- **Priority is raised automatically and lowered only by a person.** Every customer message is classified before an agent answers it; a negative or angry reading raises `priority` and may hand the conversation to a human. Nothing lowers it again, because a conversation quietly demoted out of somebody's queue is one nobody looks at. `POST .../priority` is the way back. The read model carries `sentiment`, `sentiment_score`, `intent` and `intent_confidence` so a client can show why a conversation is flagged. See [SENTIMENT.md](SENTIMENT.md).

Conversations carry identifiers rather than embedded contact objects. The models declare no ORM relationships deliberately: a lazy load inside an async request is blocking I/O that only becomes visible under load.

## Knowledge base

| Method | Path | Who |
| --- | --- | --- |
| GET | `/api/v1/knowledge/bases` | Any workspace member |
| POST | `/api/v1/knowledge/bases` | Admin or owner |
| GET | `/api/v1/knowledge/bases/{knowledge_base_id}` | Any workspace member |
| GET | `/api/v1/knowledge/bases/{knowledge_base_id}/documents` | Any workspace member |
| POST | `/api/v1/knowledge/bases/{knowledge_base_id}/documents` | Admin or owner (`202`) |
| GET | `/api/v1/knowledge/documents/{document_id}` | Any workspace member |
| POST | `/api/v1/knowledge/documents/{document_id}/ingest` | Admin or owner (`202`) |
| DELETE | `/api/v1/knowledge/documents/{document_id}` | Admin or owner (`204`) |

Reading is open to any member — the people staffing an inbox need to see what their agents can answer from. Writing is administrators only, because a document added here is something the AI will state to customers as fact.

Three behaviours worth knowing:

- **Submitting a document answers `202`, not `201`.** The document exists but is not yet retrievable; ingestion runs in the background. Poll `GET /knowledge/documents/{id}` until `status` is `ready`. A `failed` document carries the reason in `error`, and `POST .../ingest` queues it again once the cause is fixed.
- **Submitting the same text twice is not an error.** It returns the existing document with `created: false`, keyed on a hash of the extracted text. The upload was recognised, not duplicated.
- **A document read never returns the extracted text.** It reports what was ingested — status, chunk count, size, failure reason. The API is not a document store.

## Leads

**Status: Implemented.** Full behaviour in [CRM.md](CRM.md).

| Method | Path | Role |
| --- | --- | --- |
| GET | `/api/v1/leads` | Any workspace member |
| POST | `/api/v1/leads` | Any workspace member (`201`) |
| GET | `/api/v1/leads/statistics` | Admin or owner |
| GET | `/api/v1/leads/{lead_id}` | Any workspace member |
| PATCH | `/api/v1/leads/{lead_id}` | Any workspace member |
| POST | `/api/v1/leads/{lead_id}/status` | Any workspace member |
| POST | `/api/v1/leads/{lead_id}/assignment` | Admin or owner |
| POST | `/api/v1/leads/{lead_id}/score` | Any workspace member |
| GET | `/api/v1/leads/{lead_id}/notes` | Any workspace member |
| POST | `/api/v1/leads/{lead_id}/notes` | Any workspace member (`201`) |
| GET | `/api/v1/leads/{lead_id}/activity` | Any workspace member |

Working a pipeline is open to any member. Assignment and the workspace-wide statistics view require an administrator: handing someone a deal and reading across every rep's pipeline are management actions. That is a different line from the one on conversations, where any member may assign, because grabbing an unanswered conversation is triage.

Listing filters by `status`, `source`, `assigned_to_id`, `unassigned`, `tag`, `search`, `contact_id` and `conversation_id`. `status`, `source` and `tag` repeat for multiple values. Filters intersect. Paged by cursor.

Four behaviours worth knowing:

- **`PATCH` distinguishes an omitted field from an explicit null.** Omitting a field leaves it alone; sending `null` clears it. Anything touched becomes human-verified and is then protected from AI extraction — including a field you deliberately cleared.
- **Creating a second lead for a customer who already has an open one answers `409`.** One opportunity, one record; close the first or update it. A customer whose lead is `won` or `lost` can start a new one.
- **An illegal status move answers `422`.** The permitted transitions are a graph, not a free-for-all — see [CRM.md](CRM.md). Setting the status a lead already has succeeds and changes nothing, so a retry is safe.
- **`human_verified_fields` is returned on every lead.** It names the fields an agent will not overwrite, so an interface can show which values are pinned.

## Follow-ups

**Status: Implemented.** Full behaviour in [CRM.md](CRM.md).

| Method | Path | Role |
| --- | --- | --- |
| GET | `/api/v1/follow-ups` | Any workspace member |
| POST | `/api/v1/follow-ups` | Any workspace member (`201`) |
| GET | `/api/v1/follow-ups/{follow_up_id}` | Any workspace member |
| POST | `/api/v1/follow-ups/{follow_up_id}/cancel` | Any workspace member |

Open to any member on purpose: a follow-up is a message to a customer someone is already handling, and requiring an administrator to stop one would mean the person watching the conversation cannot cancel a nudge they can see has become wrong.

Listing filters by `status` (repeatable), `conversation_id` and `lead_id`, paged by cursor.

Four behaviours worth knowing:

- **Supply exactly one of `delay_minutes` or `scheduled_at`.** Both, or neither, answers `422`. Accepting both would leave the server silently choosing.
- **Supply a `body`, a template, or both.** A template needs both `template_name` and `template_language`; half a template answers `422`.
- **Scheduling a second follow-up on one conversation replaces the first.** It answers `201` either way, and there is no route that edits one in place.
- **Cancelling one already sent or cancelled succeeds and changes nothing.** Losing that race is not the caller's mistake.

There is no endpoint that sends a follow-up now. Sending is the worker's job, and "send this immediately" is just a message — use `POST /conversations/{id}/messages`.

## Templates

**Status: Implemented.** Full behaviour in [CAMPAIGNS.md](CAMPAIGNS.md).

| Method | Path | Role |
| --- | --- | --- |
| GET | `/api/v1/templates` | Any workspace member |
| GET | `/api/v1/templates/{template_id}` | Any workspace member |
| POST | `/api/v1/templates/sync?account_id=...` | Owner or admin |

Listing filters by `account_id`, `status` and `category`. Not cursor-paged, unlike conversations or leads: a WhatsApp Business account holds tens of templates, and Meta caps how many a business may have.

- **There is no route that creates, edits or deletes a template.** Approval belongs to Meta; one written here would be a local fiction that fails at send time.
- **Sync is synchronous and returns what changed** — `created`, `updated`, `withdrawn`. A workspace approves a template and then wants to use it, so an answer it can see beats a job it must wait for.
- **A template that has vanished from Meta becomes `disabled`, not deleted.** A campaign may reference it, and its `rejection_reason` is where the workspace reads why it stopped working.
- **Only `approved` may be sent.** A status Meta introduces later lands on `unknown`, which is not sendable.

## Campaigns

**Status: Implemented.** Full behaviour in [CAMPAIGNS.md](CAMPAIGNS.md).

| Method | Path | Role |
| --- | --- | --- |
| GET | `/api/v1/campaigns` | Any workspace member |
| POST | `/api/v1/campaigns` | Owner or admin (`201`) |
| POST | `/api/v1/campaigns/audience/preview` | Owner or admin |
| GET | `/api/v1/campaigns/{campaign_id}` | Any workspace member |
| POST | `/api/v1/campaigns/{campaign_id}/audience` | Owner or admin |
| POST | `/api/v1/campaigns/{campaign_id}/schedule` | Owner or admin |
| POST | `/api/v1/campaigns/{campaign_id}/pause` | Owner or admin |
| POST | `/api/v1/campaigns/{campaign_id}/cancel` | Owner or admin |
| GET | `/api/v1/campaigns/{campaign_id}/statistics` | Any workspace member |
| GET | `/api/v1/campaigns/{campaign_id}/recipients` | Any workspace member |

Reading is ordinary inbox work. Composing, targeting and starting take an administrator: a campaign writes to thousands of customers at once and is the least reversible thing the platform does.

Six behaviours worth knowing:

- **There is no `body` field, and no way to name a phone number.** A campaign sends an approved template to contacts who already have a conversation on the sending number. Both absences are the compliance boundary — see [ADR-025](../DECISIONS.md).
- **`variables` must match the template's own count.** A mismatch answers `422` at composition rather than failing at Meta ten thousand times.
- **An audience can only be set on a draft.** Rebuilding a part-sent list would duplicate some people and drop others.
- **Scheduling with no `scheduled_at` means now**, and still returns immediately: the worker sends, so no request is held open behind a broadcast.
- **`pause` and `cancel` are different.** Paused is resumable — schedule again. Cancelled is final, and what was sent stays sent.
- **Lifecycle changes are named transitions.** There is no `PATCH` that sets `status`.

## Contacts

**Status: Implemented.** Only the marketing opt-out; a contact is created by the webhook from what Meta reports and has nothing else a person edits.

| Method | Path | Role |
| --- | --- | --- |
| POST | `/api/v1/contacts/{contact_id}/opt-out` | Any workspace member |
| DELETE | `/api/v1/contacts/{contact_id}/opt-out` | Owner or admin |

Recording is any member's to do — the person handling the conversation is the one a customer says "stop" to. Clearing takes an administrator, because undoing somebody's own refusal should be deliberate.

- **Recording is idempotent and never moves the timestamp.** The first refusal is the one that counts.
- **A customer whose whole message is a stop word is opted out automatically**, on the inbound path. It does not silence the agent.

## Usage

**Status: Implemented.** What the workspace consumed, for a window.

| Method | Path | Role |
| --- | --- | --- |
| GET | `/api/v1/usage` | Owner or admin |
| GET | `/api/v1/usage/daily` | Owner or admin |

Administrators only, unlike analytics: usage is the input to a bill and to a plan limit, so it sits with billing rather than with the inbox.

- **Both take an optional `since` and `until`**, UTC, half-open `[since, until)`. Given neither, the last thirty days. A window longer than 366 days is refused.
- **The response carries the window it applied**, because a figure without its period is not quotable.
- **`counters`** are the named meters a plan limit is written against, every one present even at zero; **`totals`** is the unabridged list, so a meter added later is visible before anything is renamed to carry it.
- **`/daily` is sparse**: a day on which nothing happened has no point. Filling zeros is the client's job, because only the client knows whether it is drawing bars or a cumulative line. Narrow it with repeated `event_type=`.

## Analytics

**Status: Implemented.** How the workspace is doing, for a window.

| Method | Path | Role |
| --- | --- | --- |
| GET | `/api/v1/analytics` | Any workspace member |
| GET | `/api/v1/analytics/conversations/{conversation_id}/events` | Any workspace member |

Open to every member, unlike usage: these are the numbers that tell the people staffing an inbox how the inbox is doing.

- **Rates arrive beside the counts they came from**, never instead of them. A rate alone cannot be checked and hides the difference between nine of ten and nine hundred of a thousand.
- **`average_response_seconds` is null, not zero, when nothing was answered.** Zero would read as instant service. `unanswered` counts the customers still waiting.
- **Every lead status and sentiment label is named even at zero**, so a dashboard renders the column without knowing the vocabulary.
- **`handoffs_by_source`** splits handoffs into the agent asking, a reading escalating, and a colleague taking over — the same total with very different meanings.
- **A conversation's events answer "why did this end up with a person"**, newest first. Another workspace's id answers `404`.

## Planned tenant endpoints

`/api/v1/billing`.

## Platform

**Status: Implemented** — cross-workspace reporting, invoice administration (recording a payment, voiding an invoice), account enable/disable and the audit-log view. Every read here writes an entry of its own (ADR-095).

| Method | Path | Role |
| --- | --- | --- |
| GET | `/api/v1/platform/overview` | Platform owner or admin |
| GET | `/api/v1/platform/tenants` | Platform owner or admin |
| POST | `/api/v1/platform/users/{user_id}/disable` | Platform owner or admin |
| POST | `/api/v1/platform/users/{user_id}/enable` | Platform owner or admin |
| POST | `/api/v1/platform/invoices/{invoice_id}/payments` | Platform owner or admin |
| POST | `/api/v1/platform/invoices/{invoice_id}/void` | Platform owner or admin |
| GET | `/api/v1/platform/audit-logs` | Platform owner or admin |

**Disabling an account is a platform action, not a workspace one.** An account is a
global identity, so a tenant administrator able to suspend one could evict somebody from
workspaces that administrator has nothing to do with. Removing a person from a single
workspace is a different operation against a different object, and does not exist yet.
`enable` raises the token version as well as restoring the account, so tokens issued
before a suspension do not come back with it.

### Removing a member

`DELETE /workspace/members/{user_id}` is guarded by the workspace dependency rather than by the admin one, and that is deliberate: leaving needs no permission, so a member must be able to call it on themselves. A dependency is evaluated before the path parameter is bound, so it cannot tell "remove my colleague" from "leave" — the role rules therefore live in the service, where the target is known (ADR-038).

| Case | Answer |
| --- | --- |
| Removing yourself | Allowed at any role |
| Removing somebody else | Owner or admin only, otherwise `403` |
| An admin removing an owner | `403` — otherwise an administrator promotes themselves by subtraction |
| Removing the last active owner, including yourself | `409` — a workspace with no owner has nobody who can invite one |
| Somebody already removed | `409`, so a caller looking at a stale roster sees it refreshed |
| Somebody who is not a member here | `422`, saying nothing about whether they exist elsewhere |

Revocation takes effect on the **next request**: authorization loads the membership every time rather than trusting the token. It does not touch the person's account, their sessions, or their other workspaces — being removed from one company is not a reason to be signed out of another.

Readmission reuses the same membership row, so the removal and the return are both visible on one record, and it counts against the plan's seat limit exactly as an invitation does.

### Connecting a WhatsApp number

`POST /whatsapp/accounts` requires an `access_token` and verifies it against Meta for the `phone_number_id` being claimed before writing anything (ADR-037). `waba_id` is optional and checked rather than trusted; `display_phone_number` is no longer accepted, because it comes back from Meta. See [WHATSAPP.md](WHATSAPP.md) for the failure table.

Platform authority is a property of the user, not of a membership. Owning a workspace grants nothing here, and holding a platform role grants nothing inside a workspace — a platform administrator reading these figures still cannot open a customer's inbox.

- **Almost read-only on purpose.** The three writes here — recording a payment, voiding an invoice, enabling or disabling an account — are each audited. Suspending or deleting a *workspace* is still absent, no longer for want of an audit trail but because the product has no answer for what happens to a suspended workspace's in-flight conversations.
- **No revenue figures**, and none until there are subscriptions to compute them from. A plausible zero is worse than an absent field.
- **`/tenants` uses offset paging**, unlike the cursors elsewhere: the list is sorted by name and searched by hand, so an operator wants page three of forty results rather than a stable feed. `total` is the number matching the filter. `search` matches name or address and is escaped, so `%` finds a workspace called "100%" rather than everything.
- **Each row carries the same counters that workspace sees on its own `/usage`**, so an operator and a customer quote the same number.

## Planned platform endpoints

`/api/v1/platform/tenants/{tenant_id}`, `/api/v1/platform/billing`, `/api/v1/platform/plans`, `/api/v1/platform/audit-logs`, `/api/v1/platform/system-health`.
