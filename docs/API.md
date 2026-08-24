# API

**Status: In Progress** — health, authentication, invitations, WhatsApp accounts, the WhatsApp webhook, conversations, media, the knowledge base, leads, follow-ups, templates, campaigns, usage and analytics are Implemented. Billing and the platform surface are Planned.

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
| POST | `/api/v1/auth/password` | Change the password, ending every session | Authenticated |
| POST | `/api/v1/auth/password-reset/request` | Ask for a reset link by email (`202`, always the same body) | Public |
| POST | `/api/v1/auth/password-reset/confirm` | Redeem the emailed token for a new password | Public |

`logout` revokes one refresh token; `logout-all` revokes the whole estate by raising
`users.token_version`, which every token is checked against (ADR-036). The calling
session is ended too - exempting it would leave the one an attacker is most likely to
be holding. Both return the account's new state, never a token.

`password` is a *change*, not a reset: the current password is the proof, so nothing has
to be delivered anywhere. **There is no password reset**, deliberately - it needs a token
sent to an address the caller controls, and this deployment cannot send email. See
[SECURITY.md](SECURITY.md).

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

Only the hash of an invitation token is stored, so a database disclosure does not yield usable invitations.

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

**Status: In Progress** — the read-only reporting surface is Implemented; administration and billing are Planned.

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

- **Read-only on purpose.** Suspending or deleting a workspace is exactly the action that must be audit-logged, and there is no audit log until phase 14.
- **No revenue figures**, and none until there are subscriptions to compute them from. A plausible zero is worse than an absent field.
- **`/tenants` uses offset paging**, unlike the cursors elsewhere: the list is sorted by name and searched by hand, so an operator wants page three of forty results rather than a stable feed. `total` is the number matching the filter. `search` matches name or address and is escaped, so `%` finds a workspace called "100%" rather than everything.
- **Each row carries the same counters that workspace sees on its own `/usage`**, so an operator and a customer quote the same number.

## Planned platform endpoints

`/api/v1/platform/tenants/{tenant_id}`, `/api/v1/platform/billing`, `/api/v1/platform/plans`, `/api/v1/platform/audit-logs`, `/api/v1/platform/system-health`.
