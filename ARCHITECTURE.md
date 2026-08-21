# Wasla Architecture

Technical source of truth for the current system architecture. Every section carries an explicit status. Sections marked **Planned** describe the intended design only; no code exists for them yet.

| Legend | Meaning |
| --- | --- |
| Implemented | Exists in the repository and is exercised by tests |
| In Progress | Partially present, actively being built |
| Planned | Designed, not yet built |
| Blocked | Cannot proceed until a dependency is resolved |

## 1. System overview

**Status: In Progress** — identity, tenancy, authorization, the WhatsApp transport, conversations, the agent orchestrator with its queue and worker, tenant-scoped knowledge retrieval, lead management and scheduled follow-ups exist. Media, sentiment, campaigns, usage and billing do not.

Wasla is an API-first, multi-tenant backend. A business (tenant) connects one or more WhatsApp Business phone numbers. Inbound customer messages arrive as Meta webhooks, are resolved to a tenant, persisted, and queued for asynchronous AI processing. An agent orchestrator loads the conversation, retrieves tenant-scoped knowledge, calls the OpenAI Responses API with a controlled tool set, and replies through the WhatsApp Cloud API.

Of that pipeline, everything except retrieval is built: the webhook, tenant resolution, event persistence, the projection into conversations and messages, the Redis queue, the orchestrator, the outbound client, and the worker that joins them. The worker has no process of its own yet — it is a class nothing calls — and usage recording arrives with Phase 12.

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
|   |   +-- v1/              agents, auth, conversations, invitations, webhooks, whatsapp
|   |-- integrations/        whatsapp/ (signature, payload, client); openai/ (types, client)
|   |-- agents/              memory, tool registry, orchestrator
|   |-- workers/             job queue, AI worker (no process entrypoint yet)
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

Services own no transaction. The session is request-scoped and commits when the request succeeds, so a partially completed operation cannot be left behind; repositories stage writes and never commit. A worker is the one place that opens a session itself, because it has no request to borrow one from.

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

**Status: Implemented** — verification, signature checking, parsing, tenant resolution, idempotent event storage, projection into conversations, and enqueueing for the agent.

1. `GET /api/v1/webhooks/whatsapp` verifies the Meta challenge token with a constant-time comparison. **Implemented**
2. `POST /api/v1/webhooks/whatsapp` verifies the `X-Hub-Signature-256` signature over the payload. **Implemented**
3. The payload is parsed; `phone_number_id` resolves the WhatsApp account and therefore the tenant. The tenant is never inferred from the customer phone number. **Implemented**
4. Message and status events are persisted idempotently, keyed on the WhatsApp message/event ID (ADR-011). **Implemented**
5. The contact, conversation and message are created or updated from the stored event. **Implemented**
6. One job per conversation that received a message is enqueued to Redis, and the endpoint returns. **Implemented**

No AI or media processing happens inside the webhook request.

Four properties of the endpoint are deliberate and should not be "tidied" later:

- **The signature is computed over the raw request body**, before parsing. Verifying a re-serialised payload would verify Wasla's own serialisation rather than the bytes Meta signed.
- **Anything unactionable still answers 200.** Meta retries non-2xx deliveries and eventually disables the subscription, so returning an error for a payload that will never become valid — an unparseable body, an unknown `phone_number_id`, a disabled account — turns one bad message into an outage. Only a failed signature answers 403.
- **`phone_number_id` is unique platform-wide**, not per workspace, which is what makes step 3 trustworthy: a number can never resolve to two tenants.
- **A queue failure is logged and swallowed.** The messages are already stored, so failing the delivery would make Meta resend traffic that landed, to fix a problem retrying cannot fix.

### 5.1 Outbound

**Status: Implemented** — text, media, location, reply buttons, lists, templates and read receipts, behind `app/integrations/whatsapp/client.py`.

Retries are deliberately narrow: HTTP 429 and connection errors only, never 5xx and never read timeouts, because the Meta send endpoint accepts no idempotency key and an ambiguous retry duplicates a message in a real customer's chat (ADR-010). Sending uses the platform Meta credential; the account row stores no token (ADR-009).

A send writes its message row before calling Meta and records a rejection on that row rather than raising, so an attempt always survives as evidence; the API therefore answers `201` with a `failed` status instead of an error code (ADR-013).

A template send is recorded as `MessageKind.TEMPLATE` with `template_name` and `template_language`, and with **no body**. Meta renders an approved template from its own copy, so the text the customer read is not something Wasla holds; writing a reconstruction into the transcript would put words there that were never sent. The name and language are columns rather than a formatted string in `body`, because follow-ups (phase 8) and campaigns (phase 11) both need to ask which template went out without parsing it back out of prose. Templates are also the one message kind exempt from the 24-hour service window (ADR-012), which is precisely what they exist for.

### 5.2 Conversation projection

**Status: Implemented** — `app/services/conversation_service.py`, exercised against PostgreSQL by `tests/integration/test_conversation_projection.py`.

Storing an event and interpreting it are two services, not one. The webhook's storage path must not fail because a projection rule is wrong, and a projection bug must be fixable by replaying the stored event log rather than by asking Meta to resend traffic it already delivered.

Four rules govern the projection:

- **Only newly stored events project.** A replayed delivery — the normal case, since Meta retries until it sees a 200 — stops at the idempotency check, so it cannot duplicate a message or re-advance a status.
- **A status never moves a message backwards.** Meta does not guarantee ordering, so `delivered` arriving after `read` must not undo the read. Statuses are ranked, and only a higher rank changes the message state; the observed timestamp is still recorded either way.
- **An unrecognised message type is stored, not dropped.** It becomes `UNSUPPORTED` and keeps its raw payload, so a type Meta ships tomorrow can be replayed once it is understood.
- **The customer profile name comes from the delivery's `contacts` block**, which is the only place Meta sends it. Without it an inbox can only show a phone number.

The same customer writing to two workspaces produces two contacts and two conversations. That is the intended consequence of `tenant_id` isolation: a person is a customer of a business, not of the platform.

### 5.3 Paging the inbox

**Status: Implemented** — `app/core/pagination.py`, exercised against PostgreSQL by `tests/integration/test_pagination.py`.

Conversations and messages are paged by keyset, not offset. An inbox is read while it is being written to, and an offset shifts under every insert: a conversation arriving between page one and page two pushes a row across the boundary, so the reader sees it twice or misses it. A cursor names the last row seen and asks for what follows, which is stable under concurrent writes.

Two details make the ordering total, and both matter:

- **The row id is the tiebreaker.** Conversations order by `last_message_at DESC, id DESC`; messages by `created_at DESC, id DESC`. Two rows can share a timestamp to the microsecond, and without a second key the page boundary between them is arbitrary — which is another way of saying a row can fall through it.
- **Nulls sort last, with their own keyset.** A conversation that has never carried a message has a null `last_message_at`, and a plain descending sort would put it *first*, ahead of live traffic. It is ordered `NULLS LAST`, and because null is not comparable, the block is paged by id alone once the cursor reaches it.

The cursor is opaque by construction rather than by obfuscation. It carries nothing not already visible in the page it came from, and it is only ever applied inside a tenant-scoped query — so a cursor taken from another workspace is a position, not an authorisation, and can widen nothing. Encoding it keeps clients from building cursors by hand against a sort key that is ours to change. Every malformed cursor is one `422`; none may become a `500`, because cursors arrive in query strings and therefore arrive truncated, re-encoded and fuzzed.

## 6. AI agent flow

**Status: In Progress** — configuration, memory, tools, orchestration, the queue and the worker are Implemented, as are the knowledge-search, lead-capture and follow-up tools. Usage recording (Phase 12) is not.

```
Enqueued job -> worker reserves it -> open one session
  -> load conversation -> HUMAN mode? stop
  -> resolve agent (requested, or the workspace's active default)
  -> build token-aware memory window -> collect granted tools
  -> Responses API -> validated tool calls -> feed results back (max 3 rounds)
  -> reply text -> send via the messaging service -> commit
```

The orchestrator decides; the worker sends. `answer()` returns an outcome — reply text, whether a handoff was requested, which tools ran, token usage, rounds taken — and performs no I/O beyond the provider call and its own reads. That is what makes it testable with no WhatsApp account, no Redis and no database, and it means a defect in sending cannot be reached from a defect in reasoning.

The transaction belongs to the worker, which is the one component here with no request to borrow a session from. It opens one session per job and commits once, so the outbound message row and the conversation's timestamps land together or not at all. The provider call sits inside that session deliberately, so a tool reads and writes in the transaction the reply will be written to; the cost is a pinned connection for the length of an inference (ADR-015).

Four rules bound a turn. `HUMAN` mode stops it before any cost. A workspace with no active default answers nothing rather than falling back to a built-in prompt. The tool loop runs at most three rounds. And a tool that rejects its arguments or raises a domain error becomes output the model can read and retry against, not an exception — only the unexpected reaches the worker, which dead-letters the job.

Agent memory is a window over the conversation's own messages, bounded by both a message count and an estimated token budget, with failed outbound messages skipped: a message Meta rejected was never seen, so replaying it would make the agent reason about a conversation that never happened. Details, including why the token count is a character-ratio estimate rather than a tokeniser, are in [docs/AI_AGENTS.md](docs/AI_AGENTS.md).

## 7. RAG flow

**Status: Implemented** — ingestion, tenant-scoped retrieval and the `search_knowledge` tool, exercised against real PostgreSQL with pgvector. PDF extraction and an approximate vector index are not.

```
Upload (202)                      Question
    |                                 |
Document row, PENDING             embed the question
    |                                 |
Redis: knowledge:ingestion        tenant-filtered pgvector search
    |                                 |
IngestionWorker                   distance threshold
    |-- extract                       |
    |-- chunk (overlapping)       passages, or an explicit "nothing found"
    |-- embed (batched)               |
    +-- store chunks + vectors    search_knowledge tool output
    |                                 |
Document READY                    Agent orchestrator -> Responses API -> answer
```

Ingestion never happens in the request that submitted the document. Submitting writes a `PENDING` row, enqueues, and answers `202`; extraction, chunking and embedding call a provider, and a large document is dozens of embedding requests. It has its own queue and worker rather than sharing the agent's (ADR-019), so a bulk upload cannot sit in front of a customer waiting for a reply.

Three properties are load-bearing and each has a test:

- **Cross-tenant retrieval is structurally impossible.** The similarity search is written once, in `DocumentChunkRepository.search`, and carries a mandatory `tenant_id` predicate. The tenant id comes from the tool's context, never from an argument the model produced — a tenant id a model could supply is a tenant id a model could change.
- **Only `READY` documents answer.** The search joins the document and filters on status, so chunks written before a failure contribute nothing. A half-ingested document that still answered would be worse than one that answered not at all.
- **Nothing found is said, not implied.** An empty retrieval returns a sentence instructing the agent to say it does not have the information and offer a handoff. A model handed an empty string fills the silence from its training data, which is the invented answer grounding exists to prevent.

Chunking splits on paragraph structure first and character count only when a paragraph exceeds the budget alone, because a cut mid-sentence embeds as neither of the ideas it straddles. Chunks overlap, so an answer sitting across a boundary is reachable from either side. Ingestion is idempotent by content hash: the same text submitted twice is one document, and re-ingesting replaces a document's chunks rather than appending, which is what makes a duplicated job harmless. A failure is recorded on the document with its reason and is retryable through the API.

Embedding width is fixed in the column at 1536 rather than being configurable (ADR-018): a width that could be changed by configuration would corrupt a knowledge base quietly, because existing vectors are not recomputed and distances across widths are meaningless. No approximate vector index exists yet — ivfflat and hnsw need to be built against representative data, and exact search is correct at every size and fast at the sizes a new workspace has.

## 8. Human handoff flow

**Status: In Progress** — mode, handoff reason, assignment, and an agent's own request for a human are Implemented; the automatic triggers are Planned.

Every conversation carries a mode, `AI` or `HUMAN`, and a nullable handoff reason. Switching to `HUMAN` records the reason; returning to `AI` clears it, because a stale explanation left attached to an AI-handled conversation misleads whoever reads it next. Assignment names a member of the workspace, and that membership is verified through the repository rather than trusted from the request body, so a conversation cannot be assigned to an outsider whose id a caller happens to know.

An agent can hand over on its own. `request_human_handoff` is the one tool implemented in the registry: it records a reason of at most 200 characters, ends the tool loop, and suppresses the reply, because a conversation being handed to a person should not also receive a parting message from the agent. The orchestrator then refuses that conversation on every later turn, which is the `HUMAN`-mode guard reading `Conversation.is_ai_handled`.

Automatic handoff — triggered by low confidence, negative or angry sentiment, sensitive requests, or an agent rule — arrives with sentiment analysis in Phase 10.

## 9. CRM / lead flow

**Status: Implemented** — leads, notes, the activity timeline, the `record_lead_details` tool and the administration API, exercised against real PostgreSQL. Follow-ups (Phase 8), scoring rules (Phase 10) and lead merging are not.

```
Customer says something about themselves
    |
Agent calls record_lead_details (no lead id - it cannot name one)
    |
Resolve the conversation -> HUMAN mode? refuse
    |
Find the contact's OPEN lead        -- partial unique index: at most one
    |                                       |
  none                                   exists
    |                                       |
create (source=AGENT)              drop fields a human verified
    |                                       |
    +---------------+-----------------------+
                    |
        validate; drop what will not parse
                    |
        write LeadActivity (actor, before -> after)
```

Three properties are load-bearing, and each has a test.

**One open lead per customer, guaranteed by the database.** A partial unique index on `(tenant_id, contact_id)` covers only non-terminal statuses (ADR-020). A service-level check would lose the race — Meta retries webhooks and the queue can hand the same conversation to two workers, so "look, then create" has a window in which both callers find nothing. Partial rather than total is what keeps the rule right in the other direction: a closed lead releases the slot so a returning customer starts a fresh record, and hand-entered leads carry no contact and would otherwise all collide on a null.

**The model cannot name a lead.** It reports what it heard; the service resolves which lead that is from the conversation's own contact. An identifier the model chooses is one it can choose wrongly, and wrongly here reaches another customer's record. It also makes the tool idempotent by construction: called five times in a conversation, it updates one lead five times.

**A human edit outranks an inference.** Fields a person sets are recorded in `human_verified_fields` and extraction skips them, so the AI fills blanks and revises its own guesses but never overwrites what someone confirmed — including a field deliberately cleared, because "this customer has no email" is knowledge (ADR-021). Extraction is confined to contact details and stated interest; status, score, assignment and tags are decisions, not things to infer from one message.

Budgets arrive as plain numbers or not at all. `"500k"` is refused rather than guessed, because it reads as 500,000 to a person and 500 to a parser that gives up, and a wrong budget silently reorders a real sales pipeline. A model's value that fails validation is dropped so the rest of the capture still lands; a person's is reported, because someone typing into a form deserves to be told.

`lead_activities` is append-only — there is no service method and no route that edits or deletes an entry. An audit trail the application can rewrite does not answer the question it exists to answer, and the question here is "why does this lead say the budget is half a million".

Follow-ups are covered in the next section.

## 10. Follow-up flow

**Status: Implemented** — the model, scheduling, cancellation on reply, window and template compliance, the polling worker and the `schedule_follow_up` tool, exercised against real PostgreSQL.

```
"I'll think about it"
    |
schedule_follow_up (agent) or POST /follow-ups (person)
    |
one PENDING row per conversation  -- partial unique index
    |
    +--- customer replies ---> CANCELLED on the inbound path
    |
FollowUpWorker polls every 30s
    |
claim due rows: FOR UPDATE SKIP LOCKED   -- two replicas cannot both send
    |
inside the 24h window? --- yes ---> send free text ---> SENT
    |
    no
    |
has an approved template? -- yes --> send template ---> SENT
    |
    no
    |
  SKIPPED (recorded, never retried)
```

Unlike every other background job here, this one is **time**-triggered rather than event-triggered, so there is nothing to push and nothing to block on. It polls the database instead of holding a schedule in Redis (ADR-022): the follow-up must be a durable, cancellable, auditable row regardless, and a Redis sorted set carrying the same schedule would be a second source of truth that drifts the moment one is written without the other.

Three properties are load-bearing, and each has a test.

**A reply cancels the nudge, on the inbound path.** The follow-up exists because the customer went quiet; the moment they answer, its reason is gone. Cancellation happens in the webhook's own transaction rather than being left for the worker to notice, because the worker may sweep before that transaction's effects are visible to it — and a message that talks over someone who is already talking is exactly what a follow-up must never be. A delivery status is not a reply and cancels nothing.

**Two replicas cannot send the same message twice.** `SKIP LOCKED` settles it in the database. The sweep is bounded per claim so a backlog drains in batches rather than under one long-held lock.

**Not sending is a recorded outcome.** Outside the 24-hour window Meta accepts approved templates only, so a follow-up with no template is `SKIPPED` — distinct from `FAILED`, and never retried. `FAILED` means an attempt broke and may work later; `SKIPPED` means policy forbade it and the window will not reopen on its own. Collapsing the two would create a retry loop against a wall. The reason is written to the row, so a workspace can see why its nudge never went.

A rejected send is retried with a widening backoff until the attempt limit, then left `FAILED` with the reason. Scheduling twice on one conversation reschedules rather than queueing a second message: an agent that decides to follow up on every turn would otherwise stack notifications on one customer's phone.

**Known gap.** There is no template registry until Phase 11, so `template_name` is free text and nothing can confirm Meta has approved it before the send is attempted. That is the weakest point in this compliance story.

## 11. Background jobs and Redis usage

**Status: In Progress** — the Redis client, its health probe, the refresh-token denylist, the agent job queue, and the AI, ingestion and follow-up workers are Implemented. Media and campaign workers are not.

Redis provides job queues, caching, rate limiting, follow-up scheduling, and temporary state. Workers handle AI processing, media processing, document ingestion and embeddings, follow-ups, campaigns, and usage aggregation.

The agent queue is three lists rather than one: `agent:jobs:pending`, `agent:jobs:inflight`, and `agent:jobs:failed`. A worker reserves with a blocking `BLMOVE` into the in-flight list, removes the exact payload on success, and dead-letters it on failure, so a job whose worker dies is still visible instead of lost with the process (ADR-015). Payloads are compact JSON with sorted keys, because removal matches by exact value.

Two gaps are known and recorded rather than implied away. Nothing reaps the in-flight list, so a job abandoned by a killed worker stalls until an operator moves it; and requeueing is an operator decision, because re-running a job produces a second reply to the customer — these jobs are repeatable, not idempotent.

## 12. Database architecture

**Status: In Progress** — engine, session scope, declarative base, shared mixins, migration tooling, and the identity, tenancy, WhatsApp, conversation, agent, knowledge, CRM and follow-up tables are Implemented; campaign, usage and billing tables arrive in later phases.

PostgreSQL with SQLAlchemy 2.0 async sessions and Alembic migrations. The declarative base fixes an explicit constraint naming convention so autogenerated migrations stay stable and reviewable. Shared mixins provide UUID primary keys, `created_at`/`updated_at` timestamps, optional soft deletion, and the tenant foreign key plus index for tenant-owned tables. Migration `0001` enables the `pgcrypto` and `vector` extensions so every environment is provisioned identically; migration `0002` creates `tenants`, `users`, `memberships`, and `tenant_invitations`; migration `0003` creates `whatsapp_accounts` and `whatsapp_events`; migration `0004` creates `contacts`, `conversations`, and `messages`; migration `0005` creates `agents` and `agent_tools`.

Sessions are request-scoped and commit on success or roll back on failure. Connections use pre-ping, bounded pooling, recycling, and an explicit connect timeout.

Primary keys are generated in Python and applied at insert time, so a newly added row has no id until the session is flushed. Code that creates a parent and then references it — the projection creating a contact before its conversation, the agent service returning a created agent — must flush in between. This is a deliberate trade for portable, application-visible identifiers rather than database-generated ones.

Enum columns are native PostgreSQL types. Tables deliberately carry no `server_default` for enum and boolean columns — defaults are applied in the application — so that `alembic check` compares like with like and stays trustworthy as a drift gate.

One pitfall is recorded here because it already produced a defect. A model that declares `__table_args__` in its own class body **replaces** the value contributed by `TenantScopedMixin` instead of extending it, and so loses its `tenant_id` index with no error anywhere. Both WhatsApp models did exactly that, leaving the model metadata without two indexes that migration `0003` creates — a difference `alembic check` exists to fail on. `tests/unit/test_whatsapp_models.py` now asserts that every mapped table carrying a `tenant_id` column also declares `ix_<table>_tenant_id`, so a tenant-scoped model added in a later phase cannot reintroduce it quietly. The conversation and agent models each restate their own tenant index for the same reason.

The schema carries one deliberate denormalisation. `conversations.last_inbound_at` duplicates the timestamp of the customer's most recent message, which could be derived from the `messages` table instead. It is stored because the 24-hour service window is checked on every outbound send and returned on every conversation read, so deriving it would make that the most frequent query in the system. The projection is the only writer.

Indexes exist on `memberships (tenant_id)`, `memberships (user_id)`, `UNIQUE(user_id, tenant_id)`, `tenant_invitations (tenant_id)`, `tenant_invitations (tenant_id, email)`, the unique invitation token hash, `whatsapp_accounts (tenant_id)`, a platform-wide `UNIQUE(phone_number_id)`, `whatsapp_events (tenant_id)`, `whatsapp_events (account_id)`, `whatsapp_events (tenant_id, state)`, `UNIQUE(tenant_id, event_id)`, `contacts (tenant_id)`, `UNIQUE(tenant_id, wa_id)`, `conversations (tenant_id)`, `conversations (tenant_id, status)`, `conversations (tenant_id, last_message_at)`, `conversations (contact_id)`, `UNIQUE(tenant_id, contact_id, account_id)`, `messages (tenant_id)`, `messages (conversation_id, created_at)`, `UNIQUE(tenant_id, wa_message_id)`, `agents (tenant_id)`, `agents (tenant_id, status)`, `UNIQUE(tenant_id, name)` on agents, `agent_tools (tenant_id)`, `agent_tools (agent_id)`, and `UNIQUE(tenant_id, agent_id, name)`. Further indexes are planned on lead `(tenant_id, status)`, usage and analytics `(tenant_id, created_at)`, and document `tenant_id`.

## 13. Multi-tenancy

**Status: Implemented** — enforced in the repository layer and tested against a real database.

Shared PostgreSQL infrastructure with `tenant_id` isolation (see ADR-001). Users are global identities; the authoritative link to a company is `User -> Membership -> Tenant` (see ADR-002). Roles are scoped to the membership, never to the user. A request executes in exactly one active workspace, taken from the signed access token and re-verified against a live membership on every request.

Isolation is structural rather than a habit: `TenantScopedRepository` takes its tenant id once from the authenticated context, fixes it for the repository's lifetime, and applies it in the single method every read starts from. A subclass that fails to declare its tenant predicate cannot be instantiated. Queries that must cross workspaces — resolving which workspaces a user belongs to, resolving an invitation by its token hash before any workspace is known, and resolving a WhatsApp `phone_number_id` to its account, since inbound traffic has no workspace until that lookup succeeds — are isolated in their own small classes with one method each, so the exceptions are visible instead of scattered.

A background worker has no authenticated context to take a tenant id from, so it takes one from the job it reserved and constructs its repositories with it, exactly as a request-scoped service would. A tool handler receives that same tenant id in its context rather than reading one from model output.

Cross-tenant reads answer `not_found`, never `forbidden`, so error codes cannot be used to map another tenant's data. `tests/integration/test_authorization.py`, `tests/integration/test_whatsapp_persistence.py` and `tests/integration/test_conversation_projection.py` prove this against PostgreSQL.

## 14. SaaS owner architecture

**Status: In Progress** — the platform role authorization layer is Implemented; the `app/platform/` surface is Planned.

Platform roles (`PLATFORM_OWNER`, `PLATFORM_ADMIN`) are separate from tenant roles (`TENANT_OWNER`, `TENANT_ADMIN`, `MEMBER`) and are never conflated: a platform role grants nothing inside a workspace, which is tested. The platform layer lives in `app/platform/` and is exposed under `/api/v1/platform/*` for tenant administration, usage, revenue, plans, subscriptions, system health, and audit logs. Privileged platform actions are always audit-logged.

## 15. Authentication and authorization

**Status: Implemented** — rate limiting on authentication endpoints remains Planned (phase 14).

Argon2id password hashing with rehash-on-login, typed access and refresh tokens, rotating refresh tokens with a Redis denylist, a current-user dependency, workspace resolution and switching from the token, and role dependencies for both scopes. Access tokens are intentionally not revocable and membership is re-verified per request; the reasoning for both, and the invitation flow, is in [docs/AUTH.md](docs/AUTH.md).

Conversation routes are open to every workspace member rather than to admins only, because restricting them would exclude the people who staff an inbox. Reading agent configuration is open for the same reason. Role gates stay on administrative actions: connecting a number, inviting a colleague, revoking an invitation, and changing what an agent says to customers.

## 16. Billing and usage tracking

**Status: Planned**

Usage is a first-class subsystem of append-only usage events (`tenant_id`, `event_type`, `quantity`, `unit`, `metadata`, `created_at`) aggregated for dashboards and billing. Plans and limits are stored and configurable, enforced through a central entitlement service. Billing models are provider-agnostic behind an abstraction.

Token usage is already returned by the provider client and logged per turn, so the recorder has a source when it is built.

## 17. Observability

**Status: Implemented** — structured logging, request IDs, and health endpoints exist and are tested. OpenTelemetry, Prometheus, and Sentry remain Planned.

Structured JSON logs carry `request_id`, and where applicable `tenant_id`, `user_id`, and `conversation_id`, propagated through context variables so async work keeps its correlation. Fields whose names suggest secrets (password, token, secret, key, authorization, cookie, signature, credential) are redacted recursively before serialisation, so tokens and API keys cannot reach the logs. A console formatter is used locally and JSON in deployed environments.

Health endpoints separate liveness from readiness:

| Endpoint | Checks | Purpose |
| --- | --- | --- |
| `GET /health` | none | Service identity and status |
| `GET /health/live` | none | Process liveness; never depends on PostgreSQL or Redis |
| `GET /health/ready` | PostgreSQL, Redis | Can this instance serve traffic; `503` when degraded |

Readiness probes run concurrently, are timeout-bounded, and contain their failures: a dependency outage degrades the report instead of raising, and driver internals are never returned to callers.

Events that are expected but worth counting are logged rather than raised: an unmapped delivery status, a status for a message Wasla never sent — normal for a template sent from Meta's own console — and a queue push that failed while the message itself was stored.

An agent turn logs one summary event carrying the rounds taken, the tools run, whether it handed off, the estimated and actual token counts, and how many history turns were dropped, which is what makes a bad prompt or an over-tight budget diagnosable after the fact. Provider failures log the status, the attempt count and the provider's error `code` and `type` — never its prose, because that prose can quote the request and the request contains a customer's conversation.

## 18. CI/CD and production deployment

**Status: In Progress** — CI is Implemented; deployment automation and worker containers are Planned.

GitHub Actions runs three jobs: quality (Ruff, Black, MyPy), tests (pytest with coverage against PostgreSQL with pgvector and Redis service containers, including authorization, tenant-isolation, model-metadata parity, conversation projection, and agent memory, registry, orchestrator and queue tests that need neither a database nor a provider, plus an application startup check, migration upgrade/downgrade/upgrade validation, and model drift detection via `alembic check`), and a Docker build that boots the runtime image and asserts it answers liveness. A separate security workflow scans dependencies (`pip-audit`, including the development extra, since build and test tooling is part of the supply chain that produces the image) and the repository history for secrets. It also runs weekly on a cron, because advisories are published without this repository changing — which is the gate that keeps the pinned versions below from quietly rotting (ADR-016, ADR-017). Starlette is declared as a direct dependency rather than inherited from FastAPI, so the ASGI layer serving the webhook carries a security floor this repository controls.

The lint, format and type tools are pinned to exact versions in `pyproject.toml` rather than ranged (ADR-016). They gate the pipeline on style and typing, so a new release that changes a default turns CI red without a commit touching the project - which is precisely what happened before the pinning: Ruff, Black, MyPy and FastAPI had each drifted past the version the code was written against, and the resulting failures accumulated unseen behind a suite that could not collect at all.

PostgreSQL-backed tests build the schema once per session and run each test inside a transaction that is rolled back at teardown, rather than dropping and recreating the schema per test. That is a 27x reduction in suite runtime (2507s to 94s on the same 451 tests) and no reduction in isolation, which `tests/integration/test_fixture_isolation.py` exists to demonstrate. Two details carry it: no async fixture is session-scoped, because pytest-asyncio would give one a different event loop from the tests and an asyncpg connection cannot cross loops — the one-time schema build therefore runs in a synchronous fixture that opens and closes its own loop; and the session joins the outer transaction with `join_transaction_mode="create_savepoint"`, so a test that calls `commit()` releases a savepoint rather than committing and is still undone by the rollback. Runtime was itself a correctness problem: a forty-minute run is long enough to be interrupted by an unrelated infrastructure failure, and one such run was lost that way.

`tests/`, `tests/unit/` and `tests/integration/` are Python packages. Two test modules may otherwise share a basename - the conversation projection has both a pure-logic and a PostgreSQL-backed module - and without a package name to qualify them the second import collides with the first and *the entire suite fails to collect*. That failure mode is silent in the sense that matters: it reports one collection error rather than a test failure, and nothing else runs.

The runtime image is multi-stage and runs as a non-root user with a liveness-based container health check. Migrations are opt-in through `RUN_MIGRATIONS` so a release applies them once as an explicit step rather than racing across replicas. Production runs the API behind Nginx with health checks, restart policies, resource limits, an isolated network, and persistent volumes; workers join this topology in Phase 8. Details in [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).
