# API

**Status: In Progress** — health, authentication, invitations, WhatsApp accounts, the WhatsApp webhook and conversations are Implemented. Everything else is Planned.

Scope: API conventions and the endpoint catalogue. The interactive schema is served by FastAPI's OpenAPI docs.

## Conventions

- Versioned base path `/api/v1`; REST resource naming.
- Request and response bodies are Pydantic schemas. Request schemas reject unknown fields rather than ignoring them.
- Domain exceptions map to stable status codes through one exception handler, so every failure returns the same envelope. Stack traces are never returned.
- The workspace a request acts on comes from the signed access token, never from a path, query or body field. There is no request field a caller could forge to reach another workspace's data.
- A resource belonging to another workspace answers `404`, not `403`: a permission error would confirm the resource exists.
- Collections take a `limit` bound (default 50, maximum 100). Cursor pagination is Planned.

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

## Invitations

| Method | Path | Purpose | Access |
| --- | --- | --- | --- |
| POST | `/api/v1/invitations` | Invite someone to the workspace (`201`) | Tenant admin |
| GET | `/api/v1/invitations` | List open invitations | Tenant admin |
| DELETE | `/api/v1/invitations/{invitation_id}` | Revoke an invitation | Tenant admin |
| POST | `/api/v1/invitations/accept` | Redeem an invitation token | Public |

Only the hash of an invitation token is stored, so a database disclosure does not yield usable invitations.

## WhatsApp accounts

| Method | Path | Purpose | Access |
| --- | --- | --- | --- |
| POST | `/api/v1/whatsapp/accounts` | Connect a WhatsApp number (`201`) | Tenant admin |
| GET | `/api/v1/whatsapp/accounts` | List connected numbers | Workspace member |
| POST | `/api/v1/whatsapp/accounts/{account_id}/disable` | Stop accepting traffic | Tenant admin |
| POST | `/api/v1/whatsapp/accounts/{account_id}/enable` | Resume accepting traffic | Tenant admin |

`phone_number_id` is unique across the platform, not per workspace: it is how an inbound webhook is attributed to a workspace, so two workspaces claiming one number would make attribution ambiguous.

## Webhook

| Method | Path | Purpose | Access |
| --- | --- | --- | --- |
| GET | `/api/v1/webhooks/whatsapp` | Subscription verification challenge | Public |
| POST | `/api/v1/webhooks/whatsapp` | Message and status events | Signature |

The POST always answers `200` unless the signature check fails, which answers `403`. Meta retries anything else, and retrying a payload we could not understand would not help. See [WHATSAPP.md](WHATSAPP.md).

## Conversations

All conversation routes are available to any member of the workspace. Restricting them to admins would exclude the people who actually staff an inbox.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/v1/conversations` | Open conversations, most recently active first |
| GET | `/api/v1/conversations/{conversation_id}` | One conversation |
| GET | `/api/v1/conversations/{conversation_id}/messages` | Messages, most recent first |
| POST | `/api/v1/conversations/{conversation_id}/messages` | Send free text (`201`) |
| POST | `/api/v1/conversations/{conversation_id}/messages/template` | Send an approved template (`201`) |
| POST | `/api/v1/conversations/{conversation_id}/mode` | Switch between AI and human handling |
| POST | `/api/v1/conversations/{conversation_id}/assignment` | Assign, or clear the assignment |
| POST | `/api/v1/conversations/{conversation_id}/close` | Close the conversation |
| POST | `/api/v1/conversations/{conversation_id}/reopen` | Reopen it |

Two behaviours worth knowing before integrating:

- **Free text is only accepted inside the 24-hour service window.** Outside it, Meta accepts approved templates only, so the free-text route answers `422` and the template route still works. Every conversation read includes `service_window_open`, so a client can disable its composer instead of discovering the rule by failing a send.
- **A rejected send answers `201`, with the message in `failed` state.** The message row is written before Meta is called, and a rejection is recorded on that row. Raising instead would roll the request back and destroy the only evidence the attempt was made. A missing platform credential does raise `503`, because nothing was attempted. Callers should read `status` rather than relying on the response code.

Conversations carry identifiers rather than embedded contact objects. The models declare no ORM relationships deliberately: a lazy load inside an async request is blocking I/O that only becomes visible under load.

## Planned tenant endpoints

`/api/v1/contacts`, `/api/v1/agents`, `/api/v1/leads`, `/api/v1/knowledge`, `/api/v1/follow-ups`, `/api/v1/campaigns`, `/api/v1/analytics`, `/api/v1/usage`, `/api/v1/billing`.

## Planned platform endpoints

`/api/v1/platform/tenants`, `/api/v1/platform/tenants/{tenant_id}`, `/api/v1/platform/usage`, `/api/v1/platform/analytics`, `/api/v1/platform/billing`, `/api/v1/platform/plans`, `/api/v1/platform/audit-logs`, `/api/v1/platform/system-health`.
