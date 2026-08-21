# Wasla Architecture

Technical source of truth for the current system architecture. Every section carries an explicit status. Sections marked **Planned** describe the intended design only; no code exists for them yet.

| Legend | Meaning |
| --- | --- |
| Implemented | Exists in the repository and is exercised by tests |
| In Progress | Partially present, actively being built |
| Planned | Designed, not yet built |
| Blocked | Cannot proceed until a dependency is resolved |

## 1. System overview

**Status: In Progress** — identity, tenancy, authorization, the WhatsApp transport and conversations exist. The agent orchestrator and RAG do not.

Wasla is an API-first, multi-tenant backend. A business (tenant) connects one or more WhatsApp Business phone numbers. Inbound customer messages arrive as Meta webhooks, are resolved to a tenant, persisted, and queued for asynchronous AI processing. An agent orchestrator loads the conversation, retrieves tenant-scoped knowledge, calls the OpenAI Responses API with a controlled tool set, and replies through the WhatsApp Cloud API.

Of that pipeline, the webhook, tenant resolution, event persistence, the projection into conversations and messages, and the outbound client are built. Everything from the queue onwards is not.

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

**Status: In Progress** — directories marked *(planned)* are created by the phase that needs them.

```
wasla/
|-- app/
|   |-- main.py              application factory
|   |-- core/                config, logging, exceptions, middleware, redis, DI, security
|   |-- db/                  declarative base, mixins, async session, models
|   |-- repositories/        data access, tenant-scoped and isolation-enforcing
|   |-- schemas/             Pydantic request/response contracts
|   |-- services/            business logic / use cases
|   |-- api/                 health router, auth dependencies
|   |   +-- v1/              auth, conversations, invitations, webhooks, whatsapp
|   |-- integrations/        whatsapp/ (signature, payload, client); openai/ (planned)
|   |-- agents/              agent definitions, orchestrator  (planned)
|   |-- workers/             background job consumers         (planned)
|   +-- platform/            SaaS owner administration layer  (planned)
|-- alembic/                 migrations
|-- tests/                   unit, integration, e2e
|-- nginx/                   reverse proxy example
|-- scripts/                 container entrypoint
+-- .github/workflows/       CI and security pipelines
```

## 3. Application layers

**Status: Implemented** — the layering and dependency injection are established and exercised by the health, authentication, invitation, WhatsApp and conversation subsystems.

| Layer | Responsibility | Rule |
| --- | --- | --- |
| API routers | HTTP contract, validation, auth dependencies | Thin, no business logic |
| Services / use cases | Business logic, orchestration, transactions | No raw HTTP, no direct SQL |
| Repositories | Data access via SQLAlchemy | Always tenant-scoped |
| Integrations | External providers (Meta, OpenAI) behind interfaces | No provider calls elsewhere |
| Models | SQLAlchemy 2.0 declarative entities | Own no business rules |

Dependencies point inwards: API -> services -> repositories/integrations -> models. Dependency injection is used for sessions, clients, and settings so the orchestrator is testable without FastAPI, Meta, or OpenAI. Infrastructure is created once per process in the application lifespan, stored on application state, and injected as typed dependencies; the health service receives its probes as injected callables, which is why the endpoint tests need no real database or cache.

Services own no transaction. The session is request-scoped and commits when the request succeeds, so a partially completed operation cannot be left behind; repositories stage writes and never commit.

Workspace-scoped services take their tenant id from the injected active workspace, not from a route argument, so no endpoint can choose which workspace it operates on.

## 4. Request flow

**Status: Implemented** — middleware, routing, centralised error handling, and the authentication and workspace dependencies all exist and are tested.

`Request -> middleware (request_id, log context, timing) -> router -> auth dependency -> workspace + role dependency -> service -> repository -> PostgreSQL -> response schema -> structured access log`

Every request carries a request ID, taken from the configured header or generated, bound to the log context, and returned on the response. Errors raise domain exceptions that centralised handlers map to a stable envelope:

```json
{ "error": { "code": "not_found", "message": "...", "request_id": "..." } }
```

Stack traces are never exposed in production responses. Cross-tenant access is reported as `not_found` so error codes cannot be used to probe another tenant's data.

## 5. WhatsApp webhook flow

**Status: In Progress** — verification, signature checking, parsing, tenant resolution, idempotent event storage and projection into conversations are Implemented. Queueing for AI processing arrives with Phase 5.

1. `GET /api/v1/webhooks/whatsapp` verifies the Meta challenge token with a constant-time comparison. **Implemented**
2. `POST /api/v1/webhooks/whatsapp` verifies the `X-Hub-Signature-256` signature over the payload. **Implemented**
3. The payload is parsed; `phone_number_id` resolves the WhatsApp account and therefore the tenant. The tenant is never inferred from the customer phone number. **Implemented**
4. Message and status events are persisted idempotently, keyed on the WhatsApp message/event ID (ADR-011). **Implemented**
5. The contact, conversation and message are created or updated from the stored event, and the endpoint returns immediately. **Implemented**
6. Work is enqueued to Redis for the agent orchestrator. **Planned (Phase 5)**

No AI or media processing happens inside the webhook request.

Three properties of the endpoint are deliberate and should not be "tidied" later:

- **The signature is computed over the raw request body**, before parsing. Verifying a re-serialised payload would verify Wasla's own serialisation rather than the bytes Meta signed.
- **Anything unactionable still answers 200.** Meta retries non-2xx deliveries and eventually disables the subscription, so returning an error for a payload that will never become valid — an unparseable body, an unknown `phone_number_id`, a disabled account — turns one bad message into an outage. Only a failed signature answers 403.
- **`phone_number_id` is unique platform-wide**, not per workspace, which is what makes step 3 trustworthy: a number can never resolve to two tenants.

### 5.1 Outbound

**Status: Implemented** — text, media, location, reply buttons, lists, templates and read receipts, behind `app/integrations/whatsapp/client.py`.

Retries are deliberately narrow: HTTP 429 and connection errors only, never 5xx and never read timeouts, because the Meta send endpoint accepts no idempotency key and an ambiguous retry duplicates a message in a real customer's chat (ADR-010). Sending uses the platform Meta credential; the account row stores no token (ADR-009).

A send writes its message row before calling Meta and records a rejection on that row rather than raising, so an attempt always survives as evidence; the API therefore answers `201` with a `failed` status instead of an error code (ADR-013).

### 5.2 Conversation projection

**Status: Implemented** — `app/services/conversation_service.py`, exercised against PostgreSQL by `tests/integration/test_conversation_projection.py`.

Storing an event and interpreting it are two services, not one. The webhook's storage path must not fail because a projection rule is wrong, and a projection bug must be fixable by replaying the stored event log rather than by asking Meta to resend traffic it already delivered.

Four rules govern the projection:

- **Only newly stored events project.** A replayed delivery — the normal case, since Meta retries until it sees a 200 — stops at the idempotency check, so it cannot duplicate a message or re-advance a status.
- **A status never moves a message backwards.** Meta does not guarantee ordering, so `delivered` arriving after `read` must not undo the read. Statuses are ranked, and only a higher rank changes the message state; the observed timestamp is still recorded either way.
- **An unrecognised message type is stored, not dropped.** It becomes `UNSUPPORTED` and keeps its raw payload, so a type Meta ships tomorrow can be replayed once it is understood.
- **The customer profile name comes from the delivery's `contacts` block**, which is the only place Meta sends it. Without it an inbox can only show a phone number.

The same customer writing to two workspaces produces two contacts and two conversations. That is the intended consequence of `tenant_id` isolation: a person is a customer of a business, not of the platform.

## 6. AI agent flow

**Status: Planned**

`Load tenant -> load conversation -> check mode (HUMAN stops AI) -> select agent -> load agent config -> build token-aware memory -> retrieve knowledge -> expose allowed tools -> OpenAI Responses API -> execute validated tool calls -> final response -> send + persist -> record usage`

## 7. RAG flow

**Status: Planned** — the `vector` extension is already enabled by migration `0001`.

`Question -> embedding -> tenant-filtered pgvector search -> top chunks -> agent context -> Responses API -> answer`. Ingestion: `upload -> validate -> extract -> chunk -> embed -> store -> index`. Cross-tenant retrieval is structurally prevented by mandatory `tenant_id` filters.

## 8. Human handoff flow

**Status: In Progress** — mode, handoff reason and assignment are Implemented; the automatic triggers are Planned.

Every conversation carries a mode, `AI` or `HUMAN`, and a nullable handoff reason. Switching to `HUMAN` records the reason; returning to `AI` clears it, because a stale explanation left attached to an AI-handled conversation misleads whoever reads it next. Assignment names a member of the workspace, and that membership is verified through the repository rather than trusted from the request body, so a conversation cannot be assigned to an outsider whose id a caller happens to know.

Automatic handoff — triggered by explicit customer request, low confidence, negative or angry sentiment, sensitive requests, tool failure, or an agent rule — arrives with sentiment analysis in Phase 10. The rule that `HUMAN` mode stops automatic AI replies belongs to the orchestrator and arrives with it in Phase 5; `Conversation.is_ai_handled` already exists for it to read.

## 9. CRM / lead flow

**Status: Planned**

Conversations produce contacts and leads. Agents create or update leads through validated, tenant-scoped tools. Lead statuses: `NEW`, `CONTACTED`, `QUALIFIED`, `PROPOSAL`, `WON`, `LOST`. Follow-ups are scheduled, cancellable, and respect the WhatsApp 24-hour service window and template rules.

## 10. Background jobs and Redis usage

**Status: In Progress** — the Redis client, its health probe, and the refresh-token denylist are Implemented; queues and workers are not.

Redis provides job queues, caching, rate limiting, follow-up scheduling, and temporary state. Workers handle AI processing, media processing, document ingestion and embeddings, follow-ups, campaigns, and usage aggregation. All jobs are idempotent and support retry with an error/dead-letter strategy.

## 11. Database architecture

**Status: In Progress** — engine, session scope, declarative base, shared mixins, migration tooling, the identity and tenancy tables, the WhatsApp tables and the conversation tables are Implemented; knowledge, CRM, and billing tables arrive in later phases.

PostgreSQL with SQLAlchemy 2.0 async sessions and Alembic migrations. The declarative base fixes an explicit constraint naming convention so autogenerated migrations stay stable and reviewable. Shared mixins provide UUID primary keys, `created_at`/`updated_at` timestamps, optional soft deletion, and the tenant foreign key plus index for tenant-owned tables. Migration `0001` enables the `pgcrypto` and `vector` extensions so every environment is provisioned identically; migration `0002` creates `tenants`, `users`, `memberships`, and `tenant_invitations`; migration `0003` creates `whatsapp_accounts` and `whatsapp_events`; migration `0004` creates `contacts`, `conversations`, and `messages`.

Sessions are request-scoped and commit on success or roll back on failure. Connections use pre-ping, bounded pooling, recycling, and an explicit connect timeout.

Primary keys are generated in Python and applied at insert time, so a newly added row has no id until the session is flushed. Code that creates a parent and then references it — the projection creating a contact before its conversation — must flush in between. This is a deliberate trade for portable, application-visible identifiers rather than database-generated ones.

Enum columns are native PostgreSQL types. Tables deliberately carry no `server_default` for enum and boolean columns — defaults are applied in the application — so that `alembic check` compares like with like and stays trustworthy as a drift gate.

One pitfall is recorded here because it already produced a defect. A model that declares `__table_args__` in its own class body **replaces** the value contributed by `TenantScopedMixin` instead of extending it, and so loses its `tenant_id` index with no error anywhere. Both WhatsApp models did exactly that, leaving the model metadata without two indexes that migration `0003` creates — a difference `alembic check` exists to fail on. `tests/unit/test_whatsapp_models.py` now asserts that every mapped table carrying a `tenant_id` column also declares `ix_<table>_tenant_id`, so a tenant-scoped model added in a later phase cannot reintroduce it quietly. The conversation models each restate their own tenant index for the same reason.

The schema carries one deliberate denormalisation. `conversations.last_inbound_at` duplicates the timestamp of the customer's most recent message, which could be derived from the `messages` table instead. It is stored because the 24-hour service window is checked on every outbound send and returned on every conversation read, so deriving it would make that the most frequent query in the system. The projection is the only writer.

Indexes exist on `memberships (tenant_id)`, `memberships (user_id)`, `UNIQUE(user_id, tenant_id)`, `tenant_invitations (tenant_id)`, `tenant_invitations (tenant_id, email)`, the unique invitation token hash, `whatsapp_accounts (tenant_id)`, a platform-wide `UNIQUE(phone_number_id)`, `whatsapp_events (tenant_id)`, `whatsapp_events (account_id)`, `whatsapp_events (tenant_id, state)`, `UNIQUE(tenant_id, event_id)`, `contacts (tenant_id)`, `UNIQUE(tenant_id, wa_id)`, `conversations (tenant_id)`, `conversations (tenant_id, status)`, `conversations (tenant_id, last_message_at)`, `conversations (contact_id)`, `UNIQUE(tenant_id, contact_id, account_id)`, `messages (tenant_id)`, `messages (conversation_id, created_at)`, and `UNIQUE(tenant_id, wa_message_id)`. Further indexes are planned on lead `(tenant_id, status)`, usage and analytics `(tenant_id, created_at)`, and document `tenant_id`.

## 12. Multi-tenancy

**Status: Implemented** — enforced in the repository layer and tested against a real database.

Shared PostgreSQL infrastructure with `tenant_id` isolation (see ADR-001). Users are global identities; the authoritative link to a company is `User -> Membership -> Tenant` (see ADR-002). Roles are scoped to the membership, never to the user. A request executes in exactly one active workspace, taken from the signed access token and re-verified against a live membership on every request.

Isolation is structural rather than a habit: `TenantScopedRepository` takes its tenant id once from the authenticated context, fixes it for the repository's lifetime, and applies it in the single method every read starts from. A subclass that fails to declare its tenant predicate cannot be instantiated. Queries that must cross workspaces — resolving which workspaces a user belongs to, resolving an invitation by its token hash before any workspace is known, and resolving a WhatsApp `phone_number_id` to its account, since inbound traffic has no workspace until that lookup succeeds — are isolated in their own small classes with one method each, so the exceptions are visible instead of scattered.

Cross-tenant reads answer `not_found`, never `forbidden`, so error codes cannot be used to map another tenant's data. `tests/integration/test_authorization.py`, `tests/integration/test_whatsapp_persistence.py` and `tests/integration/test_conversation_projection.py` prove this against PostgreSQL.

## 13. SaaS owner architecture

**Status: In Progress** — the platform role authorization layer is Implemented; the `app/platform/` surface is Planned.

Platform roles (`PLATFORM_OWNER`, `PLATFORM_ADMIN`) are separate from tenant roles (`TENANT_OWNER`, `TENANT_ADMIN`, `MEMBER`) and are never conflated: a platform role grants nothing inside a workspace, which is tested. The platform layer lives in `app/platform/` and is exposed under `/api/v1/platform/*` for tenant administration, usage, revenue, plans, subscriptions, system health, and audit logs. Privileged platform actions are always audit-logged.

## 14. Authentication and authorization

**Status: Implemented** — rate limiting on authentication endpoints remains Planned (phase 14).

Argon2id password hashing with rehash-on-login, typed access and refresh tokens, rotating refresh tokens with a Redis denylist, a current-user dependency, workspace resolution and switching from the token, and role dependencies for both scopes. Access tokens are intentionally not revocable and membership is re-verified per request; the reasoning for both, and the invitation flow, is in [docs/AUTH.md](docs/AUTH.md).

Conversation routes are open to every workspace member rather than to admins only, because restricting them would exclude the people who staff an inbox. Role gates stay on administrative actions: connecting a number, inviting a colleague, revoking an invitation.

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

Events that are expected but worth counting are logged rather than raised: an unmapped delivery status, and a status for a message Wasla never sent — which is normal for a template sent from Meta's own console.

## 17. CI/CD and production deployment

**Status: In Progress** — CI is Implemented; deployment automation and worker containers are Planned.

GitHub Actions runs three jobs: quality (Ruff, Black, MyPy), tests (pytest with coverage against PostgreSQL with pgvector and Redis service containers, including authorization, tenant-isolation, model-metadata parity and conversation projection tests that build their schema from the models, plus an application startup check, migration upgrade/downgrade/upgrade validation, and model drift detection via `alembic check`), and a Docker build that boots the runtime image and asserts it answers liveness. A separate security workflow scans dependencies and the repository history for secrets.

The runtime image is multi-stage and runs as a non-root user with a liveness-based container health check. Migrations are opt-in through `RUN_MIGRATIONS` so a release applies them once as an explicit step rather than racing across replicas. Production runs the API behind Nginx with health checks, restart policies, resource limits, an isolated network, and persistent volumes; workers join this topology in Phase 8. Details in [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).
