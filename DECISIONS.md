# Architecture Decisions

Records significant architectural decisions only. Trivial implementation details are not recorded here.

## ADR-001 — Multi-Tenancy Strategy

Date:
2026-08-21

Status:
Accepted

Decision:
Use shared PostgreSQL infrastructure with `tenant_id` isolation enforced in the repository and service layers.

Context:
Wasla is a multi-tenant SaaS platform expected to serve many businesses, each owning conversations, contacts, leads, knowledge, agents, usage, and billing data.

Reason:
A shared schema keeps operations, migrations, and connection pooling simple at this stage, while still allowing a move to schema-per-tenant or database-per-tenant for large customers later. Row-level security can be layered on top without changing the data model.

Consequences:
Every tenant-owned table carries `tenant_id` with an index. Every repository query must filter by tenant. Tenant isolation must be covered by explicit tests, because a single missing filter becomes a data-leak bug.

## ADR-002 — Global Users with Tenant Memberships

Date:
2026-08-21

Status:
Accepted

Decision:
A User is a global platform identity. Company access is expressed as `User -> Membership -> Tenant`, with roles scoped to the membership and `UNIQUE(user_id, tenant_id)` enforced. `user.tenant_id` is never used as the authoritative relationship.

Context:
One person may own one company, administer another, and be a member of a third, and must be able to create and switch companies from a single account.

Reason:
Membership-scoped roles are the only model that supports multi-workspace users without duplicate accounts, and it makes the authorization chain explicit: user, membership, tenant, role, resource.

Consequences:
Requests carry an active workspace context. A client-provided tenant identifier is only trusted after membership verification. Invitations, suspension, and removal operate on memberships rather than users.

## ADR-003 — Platform Roles Separated from Tenant Roles

Date:
2026-08-21

Status:
Accepted

Decision:
Maintain two distinct role scopes: platform (`PLATFORM_OWNER`, `PLATFORM_ADMIN`) and tenant (`TENANT_OWNER`, `TENANT_ADMIN`, `MEMBER`), with a dedicated platform administration layer under `app/platform/` and `/api/v1/platform/*`.

Context:
The Wasla SaaS operator needs cross-tenant visibility for support, billing, and analytics, which is a fundamentally different permission domain from a customer company owner.

Reason:
Mixing the two scopes is a common source of privilege-escalation bugs. Separate roles, routers, and services make the boundary auditable.

Consequences:
Platform endpoints never reuse tenant authorization dependencies. Platform actions do not bypass audit logging.

## ADR-004 — Clean Architecture with Service and Repository Layers

Date:
2026-08-21

Status:
Accepted

Decision:
Routes stay thin; business logic lives in services/use cases; database access lives in repositories; external providers sit behind integration clients injected through dependency injection.

Context:
The platform must remain testable without live Meta or OpenAI credentials and must absorb new channels and providers without rewrites.

Reason:
Explicit layering keeps the agent orchestrator testable outside FastAPI and prevents provider details from leaking into business logic.

Consequences:
More files and interfaces than a flat layout, and a standing rule against business logic in route handlers. Abstractions are added when a real seam exists, not speculatively.

## ADR-005 — Async SQLAlchemy 2.0 with Alembic Migrations

Date:
2026-08-21

Status:
Accepted

Decision:
Use SQLAlchemy 2.0 declarative models with `AsyncSession` and manage all schema changes through Alembic migrations.

Context:
The workload is I/O-bound: webhooks, database calls, Redis, Meta and OpenAI HTTP requests.

Reason:
Async sessions match FastAPI's concurrency model, and migration-only schema evolution is mandatory for a production SaaS.

Consequences:
Blocking drivers and libraries must be avoided or run in worker threads. Schema is never mutated outside migrations. CI validates that migrations apply cleanly.

## ADR-006 — Redis for Queues, Cache, and Scheduling

Date:
2026-08-21

Status:
Accepted

Decision:
Use Redis as the queue, cache, rate-limit, and follow-up scheduling backend, with idempotent workers consuming queued jobs.

Context:
WhatsApp webhooks must return immediately while AI, media, ingestion, campaign, and follow-up work happens asynchronously. Meta retries webhook deliveries.

Reason:
Redis is already required for caching and rate limiting, so reusing it avoids introducing a separate broker before the scale justifies it.

Consequences:
Jobs must be idempotent and keyed on WhatsApp event IDs. Durability guarantees are weaker than a dedicated broker, so a migration path to a stronger queue is kept open behind the queue abstraction.

## ADR-007 — OpenAI Responses API Behind an Integration Layer

Date:
2026-08-21

Status:
Accepted

Decision:
Use the current official OpenAI Responses API for agent inference, accessed only through `app/integrations/openai/`, with configurable models per agent. Deprecated OpenAI APIs are not used.

Context:
Agents need tool calling, structured outputs, conversation context, and token accounting, and model choice must be configurable per tenant agent.

Reason:
A single integration boundary keeps SDK details out of services, enables deterministic tests with fakes, and centralises retries, timeouts, and token usage recording.

Consequences:
Services depend on internal request/response types rather than SDK objects. Provider changes are absorbed in one module. API keys are configuration-only and never logged.

## ADR-008 — pgvector for Tenant-Scoped RAG

Date:
2026-08-21

Status:
Accepted

Decision:
Store document chunk embeddings in PostgreSQL using the pgvector extension, with every similarity query filtered by `tenant_id`.

Context:
Each tenant has an isolated knowledge base, and retrieval must never cross tenants.

Reason:
Keeping vectors in the primary database avoids a second datastore, keeps chunks transactionally consistent with their documents, and lets tenant filtering be expressed as an ordinary SQL predicate.

Consequences:
The PostgreSQL image must ship pgvector, enabled via migration. Index strategy and embedding dimensions must be reviewed as volume grows; a dedicated vector database remains an option behind the retrieval service.

## ADR-009 — No Meta Credentials Stored at Rest

Date:
2026-08-21

Status:
Accepted

Decision:
The `whatsapp_accounts` row stores no Meta access token or app secret. Outbound calls use the platform credential held in configuration. Per-workspace credentials are deferred until encryption at rest exists (Phase 14).

Context:
Each workspace connects its own WhatsApp Business number, and the obvious multi-tenant design is a token column on the account row. A workspace also cannot be onboarded without some credential being available to send on its behalf.

Reason:
A plaintext token column places a live, customer-visible sending capability into every database dump, backup, and read replica, and into the blast radius of any SQL injection or over-broad support query. Configuration is a narrower surface: it is not dumped, not queryable, and already redacted from logs. Deferring the column is cheaper than adding it now and retrofitting encryption and key rotation around it later.

Consequences:
All workspaces currently send through one platform Meta app, so per-workspace sender identity and per-workspace rate limits are not yet available. The account row remains a pointer to Meta identifiers only. `tests/unit/test_whatsapp_models.py` asserts that no column on the account table is named like a credential, so the decision cannot erode by accident. Lifting this requires the Phase 14 encryption-at-rest and key-management work first.

## ADR-010 — Conservative Outbound Retry Policy

Date:
2026-08-21

Status:
Accepted

Decision:
Retry an outbound WhatsApp send only on HTTP 429 and on connection errors, for at most three attempts with a fixed short backoff. Never retry a 5xx response or a read timeout. Requests are bounded by a 10 second timeout.

Context:
The Meta Cloud API send endpoint accepts no idempotency key, so the caller cannot make a retry provably safe. A retry that duplicates a send produces a duplicate message in a real customer's chat.

Reason:
The two retried cases are the ones where the request demonstrably did not reach the send handler: a 429 is a refusal to process, and a connection error means no request was ever established. A 5xx or a read timeout is ambiguous — the message may have been accepted and only the response lost — and in an ambiguous case a duplicate customer-visible message is worse than a failure the system can observe and act on deliberately.

Consequences:
Transient upstream 5xx errors surface as failures instead of being absorbed, which makes the failure rate visible rather than hidden in retries. Failures are logged with structured send events and left for the Phase 8 queue to retry with business context, where a decision about duplicates can be made with the conversation in view. If Meta later supports an idempotency key, this decision should be revisited, since the ambiguity is the only reason for the restriction.

## ADR-011 — Workspace-Scoped Webhook Idempotency Keys

Date:
2026-08-21

Status:
Accepted

Decision:
Inbound event de-duplication is enforced by `UNIQUE(tenant_id, event_id)` rather than a global unique event id. Status events are keyed by the message id joined to the status, not by the message id alone.

Context:
Meta retries webhook deliveries until it receives a 2xx, so the same event arrives more than once. Delivery statuses for one message arrive as separate events that all carry the same message id.

Reason:
A globally unique event id would let one workspace's traffic suppress another's: an id collision across tenants, whether accidental or induced, would silently discard a real message. Scoping the constraint by workspace makes that impossible. Keying status events on the message id alone would store `sent` and then discard `delivered` and `read` as duplicates, losing the delivery timeline.

Consequences:
The same event id may legitimately exist once per workspace, so no query may assume event ids are globally unique. The composed status key must stay stable, because changing its shape would make previously stored statuses look like new events. The uniqueness constraint, not the repository's existence check, is the actual guarantee: concurrent deliveries can both pass the check, and the database rejects the loser.

## ADR-012 — Service Window Enforced on Free Text Only

Date:
2026-08-21

Status:
Accepted

Decision:
Enforce Meta's 24-hour service window on free-text sends only; approved templates bypass the check deliberately. The window is measured from `conversations.last_inbound_at`, a stored copy of the customer's most recent message timestamp, and `service_window_open` is returned on every conversation read.

Context:
Meta accepts free-form messages for 24 hours after the customer's last message. Outside that window it accepts approved templates only. A conversation the customer has never written in has no open window at all.

Reason:
Checking the rule locally turns a provider rejection into an explainable `422` before a network call, and returning the window state on reads lets a client disable its composer rather than discover the rule by failing a send. Templates must not be subject to the check, because they are the sanctioned way to write outside the window; a single shared guard would have made the one legitimate escape route impossible. The timestamp is stored rather than derived because it is read on every send and every conversation read, which would otherwise make it the most frequent query in the system.

Consequences:
The projection is the only writer of `last_inbound_at`, so a projection defect would silently close windows that should be open; the PostgreSQL-backed projection tests assert it is set on arrival. The stored value is a denormalisation and can drift from the `messages` table, so it must be recomputed rather than trusted if the projection is ever changed. Template sends are unrestricted by Wasla and rely on Meta's own approval process as the control.

## ADR-013 — Failed Sends Are Recorded, Not Raised

Date:
2026-08-21

Status:
Accepted

Decision:
An outbound message row is written and flushed before Meta is called. If Meta rejects the send, the row is marked failed with a reason and returned, and the endpoint answers `201` with a `failed` status rather than an error code. A missing platform credential still raises `503`.

Context:
The database session is request-scoped and commits only when the request succeeds. Raising after recording a failure therefore rolls back the very row that recorded it, leaving no trace that an attempt was ever made.

Reason:
An attempt to message a customer is a business event worth keeping even when it fails, and it is the only record an operator can act on afterwards. Losing it to preserve conventional status-code semantics is the wrong trade. A missing credential is different: nothing was attempted, so there is nothing to preserve, and a `503` correctly names it as our misconfiguration rather than the caller's mistake.

Consequences:
Callers must read the returned `status` rather than relying on the HTTP code, which is documented in `docs/API.md`. A `201` never meant delivered in any case, since delivery arrives later as a webhook status. When the Phase 8 queue takes over sending, this behaviour moves to the worker and the endpoint becomes an accepted-for-send acknowledgement, at which point the response contract should be revisited.

## ADR-014 — OpenAI Spoken Over HTTP, Conversation State Kept in PostgreSQL

Date:
2026-08-21

Status:
Accepted

Decision:
Call the Responses API directly with `httpx` from `app/integrations/openai/`, with no `openai` package dependency and hand-written request and response types. Every request sets `store: false`, and provider-side threading is not used: the client has no parameter through which a `previous_response_id` could be passed. Conversation memory is assembled from the workspace's own `messages` rows on every turn.

Context:
ADR-007 settled which API is used and that it sits behind an integration boundary. It did not settle how that boundary is implemented, nor who holds the conversation between turns. The API can thread turns itself from a stored response id, which is the shortest path to a working agent and the one most examples take.

Reason:
One endpoint does not justify a large transitive dependency on its own release cadence, and the parts actually worth controlling are ours already: the retry table, the timeout, and the mapping from provider failures onto domain errors. ADR-010 is the precedent — a retry policy is a judgement about customer-visible duplication, not a library default.

On state, the decisive question is where customer conversations live. Rebuilding the window from our own tables keeps them in one tenant-scoped, backed-up, replayable place, and makes the memory window testable with no provider at all. Provider-side threading would put the same content in a second store with different retention, outside any tenant boundary of ours, in exchange for saving a database read we are performing anyway to decide whether to answer.

Consequences:
The payload and the parser are hand-written, so an API change lands in this module: the parser ignores output item types it does not recognise rather than failing a reply, and unparseable tool arguments become an empty object for the registry to reject. No SDK conveniences are available, and streaming will need explicit work when it is wanted. Every turn resends the whole window, so token cost grows with the window rather than being amortised by the provider — that is precisely what `memory_message_limit` and `memory_token_budget` exist to bound. `store: false` is sent explicitly on every call rather than relied on as a default, because a silent change of default would begin retaining customer conversations. The retry table is deliberately the inverse of the WhatsApp client's: a duplicated inference costs tokens and reaches nobody, because the orchestrator decides what is sent.

## ADR-015 — Reliable Redis Queue, and the Provider Call Inside the Session

Date:
2026-08-21

Status:
Accepted

Decision:
Agent jobs move through three Redis lists: `agent:jobs:pending`, `agent:jobs:inflight`, and `agent:jobs:failed`. A worker reserves with a blocking `BLMOVE` into the in-flight list, removes the exact payload with `LREM` on success, and moves it to the failed list on error or on a payload it cannot decode. Payloads are compact JSON with sorted keys. The worker opens one database session per job and makes the provider call inside it.

Context:
ADR-006 chose Redis as the queue. The obvious implementation is a blocking pop, which hands the job to the worker and forgets it. If that worker then dies — a deploy, an OOM kill, a lost node — the job is gone, and what is gone is a customer waiting for a reply.

Reason:
The in-flight list is the difference between losing work silently and losing it visibly. A job a worker took and never finished is still in `agent:jobs:inflight` where it can be seen and requeued, whereas a popped job vanishes with the process that held it. Sorted-key compact encoding is not cosmetic: `LREM` matches by exact value, so the byte string a worker removes must be reproducible from the job it decoded.

Dead-lettering rather than endless retry follows from the failures being mostly deterministic. A malformed payload or a conversation that no longer exists will fail identically forever, and a job cycling forever hides the failures worth acting on.

Holding the database session across the provider call is the uncomfortable part, and it is deliberate. A connection is pinned for as long as an inference takes, which at scale is the first thing to change. It is accepted because the alternative — close the session, call the provider, reopen — takes away the one thing that makes tools useful: a tool runs inside the same transaction the reply will be written to, so what it reads and what it writes cannot disagree. The exposure is bounded by the client's 60 second timeout, and the remedy when it starts to hurt is a separate pool sized for workers, not a different transaction shape.

Consequences:
Nothing reaps the in-flight list yet, so a stalled worker leaves entries behind; `depth()` and `failed_depth()` exist for monitoring, and a reaper belongs with the Phase 8 worker service. Requeueing is an operator decision rather than an automatic one, because re-running a job produces a second reply to the customer — the job is repeatable, not idempotent. Enqueueing happens inside the web request, before its transaction commits, so a rolled-back transaction can leave a job naming a conversation that does not exist; the worker dead-letters it, which is the cheaper of the two orderings. The queue stays a thin wrapper over lists, so ADR-006's escape hatch to a real broker remains behind `AgentQueue`.

## ADR-016 — Developer Toolchain Pinned Exactly

Date:
2026-08-21

Status:
Accepted

Decision:
Pin `pytest`, `pytest-asyncio`, `pytest-cov`, `ruff`, `black` and `mypy` to exact versions in the `dev` extra, rather than to compatible ranges.

Context:
The dev extra previously carried ranges (`ruff>=0.8,<0.13`, `black>=24.10,<26`, `mypy>=1.13,<2`). CI resolves the newest version in range on every run, so the tools that decide whether the pipeline is green changed underneath the project without any commit. By the time this was noticed four independent failures had accumulated on `main`: Ruff had gained rules the code predated, Black 25 reformatted eleven files, MyPy 1.20 reported twenty-five errors, and FastAPI 0.116.2 stopped resolving `-> None` under postponed annotations and refused to build the two `204` routes, so the application would not start at all.

Reason:
A linter or formatter release is not a change to this project, and should not be able to fail this project's pipeline. Style and typing gates only mean something if the standard they enforce is fixed; a moving standard produces failures that carry no information about the commit under test. Pinning makes a toolchain upgrade a deliberate change with its own diff and its own review, which is where the churn belongs.

Consequences:
Upgrades become explicit work: bump the pin, run the gates, fix what the new version finds, commit that as its own change. The pins must be revisited periodically or they rot — a stale MyPy stops catching what a current one would. Runtime dependencies stay ranged and upper-bounded; this decision covers the tools that gate CI, not the libraries the product runs on. The FastAPI incident is a live argument for extending the same treatment to runtime dependencies, and that remains open.

**Amended.** The rot warning above proved true in the very next push: the `pytest` and `black` versions pinned here were themselves carrying advisories, and the dependency audit failed. Pinning is therefore only half a policy — the other half is a gate that notices, which is the weekly `pip-audit` run in the security workflow. Pins are safe precisely because something forces them to be revisited. See ADR-017.

## ADR-017 — Security-Critical Transitive Dependencies Are Declared Directly

Date:
2026-08-21

Status:
Accepted

Decision:
Declare Starlette as a direct dependency with an explicit lower bound, rather than accepting whatever FastAPI resolves. Treat the `pip-audit` job as the mechanism that forces every pin and floor in `pyproject.toml` to be revisited.

Context:
The dependency audit failed on the first push after the toolchain was pinned. Three packages were implicated. `pytest` and `black` were pins introduced by the pinning commit itself. The third was Starlette 0.48.0, carrying five advisories including CVE-2026-48710, and nothing in this repository named it: it arrived through FastAPI, which asks only for `starlette>=0.46.0`. An undeclared dependency has no floor anyone can raise, and Starlette is not incidental here — it is the ASGI layer serving the Meta webhook, and the application imports `Request` and the middleware base from it directly.

Reason:
A dependency the application imports is a direct dependency whether or not it is declared, and only a declared one can carry a security floor. Leaving it implicit meant the version serving customer traffic was decided by FastAPI's loosest acceptable bound. Pinning without an audit gate is worse than ranges, because a pin freezes a known-vulnerable version indefinitely and nothing complains; the audit is what converts a pin from a liability into a deliberate choice with an expiry.

Consequences:
Upgrading FastAPI now requires checking the Starlette bound too, since the two are coupled more tightly than either declares. The FastAPI bound was narrowed to a single minor (`>=0.141.1,<0.142`) because its 0.x minors have broken this project before — 0.116.2 stopped resolving `-> None` under postponed annotations and refused to build the two `204` routes. The weekly cron on the security workflow means new advisories surface without a commit, and a failing audit is the signal to bump. Any dependency whose behaviour the application relies on directly should be declared the same way when it is next touched; `anyio` and `h11` are the obvious remaining candidates, reached only through Starlette and httpx today.
