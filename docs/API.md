# API

**Status: In Progress** — only foundation health endpoints are being built (Phase 0). All business endpoints are Planned.

Scope: API conventions and the endpoint catalogue. Interactive schema is served by FastAPI's OpenAPI docs.

## Conventions

- Versioned base path `/api/v1`; REST resource naming.
- Consistent response envelopes and error payloads; validated Pydantic request and response schemas.
- Pagination on all collections, with cursor pagination where datasets grow large; filtering and sorting where useful.
- Errors map from domain exceptions to stable HTTP status codes; stack traces are never returned in production.
- Tenant-scoped endpoints execute in the active workspace context; platform endpoints are separately authorised.

## Foundation endpoints

| Method | Path | Purpose | Status |
| --- | --- | --- | --- |
| GET | `/health` | Aggregate health summary | In Progress |
| GET | `/health/live` | Liveness, process only, no dependency checks | In Progress |
| GET | `/health/ready` | Readiness, verifies PostgreSQL and Redis | In Progress |

## Planned tenant endpoints

`/api/v1/auth`, `/api/v1/tenants`, `/api/v1/users`, `/api/v1/whatsapp`, `/api/v1/agents`, `/api/v1/conversations`, `/api/v1/messages`, `/api/v1/contacts`, `/api/v1/leads`, `/api/v1/knowledge`, `/api/v1/follow-ups`, `/api/v1/campaigns`, `/api/v1/analytics`, `/api/v1/usage`, `/api/v1/billing`.

## Planned platform endpoints

`/api/v1/platform/tenants`, `/api/v1/platform/tenants/{tenant_id}`, `/api/v1/platform/usage`, `/api/v1/platform/analytics`, `/api/v1/platform/billing`, `/api/v1/platform/plans`, `/api/v1/platform/audit-logs`, `/api/v1/platform/system-health`.

## Planned webhooks

`GET /webhooks/whatsapp` for subscription verification and `POST /webhooks/whatsapp` for message and status events; see [WHATSAPP.md](WHATSAPP.md).
