# Wasla Architecture

Technical source of truth for the current system architecture. Every section carries an explicit status. Sections marked **Planned** describe the intended design only; no code exists for them yet.

| Legend | Meaning |
| --- | --- |
| Implemented | Exists in the repository and is exercised by tests |
| In Progress | Partially present, actively being built |
| Planned | Designed, not yet built |
| Blocked | Cannot proceed until a dependency is resolved |

## 1. System overview

**Status: Planned**

Wasla is an API-first, multi-tenant backend. A business (tenant) connects one or more WhatsApp Business phone numbers. Inbound customer messages arrive as Meta webhooks, are resolved to a tenant, persisted, and queued for asynchronous AI processing. An agent orchestrator loads the conversation, retrieves tenant-scoped knowledge, calls the OpenAI Responses API with a controlled tool set, and replies through the WhatsApp Cloud API.

```
WhatsApp
    |
Webhook (fast, idempotent)
    |
Tenant Resolver (phone_number_id -> tenant)
    |
Message Service (persist + update conversation)
    |
Redis Queue
    |
Agent Orchestrator
    |-- Conversation memory
    |-- RAG (pgvector, tenant-filtered)
    |-- Business tools
    +-- OpenAI Responses API
    |
WhatsApp Client (outbound)
    |
Persistence + Usage + Analytics
```

## 2. Project structure

**Status: In Progress** — the Phase 0 skeleton exists; directories marked *(planned)* are created by the phase that needs them.

```
wasla/
|-- app/
|   |-- main.py              application factory
|   |-- core/                config, logging, exceptions, middleware, redis, DI
|   |-- db/                  declarative base, mixins, async session, models
|   |-- repositories/        data access, tenant-scoped (models from Phase 1)
|   |-- schemas/             Pydantic request/response contracts
|   |-- services/            business logic / use cases
|   |-- api/                 health router
|   |   +-- v1/              versioned business routers
|   |-- integrations/        whatsapp/, openai/               (planned)
|   |-- agents/              agent definitions, orchestrator  (planned)
|   |-- workers/             background job consumers         (planned)
|   +-- platform/            SaaS owner administration layer  (planned)
|-- alembic/                 migrations
|-- tests/                   unit, integration, e2e
|-- nginx/                   reverse proxy example
|-- scripts/                 container entrypoint
+-- .github/workflows/       CI pipeline
```

## 3. Application layers

**Status: In Progress** — the layering and dependency injection are established and exercised by the health subsystem; repositories and models arrive in Phase 1.

| Layer | Responsibility | Rule |
| --- | --- | --- |
| API routers | HTTP contract, validation, auth dependencies | Thin, no business logic |
| Services / use cases | Business logic, orchestration, transactions | No raw HTTP, no direct SQL |
| Repositories | Data access via SQLAlchemy | Always tenant-scoped |
| Integrations | External providers (Meta, OpenAI) behind interfaces | No provider calls elsewhere |
| Models | SQLAlchemy 2.0 declarative entities | Own no business rules |

Dependencies point inwards: API -> services -> repositories/integrations -> models. Dependency injection is used for sessions, clients, and settings so the orchestrator is testable without FastAPI, Meta, or OpenAI. Infrastructure is created once per process in the application lifespan, stored on application state, and injected as typed dependencies; the health service receives its probes as injected callables, which is why the endpoint tests need no real database or cache.

## 4. Request flow

**Status: Implemented** — middleware, routing, and centralised error handling exist and are tested. The auth and tenant dependencies are Planned (Phase 2).

`Request -> middleware (request_id, log context, timing) -> router -> auth/tenant dependency (planned) -> service -> repository -> PostgreSQL -> response schema -> structured access log`

Every request carries a request ID, taken from the configured header or generated, bound to the log context, and returned on the response. Errors raise domain exceptions that centralised handlers map to a stable envelope:

```json
{ "error": { "code": "not_found", "message": "...", "request_id": "..." } }
```

Stack traces are never exposed in production responses. Cross-tenant access is reported as `not_found` so error codes cannot be used to probe another tenant's data.

## 5. WhatsApp webhook flow

**Status: Planned**

1. `GET /webhooks/whatsapp` verifies the Meta challenge token.
2. `POST /webhooks/whatsapp` validates the `X-Hub-Signature-256` payload signature.
3. Payload is parsed; `phone_number_id` resolves the WhatsApp account and therefore the tenant. The tenant is never inferred from the customer phone number.
4. Message and status events are persisted idempotently, keyed on the WhatsApp message/event ID.
5. The conversation is created or updated; work is enqueued to Redis; the endpoint returns immediately.

No AI or media processing happens inside the webhook request.

## 6. AI agent flow

**Status: Planned**

`Load tenant -> load conversation -> check mode (HUMAN stops AI) -> select agent -> load agent config -> build token-aware memory -> retrieve knowledge -> expose allowed tools -> OpenAI Responses API -> execute validated tool calls -> final response -> send + persist -> record usage`

## 7. RAG flow

**Status: Planned** — the `vector` extension is already enabled by migration `0001`.

`Question -> embedding -> tenant-filtered pgvector search -> top chunks -> agent context -> Responses API -> answer`. Ingestion: `upload -> validate -> extract -> chunk -> embed -> store -> index`. Cross-tenant retrieval is structurally prevented by mandatory `tenant_id` filters.

## 8. Human handoff flow

**Status: Planned**

Every conversation has a mode: `AI` or `HUMAN`. Handoff is triggered by explicit customer request, low confidence, negative or angry sentiment, sensitive requests, tool failure, or an agent rule. In `HUMAN` mode automatic AI replies stop, ownership and reason are tracked, and the conversation can be assigned to a team member. Resuming returns the conversation to `AI`.

## 9. CRM / lead flow

**Status: Planned**

Conversations produce contacts and leads. Agents create or update leads through validated, tenant-scoped tools. Lead statuses: `NEW`, `CONTACTED`, `QUALIFIED`, `PROPOSAL`, `WON`, `LOST`. Follow-ups are scheduled, cancellable, and respect the WhatsApp 24-hour service window and template rules.

## 10. Background jobs and Redis usage

**Status: Planned** — the Redis client and its health probe are Implemented; queues and workers are not.

Redis provides job queues, caching, rate limiting, follow-up scheduling, and temporary state. Workers handle AI processing, media processing, document ingestion and embeddings, follow-ups, campaigns, and usage aggregation. All jobs are idempotent and support retry with an error/dead-letter strategy.

## 11. Database architecture

**Status: In Progress** — engine, session scope, declarative base, shared mixins, and migration tooling are Implemented; domain models and their indexes arrive in Phase 1 onwards.

PostgreSQL with SQLAlchemy 2.0 async sessions and Alembic migrations. The declarative base fixes an explicit constraint naming convention so autogenerated migrations stay stable and reviewable. Shared mixins provide UUID primary keys, `created_at`/`updated_at` timestamps, optional soft deletion, and the tenant foreign key plus index for tenant-owned tables. Migration `0001` enables the `pgcrypto` and `vector` extensions so every environment is provisioned identically.

Sessions are request-scoped and commit on success or roll back on failure. Connections use pre-ping, bounded pooling, recycling, and an explicit connect timeout.

Indexes are planned on `tenant_id`, conversation `(tenant_id, status)`, message `(conversation_id, created_at)`, contact `(tenant_id, phone)`, lead `(tenant_id, status)`, WhatsApp `phone_number_id`, usage and analytics `(tenant_id, created_at)`, and document `tenant_id`.

## 12. Multi-tenancy

**Status: Planned** — the tenant-scoped model mixin exists; enforcement arrives with the models and repositories in Phase 1.

Shared PostgreSQL infrastructure with `tenant_id` isolation (see ADR-001). Users are global identities; the authoritative link to a company is `User -> Membership -> Tenant` (see ADR-002). Roles are scoped to the membership, never to the user. A request executes in exactly one active workspace, and a client-supplied `tenant_id` is only honoured after membership verification. Tenant isolation is enforced in repositories and services and is explicitly tested.

## 13. SaaS owner architecture

**Status: Planned**

Platform roles (`PLATFORM_OWNER`, `PLATFORM_ADMIN`) are separate from tenant roles (`TENANT_OWNER`, `TENANT_ADMIN`, `MEMBER`) and are never conflated. The platform layer lives in `app/platform/` and is exposed under `/api/v1/platform/*` for tenant administration, usage, revenue, plans, subscriptions, system health, and audit logs. Privileged platform actions are always audit-logged.

## 14. Authentication and authorization

**Status: Planned**

Modern password hashing, access/refresh tokens, and revocation. Authorization answers: who is the user, which membership and role apply, which tenant owns the resource, and is the action permitted. See [docs/AUTH.md](docs/AUTH.md).

## 15. Billing and usage tracking

**Status: Planned**

Usage is a first-class subsystem of append-only usage events (`tenant_id`, `event_type`, `quantity`, `unit`, `metadata`, `created_at`) aggregated for dashboards and billing. Plans and limits are stored and configurable, enforced through a central entitlement service. Billing models are provider-agnostic behind an abstraction.

## 16. Observability

**Status: Implemented** — structured logging, request IDs, and health endpoints exist and are tested. OpenTelemetry, Prometheus, and Sentry remain Planned.

Structured JSON logs carry `request_id`, and where applicable `tenant_id`, `user_id`, and `conversation_id`, propagated through context variables so async work keeps its correlation. Fields whose names suggest secrets (password, token, secret, key, authorization, cookie, signature, credential) are redacted recursively before serialisation, so tokens and API keys cannot reach the logs. A console formatter is used locally and JSON in deployed environments.

Health endpoints separate liveness from readiness:

| Endpoint | Checks | Purpose |
| --- | --- | --- |
| `GET /health` | none | Service identity and status |
| `GET /health/live` | none | Process liveness; never depends on PostgreSQL or Redis |
| `GET /health/ready` | PostgreSQL, Redis | Can this instance serve traffic; `503` when degraded |

Readiness probes run concurrently, are timeout-bounded, and contain their failures: a dependency outage degrades the report instead of raising, and driver internals are never returned to callers.

## 17. CI/CD and production deployment

**Status: In Progress** — CI is Implemented; deployment automation and worker containers are Planned.

GitHub Actions runs three jobs: quality (Ruff, Black, MyPy), tests (pytest with coverage against PostgreSQL with pgvector and Redis service containers, an application startup check, migration upgrade/downgrade/upgrade validation, and model drift detection via `alembic check`), and a Docker build that boots the runtime image and asserts it answers liveness.

The runtime image is multi-stage and runs as a non-root user with a liveness-based container health check. Migrations are opt-in through `RUN_MIGRATIONS` so a release applies them once as an explicit step rather than racing across replicas. Production runs the API behind Nginx with health checks, restart policies, resource limits, an isolated network, and persistent volumes; workers join this topology in Phase 8. Details in [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).
