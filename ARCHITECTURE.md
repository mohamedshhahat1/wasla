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

**Status: In Progress** — created incrementally during Phase 0 and later phases.

```
wasla/
|-- app/
|   |-- main.py              application factory
|   |-- core/                config, logging, exceptions, middleware, DI
|   |-- db/                  declarative base, async session, models
|   |-- repositories/        data access, tenant-scoped
|   |-- schemas/             Pydantic request/response contracts
|   |-- services/            business logic / use cases
|   |-- integrations/        whatsapp/, openai/
|   |-- agents/              agent definitions, orchestrator, registry
|   |-- workers/             background job consumers
|   |-- api/v1/              thin HTTP routers
|   +-- platform/            SaaS owner administration layer
|-- alembic/                 migrations
|-- tests/                   unit, integration, e2e
|-- nginx/                   reverse proxy example
|-- scripts/
+-- .github/workflows/       CI pipelines
```

## 3. Application layers

**Status: Planned**

| Layer | Responsibility | Rule |
| --- | --- | --- |
| API routers | HTTP contract, validation, auth dependencies | Thin, no business logic |
| Services / use cases | Business logic, orchestration, transactions | No raw HTTP, no direct SQL |
| Repositories | Data access via SQLAlchemy | Always tenant-scoped |
| Integrations | External providers (Meta, OpenAI) behind interfaces | No provider calls elsewhere |
| Models | SQLAlchemy 2.0 declarative entities | Own no business rules |

Dependencies point inwards: API -> services -> repositories/integrations -> models. Dependency injection is used for sessions, clients, and settings so the orchestrator is testable without FastAPI, Meta, or OpenAI.

## 4. Request flow

**Status: Planned**

`Request -> middleware (request_id, logging, timing) -> router -> auth/tenant dependency -> service -> repository -> PostgreSQL -> response schema -> structured access log`

Errors raise domain exceptions that centralised handlers map to consistent HTTP payloads. Stack traces are never exposed in production responses.

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

**Status: Planned**

`Question -> embedding -> tenant-filtered pgvector search -> top chunks -> agent context -> Responses API -> answer`. Ingestion: `upload -> validate -> extract -> chunk -> embed -> store -> index`. Cross-tenant retrieval is structurally prevented by mandatory `tenant_id` filters.

## 8. Human handoff flow

**Status: Planned**

Every conversation has a mode: `AI` or `HUMAN`. Handoff is triggered by explicit customer request, low confidence, negative or angry sentiment, sensitive requests, tool failure, or an agent rule. In `HUMAN` mode automatic AI replies stop, ownership and reason are tracked, and the conversation can be assigned to a team member. Resuming returns the conversation to `AI`.

## 9. CRM / lead flow

**Status: Planned**

Conversations produce contacts and leads. Agents create or update leads through validated, tenant-scoped tools. Lead statuses: `NEW`, `CONTACTED`, `QUALIFIED`, `PROPOSAL`, `WON`, `LOST`. Follow-ups are scheduled, cancellable, and respect the WhatsApp 24-hour service window and template rules.

## 10. Background jobs and Redis usage

**Status: Planned**

Redis provides job queues, caching, rate limiting, follow-up scheduling, and temporary state. Workers handle AI processing, media processing, document ingestion and embeddings, follow-ups, campaigns, and usage aggregation. All jobs are idempotent and support retry with an error/dead-letter strategy.

## 11. Database architecture

**Status: In Progress** — engine, session, and migration tooling land in Phase 0; domain models in Phase 1 onwards.

PostgreSQL with SQLAlchemy 2.0 async sessions and Alembic migrations. UUID primary keys, `created_at`/`updated_at` timestamps, foreign keys and constraints, selective soft deletion. Indexes are planned on `tenant_id`, conversation `(tenant_id, status)`, message `(conversation_id, created_at)`, contact `(tenant_id, phone)`, lead `(tenant_id, status)`, WhatsApp `phone_number_id`, usage and analytics `(tenant_id, created_at)`, and document `tenant_id`.

## 12. Multi-tenancy

**Status: Planned**

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

**Status: In Progress** — structured logging, request IDs, and health endpoints are part of Phase 0.

Structured JSON logs carrying `request_id`, and where applicable `tenant_id`, `user_id`, and `conversation_id`. Health endpoints separate liveness (process only) from readiness (PostgreSQL, Redis, and other required dependencies). Secrets, tokens, and API keys are never logged. The design stays lightweight but extensible towards OpenTelemetry, Prometheus, and Sentry.

## 17. CI/CD and production deployment

**Status: In Progress** — the CI pipeline is part of Phase 0; deployment automation is Planned.

GitHub Actions runs formatting, Ruff, MyPy, tests, and migration checks. Production runs the API and workers as separate non-root containers behind Nginx, with health checks, restart policies, isolated networks, and persistent volumes. Details in [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).
