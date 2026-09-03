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
Superseded by ADR-034 (2026-08-23), which adds the encryption at rest and key
management this record made a precondition. The reasoning below is why the
column did not exist for thirteen phases, and it still explains the shape of
what replaced it.

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

## ADR-018 — Embedding Width Fixed in the Column, Not in Configuration

Date:
2026-08-21

Status:
Accepted

Decision:
Declare `document_chunks.embedding` as `vector(1536)` — the width of `text-embedding-3-small` — as a constant in the model and the migration, and request that width explicitly on every embedding call. Changing the embedding model to one of a different width is a migration.

Context:
pgvector columns carry a fixed dimension. The embedding model is already configurable (`OPENAI_EMBEDDING_MODEL`), which invites making the width configurable alongside it, and the OpenAI `text-embedding-3-*` models can be asked to truncate to an arbitrary width.

Reason:
A configurable width would be a setting that silently corrupts a knowledge base. Changing it does not re-embed anything: the existing rows keep vectors of the old width, new rows get the new one, and cosine distance between them is meaningless — so retrieval degrades quietly rather than failing. Making the width a schema fact means the database refuses the mismatch instead. Requesting the width on each call is the same argument one layer out: the provider cannot hand back a vector the column will not take.

Consequences:
Moving to a different embedding model means a migration that alters the column and a re-ingestion of every document, which is the honest cost — the vectors would have to be recomputed anyway. The client checks the returned width and fails the batch rather than writing a partial document. Two models of the same width can be swapped by configuration alone, and that is safe only because their vectors are at least comparable in shape; the documents still want re-ingesting for the results to mean anything.

## ADR-019 — Ingestion Has Its Own Queue

Date:
2026-08-21

Status:
Accepted

Decision:
Run document ingestion through a separate Redis queue (`knowledge:ingestion`) with its own worker, rather than adding a job type to the agent queue.

Context:
Both are background work driven by the same reliable-queue mechanics (ADR-015). Sharing one list and one worker pool would be less code.

Reason:
The two have different urgency and different idempotency. An agent job is a customer waiting for a reply, measured in seconds; an ingestion job is a document that will be searchable in a minute. On a shared list, one bulk upload of a hundred documents sits in front of every question asked while it drains. They also differ in what a repeat costs: re-running ingestion replaces the document's chunks and changes nothing, so requeueing is safe, while re-running an agent turn sends the customer a second reply — which is why the agent queue leaves requeueing to an operator and this one does not have to.

Consequences:
Two workers to deploy rather than one, and two dead-letter lists to watch. The queues share their encoding and reservation logic, so a fix to one applies to both. A stranded document — enqueued while Redis was unavailable — stays `PENDING` and is findable through `DocumentRepository.list_pending`; a sweeper for those belongs with the Phase 8 worker service.

## ADR-020 — One Open Lead per Customer, Enforced by a Partial Unique Index

Date:
2026-08-22

Status:
Accepted

Decision:
Allow at most one lead per contact whose status is not `won` or `lost`, enforced by a partial unique index on `(tenant_id, contact_id)`. An agent capturing details from a conversation resolves the lead from the conversation's contact rather than being given a lead identifier.

Context:
An AI agent calls `record_lead_details` whenever it learns something, which across one conversation is several times. Something has to decide whether each call opens a new opportunity or updates the existing one, and a customer who buys and returns a year later genuinely is a new opportunity.

Reason:
A service-level check would lose the race. Two webhook deliveries can be in flight at once — Meta retries, and the queue can hand the same conversation to two workers — so "look for an open lead, then create one if there is none" has a window between the two statements in which both callers find nothing. Only a constraint settles that. Making it *partial* is what keeps the rule from being wrong in the other direction: a closed lead releases the slot, so a returning customer starts a fresh record instead of having their old, closed deal reopened and overwritten, and leads entered by hand carry no contact at all and would otherwise all collide on a null.

Resolving the lead from the conversation rather than from a model-supplied identifier follows from the same reasoning one layer up. An identifier the model chooses is an identifier it can choose wrongly, and "wrongly" here reaches another customer's record. Since the tool cannot name a lead, calling it five times updates one lead five times, which makes the tool idempotent by construction rather than by convention.

Consequences:
Creating a second open lead answers 409 rather than silently splitting one opportunity's history across two records. A workspace that genuinely wants two concurrent opportunities for the same customer cannot have them today; that is a real limitation, and the alternative — an unbounded number of leads per customer with no way to tell which the agent should update — is worse. Merging duplicate leads is not implemented, because this design is what stops the duplicates arising.

## ADR-021 — Human-Entered Lead Fields Are Protected from AI Overwrite

Date:
2026-08-22

Status:
Accepted

Decision:
Record on each lead the set of fields a person has set (`human_verified_fields`). Extraction skips every field in that set, and can only write the fields in `AGENT_WRITABLE_FIELDS` — contact details and stated interest — never status, score, assignment or tags. Every change writes a `LeadActivity` row naming the actor and carrying the previous value.

Context:
Two kinds of caller write to a lead and they are not equally reliable. A person using the API states a fact: they typed the customer's name. A model infers one from a sentence written in passing, and is frequently right and occasionally confidently wrong. Without a rule, whichever wrote last wins.

Reason:
The asymmetry is the whole point: a model correcting its own earlier guess is an improvement, and a model overwriting what a colleague typed is data loss that nobody notices until the call goes to the wrong number. Storing provenance per field rather than per row is what allows both — the AI fills blanks and revises its own inferences while what a person confirmed stays put. A field a person deliberately *cleared* is protected too, because "this customer has no email" is knowledge rather than an empty slot to fill.

Keeping status and score outside the writable set is a separate judgement. Those are decisions about an opportunity, not facts a customer stated, and inferring "qualified" from one enthusiastic message would let the pipeline be reordered by the model's mood.

The activity log carries the previous value because the protection is not sufficient on its own: someone still has to be able to answer "why does this lead say the budget is half a million", and a lead row alone cannot.

Consequences:
An extra array column and an activity row per change — the log grows faster than the leads do, which is the expected cost of an audit trail. A person who typed a value wrongly must correct it themselves; the AI will not fix it for them. Extraction that is refused is recorded rather than silent, so the skip is visible in the timeline instead of looking like the model never tried.

## ADR-022 — Follow-ups Are Polled From PostgreSQL, Not Queued in Redis

Date:
2026-08-22

Status:
Accepted

Decision:
Store each follow-up as a row in `follow_ups` and have a worker poll for rows whose `scheduled_at` has passed, claiming them with `SELECT ... FOR UPDATE SKIP LOCKED`. Do not put the schedule in Redis.

Context:
Every other background job in Wasla is event-triggered and arrives through a reliable Redis queue (ADR-015, ADR-019). A follow-up is different: it is triggered by *time*, and its due moment may be days after it was scheduled. Redis can express that — a sorted set keyed on the due timestamp is the standard trick — so the consistent-looking choice was to use one.

Reason:
The follow-up has to be a durable row regardless. A person must be able to see what is scheduled, cancel it, and read afterwards why it was or was not sent; a customer's reply has to cancel it; and the whole thing must survive a restart. Once that row exists, a Redis sorted set holding the same schedule is a second source of truth, and the two drift the moment one is written without the other — a cancellation that updates the row but not the set sends a message the business explicitly stopped.

Polling costs one indexed query per interval, against a partial index covering only pending rows, which stays cheap as finished follow-ups accumulate. `SKIP LOCKED` gives the property the Redis queue was wanted for: two replicas sweeping at the same instant do not both send the same nudge, because the second steps over the rows the first has locked rather than blocking on them.

Consequences:
Precision is bounded by the poll interval — a follow-up fires within one interval of its due time rather than at it. For a nudge measured in half-hours that is not a meaningful difference, and buying exactness would mean a scheduler holding in-memory state that a restart loses. The claim is bounded per sweep so a backlog drains in batches rather than under one long-held lock. The worker is the only component in the codebase that queries across tenants; it does so through `DueFollowUpClaim`, a separate unscoped repository class named so that reaching for it is deliberate and visible in review, and every row it returns is handed to a `FollowUpService` scoped to that row's own tenant.

## ADR-023 — Media Bytes Live Behind a Storage Interface, on Local Disk for Now

Date:
2026-08-22

Status:
Accepted

Decision:
Store downloaded and sent files through a `MediaStorage` protocol, with `LocalMediaStorage` as the only implementation. Keys are produced by the store from a generated UUID under a tenant prefix, never by a caller and never from anything a customer supplied. Do not put file bytes in PostgreSQL.

Context:
Phase 9 needs somewhere to put a customer's photograph between the worker that downloads it and the request that serves it back. Three options existed: a `bytea` column, an object store, or the local filesystem. An interface with a single implementation is normally a smell, and this project's rules say not to abstract prematurely.

Reason:
PostgreSQL is out on its own merits (claude.md §26). A voice note is a megabyte and a video is fifteen; in rows they are carried by every backup, every replica and every dump, and the database is the one component that cannot be scaled by adding disks.

The interface is not premature because *where files live is a deployment decision that has already changed once and will change again*. Local disk is right for development and for a single host; object storage is right the moment there are two. Without a named boundary that change means rewriting every service that had opened a file. With one it is a new class and a changed dependency. The one implementation that exists is the one that runs today, not a placeholder.

Keys are the store's own business because they are the only thing between a store and the rest of the host's filesystem. A filename arriving from a stranger's phone is untrusted input, and `../../etc/passwd` is a request rather than an accident, so the customer's filename is recorded for display and never consulted when building a path. Keys are validated on the way out as well as in: one read back from a database row is still input, whatever wrote it.

Consequences:
The local implementation requires the API and worker containers to share a volume — one writes the file, the other serves it back — which both compose files now configure. That is a genuine single-host constraint, and it is the trigger for writing the object-store implementation rather than a detail to discover during an incident. Deleting a workspace's files is a prefix operation because the tenant id leads the key. Nothing streams: a file is read into memory whole, which is bounded by the download cap and by a separate, smaller cap on the upload endpoint, since that one is holding the bytes inside an API process rather than a worker.

## ADR-024 — Sentiment Is Read Before the Agent Replies, Not After

Date:
2026-08-22

Status:
Accepted

Decision:
Classify the newest inbound message inside the agent turn, after the agent is resolved and before any reply is composed. Store one reading per message, keyed uniquely on `message_id`. Let a reading raise a conversation's priority and never lower it. Escalate to a human only above a per-agent threshold and above a fixed confidence floor.

Context:
Phase 10 needs sentiment to do something rather than merely be recorded. Three placements were available: after the reply (cheapest to build, since the turn is untouched), in parallel with it, or before it. A fourth option — a queue of its own, as media has — was also on the table.

Reason:
Only "before" works. An escalation that lands after the reply means the agent has already answered an angry customer, and the reply cannot be recalled: it is on the customer's phone. Worse, the tools that ran during that turn have already created the lead, scheduled the follow-up or searched the knowledge base. A system that escalates correctly and one message too late helps nobody.

A separate queue was rejected because the ordering problem it would create is the one Phase 9 had to solve with a conversation lock. The agent job already exists, already arrives once per conversation, and already runs before any reply — putting the classification inside it costs one small call and no new coordination.

Priority rises and never falls because the failure modes are not symmetric. A conversation wrongly left at `urgent` is looked at by a person who finds nothing wrong. A conversation quietly demoted out of the queue somebody is working is never looked at again. The first wastes a minute; the second loses a customer. Lowering it is therefore a person's decision, made explicitly through the API.

The confidence floor exists for the same asymmetry one level down. Raising a flag on a doubtful reading costs nothing, so the flag goes up regardless. Silencing an agent on a doubtful reading leaves the customer waiting on a colleague who was told nothing, so that requires the model to be sure. The model's own confidence is weakly calibrated and is used as a floor, never as evidence.

Consequences:
Every customer message now costs one extra small inference, on the path that a customer is waiting on. That is the price of the ordering, and it is why the model is configurable separately from the answering model.

A reading is stored per message and reused, which makes a retried job free and — less obviously — stops a conversation that a colleague deliberately handed back to the AI from re-escalating on words already read.

Escalation leaves the customer with silence until a person arrives. That is deliberate for now: an agent's parting words to an angry customer are the words most likely to make things worse. A holding message needs per-workspace wording and waits for templates in Phase 11.

Existing agents escalate by default, because the migration adds `escalation_sentiment` with a server default of `angry` rather than a null. A workspace that never touches its configuration still gets an angry customer in front of a person; one that does not want that sets the column to null.

---

## ADR-025 — A Campaign Can Only Reach Someone Who Wrote First

Date:
2026-08-23

Status:
Accepted

Decision:
Build every campaign audience from contacts that already have a conversation on the sending number, minus anyone who has opted out. Provide no route that uploads phone numbers, imports a list, or otherwise creates a recipient who is not already a contact of the workspace.

Context:
Phase 11 adds broadcast messaging. The obvious shape — the one every bulk-messaging tool has — is an audience built from an uploaded CSV, because that is what a business asks for on day one. The alternative is to derive the audience from what the platform already knows about who has talked to this business.

Reason:
The upload is the feature that turns a customer engagement platform into a spam tool, and it does so without any further decision by anybody. Once a list can be pasted in, the platform's compliance story rests entirely on the promise that the business collected consent somewhere else — a promise nothing here can check, and one that WhatsApp will hold the *number* responsible for rather than the claim.

Deriving the audience from conversations makes consent structural instead of asserted. Somebody who wrote to this business chose to start a conversation with it; that is a weaker signal than a signed marketing opt-in, and it is a real one that exists in the data rather than in a policy document. It also has the property that matters operationally: a workspace cannot exceed it by accident, because there is no input that would let it.

The cost is that a business with an existing customer list cannot message it through Wasla on day one. That is the intended trade. Importing is a decision to be made deliberately later, with whatever consent evidence a compliance review demands — not a text field that ships by default.

Opt-out sits inside the same rule rather than beside it. It is part of the base population in `AudienceRepository`, not a filter a caller passes, so no future endpoint can construct an audience that omits the check. It is then re-checked at send time, because a campaign may run for hours and somebody who says stop in the middle of one must not receive the rest of it.

Consequences:
There is no bulk import, and adding one later means answering the consent question rather than adding a parser.

An audience is bounded by the workspace's own conversation history, so a new workspace's first campaign is small. That is the honest size of its permission.

`AudienceRepository` is the single place the rule lives. A second query written elsewhere would bypass it, which is why the audience is materialised through this one path and campaigns store the filter they used.

---

## ADR-026 — Campaign Rate Limits Live in the Database, Not in the Worker

Date:
2026-08-23

Status:
Accepted

Decision:
Send a campaign in batches, and after each batch write the next permitted moment onto the campaign row (`next_send_at`). Claim due campaigns with `FOR UPDATE SKIP LOCKED` and claim the recipients inside them the same way. Never sleep to pace a campaign.

Context:
A campaign must not write to ten thousand people as fast as the network allows. Meta's own throughput limit is far higher than what is safe: a number that suddenly broadcasts collects blocks, and a blocked number takes the whole business down rather than one campaign. Some mechanism has to pace the send. The obvious one is to sleep between messages in the worker.

Reason:
A sleep is wrong in three separate ways here, and each is enough on its own.

It holds the lock. The campaign row is claimed for the duration of the batch, so a worker sleeping through a minute is a worker keeping every other replica off that campaign for a minute while doing nothing.

It does not survive a restart. A process killed mid-sleep loses its knowledge of when it was allowed to resume, and the replacement starts by sending immediately — precisely at the moment a deployment is rolling, which is when several replicas are starting at once.

It does not compose across replicas. Two workers each sleeping their own way through their own idea of the rate produce twice the rate, and neither is wrong about its own arithmetic.

A timestamp on the row has none of those properties. It is durable, it is shared, and the claim query reads it, so a campaign that has used its allowance is simply not claimed until the moment arrives. The pacing then holds however many replicas are running, including zero.

`SKIP LOCKED` at both levels is what makes concurrency safe rather than merely fast. Without it on recipients, two replicas working one campaign would read the same pending rows and send the same person the same message twice — the one failure a broadcast must never have, because there is no way to take it back.

Consequences:
Precision is bounded by the poll interval: a campaign sends at or slightly under its configured rate, never above it. Under is the right direction to err.

The worker holds no campaign state at all, so scaling `WORKER_KINDS=campaign` across replicas needs no coordination beyond what PostgreSQL already provides.

The rate is per campaign rather than per number. Two campaigns running at once on the same number can therefore exceed either one's rate. That is recorded as a limit rather than solved: a per-number budget needs a shared counter, and the workspace that runs two simultaneous broadcasts on one number has made a decision this system can surface but should not silently override.

## ADR-027 — Usage Is Metered in the Transaction That Consumed It

Date:
2026-08-23

Status:
Accepted

Decision:
Record usage as append-only rows in `usage_events`, staged through the same `AsyncSession` — and therefore the same transaction — as the work being measured. No worker, no queue, no second connection, no `updated_at`. The unit of each meter is a property of its event type rather than an argument a caller passes, and exactly-once comes from the idempotency keys each metered path already has.

Context:
Usage is the input to billing and to plan limits, so two failures matter more than throughput: charging for work that never happened, and under-counting work that did. Every metered path already runs inside a transaction — the webhook's, the agent worker's, the campaign worker's — and each of those transactions can roll back after the point where the meter would fire.

The alternatives were a fire-and-forget write on its own connection, a Redis counter flushed periodically, or an event stream consumed by an aggregation worker. All three decouple the meter from the work.

Reason:
Decoupling is exactly what must not happen here. A usage row written on its own connection survives the rollback of the turn it measured: the customer never got a reply, and the bill says they did. A Redis counter loses whatever was in it when the process dies, which is under-counting with no record that it happened. An event stream has both properties and adds a component that has to be running for the bill to be right.

Sharing the caller's transaction makes the invariant structural rather than procedural: the metered work and its meter are the same commit. A rolled-back agent turn is not billed because the row went with it, and a message that committed is always counted because the row could not have been left behind.

The cost is that a metering bug can fail a request that would otherwise have succeeded. That is the right direction to fail. Staging performs no I/O — `session.add` is in-memory — so the realistic failure is a constraint violation at flush, which means the row was wrong and the alternative was a wrong bill.

Aggregation is a `GROUP BY` over an indexed range, not a maintained counter. Counters drift, and a drifted counter cannot be recomputed from anything; a sum over rows can be re-run for any window, which is what makes a disputed invoice answerable months later. When aggregation becomes the bottleneck, rollups are added *beside* the rows rather than instead of them.

Consequences:
`usage_events` becomes the largest table in the schema. Its indexes are chosen for the three queries that exist — one workspace over a window, one meter over a window, and the platform total — and the row is deliberately narrow.

There is no `updated_at`, because nothing updates a row. A correction is a new row, which is what keeps last month's figure reproducible after the fact.

Deduplication is not attempted here. Every metered path has an idempotency key upstream — the WhatsApp event id, the message row, the media row, `UNIQUE(campaign_id, contact_id)` — so a retry that skips the work also skips the meter. A retry that genuinely re-does the work is genuinely counted, because it genuinely consumed something.

Retention is not solved. Nothing sweeps old rows, and nothing should until a billing period is closed and the figures for it are stored somewhere a sweep cannot change. That belongs with Phase 13.

## ADR-028 — Analytics Are Derived From the Domain, Except What It Forgets

Date:
2026-08-23

Status:
Accepted

Decision:
Compute tenant analytics from the domain tables that already hold the facts — `messages`, `conversations`, `leads`, `lead_activities`, `message_sentiments`, `campaign_recipients` — and record analytics events only for occurrences that leave no other trace. Today that is exactly one thing: a handoff, in `analytics_events`, with the source that decided it.

Context:
The product specification lists a dozen analytics events: `message_received`, `message_sent`, `conversation_created`, `lead_created`, `lead_qualified`, `handoff`, `follow_up_sent`, `agent_response`, `customer_angry`, `campaign_sent`, `campaign_delivered`. The obvious reading is a table with a row per occurrence, written alongside the domain write.

Every one of those except the handoff is already a timestamped, tenant-scoped row. A message received is a row in `messages`. A lead qualified is a row in `lead_activities`. An angry customer is a row in `message_sentiments`, carrying the label, the score and whether it escalated. A campaign delivered is a recipient row joined to its message's status.

Reason:
A second copy of a fact is a second thing to keep true. Two writes in one transaction can still diverge — one path updated and the other not, a backfill applied to one shape, a bug fixed in one query — and when they diverge there is no way to tell which number is right, because both were derived from the same event and only one was wrong. Every count in this system would then have two possible answers.

Deriving is also *retroactive*, and that matters more than it sounds. A metric defined next month can be computed for last month, because the rows it reads have been there all along. An event stream can only answer questions somebody thought to emit an event for.

The objection to deriving is cost: aggregating raw rows is slower than reading a counter. That is true and not yet relevant. These are indexed range scans over one workspace's rows for a bounded window, and when they stop being fast enough the answer is a rollup built *from* the rows — which is only possible because the rows exist.

The handoff is the exception that proves the rule. `conversations.mode` is a current state, not a history: it cannot say when a conversation moved, how many times, or who decided. And the three causes — an agent giving up, a classifier judging a customer angry, a colleague taking over — are indistinguishable afterwards, while being the most important distinction on the dashboard. A business whose agents hand over half their conversations has a different problem from one whose staff take them over by hand.

Consequences:
`analytics_events` starts with two event types and will grow slowly, by the same test each time: does anything else record this? A member added to mirror a count that `messages` already answers is a bug, not a feature.

Analytics reads are a handful of grouped queries rather than one scan of an event table. They are written in the repository layer like every other query, and they are tenant-scoped by the same mechanism.

Metrics have to be *defined*, not merely counted, and the definitions live with the queries: average response time means the first business reply to a customer message that started a burst; AI resolution rate means conversations created in the window that were never handed to a person. Both are stated in the code and in `docs/ANALYTICS.md`, because a number whose definition is unwritten is a number two people will read differently.

`usage_events` remains a separate table with the opposite policy (ADR-027), and the difference is deliberate. Usage is billing input: it must be reproducible exactly as it was recorded, even after the domain rows it describes are edited or deleted. Analytics is reporting: it should reflect what the data says now.

## ADR-029 — Plan Limits Are Data With a Closed Vocabulary, and Absent Means Unlimited

Date:
2026-08-23

Status:
Accepted

Decision:
Store a plan's limits as JSONB on the `plans` row, keyed by a closed vocabulary (`LimitKey`), with an absent key meaning *unlimited*. Give every workspace at most one subscription, enforced by a unique index. Where a workspace has no subscription, entitle it to a configured default plan; where that plan is missing too, do not enforce limits at all and log it.

Context:
Plan limits appear in three shapes across the specification — a marketing table, a check before an action, and a figure on a settings page — and the requirement is explicit that they must not be hardcoded. The obvious implementations are a column per limit, a `plan_limits` child table, or a JSON document.

Reason:
A column per limit is the wrong unit of change. The set of things worth limiting grows with the product — documents arrived with the knowledge base, campaign messages with broadcasts — and a schema migration per pricing idea is a tax on exactly the experiments a SaaS needs to run. A child table avoids the migrations but buys a join and a second thing to keep referentially tidy for what is, in practice, a dictionary of at most a dozen small integers read as a unit.

JSONB carries the obvious risk that a typo becomes a silently missing limit. `LimitKey` is what removes it: keys are validated against the enum at the service boundary, a limit is only ever read through `Plan.limit_for`, and nothing outside the entitlement service reads the dictionary at all. A key nobody recognises therefore cannot grant an allowance; it simply is not a limit.

Absent-means-unlimited is the load-bearing convention, and the alternatives are all worse. A magic sentinel (`-1`, `999999`) is a number somebody eventually compares, sums or displays. A nullable column per limit is the column-per-limit problem with extra steps. And "unlimited" is not an edge case here — it is the definition of the Enterprise plan, so the encoding for it should be the simplest thing in the file. A malformed value is read as unlimited for the same reason: a plan edited badly must not lock a paying customer out of their own product, and zero is never what a broken row was trying to say.

One subscription per workspace is a unique index rather than a service rule because two subscriptions give two answers to "what am I allowed to do" and there is no correct way to choose between them. A workspace on bespoke terms gets its own plan row, not an override on its subscription, so there is exactly one place that answers the question.

The fallback matters more than it looks. Every workspace that existed before this phase has no subscription, and a limit check that refused them would take a working product away from paying customers on deployment day. Falling back to the configured default plan keeps limits meaningful for them. If that plan is missing as well — a fresh database, a mistyped code — limits are not enforced and a warning is logged, because a catalogue row failing to exist should not be able to take a deployment offline.

Consequences:
Adding a limit is a new `LimitKey` member, a branch in the entitlement service saying how to count it, and an edit to the plans that use it. No migration.

Limits cannot be queried in SQL as conveniently as columns — "which plans allow more than 10 agents" is a JSONB expression rather than a comparison. That query is a platform reporting question about a handful of rows, and it is not on any hot path.

Because limits are data, a plan edited in the database changes what a workspace may do immediately, with no deploy. That is the point, and it is also the risk: there is no approval workflow around a plan edit, which is acceptable while only platform staff can reach the rows, and becomes a real requirement the moment plans are editable through an API.

## ADR-030 — Limits Are Enforced Where Somebody Chooses, Never on the Inbound Path

Date:
2026-08-23

Status:
Accepted

Decision:
Enforce plan limits as a route dependency on the actions that *create* something a plan counts — an agent, a number, a colleague, a document — and inside the service for the one action whose cost depends on its size, scheduling a campaign. Check the AI allowance in the agent worker and stop quietly when it is gone. Enforce nothing on the inbound webhook path, on any read, or on a message a person sends by hand.

Context:
Once limits exist, the tempting rule is "check everywhere something is consumed". Usage is metered at nine places (ADR-027), and each is a candidate for a check. But a limit is a refusal, and a refusal has a victim: the question is always *who* is stopped, and whether they are the person with the billing problem.

Reason:
Three groups, and they need three different answers.

**Somebody in the workspace choosing to add something.** Creating an agent, connecting a number, inviting a colleague, uploading a document. The person acting is the person who can fix it, the refusal is immediate and comprehensible, and nothing is half-done. These get a hard 402. The check is a dependency in the route signature rather than a call inside the handler, exactly like the role guard: a check written in a body is a check the next handler forgets, and a declared one cannot be skipped without deleting it.

**A customer writing to the business.** The inbound webhook carries no limit check at all, and that is the most important line in this record. The words belong to a customer who owes us nothing; refusing them would lose a message a business needed. Worse, refusing means a non-2xx to Meta, which retries and eventually disables the subscription — so a billing problem would become a permanently broken integration. Inbound messages are metered and counted against the allowance; they are never rejected because of it.

**Work a worker was already asked to do.** An agent job whose workspace is out of AI requests returns without composing a reply, and does not raise. Raising would dead-letter the job, which loses the customer's turn permanently for a problem that will be fixed by a card being updated. The message is stored, the conversation is waiting for a person, and a warning is logged. Silence from the agent is the honest outcome of an exhausted allowance; a lost message is not.

Campaigns sit between the first and the third and are checked for the whole audience at once, at scheduling, in the service. The limit depends on how many people it will reach, so no static route guard can express it — and checking per recipient in the worker would refuse a broadcast halfway, leaving a workspace having written to some of its customers and not others. That is worse than refusing it outright.

Reads are never refused, including reads of usage and of the entitlements themselves. Locking somebody out of their own data over a bill is not a limit; it is a hostage, and it removes the very screens that explain the charge.

Consequences:
A workspace can exceed a period limit, and by design: the inbound messages that push it over are never refused, and a person's own reply is never blocked. The overage is visible in usage and is the platform's to price or to chase, which is a commercial decision rather than an engineering one.

A workspace that downgrades keeps every resource it already has. Its limits stop it adding more; nothing deletes an agent or disconnects a number, because a plan change is not a reason to destroy somebody's work.

The dependency guards are static, one per limit key, so a route that creates something new must declare the right one. Nothing detects a route that forgets — the same gap the role guards have — and `tests/integration/test_plan_enforcement.py` is where each is pinned.

## ADR-031 — An Invoice Is a Record, and the Provider Boundary Is One Method

Date:
2026-08-23

Status:
Accepted

Decision:
Store issued invoices with their amounts **copied**, never joined for, and never edit one after it is issued — a mistake is voided, not corrected. Record every payment attempt as its own row. Reduce the payment provider interface to a single operation, `charge`, and ship a `ManualProvider` that records what a human collected rather than pretending to collect anything.

Context:
Two designs were available for invoicing. One computes a bill on demand from the plan, the subscription and the usage rows — no invoice table, no duplication, always consistent with configuration. The other writes the figures down at the end of a period and never touches them again.

For the provider, the pull is the opposite: processors have large APIs (customers, payment methods, intents, captures, webhooks, disputes), and adopting their vocabulary is the fastest way to get billing working.

Reason:
Computing a bill on demand fails the only question anybody asks about an invoice, which is "why was I charged this in March". A figure derived from today's configuration cannot answer it: the plan may have been repriced, the workspace may have changed plans, the limits may have moved. Worse, the answer would silently *change* — a customer looking at the same invoice twice would see two numbers, and the second one would be indefensible. So amounts and the plan code are copied onto the row at issue time, and a test asserts that repricing the plan afterwards leaves the invoice alone.

Never editing follows from the same place. The customer has seen it. A bill that quietly changes is worse than one visibly withdrawn, so a mistake is voided and reissued, and a paid invoice cannot be voided at all — that is a refund, a different operation and a different conversation.

Payments are rows rather than a status on the invoice because a failed attempt is not forgotten when a later one succeeds. The history is precisely what a dispute, a chargeback and an angry email turn on, and a status field keeps only the last thing that happened.

On the provider: the moment a service knows what a payment intent is, the system belongs to that processor. So the interface is one method that takes an amount, a currency, an idempotency key and a description, and returns an outcome. Subscriptions, plans, periods and entitlements stay Wasla's, because they are what the product *means* and every processor models them slightly differently. A decline is an outcome rather than an exception, since it is an ordinary answer that produces a row and a message for the customer; only an unreachable provider raises.

`ManualProvider` is not a stub. A great deal of business-to-business SaaS is invoiced and paid by bank transfer weeks later, and this models that honestly: `charge` returns `PENDING`, and only a person who has seen the money marks the invoice paid. A provider that reported success would put a paid invoice in front of a finance team that has not paid, which is worse than no billing at all — and it is the same reason the platform overview still shows no revenue figures it cannot substantiate.

Consequences:
Usage lines on an invoice carry a quantity and **no amount**. Nothing stores a per-unit overage price, and inventing one would put a number on a bill that no pricing decision stands behind. When overage pricing exists, those lines gain amounts and nothing else changes.

`UNIQUE(tenant_id, period_start)` makes billing a period twice impossible rather than unlikely, which matters because the caller is a sweep that may run on two replicas. `UNIQUE(provider, provider_reference)` does the same for a processor's idempotency key.

Adding a real processor means writing one class and choosing it in configuration. What it cannot do is change the meaning of a subscription, because it is never asked about one.

Refunds, credits, proration and tax are all absent, and each is absent for the same reason as the rest of this record: they are decisions about money that nobody has made yet, and a system that guesses at them produces numbers a customer is asked to pay.

## ADR-032 — Rate Limits Fail Open, and Never Touch the Webhook

Date:
2026-08-23

Status:
Accepted

Decision:
Limit requests with a fixed window counted in Redis, applied as router-level dependencies. Count authentication by client address and everything a workspace does by workspace. **Allow the request when Redis is unavailable**, and apply **no limit at all** to the WhatsApp webhook.

Context:
The product has three kinds of caller with genuinely different risks: somebody trying passwords, a signed-in workspace making ordinary requests, and Meta delivering webhooks. A single limiter over all of them is either too loose to stop the first or tight enough to break the third.

Reason:
**The webhook is the important one.** Meta retries anything that is not a 2xx and eventually disables a subscription that keeps failing. A 429 there does not shed load — it loses a customer's message, and if the condition persists it removes the integration entirely. So the webhook routers carry no limiter, and a test asserts that twenty consecutive deliveries all get the same answer. The webhook is protected instead by the things that actually bound its cost: signature verification, idempotency on the event id, and doing no inference on the request path.

**Failing open is the same argument in a different place.** A limiter that refuses when Redis is down converts a cache outage into a total outage of a product whose critical path — storing an inbound message — does not need Redis at all. The exception is caught inside the limiter, so no caller has to remember.

**Authentication counts by address** because the caller has no identity yet; that is what they are trying to establish. It is the weakest identity in this system and it is still the right one, because the traffic being stopped is a script and a script has an address. It limits attempts rather than authorising anything, so trusting `X-Forwarded-For` here is a bounded risk rather than a hole.

**Workspace traffic counts by workspace, not by user.** The limit protects shared platform resources, and a workspace with fifty colleagues legitimately generates fifty times the load of one with a single person. Counting per user would let a large customer exhaust the platform while every individual stayed politely under their own limit.

A fixed window rather than a sliding one or a token bucket: both alternatives smooth bursts better and both need a sorted set per caller or a Lua script to stay atomic. A fixed window is `INCR` plus `EXPIRE`, is obviously correct under concurrency, and its worst case — twice the limit across a boundary — is not a failure mode that matters for logins or dashboard traffic.

Consequences:
Limits are attached to routers rather than routes, so `app/api/v1/__init__.py` is the one place that answers "what is limited". A router must therefore be uniformly scoped: mixing workspace routes and platform routes on one router breaks under a workspace-scoped guard, which is exactly what happened to the invoice routes and is why recording a payment and voiding a bill moved to `/platform/invoices/*` — where the authority they need was always visible in the path anyway.

A refusal carries `Retry-After`, because a client told to back off without being told for how long retries immediately. `WaslaError` grew an optional `headers` for it.

Limiting is off in the test suite by default. A limiter counting across a file makes every test in it order-dependent, and the eleventh login failing for a reason the test never mentions is a debugging session nobody should have to have. It has its own tests, which switch it on.

Platform administration is unlimited: it is a handful of staff, and a limit there would first bite during an incident, which is when it is least welcome.

## ADR-033 — The Audit Trail Copies What Was True, and the Platform Is Not Exempt

Date:
2026-08-23

Status:
Accepted

Decision:
Record deliberate acts in an append-only `audit_logs` table, staged in the same transaction as the act. Copy the actor's and target's labels onto the row rather than joining for them. Make `tenant_id` nullable so a platform action is recorded the same way a workspace one is, and record a platform action *against* a workspace in that workspace's own trail. Offer no update or delete path anywhere.

Context:
An audit log is read when somebody is asking a hostile question: who disconnected that number, who let this person in, who marked that invoice paid. The specification requires that platform actions are always logged and that the platform owner cannot bypass it (claude.md §8).

The tempting implementation is a normalised one: foreign keys to the actor and the target, joined at read time, with a generic `entity_type`/`entity_id` pair.

Reason:
Joining fails at exactly the moment the log matters. The interesting entries are about accounts that have since been deleted, workspaces that have been closed and leads that were merged away — a join produces a blank column for precisely the row somebody is asking about, and `actor_id 8f3c… did something to 91ab…` answers nothing. So the email and a human label are copied at write time, and `actor_id` is `SET NULL` rather than `CASCADE`: deleting an account must not erase what that account did, which is exactly what somebody would do if it worked.

Staging in the caller's transaction is the same argument the usage recorder makes (ADR-027), with the sign flipped. There, an over-count is the danger; here it is a *claim*: an entry that survives the rollback of the thing it describes says somebody did something they did not do, which is worse than no log because it is believed. And nothing swallows an exception on the way in — if we cannot record who disconnected the number, we do not disconnect it.

A closed `AuditAction` vocabulary rather than free text, because an audit log is read by filtering. Free-text actions become a dozen spellings of one event and a search that silently misses half of them. The vocabulary is deliberately narrow: only acts somebody could be asked about later. Reads are not audited — that would bury the entries that matter under a million that do not, and it is the wrong tool for that question anyway.

A platform action taken against a workspace is written to *that workspace's* trail, attributed to the staff member. The customer is entitled to see who marked their invoice paid; hiding it in a platform-only log would make the trail a tool for the platform's convenience rather than a record.

Consequences:
There is no route, repository method or service call that edits or deletes an entry. Handing somebody the ability to rewrite the record of what they did defeats the point, so the capability does not exist to be misused.

`audit_logs` grows without bound and nothing prunes it. That is correct for now: retention is a legal question rather than a storage one, and deleting audit history to save disk is the decision most likely to be regretted.

This is not an analytics event. `analytics_events` counts things for a dashboard and is derived from the domain where possible (ADR-028); this records deliberate acts by people, is never derived, and is kept after the thing it describes is gone.

The guard asserting every tenant-scoped table has a tenant index was refined here rather than satisfied. It demanded an index *named* `ix_<table>_tenant_id`; it now demands an index whose first column is `tenant_id`, which is the property that actually makes a workspace's rows findable — `audit_logs` leads with `(tenant_id, occurred_at)`, and adding a redundant bare index alongside it would cost every write for nothing.

## ADR-034 — Per-Workspace Credentials, Encrypted and Bound to Their Workspace

Date:
2026-08-23

Status:
Accepted (supersedes ADR-009)

Decision:
Store a workspace's own Meta token on the account row, encrypted with AES-256-GCM under a key ring, with the workspace id as additional authenticated data. Resolve the token per send: a workspace that supplied one sends as itself, one that did not sends through the platform credential. Refuse to store a credential at all when no encryption key is configured, and never fall back to the platform token when a stored credential cannot be read.

Context:
ADR-009 refused this column outright — a plaintext token puts a live, customer-visible sending capability into every backup, read replica and over-broad support query — and was explicit that lifting the ban required encryption at rest and key management first. This is that work. Until now every workspace sent through one platform Meta app, so no workspace had its own sender identity or its own rate limits.

Reason:
**GCM rather than an unauthenticated mode**, because the threat is not only reading the column but writing to it. Authenticated encryption makes a tampered ciphertext fail rather than decrypt into attacker-chosen bytes that are then used as a bearer token against Meta.

**The workspace is authenticated data.** Once a column of tokens exists, the obvious attack is not breaking the cipher — it is copying one row's ciphertext into another row. Binding the tenant id into the encryption makes those bytes useless anywhere but where they were written, and costs one string.

**A key ring rather than a key.** Rotation is the half of "encryption at rest" that gets deferred and then cannot be added, because there is nowhere to record which key encrypted what. The envelope names its key by digest — not by position, so reordering configuration cannot orphan every ciphertext — and `needs_rotation` exists so a sweep can find the stragglers even though nothing sweeps yet.

**No key configured is a supported state, storing plaintext is not.** A deployment without a key connects numbers and sends through the platform token exactly as before; an attempt to store a workspace credential is refused with an explanation. "Store it in the clear for now" is precisely how a plaintext token column comes to exist, and ADR-009 spent thirteen phases refusing it.

**An unreadable credential does not fall back.** Sending as the platform when a workspace asked to send as itself is a different act with a different sender identity, and doing it silently because a key was rotated badly is the kind of failure nobody notices until a customer does. The send fails loudly and a person re-enters the token.

Consequences:
The plaintext exists only inside a single call: it arrives in a request, is encrypted, and is discarded; it is decrypted at the moment a send needs it. No response model contains it — a caller can learn only *whether* a number has its own credential — and `ResolvedCredential.__repr__` hides it so a traceback or a debugger cannot print one.

`cryptography` is now a declared dependency rather than one that happened to be installed. An undeclared import is a deployment that works until whatever pulled it in is removed.

The guard from ADR-009 survives the change rather than being deleted with it. It asserted that no column looked like a credential; it now asserts that the only column which does is the encrypted one, so a plaintext `access_token` cannot be added later without a test failing.

Key management stops at the process boundary: keys come from configuration, which means they are in the environment of every API and worker container. That is a real limit — a KMS or a secret manager would be better — and it is the same limit `JWT_SECRET` already has, so it is not a new class of exposure.

Rotation is possible but not automated. Prepending a key makes new credentials use it; old ones keep working until somebody rewrites them, and nothing does that yet.

---

## ADR-035 — A Release Is a Digest, and CI Is the Gate Rather Than a Second Opinion

Date:
2026-08-23

Status:
Accepted

Decision:
Publish every commit on `main` to GitHub Container Registry as an image tagged `sha-<commit>`, triggered by the CI workflow *concluding successfully* rather than by the push itself. Deploy by digest, never by tag. Scan the published image after the push, not before it. Build the commit CI verified, not the branch head.

Context:
Phase 15 is the first time this project ships anything anywhere. Everything before it ran on a developer's machine or in a CI runner that threw itself away afterwards. The decisions here are cheap now and expensive to reverse once a deployment exists that people depend on.

Reason:
**`workflow_run` rather than a duplicated test job.** "Do not deploy if tests fail" can be expressed as a dependency or as a copy of the test suite inside the deploy workflow. The copy drifts: someone adds a check to CI and not to deploy, and the release path is quietly weaker than the pull-request path. Waiting on the CI workflow's conclusion means there is exactly one definition of "passing".

**The commit CI verified, not the branch head.** Between CI finishing and the publish job starting, `main` can move. Checking out `main` would build a commit no test ever saw while reporting the green tick of the one that was tested. `workflow_run.head_sha` is the commit that was actually verified.

**A digest, not a tag.** `latest` and `main` are conveniences for a human reading a registry listing. A digest names exactly one set of bytes and cannot be moved, so "roll back to what was running yesterday" is an exact instruction rather than a hope that nobody re-pushed the tag. This is also what makes the scan meaningful: the thing scanned and the thing deployed are provably the same image.

**Scan after the push, not before.** Blocking the push on a scan sounds stricter and is worse. A critical CVE published against the base image would then mean nothing can be published *at all* — including the commit that fixes it. Publishing first and failing the job second leaves the image available (for a rollback, for inspection) while making the finding visible and stopping the deploy job that depends on it. The pull-request scan in the security workflow is the one that catches this earlier, and it deliberately ignores unfixed findings so that a gate nobody can pass does not become a gate people learn to bypass.

**A missing deployment target fails the job.** A workflow that "succeeds" without touching a server is worse than one that fails, because a green tick is read as "it shipped". With no `DEPLOY_HOST` configured the job exits non-zero and names what is missing.

**Migrations are a step, not a container's startup.** `RUN_MIGRATIONS` already exists so a release applies them once instead of racing across replicas; the deploy job runs `migrate` as its own command and only then starts the new version. Rolling *back* deliberately does not run it — that is in the runbook, because `alembic downgrade` over live data drops columns.

Consequences:
The registry is GitHub's, and the credential is the workflow's own token. No registry secret exists to leak or rotate. Moving to another registry is a change to two `env` values and a login step.

Every image carries OCI labels naming its commit, version and build time, and the revision is also an environment variable inside the container — so `docker inspect` answers "what is running" without the pipeline's help, and so can a log line.

The deploy job is written and has never run against a real host. It is gated on secrets that do not exist yet, so it fails rather than pretending; what it does is documented in `docs/DEPLOYMENT.md` and what to do when it goes wrong is in `docs/RUNBOOK.md`. Both say plainly that no production deployment exists.

The workflows themselves are now under test (`tests/unit/test_delivery_pipeline.py`). YAML is the one part of this repository nothing else checks — it is not imported and not typed — and the failure mode is a broken release rather than a red build. The tests assert rules rather than contents, so renaming a step does not break them.

**TLS is documented and not shipped.** A certificate is issued to a domain this repository does not know. The nginx configuration contains the HTTP listener, the ACME challenge path served *before* the redirect (a redirect to a certificate that does not exist yet cannot be verified — that deadlock is the usual first-issuance failure), and a complete but commented TLS server block. A self-signed certificate shipped here would be worse than none: it would look like TLS while failing every client that checks.

---

## ADR-036 — Revocation Is a Version on the User, Checked on a Row Already Loaded

Date:
2026-08-23

Status:
Accepted

Decision:
Add `users.token_version`, an integer stamped into every access and refresh token as a `ver` claim and compared against the row on use. Raising it by one ends every session that person holds. Bump it when an account is disabled, when it is re-enabled, when the person revokes their sessions, and when they change their password. Do **not** introduce a session table.

Context:
Session revocation did not exist. `users.is_active` was checked on every request by `get_current_user`, but the only write to it anywhere in the application was `is_active=True` in `UserRepository.create` — so the check guarded a column no code path could change. There was no member-removal API, no password reset, and no password change. Refresh tokens live fourteen days.

Rotation was already in place and does not help: it spends the token that is *presented*, which is the victim's, never the thief's. A stolen refresh token could therefore be exchanged indefinitely, and the victim's own activity would not disturb it.

That left exactly one lever: rotating `JWT_SECRET`. It works, and it signs out every user of every tenant at once. That is an outage, not a revocation, and `docs/RUNBOOK.md` already said so.

Reason:
**The check is free, which is what makes it immediate.** `get_current_user` already loads the user row on every request to check `is_active`. Comparing an integer on a row that is already in hand costs no additional query, so revocation does not have to wait out the access token's fifteen minutes — it takes effect on the next request. Had the lookup not already existed, the honest choice would have been to accept a bounded window rather than add a per-request query solely for revocation.

**Per-user, not per-session, and that is a real limitation.** Bumping the version signs out every device that person has. "Sign the stolen laptop out and leave the phone alone" is not expressible. Getting that needs a row per refresh token, which means a write on every rotation, a cleanup job for expired rows, and a new failure mode when that table is unavailable — capability built for a device-management surface this product does not have. The threat that actually exists is "a token leaked and I need it dead now", and the coarse lever closes it completely. If per-session revocation is ever needed, this column survives beside it as the "everything" lever rather than being replaced.

**Re-enabling bumps too, and this is the part that is easy to get wrong.** A token minted before a suspension is still signed and may still be inside its lifetime. If only `disable` bumped, restoring the account would hand that token its authority back — so a disable/enable cycle would resurrect precisely the credentials the disable existed to kill. Somebody returning from suspension signs in again.

**Authority follows the object.** An account is a global identity: one person reaches every workspace they belong to through it. Disabling one is therefore a platform action, because a tenant administrator able to do it could evict somebody from workspaces that administrator has nothing to do with. What a person does to their own account — revoke my sessions, change my password — needs no administrator at all. Removing a person from *one* workspace is a different operation against a different object, and it is still missing.

**A token with no version is refused.** Tokens minted before the column existed carry no `ver` claim, so the comparison fails and they stop working. Every session open at deploy time therefore ends and everybody signs in once. That is the correct direction to fail: treating an unversioned token as current would leave exactly the tokens this mechanism exists to revoke permanently exempt from it. The migration says so in its docstring.

Consequences:
Revocation is one `UPDATE`. `POST /auth/logout-all` and `POST /auth/password` are self-service; `POST /platform/users/{id}/disable` and `/enable` are platform-authorized and audited with the resulting version in the entry, so a revocation is provable afterwards. The version is a counter rather than a secret — it discloses nothing about any token — which is why it is safe in an audit entry and in a response body.

The refresh denylist in Redis stays. It handles the ordinary rotation case within a session; this column handles the case where somebody needs a whole estate gone. Neither replaces the other, and the denylist failing open on a Redis outage no longer means revocation is impossible, because this check reads PostgreSQL.

One deploy-time cost, stated plainly: applying migration `0021` signs out every existing session.

**Password reset remains absent, and is deferred rather than faked** — see `docs/SECURITY.md`. A reset serves somebody who cannot sign in, so its token has to reach an address they control, and this repository has no email delivery of any kind. The invitation flow returns its token through the API because an administrator is already trusted to hold it; doing the same for a reset would let anybody request a token for any address and read it out of the response, which is account takeover with extra steps. A password *change* is shipped instead, because the current password is proof enough and needs nobody's help to deliver.

## ADR-037 — A Number Is Claimed by Proving Control of It, Not by Naming It

Date:
2026-08-23

Status:
Accepted

Decision:
Require a Meta access token on `POST /whatsapp/accounts` and verify it against the Graph API for the `phone_number_id` being claimed before writing anything. Take the owning business account, the display number and the verified name from Meta's answer rather than from the request. Never accept the platform credential as proof. Change the platform-wide uniqueness of `phone_number_id` from a `UNIQUE` constraint to a partial unique index over live claims, and add a `release` operation that gives a number up without deleting its history.

Context:
`phone_number_id` was unique platform-wide, and that uniqueness was the only thing standing between a workspace and somebody else's number. The connect endpoint validated shape and role and wrote the row.

A unique constraint answers "has anybody claimed this?" It does not answer "may *you* claim it?", and those are different questions. The identifier is not secret: it appears in every webhook payload, in Meta's dashboard, and in support threads. A workspace that knew a competitor's `phone_number_id` could claim it first, and from that moment every inbound message for that number resolved to the attacker's tenant — conversations, contacts, leads, the lot. That is the product's isolation boundary defeated by typing a published number, and it was reachable by any workspace administrator through ordinary use. It is the finding recorded as W-02 / M-01.

The `waba_id` on the request was worse than useless: it was stored and then used to read the account's message templates from Meta, so a workspace could name a business account it had nothing to do with and have the *platform* credential fetch that account's template list.

Reason:
**The credential is the proof, because nothing else is.** A Meta access token that can read a phone number node is a token the owning business issued; no other party can read it. So the check is: read `GET /{phone_number_id}` with the caller's token, and require the node that comes back to be the node that was asked for. Everything else — a challenge code sent to the number, a DNS record, a support ticket — is either something Meta does not offer or something that costs a person a day.

**The platform token can never be the proof, and removing that path was the point.** It can read every number the platform is connected to, so a claim verified with it would succeed for every workspace and prove nothing about any of them. The connect service is therefore built with a verifier that holds no credential of its own and has no access to settings; the bypass is closed structurally rather than by a condition somebody could invert later.

**Nothing user-supplied survives as an identifier.** The business account, display number and verified name are overwritten with Meta's answer. A supplied `waba_id` is treated as an assertion to check — a mismatch is a refusal, not a correction, because a mismatch means the person connecting believes something untrue about where the number sits.

**Every failure is the same failure.** A wrong id, a revoked token, a Graph outage, a malformed reply and a timeout all raise one error with one message. Distinguishing them would turn the endpoint into an oracle for mapping which numbers exist and which credentials reach them. Meta's own error text is logged with its numeric code and never returned: provider error strings quote the request back, and this request carries a live credential.

**A verification that did not complete did not succeed.** The timeout case is the one it would be most tempting to answer optimistically, and answering it optimistically would make an outage at Meta into an open claim window.

**Proof and storage are separate questions with separate answers.** Verification needs the plaintext for the length of one call; storing it needs a configured encryption key (ADR-034). A deployment without a key still connects numbers — proof happens, the plaintext is discarded, and sending falls back to the platform credential as before. Refusing to store a secret we do not need to keep is not a reason to refuse the claim.

**Uniqueness became a partial index so that a number can move.** A plain `UNIQUE(phone_number_id)` forces a choice between never letting a number change hands and deleting the row — which cascades to every conversation and message the workspace ever had on it. Neither is acceptable. `released_at IS NULL` in the index predicate means a released row keeps its history while no longer occupying the number, and the same column takes it out of inbound resolution, out of `is_active`, and out of the plan's number count.

**Releasing is not reversible from inside.** Taking a number back means proving control of it again, at the bar anybody else has to clear. Otherwise "release" becomes a way to hold a number in reserve without holding it.

**The read check stays, and the index is the guarantee.** Two claims arriving together both see the number free, both insert, and one loses at flush. That loss is translated to the same 409 the read produces, on the same index, so the racing callers get one success and one conflict in either order — rather than a 500 for a situation that is neither internal nor an error.

Consequences:
Connecting a number now requires a workspace access token with permission to read it, and the API contract changed: `access_token` is required, `waba_id` is optional and checked, `display_phone_number` is no longer accepted at all. `ownership_verified_at` and `verified_name` are recorded on the row and returned by the API.

Rows claimed before this existed have a null `ownership_verified_at`. They are left alone rather than back-dated, because that null is exactly the list an operator needs in order to re-verify them, and inventing a timestamp would erase it. They are not refused at send time: breaking every existing deployment's traffic to close a claim-time hole would be a worse outage than the hole.

A deployment whose verifier cannot be constructed answers 503 to a connect rather than accepting an unproven claim.

## ADR-038 — Membership Revocation Is a Status, Enforced Where the Membership Is Loaded

Date:
2026-08-23

Status:
Accepted

Decision:
Add `memberships.status` with `revoked_at` and `revoked_by_id`. Filter every membership read to active rows by default, with two explicitly named exceptions for administration. Enforce revocation in `get_active_workspace`, which every workspace-scoped route already resolves. Do not delete the row, and do not touch `users.token_version`.

Context:
A workspace could invite people and could not remove them. `memberships` had no status column and there was no members router at all. Since ADR-036 platform staff could disable a whole *account* and a person could end their own sessions, but a workspace owner had no way to withdraw one colleague's access to one workspace — the ordinary case, and the one that comes up the day somebody leaves.

Reason:
**A status, not a delete.** Deleting the row works and takes three things with it: who removed whom and when, the difference between a re-invitation and a first invitation, and any way back from a mistake. A revoked membership is kept and ignored.

**Not a token bump.** Raising `token_version` would end every session that person holds — including the ones for other workspaces they belong to, and their own account. Being removed from one company is not a reason to be signed out of another. The status is per-membership, so the blast radius matches the act.

**One enforcement point, and it was already there.** `get_active_workspace` re-reads the membership on every request rather than trusting the token, so filtering that read to active rows makes revocation take effect on the next request with no new query, no token operation and nothing to expire. The dependency graph was walked mechanically to confirm that every workspace-scoped route resolves it; the test that proves it walks the graph too, so a route added later is covered without anybody remembering.

**The default read hides revoked rows; the exceptions are named.** `get_for_user` and `list_members` are the access decisions and see only active rows. `get_any_for_user` and `list_members(include_revoked=True)` exist for administration and readmission and are named so they cannot be reached for by accident.

**The rules are about not stranding a workspace.** Leaving needs no permission — a member may remove themselves. Removing somebody else needs owner or admin, or the role boundary is decorative. Only an owner may remove an owner, or an administrator promotes themselves by subtraction. The last active owner cannot be removed, by anybody including themselves: a workspace with no owner has nobody who can invite one, it is not recoverable from inside, and the person who did it usually did not mean to.

**Readmission reuses the row.** `UNIQUE(user_id, tenant_id)` requires it, and a second grant would give authorization two rows to rank. Both the invitation path and the explicit reinstate endpoint reactivate the existing membership; the role on the new invitation wins, because whoever issued it decided what the person is coming back as.

**A revoked member does not occupy a seat.** The plan's team-member count reads active memberships. Counting revoked ones would make removal a one-way door: a workspace on a two-seat plan that removed a colleague could never hire a replacement, and the only fix would be paying for capacity nobody is using.

**The removal route is not behind the admin guard, deliberately.** A dependency is evaluated before the path parameter is bound, so it cannot tell "remove my colleague" from "leave". The role rules therefore live in the service, where the target is known, and the route carries only the workspace guard.

Consequences:
`GET /workspace/members`, `DELETE /workspace/members/{user_id}` and `POST /workspace/members/{user_id}/reinstate`. Removals and departures are separate audit actions, because "who threw them out" and "they walked" are different answers to the same question.

Revocation is immediate and scoped: the person keeps their account, their other workspaces and their sessions. What they lose is one workspace, at the next request.

## ADR-039 — A Replayed Refresh Token Tears Down the Whole Session Estate

Date:
2026-08-23

Status:
Accepted

Decision:
Spend a refresh token with a single atomic `SET NX` rather than a denylist read followed by a write. Losing that race *is* the detection: raise `users.token_version` with one `UPDATE ... RETURNING`, record a `refresh_token_reused` audit entry, commit that before raising, and return the ordinary credential failure.

Context:
Reuse was detected and the presented token refused — and nothing else happened. A thief who had refreshed once held an independent chain that the victim's own activity never disturbed, so the refusal punished whichever party arrived second, usually the victim. ADR-036 built the lever that kills a whole estate; it was simply not pulled here. Recorded as W-10.

Reason:
**Detection depends on atomicity, and the read-then-write version cannot detect the thing it exists for.** Two requests carrying the same token both read "unspent", both write, and both are issued a fresh pair. That interleaving is precisely what a stolen token used alongside the real one looks like. `SET NX` lets exactly one caller win no matter how the two requests interleave, and whether the key already existed is the answer.

**The response has to be heavy, because a light one leaves the thief holding a live chain.** Rotation spends only the copy that is presented. Raising the version invalidates every access and refresh token the account holds, so both parties are signed out; the real person signs in again with a password the thief does not have, and the thief has nothing. There is no way from inside the request to tell a leak from a client bug, and the cost of being wrong is one sign-in.

**The teardown commits before the refusal is raised.** The caller raises immediately afterwards and an exception rolls the request's transaction back, so a revocation staged the ordinary way would be undone by the very refusal that accompanies it — leaving an audit entry describing something that did not happen and an estate still live.

**The increment is a single statement.** `token_version += 1` in Python reads a value and writes it back, so simultaneous teardowns would collapse into one and could leave the account on a version an outstanding token still matches. `UPDATE ... RETURNING` makes concurrent replays compose.

**A version-stale token is not a replay.** It is a token that was valid and has since been invalidated wholesale — by a password change, a sign-out-everywhere, or an earlier teardown. Refusing is enough; bumping again would punish the holder of a merely old session for something already handled.

**Signature verification happens first.** An unsigned string never reaches the teardown, so this cannot be turned into a denial-of-service against any account whose id an attacker can guess.

**The refusal says nothing.** A caller replaying a token learns only that it did not work. Naming the teardown would tell a thief to move faster. Nothing here logs or records token material: the audit entry names the account and the new version, and the token that was replayed is identified nowhere, because an audit log is read by people and a log of credentials is a second copy of them.

Consequences:
Logging out and then refreshing with the same token is treated as a replay, because it is one by the same definition — the token is on the denylist. The account's other sessions end. That is the right answer for a client that is either confused or not the client.

`RefreshTokenStore.spend` is the refresh path's primitive; `revoke` remains for logout, where "it was already revoked" is not an error and there is nothing to detect. `is_revoked` stays as a read for callers that only want to know.

The audit entry is platform-level, with no tenant: an account is a global identity, and a leak is not one workspace's business.

## ADR-040 — A Degraded Dependency Degrades the Limit, It Does Not Remove It

Date:
2026-08-23

Status:
Accepted

Decision:
Split rate-limit policies by what they protect. A limit on shared capacity keeps failing open when Redis is unreachable. A limit in front of a credential falls back to a bounded process-local counter. Pin every outbound HTTP connection to an address that was validated, rather than validating a name and connecting to it. Rate-limit `POST /auth/logout` instead of authenticating it. Accept registration enumeration as a bounded, documented leak.

Context:
Four of the residual findings from the previous pass turned out to share one question — *what should happen when the control cannot be applied?* — and the honest answer differs by control, so they are recorded together.

**DNS rebinding was real, not theoretical.** `validate_outbound_url` resolved a host, refused private addresses, and then handed the *name* to httpx, which resolved it again when it opened the socket. Reproduced against this codebase with a resolver answering public once and loopback afterwards: the validator allowed the URL and the connection returned the body of a service on 127.0.0.1.

**The rate limiter failed open on every Redis failure** — connection refused, timeout and authentication failure alike, all measured. That included the two policies in front of `/auth/login`, which are the only anti-automation controls the product has.

**`/auth/logout` was unauthenticated and unlimited.** **Registration answers 409 for a taken address**, which is an enumeration oracle.

Reason:
**A name is not a destination, so the connection must be pinned.** Validating a hostname proves something about a lookup, not about a socket. `GuardedTransport` resolves once, judges every address the name answers with, and rewrites the request to an address literal — which anyio connects to directly. The decisive property is not that a rebind is refused but that **there is no second resolution to poison**, and that is what the regression test asserts: zero resolutions at the connection layer.

**Pinning the route must not weaken the identity check.** The `Host` header keeps the original authority and `sni_hostname` keeps the original server name, so the certificate is still verified against the host the caller asked for. A transport that connected to an IP and verified against that IP would have traded one hole for a larger one.

**Every outbound client is guarded, not only the one that needs it.** Only the WhatsApp media fetch uses a URL this application did not build. The others are guarded anyway so that "which of our clients can be aimed at the deployment network?" has the answer "none" rather than a list that goes stale the first time somebody adds an integration.

**Fail-open and fail-closed are both wrong answers to "Redis is down"; the right one depends on the control.** A workspace throughput limit protects shared capacity, and refusing a paying customer's colleagues in order to protect capacity that is not currently contended *is* the outage. A login limit protects a credential, and allowing it means unlimited password attempts for the duration. Failing closed on the credential path was considered and rejected twice over: it makes signing in impossible whenever the cache is down, and it is *attacker-triggerable* — anyone able to degrade Redis would convert that into a denial of service against authentication.

**So the credential policies count in-process instead.** It is not a distributed limit and does not pretend to be: with N API processes an attacker gets N budgets. What it does is turn "unlimited until somebody notices" into a small bounded number, and it can never cause an outage of its own because it refuses nothing the policy would not already refuse. The store is capacity-bounded and prunes expired windows before live ones, so it cannot become a memory-exhaustion primitive.

**Refresh-token spending stays fail-closed, and that asymmetry is the point.** Reuse detection *is* the atomic write, so when Redis cannot answer, whether a token has been spent is unknowable — and issuing a fresh pair on an unknown is precisely the case ADR-039 exists to catch. It raises, the request becomes a 5xx, and nothing is issued.

**Logout is limited rather than authenticated.** Requiring an access token would break it exactly when it is used: the access token has expired, which is why somebody is signing out. And it would add nothing against the adversary it appears to guard — somebody holding a victim's refresh token can exchange it for a live session, which is strictly worse than revoking it. What was actually missing is a budget, because the endpoint verifies a JWT signature for any caller.

**Registration enumeration is accepted, and merging the error messages was rejected as theatre.** The attacker chooses the workspace slug, so a unique slug makes a 409 mean "that address exists" whatever the wording says — while a merged message would leave a real person unable to tell which of their two fields was wrong. The only real fix is to stop creating the account synchronously and confirm through the address instead, which needs a delivery channel this deployment does not have (see ADR-036 on password reset, for the same reason). Security theatre that costs usability is worse than an honest bounded leak.

Consequences:
The exposure that remains on registration is bounded by the client-address limit, and since this ADR that bound survives a Redis outage too. It discloses account *existence* and nothing else — no tenant data, no identifiers — and a test pins the blast radius so it cannot silently widen.

Login and invitation acceptance were measured and are not enumerable: identical status, code and message, and a timing gap of 0.76 ms against a 58 ms verification, because a miss spends the same Argon2 work against a dummy hash.

`docs/SECURITY.md` carries the final policy table. The one operational note is that the local fallback is per process, so the effective budget during a Redis outage scales with the number of API processes — deliberate, and preferable to either alternative.

## ADR-041 — A Number Already Held Is Proven In Place, Never By Giving It Up

Date:
2026-08-23

Status:
Accepted

Decision:
Add `POST /whatsapp/accounts/{id}/verify`, which proves control of a number the workspace already holds and stamps `ownership_verified_at`. The number comes from the row rather than from the request. Expose `ownership_verified` on the API. Do not refuse unverified numbers at send time.

Context:
ADR-037 made a claim on a number require proof, and migration 0022 left every row claimed before it with `ownership_verified_at IS NULL`. Those rows were deliberately not back-dated — a manufactured timestamp would erase exactly the list an operator needs — and deliberately not refused, because breaking every existing deployment's traffic to close a claim-time hole is the worse outage.

Walking every path such a row can take turned up the gap. It can send, it can receive, and no other workspace can claim it — all correct. But `connect` refuses a number that is already claimed, so **there was no way to attach proof to it at all**. The only route was to release the number and claim it again, which frees it to the entire platform in between and hands anybody watching a race worth running.

That is the worst shape a migration story can have: the safe-looking action is the dangerous one, and the administrator doing the right thing is the one exposed.

Reason:
**The number is not a parameter, and that is what keeps this from being a second way in.** It is read from the row being verified, so an administrator proving control of a number they hold can never move a claim the way `connect` grants one. A `verify` that accepted a `phone_number_id` would be `connect` with the uniqueness check removed.

**Meta's answer overwrites what is stored, exactly as at claim time.** For a legacy row the business account was typed in when nothing checked it, so Meta's reply is the first trustworthy value that row has ever had. The audit entry records the previous value and whether it changed — on a legacy row that usually means the typed value was simply wrong; on an already-proven one it means the number moved, which somebody should know about.

**The stored business account is not passed as an assertion to check.** Doing so would refuse precisely the rows this exists to rescue.

**Released numbers cannot be verified.** Proof on a row that no longer entitles the workspace to anything is meaningless, and if somebody else now holds the number it would be a claim about their traffic.

**It is not only a migration tool.** Re-proving is also how an operator establishes that a number they still hold is still theirs at Meta, and it is the only path by which a legacy row can acquire a stored credential — there is no update-credential endpoint, and `connect` refuses an already-claimed number.

**The state is exposed as a boolean, not left as a null timestamp.** The security state of a number is not something an operator should have to deduce, and the set of unverified rows is the migration list.

Consequences:
An unverified number keeps working. That is the deliberate half, and a test pins it so nobody "fixes" it into an outage: the migration path is re-verification, not amputation.

Ownership is still proven at a point in time rather than continuously. A number that moves at Meta after the fact is not noticed, and re-verification on a schedule remains the obvious next step — it is now cheap to build, because the mechanism exists and only the trigger is missing.

## ADR-042 — Email Is an Outbox Row, Delivered At Least Once, and Events Are Believed About Nothing But Delivery

Date:
2026-08-24

Status:
Accepted

Decision:
Send transactional email through a provider abstraction (`EmailProvider`) with Resend behind it, spoken to over plain HTTPS with no SDK. Never send inside a request or a domain transaction: the action that decides an email should exist writes a row to `email_messages` on its own session, and a separate email worker claims and delivers it. Delivery is **at least once**, stated rather than discovered. Verify Resend's Svix-signed delivery events, and let a verified event change exactly two things — the status of the row it names, and the suppression of the address *that row* recorded. Store password reset tokens only as SHA-256 hashes. Do not implement email verification.

Context:
Four things needed email before this existed: invitations carried a token nobody could deliver, password reset was deferred in `docs/SECURITY.md` for exactly that reason, account security changes told the audit trail but never the person, and billing events reached no one.

The obvious implementation — call the provider from the handler — fails in both directions. A provider outage becomes a failed invitation. A transaction that rolls back after the call has already sent the mail, and mail cannot be rolled back.

Reason:
**The outbox row commits with the action or not at all.** That is the whole guarantee, and it is why `EmailOutbox.enqueue` takes the caller's session and writes nothing else. An invitation that rolled back was never announced; an announced invitation exists.

**The idempotency key is a unique constraint, not a convention.** Every business email is keyed to the domain row that caused it — `invitation:{id}`, `security-password_changed:{user}:{version}`. Two racing callers both see no row, and the constraint rather than the check decides which one wins.

**At-least-once, and the window is one message.** Nothing lets PostgreSQL and a provider commit together, so a duplicate is possible by construction. The claim is committed *before* any network call, and each message is then delivered in its own transaction — so a worker killed mid-batch re-sends one message rather than the batch. Exactly-once is not on offer and is not claimed anywhere.

**A verified webhook proves the delivery, not the payload.** This is the load-bearing distinction. An address inside a bounce event could be anybody's, including a mailbox an attacker wants this platform to stop writing to. So the only field taken from an event is the provider's message id, looked up against a row we issued; the address suppressed is the one *our* row recorded. A forged event about a stranger's mailbox suppresses nothing.

**Suppression is a mail-delivery fact and nothing else.** It never touches `is_active`, never bumps `token_version`, and never denies a sign-in. An account that could be disabled by bouncing its mail would be an account anybody could disable.

**Opens and clicks are dropped rather than stored.** An image proxy fetches pixels and a scanner follows links, so neither is evidence a person read anything — and anything stored is eventually treated as proof.

**No SDK.** One endpoint and one JSON POST does not justify a supply-chain entry the scanner must watch (ADR-017's reasoning, applied in the other direction). The API key appears only in an `Authorization` header, and provider error bodies are truncated before they reach a log or a row, because provider errors quote the credentialed request back.

**Every emailed link is `APP_PUBLIC_URL` plus a literal path.** No variable is ever a URL, so no template value can redirect a link; the origin is configuration and never derived from a request `Host`. The scheme is checked against an allowlist at startup, because whatever it holds is what a recipient clicks.

**Email verification is deliberately not implemented.** Nothing in the authorization model reads a verified flag: a membership decides what a user may do, an invitation proves inbox control before it grants anything, and a reset proves it again. Adding a flag no decision consults would be ceremony. The residual risk is account squatting — registering an address one does not own — and the mitigation is that a reset by the real owner bumps `token_version`, ending every session the squatter holds. The day an unverified address grants a capability, this decision has to be revisited, and that is the trigger to watch for.

Consequences:
Duplicate mail is possible and accepted. Every template is written so a second copy is an annoyance rather than a harm, and no template says anything that is only true once.

Production fails closed on a half-configured email setup: no sender, no public URL, an unusable sender address, a dangerous scheme, the fake provider, or a missing webhook secret each refuse the boot. A deployment with `EMAIL_ENABLED=false` sends nothing at all — and password reset silently does nothing, which is the cost of the no-op and is documented in `docs/EMAIL.md` rather than hidden.

Bounce and complaint handling depends on the webhook actually being configured at Resend. Until it is, suppression never populates and the platform keeps writing to dead addresses — which is why the secret is required in production rather than optional.

The provider boundary is one method. Swapping Resend for another provider is a new adapter and a configuration value; nothing in the services, templates or outbox names a provider.

## ADR-043 — Email Verification Is a Six-Digit Code That Proves an Inbox and Grants Nothing

Date:
2026-08-27

Status:
Accepted. Supersedes the "do not implement email verification" clause of ADR-042; every other part of ADR-042 stands.

Decision:
Prove control of the address on an account with a six-digit code, delivered through the existing outbox and Resend adapter, stored only as an Argon2 verifier, single-use, superseded on reissue, capped at a configurable number of attempts, and expiring in ten minutes by default. Record the result in `users.email_verified_at` as a nullable timestamp. Make **both** endpoints authenticated and act only on the calling account. Let verification grant, revoke and gate **nothing**.

Context:
ADR-042 decided against verification and named the trigger to revisit it: "the day an unverified address grants a capability". That day has not arrived — nothing in the authorization model reads a verified flag, and this ADR does not add one. What arrived instead is the weaker but real problem ADR-042 also named: **account squatting**. Registration is self-service and takes any syntactically valid address, so an account can exist on an address its holder cannot read. Nothing detected that, and nothing recorded which accounts were affected.

The mitigation ADR-042 relied on — a reset by the real owner bumps `token_version` and ends the squatter's sessions — is real and remains true. It is also entirely reactive: it fires only if the real owner happens to try. A verified-at column is what lets the question "which accounts have proven their address" be asked at all, and that is worth building before, not after, a product rule needs the answer.

Reason:
**A code, not a link, and this is the one place this repository diverges from its own habit.** Every other secret-bearing template builds a URL from `APP_PUBLIC_URL` plus a literal path. A verification link would be a fourth. It was rejected because a code in a URL is a code in browser history, in a `Referer` header, and in whatever proxy logged the request — and because a link verifies whoever *clicks* it, which on a shared or forwarded mailbox is not necessarily the account holder. A code has to be carried back into an authenticated session by hand, which binds the proof to the person holding the account rather than to the person holding the mail.

**Argon2, where the reset token uses SHA-256.** The reset token is 256 bits of randomness: there is nothing to brute-force, and a slow hash would only tax the lookup. Six digits inverts every part of that. A million candidates is not a keyspace, it is a list — a leaked table of SHA-256 digests would be a leaked table of live codes. Argon2 makes each candidate cost real work. The price is that a code cannot be a lookup key, since every hash is salted; the challenge is found by account instead, which is the right shape anyway because verification acts on the authenticated caller.

**Both endpoints are authenticated, which removes the enumeration surface instead of mitigating it.** An unauthenticated send endpoint would have to answer identically for an unknown address, a registered unverified one and an already-verified one — achievable, and permanently one careless change away from becoming an oracle. There is no such endpoint. Neither route accepts an address or an account id: the recipient is the session's own row. There is nothing to probe, and no way to make the platform mail a stranger.

This is possible only because registration already returns a session. An account is authenticated from the moment it exists, so requiring a session to verify locks nobody out. A product that gated sign-in on verification could not make this choice, which is a second reason not to gate sign-in on verification.

**Three independent bounds on guessing, because one is not enough.** The keyspace is a million values; `attempts` is capped per challenge, after which the challenge is dead even for the correct code; and the endpoint is limited to ten attempts per fifteen minutes per account. The attempt is counted *before* the code is compared, so concurrent guesses cannot slip between a read and an increment, and the consuming UPDATE re-checks every precondition — including the ceiling — because the Argon2 comparison happens in Python and the row can move underneath it.

**Keying the limits by account is safe only because neither limit locks anything.** A per-account budget that could be spent by a stranger would be an account lockout anybody could trigger. Nothing here can be: the routes require a session for the account being limited, so only the account holder can spend its budget, and the budget refuses for a window rather than ending anything.

**A challenge records the address it was issued for.** Binding by `user_id` alone leaves a code valid across an email change, which is a genuine bypass: request a code at an address you control, change the account's address to somebody else's, submit the code, and the account claims a verified address its owner never proved. There is no email-change flow in this repository today — this is defence for the one that will exist, enforced by the data rather than by whoever writes it remembering to invalidate anything.

**The plaintext code lives in the outbox context, and that is stated rather than hidden.** The worker renders the message, so the code has to reach it; this is the same arrangement the reset link already uses, and the same bound applies — terminal transitions clear the context, so the exposure is the life of the send rather than the life of the row. No endpoint reads that table, and no log line carries the value.

**Redis degradation follows ADR-040 unchanged.** Both policies stand in front of a guessable secret, so both carry the process-local fallback. An outage makes the limit weaker; it never makes it absent, and it never makes verification impossible — fail-closed here would be attacker-triggerable by anyone who can degrade the cache.

**Verification grants nothing, and that is a decision rather than an omission.** No route reads `email_verified_at`, no permission depends on it, no entitlement consults it. Workspace access is a membership row, platform authority is `platform_role`, and plan limits are a subscription; a verified address is an input to none of them. Wiring it into authorization now would invent a product rule nobody has asked for and would lock out every account created before the column existed. A test asserts that an unverified account can use the application, so the property is enforced rather than merely intended.

Consequences:
Every account that predates this migration is unverified, and unverified is an ordinary state. There is no backfill, because backfilling them as verified would record a proof that never happened.

The audit vocabulary gains three actions. Unlike password reset — where only completion is recorded, because the request is unauthenticated and would let anyone write into a stranger's trail — the request half is audited here, since sending requires a session. Failures share one action with the reason in `meta`, because "why did verification not work" is one question and a burst of them against one account is the signal worth alerting on whatever the reason says.

**Every refusal commits before it raises**, and this turned out to be load-bearing rather than tidy. `confirm` raises on failure, and an exception unwinds the request's transaction — so a failure recorded the ordinary way is discarded by the very refusal that records it. The consequence is not a missing log line: `attempts` is what ends a challenge, so a counter that rolls back is an attempt cap that does not exist, leaving guessing bounded only by the rate limit. It was found by driving a container over HTTP (seven wrong codes, `attempts` still zero) and was invisible to every service-level test, because those run on a session nobody rolls back. Rate-limit refusals go through the same path.

Delivery is at-least-once, so somebody may receive the same code twice. That is harmless — it is one challenge — and the template says nothing that is only true once.

Nothing sweeps dead challenges. They are small, they carry no usable secret once superseded, and a cleanup job would be operational work this repository does not otherwise have.

Delivery through Resend was observed on 2026-08-27 rather than assumed: a code was mailed to a real mailbox, reported `delivered` by Resend's API, and accepted by the endpoint. The send half of ADR-042 is therefore no longer a claim about code. The *receive* half still is — no webhook event has ever arrived from Resend's infrastructure, so the trust boundary this ADR inherits remains exercised only against synthesised payloads.

## ADR-044 — Money Arrives Through a Hosted Checkout and Is Believed Only From a Signed Callback

Date:
2026-08-27

Status:
Accepted. Extends ADR-031's provider boundary rather than replacing it; `ManualProvider` is untouched and remains the default.

Decision:
Collect card payments through Paymob's Intention API and Unified Checkout. Add a second provider protocol, `CheckoutProvider`, alongside the existing `PaymentProvider` rather than widening it. Settle an invoice only from a callback whose HMAC verifies, never from the customer's redirect. Make idempotency a unique constraint on `payment_events` rather than a check. Keep `manual` the default provider, so a deployment that configures nothing bills exactly as it did before.

Context:
The billing domain was complete and had never taken a payment. `PaymentProvider` existed with one implementation — `ManualProvider`, which records what is owed and waits for a person to confirm a bank transfer — and `Invoice`, `Payment`, `Subscription` and `Entitlement` were all wired to it. What was missing was a way for a customer to pay by card without anybody being involved.

Two defects surfaced while reading that domain, and both are fixed in the commit before this one because payment integration makes them worse rather than because they were found here: a cancelled subscription still granted its plan's entitlements, and a private plan could be self-selected by posting its code. The first decides what somebody keeps after they stop paying; the second decides what they can put themselves on before they pay at all.

Reason:
**A hosted checkout is a different shape from a charge, so it gets a different protocol.** `PaymentProvider.charge` models a pull: hand a processor an amount, get an outcome in the same call. That is right for a stored card and for the manual provider. A hosted checkout inverts it — nothing is collected during the call we make, and the answer arrives later on a connection the provider opens to us. Forcing it into `charge` would mean returning `PENDING`, a value meaning "ask again later" with no later to ask in. `ManualProvider` cannot host a checkout and should not have to raise `NotImplementedError` to say so, so a provider implements whichever shapes it actually has.

**Redirection rather than an embedded form.** Wasla is a backend; there is deliberately no frontend to embed a card field into, and `docs/PRODUCT.md` keeps it that way. Redirecting also means no card data ever touches this infrastructure: the callback carries the last four digits Paymob puts in `source_data.pan` and nothing else, and even those are not persisted. Pixel would buy a smoother checkout at the price of a PCI surface this product has no reason to take on.

**The browser is never believed.** The request names a plan code; the price, the currency and the workspace come from the database and the authenticated session. Paymob also redirects the customer back with the transaction in the query string, and that redirect settles nothing — anybody can visit a URL. There is deliberately no endpoint that reads it. The server-to-server callback is the authoritative signal, and four refusals stand between a verified one and a paid invoice: it must be new, name a payment we issued by a reference we generated, belong to the workspace that owns that payment, and report the amount and currency we asked for.

**Idempotency is a constraint, not a check.** Every processor retries a callback it did not get a 2xx for, and processing a retry twice settles an invoice twice and extends a billing period twice. `payment_events` carries `UNIQUE(provider, provider_event_id)` and the *insert is the claim*: whoever writes the row owns the event, whoever collides knows somebody already does, and there is no window between a check and a write because there is no check. The id is Paymob's transaction id, which is stable across retries of one notification and different for a later refund — so a refund is a new event rather than a duplicate of the payment it reverses. Hashing the body was rejected: a payload differing by one whitespace character would hash differently and be processed again.

**The signature is pinned to the vendor's own worked example.** A signature test that signs with our function and verifies with our function passes for any consistent field order, including a wrong one, and a wrong order is not discovered until a live callback fails — at which point the tempting fix is to stop verifying. Paymob publishes a sample transaction and the exact concatenated string it produces; that string is asserted verbatim. Twenty fields, SHA-512, hex, booleans spelled as JSON spells them, compared with `hmac.compare_digest`.

**Fail closed everywhere it matters.** A deployment with `BILLING_PROVIDER=paymob` and any credential missing refuses to boot — in every environment, not only production, because a staging deployment taking real payments with no HMAC secret would answer 503 to every callback while transactions completed at Paymob. A callback arriving when no provider is configured gets 503 rather than 200, so the provider retries: answering 200 would report a payment as recorded when nothing was, and it would never be sent again. That is the worst failure this subsystem has, because the customer has already been charged.

Consequences:
`manual` remains the default, so nothing changes for a deployment that does not configure Paymob — including every test and every local run. The checkout endpoint refuses in that state rather than the dependency failing to resolve; reading an invoice must not 404 because nobody configured a processor.

Recurring billing is **not** implemented. Paymob documents a Subscription API and card tokenisation, and whether either is available depends on merchant configuration that cannot be inspected without an account. A customer today pays per invoice through a checkout; nothing renews itself. That is written down in `docs/BILLING.md` rather than approximated.

Refunds are **not** implemented, and were not before this. Paymob documents Refund, Void and Capture endpoints; Wasla has `PaymentStatus.REFUNDED` and no flow that produces it, and inventing a refund subsystem to match a provider capability would be building for a product decision nobody has made. The provider seam is where one goes.

The `payments` table gains one column, `provider_intent_reference`, holding the provider's id for an *intended* payment — distinct from `provider_reference`, which is the transaction that settled it and does not exist when a customer is sent to a payment page. It is nullable with no backfill, because no payment predating hosted checkout ever had one.

Nothing has been verified against a live Paymob account. Every claim here is a claim about code checked against published documentation, and the HTTP boundary is exercised against a mock transport. The first real transaction still has to be walked through with merchant credentials.

## ADR-045 — A Payment May Only Move Where the Rules Allow, and a Reversal Is a Second Event

Date:
2026-08-29

Status:
Accepted. Completes ADR-044 rather than replacing it; the provider boundary, the redirection model and the callback-as-authority rule are all unchanged.

Decision:
Enforce explicit transition tables for payments and invoices, so a status arriving from a processor is checked against what we already believe rather than applied because it was signed. Key provider events on the transaction *paired with the state reported*, so one transaction can produce several events without any of them being mistaken for a duplicate. Add refunds as request-then-confirm, with the amount computed server-side and `refunded_amount` written only by a callback. Let a checkout collect an invoice that already exists, and mark a workspace `past_due` when a renewal goes unpaid past a grace period. Do not build automatic card debits.

Context:
ADR-044 left the domain able to take one payment for one plan. Three things were missing before it was a billing cycle, and one thing was quietly wrong.

The wrong thing: `payment_events` recorded every callback with an outcome of `applied`, whatever was actually decided. An event naming an unknown payment, or reporting an amount that disagreed with the invoice, was refused and then filed as a success — so the one table an operator would read to find out why a customer's payment never landed said that it had.

The missing things: a renewal invoice could be issued and never collected, because the only way to open a payment page was to choose a plan; nothing ever looked at whether a renewal was paid, so a workspace that stopped paying stayed `active` for ever and kept its whole plan; and there was no way to give anybody their money back.

Reason:
**A signed callback is authenticated, not true.** The signature proves who sent it and says nothing about whether what it claims is possible. A late delivery, an out-of-order retry, or a compromised secret can all produce a perfectly valid callback saying a refunded payment succeeded — which would settle the invoice a second time and leave a customer with their money back and a paid invoice. The transition tables are the check that a statement about state is a state this row can reach. `refunded → succeeded` and `failed → succeeded` are the two that matter; both are written down and both are tested.

**One transaction is not one event.** This is the correction that mattered most, and it was not visible from ADR-044. Paymob sends a callback per *thing that happened*: in flight, then collected, then — if somebody refunds it — collected-and-refunded, and the documentation is explicit that the refund notification arrives on the **parent** transaction carrying `is_refunded: true`. Keying idempotency on `obj.id` alone therefore files every callback after the first as a duplicate of it. A 3-D Secure payment that reported `pending` before `success` would settle nothing at all, and a refund would be silently swallowed. So `provider_event_id` is `{transaction}:{state}` — deterministic, so a genuine retry still deduplicates, and distinct, so a progression is not lost. The raw id keeps its own column, because that is the number the provider's dashboard uses.

**A refund is requested here and confirmed elsewhere.** A 200 from a refund API means the reversal was accepted, not that a customer has their money. Writing `refunded_amount` on that response would tell somebody their money is back before it is — and would make the callback that says it *is* look like a duplicate. So the request records `refund_requested_at` and the reversal's reference, and exactly one piece of code writes `refunded_amount`: the callback handler, which is also where a refund issued from Paymob's own dashboard arrives. The gap between the two is a findable state, and it is the state that means the callback URL is wrong and a customer is waiting.

**The refund amount is never asked for.** It is the payment's own unreturned balance. That removes the field somebody would send to be refunded more than they paid, and it settles the partial-refund question honestly: Wasla has no credit notes and no way to render "half of March" on an invoice, so full-remainder semantics is what the model can actually express. Inventing partial refunds to match a provider capability would be building a concept no invoice could show.

**A refund reopens the invoice.** `amount_paid` records money we *hold*, so giving it back leaves an invoice its payments no longer cover, and an invoice that is not covered is not paid. Voiding it afterwards — because the customer is leaving — stays a separate deliberate act rather than something inferred from a reversal.

**Checkout idempotency refuses rather than replays.** A replay would have to return the same URL, and that URL carries the provider's client secret, which ADR-044 decided not to store. Paymob also documents `special_reference` as unique, so the page cannot be re-fetched under the same reference. Refusing with a conflict tells the caller its first request was accepted and points it at the payment's status, which is the thing it wanted; creating a second intention would leave two live payment pages for one invoice and no way to say which one a customer should use. The guarantee is `UNIQUE(tenant_id, idempotency_key)`; the read that produces the good message is a courtesy, and the constraint is what decides two simultaneous retries.

**Dunning is `past_due`, not disconnection.** `PAST_DUE` is in `SERVING_STATUSES` deliberately: a failed payment is a conversation to have, and cutting somebody off over a first expired card is how a relationship ends over an administrative detail. The grace runs from `issued_at` rather than from the period boundary, so a customer gets seven days from being *asked* — and a sweep that had been down for a fortnight does not mark every one of its customers behind the moment it comes back.

Consequences:
Automatic card debits are **not** built, and that is a decision rather than a gap. Paymob documents a Subscription API and CIT/MIT tokenisation; using either requires a MOTO integration id that Paymob enables per merchant, a Bearer auth token from the older `/api/auth/tokens` flow needing a fourth credential, and a billing frequency expressed as a fixed number of days — 7, 15, 30, 60, 90, 180, 360 — where Wasla bills on calendar months. A Paymob subscription on `30` drifts away from the period this system charges for and the two would disagree about what a customer owes within a year. Writing that adapter against a payload shape nobody here can exercise would be an unverifiable subsystem built to match a provider capability. The remaining dependency is *merchant must enable a MOTO integration and issue an API key*, not missing code.

Void is available at the provider seam and through Paymob's dashboard, and is not wired to an endpoint. Choosing between void and refund needs error semantics this integration has never seen, and guessing at them would mean a reversal that silently does nothing.

`payment_events.processed_at` becomes nullable. The row is claimed before the decision is made — which is what makes two simultaneous deliveries safe — so between the claim and the decision there genuinely is no processing time, and a crash in that window should leave a row that says so.

Nothing here has been verified against a live Paymob account. The refund endpoint, the reversal callbacks and the intention call are exercised against `httpx.MockTransport`; the signature is pinned to the vendor's published worked example. The first real transaction still has to be walked through with merchant credentials.

## ADR-046 — A Renewal May Be Taken From a Saved Card, and Almost Never Is

Date:
2026-08-29

Status:
Accepted. Extends ADR-045; the settlement path, the transition tables and the callback-as-authority rule are all unchanged.

Decision:
Store saved cards as provider tokens in their own table, add a merchant-initiated charge behind a third provider protocol, and let the billing sweep collect a due renewal from a workspace's default card. Gate the whole thing on a capability flag the provider answers, so a deployment without it bills exactly as it did before. Never charge a subscription that is not being served, and bound the attempts.

Context:
ADR-045 left recurring billing as: issue an invoice at each period end, email it, and wait. That is a renewal cycle a person completes. What was missing is the half where nobody is present.

Two Paymob paths lead there, and the current documentation was read for both rather than trusting an earlier note. The Subscriptions Module creates a plan and attaches subscriptions to intentions; MIT charges a card the customer previously saved. **Both require a Moto integration id**, which the documentation states explicitly in each case, and which the account's own dashboard cannot create - its integration types are PAYPAL, MIGS, UIG, CAGG and CASH. Paymob's overview says as much in general terms: "Not all payment methods are enabled by default. Availability depends on your merchant account setup."

Reason:
**MIT rather than the Subscriptions Module.** Both are gated identically, so the choice is on fit rather than availability. Paymob's subscription plans bill on a fixed number of days - 7, 15, 30, 60, 90, 180, 360 - and Wasla bills on calendar months. A plan on `30` drifts away from the period this system charges for and the two disagree about what a customer owes within a year. It would also mean mirroring the plan catalogue into Paymob and keeping it in step through `change_plan` and `cancel`, which is a second source of truth for pricing. MIT leaves the schedule here, where the product already decides it, and asks the processor only to move money.

**A capability, not an exception.** `can_charge_saved_methods` is a property the caller reads before doing anything, rather than an error it catches afterwards. Without a Moto integration this is not a failure to handle - it is a deployment that collects renewals by invoicing, which is a supported and previously the only way this product billed. Making it an exception would have meant every renewal producing a stack trace on a perfectly healthy system.

**A cancelled workspace is never charged.** This is the single most important line in the subsystem and it has its own guard, its own test, and a mutation check proving the test fails without it. Every other billing mistake here is recoverable with a refund and an apology; debiting somebody who has left is the one customers do not forgive, and it is the failure a scheme treats as unauthorised.

**Attempts are counted before the provider is called, and bounded at three.** Counting afterwards would let a request that timed out be retried for ever, and a timeout is exactly the case where the charge may already have happened. Three because a decline is usually a fact about the card rather than a moment - expired, blocked, empty - and a merchant that retries indefinitely is one a processor's risk team looks at. The claim is a payment row keyed `auto:{invoice}:{attempt}`, so two sweeps racing cannot both charge: one inserts and the other loses on `UNIQUE(tenant_id, idempotency_key)`.

**The charge settles nothing.** It returns a provider reference and no outcome. Money moving is decided by the same signed callback a customer-initiated payment produces, so an automatic renewal and somebody clicking a link converge on one settlement path with one set of rules - and there is no second place where an invoice can be marked paid.

**Saved cards get their own table and their own signature scheme.** A card belongs to a workspace rather than to a subscription: it outlives the subscription it was added for, a workspace may have several, and replacing one must not lose the record of what paid last month. Paymob signs card-token callbacks over eight fields rather than the transaction's twenty, so verification is separate - checking a token callback against the transaction field list would be computing a digest over the wrong string, which is not a weaker check but no check.

Consequences:
`payment_methods` stores an opaque token, the provider's id for it, the masked last four digits and the scheme name. **There is no column for a card number, an expiry or a security code**, and there is no code path that could populate one: the customer types those into the provider's page and what returns is a token. A schema with nowhere to put card data is a better guarantee than a rule saying not to store it.

The webhook endpoint now dispatches on the callback's declared `type`. That declaration is trusted for nothing - a body claiming to be a card token is still checked against the card-token signature, so lying about the type only changes which way it is refused.

The first card a workspace saves becomes its default; later ones do not. Silently moving renewals onto a card somebody used for a single payment is a surprise, and the API has an explicit call for changing it.

**Automatic charging has not been exercised against Paymob.** The account this was built against has no Moto integration, so `charge_saved_method` has never run against the live API - only against a mock transport shaped from the documented request and response. Everything up to the charge, including the card-token signature, is pinned to Paymob's published worked examples. The remaining dependency is a merchant capability, not code.

---

## ADR-047 — Wasla Is a Confidential Client and Uses PKCE Anyway, and the Callback Is a POST From the Frontend

Date:
2026-08-30

Status:
Accepted.

Decision:
Add Google as a second issuer that may open a session. Use the authorization code flow with a client secret held server-side, and add PKCE on top of it. Initiate with a `POST` that writes a single-use flow record, and have Google redirect the browser to a **frontend** route which posts `code` and `state` to the API, rather than redirecting to the API itself.

Context:
This API is cookieless: it returns access and refresh tokens in response bodies. That single fact decides the shape of the callback. A conventional `GET` callback reached by top-level navigation would have to render a document containing a refresh token — unreadable by the single-page application that needs it, and visible to anything that can see the page, including the browser history and any referrer.

Reason:
**PKCE despite the secret.** A confidential client is not required to use PKCE, and the client secret already stops a stolen code being exchanged by somebody else. PKCE costs one hash and closes the case where the code leaks *and* the secret has leaked separately — a defence in depth that is nearly free.

**Initiation is a `POST`.** It writes server state, and a `GET` that writes state is one a browser prefetch or a link preview will fetch on its own, filling the store with flows nobody started.

**The frontend owns the redirect URI.** `GOOGLE_REDIRECT_URI` is still fixed configuration that Google exact-matches, the code is still exchanged server-side with the client secret and the PKCE verifier, and the frontend never sees a Google token. Only the two opaque values `code` and `state` pass through it.

Consequences:
The residual gap is disclosed rather than papered over: without a cookie, the state is unpredictable, single-use, short-lived and server-side, but it cannot prove that the browser finishing a flow is the one that started it. For linking, the binding is strong regardless, because the flow record holds the initiating account and a caller cannot influence it.

Deployments must register the frontend route in the Google console, not the API. A mismatch is refused by Google before Wasla is reached.

---

## ADR-048 — Identity Lives in Its Own Table Keyed on the Subject, and the Profile Lives on the Account

Date:
2026-08-30

Status:
Accepted.

Decision:
Store federated identities in `user_identities`, keyed on `(provider, provider_subject)` and unique also on `(user_id, provider)`. Store no attributes on that table and no provider-shaped columns on `users`. Keep the account's display fields — `full_name`, `avatar_url` — on `users`, and refresh them from Google on every login.

Context:
An account is not "a Google account". It is an account that Google is willing to vouch for, and over its life it may be vouched for by more than one issuer, or by none.

Reason:
**Keyed on the subject and nothing else.** Google's `sub` is stable for the life of the account and is the only claim documented as such. An email address is not: people change them, corporate domains change hands, and a Workspace administrator can reassign one to a different human being. Keying on anything else means an address change silently orphans an account, or — far worse — an address reassignment silently hands one over.

**A table rather than `users.google_sub`.** A column models "a user has at most one Google account, forever". A table models "a user has some identities", which is what becomes true the moment a second provider exists, and costs one join today. `(provider, provider_subject)` is the security-relevant constraint: it makes "one Google account cannot open two Wasla accounts" true of the data rather than of the code paths somebody remembered, and it is the race backstop for two simultaneous first logins.

**The profile is on the account, not the identity.** What an interface needs is "this person's name and picture". A field resolved by asking which issuer vouched most recently would be answering a different question, and would have to be re-answered every time a provider was added. `avatar_url` is provider-agnostic; a second issuer writes the same column.

**No credential is stored.** No ID token, access token, authorization code — and no refresh token, because the authorization request asks for `access_type=online` and Google therefore never issues one. "It is never issued" is a stronger guarantee than "do not store it", which is a rule somebody has to keep remembering.

Consequences:
`ON DELETE CASCADE` from `users`: a stranded identity row would grant access to nothing while occupying the unique slot its rightful owner needs to reconnect.

A name edited inside Wasla is overwritten at the next Google login. That is the accepted cost of following the issuer while Google is the only source, and it must be revisited the day Wasla offers profile editing of its own.

---

## ADR-049 — A Matching Email Address Never Links Anything

Date:
2026-08-30

Status:
Accepted.

Decision:
A first Google login onto an address that already has a Wasla account is refused with `409`. It is never signed in, never linked, and the existing account's password is never touched. Linking requires an authenticated request, and binds to the account recorded server-side when the flow began — never to the address in the token.

Context:
The convenient behaviour is obvious and wrong: see a verified Google address matching an existing account, attach the identity, sign them in.

Reason:
Proving control of a *mailbox* is not proof of anything about an account registered under it. The account may have been opened by whoever held that address before them — corporate addresses are reassigned, personal ones are recycled by providers. Automatic linking would mean that acquiring an address is enough to acquire every account ever registered with it.

The refusal is ordered before the account lookup for the unverified case, so that the answer cannot be turned into an oracle for which addresses have accounts.

Consequences:
A person whose Wasla account predates their Google sign-in must sign in with their password once and link deliberately. That is a real friction, accepted knowingly, and it is the only step in the flow where the product asks something a competitor might not.

`GOOGLE_LOGIN_FAILED` is recorded for this refusal because it names a real account — one of only two refusals that do. A bad signature or a stale nonce gets a log line and no audit row, so that an unauthenticated stranger cannot flood a trail colleagues have to read.

---

## ADR-050 — Google's `email_verified` Is Trusted, Because It Buys Nothing

Date:
2026-08-30

Status:
Accepted.

Decision:
Require `email_verified` to be `true` before enrolling a new account, and stamp `users.email_verified_at` from it. Compare the claim against `True` rather than evaluating truthiness.

Context:
ADR-043 built email verification as a six-digit code and gave it deliberately no authority. That decision is what makes trusting Google here cheap.

Reason:
The address arrived inside a signature this system checked against Google's published keys, which is a stronger proof than a code mailed to the address and typed back. And because verification grants nothing — no route reads the column, no permission depends on it — trusting the claim writes down a fact rather than handing out access. The blast radius of Google being wrong is a timestamp that should not be there.

The type check is not pedantry. The string `"false"` is truthy in Python, so a provider that sent one would otherwise be read as having verified the address.

Consequences:
An unverified Google address is refused enrolment rather than enrolled-and-unverified, because the address is the only identifier the new account would have.

If a future rule ever makes `email_verified_at` grant something, this ADR is one of the two places that must be revisited — the other being ADR-043 itself.

---

## ADR-051 — State and Nonce Live in Redis, and Refuse When It Is Gone

Date:
2026-08-30

Status:
Accepted.

Decision:
Keep the state, nonce and PKCE verifier for an in-flight authorization in Redis, single-use and short-lived. When Redis is unavailable, Google sign-in fails closed and becomes unavailable. Password login is unaffected.

Context:
ADR-040 established the opposite posture for the rate limiter: a degraded dependency degrades the limit rather than removing it, and a Redis outage must not lock everybody out.

Reason:
The two are not inconsistent, and the difference is what the control actually is. A degraded limiter still slows an attacker — a process-local approximation of "how many attempts per minute" is weaker but real. There is no weaker-but-real version of a *single-use* replay control: a per-process store would let the same state be spent once on every worker, which is not a degraded defence but an absent one. A replay window that opens exactly when infrastructure is unhealthy is the worst possible time for it to open.

Failing closed also keeps the failure honest. An unavailable Google sign-in is visible, alarming and quickly fixed; a silently unverified one is none of those things.

Consequences:
Google sign-in is unavailable during a Redis outage, and this is documented as a deployment dependency rather than discovered during one. Because password login does not touch this store, an outage degrades one route rather than locking every customer out — which is what makes failing closed here affordable.

The `404`-when-unconfigured behaviour is separate and deliberate: a feature nobody enabled does not exist in this deployment, which is a different statement from `503`'s "it is temporarily unwell", and it declines to tell an unauthenticated caller that the feature exists and is broken.

---

## ADR-052 — An Agent's Mutations Are Audited As the Agent, and Only After They Happen

Date:
2026-08-30

Status:
Accepted.

Decision:
Add `AuditActorKind.AGENT` and three actions — `AGENT_HANDOFF_REQUESTED`, `AGENT_LEAD_RECORDED`, `AGENT_FOLLOW_UP_SCHEDULED`. Write them from the tool handlers, after the mutation has succeeded, with the actor kind passed literally and the scope taken from `ToolContext`. Record shapes in `meta`, never customer content. Do not audit ordinary inference.

Context:
Leads already carried `ActorKind.AGENT` attribution and handoffs already emitted analytics, so an agent's work was partly reconstructable. What did not exist was a row in `audit_logs` — the artefact an incident review actually reads. After a prompt-injection report the first question is "which conversations did the agent act in, and when", and the trail could not answer it.

Reason:
**A distinct actor kind rather than `SYSTEM`.** `SYSTEM` means the scheduler acting on its own clock — a subscription the sweep expired. "An agent decided this" and "time decided this" are different answers to "who did this", and only the first is worth filtering on after an injection report. Collapsing them would have made the new rows unfindable among the billing sweep's.

**Written after the mutation, never before.** Recording at the top of a handler produces a trail full of actions that did not happen, which is worse than no trail because it is believed. Every call sits below its service call, past the branches that return early on refusal, so a refused tool, a rejected argument or a conversation a colleague had already taken over leaves nothing.

**Shapes, not content.** `meta` carries which lead fields were filled and how long a follow-up delay was. It does not carry the customer's name, their number, the follow-up body or the handoff reason. An audit log is append-only and outlives the rows it describes; copying personal data into it would create a second store with a different retention story and would survive every deletion request made against the first.

**Inference is not audited.** A row per reply would bury three real mutations in traffic and turn the log into a worse copy of the message table. Only acts that change state are recorded.

Consequences:
The model cannot influence the actor. Identity is passed as a literal by the handler and the scope comes from a context the orchestrator built from the worker's job, so there is no argument through which a compromised model could claim to be a user or write into another workspace's trail. `tests/integration/test_ai_security.py` asserts this, and mutating the actor kind to `SYSTEM` fails three of its tests.

Migration 0036 extends two enums. It has no downgrade, for the reason 0025, 0029 and 0034 record.

---

## ADR-053 — A Workspace May Only Name a Model the Deployment Will Pay For

Date:
2026-08-30

Status:
Accepted.

Decision:
Add `OPENAI_ALLOWED_MODELS` and `OPENAI_MAX_OUTPUT_TOKENS`. Validate both in `AgentService` on create and on update. An empty allowlist means no restriction; the configured `OPENAI_MODEL` is always permitted. `max_output_tokens` defaults to the configured ceiling and a request above it is refused rather than clamped.

Context:
`model` was free text bounded only by length, and `max_output_tokens` was nullable. A workspace administrator could therefore point an agent at the most expensive model a provider offers and buy unbounded output per call. The plan caps the *number* of AI requests through `PERIOD_AI_REQUESTS`; nothing capped what one request cost, so plan economics could be inverted by an authenticated customer acting entirely inside their own workspace.

Reason:
**In the service, not the schema.** A Pydantic schema cannot see configuration, and the check has to be against what *this deployment* funds rather than a constant compiled into the application. Putting it in `AgentService` also covers every caller rather than only the HTTP route.

**On the way in, not at the provider.** A refused model must be a `422` an administrator can act on. Deferring the check to the provider turns a configuration mistake into a failed inference that a customer waits for and that surfaces as "the agent stopped answering".

**Refused, not clamped.** An administrator who asked for 8,192 output tokens and silently got 2,048 would believe something false about what they configured. The same reasoning as the email-verification bounds in ADR-043.

**Empty means unrestricted.** A developer's container should not need a curated list to run, and a deployment paying a provider bill should not be able to forget one — so `.env.example` ships an explicit list and the documentation states the consequence of leaving it empty.

Consequences:
Neither setting is reachable from a prompt or a tool. Model choice, token ceiling, temperature and system prompt are configuration, and no tool declares an argument by any of those names; the test suite asserts that structurally over the whole registry so a tool added later cannot quietly expose one.

Per-plan model tiers were considered and not built. The entitlement system keys on countable usage rather than on configuration values, so expressing "this plan may use these models" would mean a new kind of limit rather than a new limit — a billing change rather than a security fix, and out of scope for the review that produced this. The global allowlist is the operator's control; per-plan tiers remain open.

---

## ADR-054 — An Agent Turn May Not Spend More Rounds Than the Allowance Permits

Date:
2026-08-30

Status:
Accepted, and its residual race **closed by ADR-056**. The paragraph below
recording the concurrent overshoot as unfixed was true when written and is no
longer; it is left in place because the reasoning about *why* it was not
half-fixed is what led to the general primitive.

Decision:
The AI worker reads the remaining `PERIOD_AI_REQUESTS` balance rather than a yes/no, and caps the turn's tool rounds at what remains. The concurrent-worker race is left open and documented rather than half-closed.

Context:
The worker asked "may I make one request?", then ran a turn of up to `MAX_ROUNDS` provider calls and metered all of them. A workspace on its last permitted request could therefore be billed for three, deterministically, every time.

Reason:
**Bounding the rounds rather than reserving the worst case.** Reserving three up front would refuse a turn that only needed one, leaving customers unanswered while allowance remained — a product regression in exchange for the same guarantee. Capping the budget refuses nothing that fits.

**The concurrent race is not closed, and saying so is the point.** Two workers answering one workspace both read the balance before either records against it, so *N* workers can spend *N* rounds past the limit. Closing it needs an atomic reservation, which the entitlement system has for *no* limit — messages and campaign sends share the property. Fixing it here alone would leave the same race everywhere else while implying it had been solved, which is a worse outcome than a documented bound.

Consequences:
The deterministic 3× overshoot is gone; a bounded concurrent overshoot remains, proportional to worker count rather than to traffic. It is recorded in `docs/AI_AGENTS.md` under the allowance bound, and closing it platform-wide is a separate piece of work against the entitlement system rather than against the AI subsystem.

---

## ADR-055 — What Leaves Wasla Is Documented, and Provider Retention Is Not Asserted

Date:
2026-08-30

Status:
Accepted.

Decision:
Document the complete inventory of data sent to OpenAI in `docs/AI_AGENTS.md`, including what is deliberately *not* sent. State what this application controls — `store: false`, no `previous_response_id`, no internal identifiers in any payload — and explicitly decline to assert what the provider retains beyond that.

Context:
Customer conversation text, voice transcripts, image content and retrieved knowledge-base passages all reach OpenAI, and no document said so. For a product whose tenants' end users are consumers messaging over WhatsApp, "what leaves, to whom, for how long" is the first question a tenant's legal review asks, and the repository had no answer to point at.

Reason:
**Written from the request builders, not from intent.** The inventory was produced by reading every payload construction in `app/integrations/openai/`, which is also how the negative claim was established: no `tenant_id`, `conversation_id`, `user_id` or agent id appears in any provider request.

**Retention is not asserted, deliberately.** `store: false` is a property of this code and can be stated. Whether a given OpenAI account has zero-retention eligibility, what its abuse-monitoring window is, and whether a data-processing agreement is in force are facts about an account and a contract that this repository cannot inspect. Documenting an assumption as a guarantee would be worse than documenting nothing, because a tenant would rely on it.

Consequences:
An operator running Wasla for third-party businesses must confirm the retention posture on their own provider account and record it. The documentation says so rather than implying the question is settled.

Prompts are not persisted anywhere: the memory window is rebuilt from message rows each turn and discarded. That is a deletion story worth having — there is no prompt archive to leak, and deleting a conversation removes the material a prompt would have been built from.


---

## ADR-056 — A Metered Allowance Is Reserved Atomically, For Every Limit

Date:
2026-08-30

Status:
Accepted. Closes the residual race recorded in ADR-054.

Decision:
Add `EntitlementService.consume(key, event_type, amount)`: take a PostgreSQL advisory transaction lock keyed on (workspace, limit), re-check the allowance under it, record the usage meter, and flush before the lock is released. Make it a general primitive available to every period limit rather than an AI-specific mechanism. Use it in the agent worker to reserve one AI request before each provider round, in a short transaction of its own.

Context:
ADR-054 bounded a turn's rounds by the remaining allowance, which removed a deterministic three-times overshoot. It explicitly left the concurrent case open on the grounds that it was small and platform-wide.

Measuring it showed the estimate was wrong. Ten connections reserving simultaneously against an allowance of three **all ten succeeded** — every concurrent attempt reads a total none of the others has yet written to, so the overshoot is bounded by concurrency rather than by anything small. On the path taken by every provider call this product makes, that is a real hole in plan enforcement.

Reason:
**An advisory lock rather than a counter table.** `usage_events` is append-only and is the single source of truth for what a workspace has spent (ADR-030). A counter column beside it would be a second source that can disagree with the first, and disagreeing about billing is a worse failure than serialising briefly.

**Rather than SERIALIZABLE.** That would push retry handling into every caller for a conflict that is rare, and would make correctness depend on every future caller remembering to retry.

**Keyed on (workspace, limit).** Only the workspaces actually contending serialise. Two different workspaces never wait for each other, and neither do two different limits within one workspace.

**Held briefly, and never across an inference.** The lock lives until its transaction ends, so the worker reserves on a session of its own and commits before calling the provider. Holding a workspace's lock for the length of an inference would serialise every conversation that workspace is having — trading a billing leak for a throughput collapse.

**The caller names the meter.** A key can be fed by more than one meter: `PERIOD_MESSAGES` counts sent *and* received. Incrementing every meter for a key would bill a workspace twice for one message, so `consume` takes the event type and refuses one that does not count toward the key. Resource limits — agents, numbers, seats — count rows that already exist and cannot be consumed at all; asking to is an error rather than a silent no-op.

Consequences:
The request meter moved. It is now written by the reservation before each provider call rather than by the worker after the turn, so the worker records tokens only and passes `requests=0`. A turn that is reserved and then abandoned has still spent the request: usage is append-only and there is no refund, which is the safe direction for a cost control.

A crash between reserving and calling bills a request that did not happen. That is accepted, and it is the direction to fail in.

The primitive is general and unused by the other limits so far. Adopting it for messages and campaigns is follow-on work against those call sites, not a change to this one.

## ADR-057 — An Invitation Grants a Membership, Never an Identity

Date:
2026-09-01

Status:
Accepted

Decision:
`InvitationService.accept` may set a password **only on an account it created in that same call**. For an address that already has an account it adds or reinstates the membership and touches nothing else; a supplied password is ignored. The legitimate way for a passwordless account to acquire a password is `POST /auth/password/set`, which is authenticated and self-only. The raw invitation token is no longer returned by `POST /api/v1/invitations`; it travels only in the outbox row addressed to the invited mailbox.

Context:
Acceptance contained a branch that set a password whenever the account had none:

    elif user.hashed_password is None and password is not None:
        user.hashed_password = hash_password(password)

Its comment named the case it was written for — "an account created by an earlier invitation that was never completed" — and at the time the inference held, because acceptance was the only thing that created an account without a password. Google sign-in ended that. `GoogleAuthService._enrol` creates accounts with `hashed_password=None` deliberately, which is what makes them unreachable by password. Passwordlessness stopped meaning "this invitation owns this account" and started meaning "somebody signs in with Google".

The result was a full account takeover, available to anybody who could register: registration makes its holder the owner of a workspace, an owner may invite any address, the 201 response carried the raw token, and `POST /invitations/accept` is unauthenticated. Invite a Google user, redeem the invitation with a password of your choosing, sign in as them — and with them every workspace they belong to.

Neither subsystem's tests could see it. The invitation tests never meet a Google account and the Google tests never meet an invitation.

Reason:
**The branch cannot be repaired by narrowing its condition.** Any test on the *state* of the account is a proxy for the question actually being asked, and the last proxy was true until a feature three phases away made it false. The question is "did this call create this account", which is not a property of a row; it is a fact the call knows. So it is expressed as control flow — the create branch sets a password because it is the branch that created the account, and no other branch writes one.

**A supplied password is ignored rather than refused.** Every other answer this endpoint gives is uniform, because unknown, spent, revoked and expired invitations must be indistinguishable. A distinct error for "that address already has an account" would tell whoever holds the token exactly that.

**The token stops at the outbox.** `InvitationCreatedResponse` existed because there was no mail delivery, and its docstring said the field would go once there was. There is: `issue` queues the token to the invited address (ADR-042). A 201 body reaches reverse-proxy logs, APM payloads and browser captures, so returning a credential there publishes it — and this token both joins a workspace and, until this change, could claim an identity.

**Setting a first password needs its own route, not a relaxed one.** `/auth/password` proves control with the current password, which these accounts do not have; a reset declines passwordless accounts rather than becoming an oracle. So the proof is the session, and the route refuses any account that already has a hash — it is not a second way to replace a password without knowing it. It reuses `change_password`'s policy exactly: the same strength rule, the same `token_version` bump, the same audit action, the same notice to the address on the account.

Consequences:
Accepting an invitation for an existing address no longer sets a password, and a client that relied on that was relying on the vulnerability. `POST /api/v1/invitations` no longer returns `token`; a deployment with `EMAIL_ENABLED=false` therefore has no way to deliver an invitation, which is correct — it also has no way to deliver a password reset.

A Google-first account can now set a password and afterwards disconnect Google, which is what `unlink`'s refusal message has always instructed and what ADR-049 claimed the recovery path was.

## ADR-058 — One Access Token Issuance Policy, In One Function

Date:
2026-09-01

Status:
Accepted

Decision:
Every access token this application mints is built by `AuthService._access_token`. `select_workspace` and `_issue` differ in what they pass it, not in how they build a token.

Context:
`select_workspace` called `create_access_token` directly and omitted `token_version`. A token without a `ver` claim decodes to `None`; `get_current_user` compares that claim against `users.token_version`, which defaults to 1. `None != 1`, so `POST /auth/workspace` answered 200 with a token that every subsequent request refused as revoked. Multi-workspace switching — a headline requirement of this product — did not work at all.

The endpoint's tests replaced `AuthService` with a stub returning the literal `"switched-value"`, and the one test that ran against the real service asserted only the 404 refusal. No test ever used the token that came back.

Reason:
A second issuance site is a second policy. This one differed by an omission nobody could see, and the next claim added to `_create_token` would have had to be remembered in two places. Extracting the common function makes the two callers differ only in their arguments — whether a workspace is selected, and whether a refresh token accompanies it.

Consequences:
A test that asserts on a token must spend it. A stub returning a token shape proves the route is wired, not that the token works, and the regression tests for this switch workspace and then call `/auth/me` and a workspace-scoped route with what came back.

## ADR-059 — A Priced Plan Is Granted by Settlement, Never by Asking

Date:
2026-09-01

Status:
Accepted

Decision:
`SubscriptionService.start` and `change_plan` refuse a plan with `price > 0` when `self_service=True`, answering 402 `payment_required`. A priced plan is reached through `POST /billing/checkout` and applied by `CheckoutService._settle`, from `invoice.plan_code`, when a signed provider callback says the invoice is paid. Free plans are unaffected.

Context:
Two correct-in-isolation halves with nothing between them. The money path was already strict — an HMAC over the provider's payload, a reference this system generated, amount and currency compared against the invoice, a transition table deciding legality, idempotency by unique constraint. And `change_plan` moved a workspace onto any public plan the moment an owner asked, with no reference to an invoice at all. `_settle` deliberately did not touch the plan, on the sound reasoning that paying an invoice settles an invoice.

So the entire checkout pipeline was optional decoration around a self-service upgrade that cost nothing: `POST /billing/subscription/plan {"plan_code": "business"}` and every Business limit applied. Every test passed — the billing tests exercised the money, the entitlement tests exercised the limits, and nothing exercised the sentence joining them.

Reason:
**The gate belongs on `_require_plan`**, which both doors already pass through and which already carries the `is_public` rule for the same reason: a catalogue filter was standing in for an authorization rule and had to become one. `self_service=False` already existed for the platform assigning what a customer may not choose — registration putting a new workspace on the default plan — and settlement is exactly that kind of caller.

**The grant reuses `change_plan` rather than reimplementing it.** Period arithmetic, trial clearing, the cancellation reset and the audit entry are one state machine with one owner. A second copy inside settlement would be the parallel billing machine this change exists to avoid.

**The plan comes from the invoice, not from the callback.** `invoice.plan_code` was copied from the plan the customer chose before the provider was ever called, so the grant is decided by a row this system wrote. The callback contributes one fact: the money arrived.

**402 with its own code.** `PlanLimitExceededError` is also 402, but it says the current plan does not stretch that far. This one says the plan being asked for has not been paid for, and a client rendering them alike would tell somebody to upgrade while they are trying to.

Four cases are deliberately left alone: no subscription (nothing to move, and inventing trial rules inside settlement is the parallel machine again — logged loudly instead), a renewal (the invoice names the plan already held, and the sweep owns periods), a terminal subscription (paying is not a request to resubscribe), and a retired plan code.

Consequences:
`POST /billing/subscription` and `/subscription/plan` answer 402 for a priced plan. The seeded catalogue makes starter free and pro and business priced, so downgrading remains self-service and upgrading is not.

A workspace with no subscription that pays for a plan gets a settled invoice and no plan. That state requires `DEFAULT_PLAN_CODE` to name no plan — where limits are already unenforced — and it is logged rather than guessed at.

Dunning is untouched and remains open: `PAST_DUE` still serves, and deciding when that grace runs out is a separate change.

## ADR-060 — A Trusted Proxy Is an Address or a Network, Never a Name

Date:
2026-09-01

Status:
Accepted

Decision:
`TRUSTED_PROXY_IPS` entries are parsed as IP addresses or CIDR networks, IPv4 or IPv6, and compared against the parsed peer address. A malformed entry — a hostname included — is refused at startup in every environment. `docker-compose.prod.yml` gives the internal network an explicit subnet and nginx a fixed address on it, and trusts that address.

Context:
The comparison was `peer not in trusted_proxies`, a string membership test against `request.client.host`, which is an IP address. The shipped `docker-compose.prod.yml` set `TRUSTED_PROXY_IPS=nginx` — a Docker service name, which can never equal an address. Nothing ever matched, and two controls failed together with no error anywhere:

- **Authentication rate limiting collapsed to one bucket.** Every request from the internet was counted under the nginx container's own address, so the whole world shared a ten-per-minute budget in front of `/auth/login`. That is not a weakened limit; it is an outage anybody can trigger.
- **HSTS was never emitted.** `SecurityHeadersMiddleware` decides whether to believe `X-Forwarded-Proto` from the same trust test.

`TRUSTED_PROXY_IPS` was also the one security-relevant setting absent from `.env.example`, so an operator had no prompt to set it correctly.

Reason:
**Addresses, because that is what is being compared.** `ipaddress.ip_network` with `strict=False` accepts a bare address as a single-host network, so naming one proxy is still writing one address.

**Fail-fast, because the failure was silent.** The settings module already refuses rather than clamps a lifetime out of range, on the argument that silently correcting configuration is how an operator comes to believe something is set that is not. A trust list matching nothing is the same failure with a security consequence.

**No name resolution.** Resolving a hostname would put the trust anchor for forwarding headers under whatever answers DNS, and this list exists precisely because that decision must not be influenceable from outside. A name is refused with a message saying so.

**A fixed address rather than the whole subnet.** Docker allocates bridge subnets per host, so there was no stable range to name; the compose file now declares `10.89.0.0/24` — outside Docker's default pools — and pins nginx to `10.89.0.10`. Trusting one address keeps forwarding headers believable from the proxy alone rather than from every container on the network.

Consequences:
A deployment carrying a hostname in `TRUSTED_PROXY_IPS` will not start, and says which value to fix. That is the intended migration: it was not working before, it was failing quietly.

Addresses read from forwarding headers are normalised, so one client written two ways is one rate-limit bucket rather than two.

## ADR-061 — An Unpaid Subscription Is Suspended, Not Cancelled and Not Served For Ever

Date:
2026-09-01

Status:
Accepted

Decision:
`SubscriptionStatus` gains `SUSPENDED`. The billing worker moves a `PAST_DUE`
subscription there once its invoice has been unpaid for `BILLING_SUSPEND_AFTER_DAYS`
from `issued_at`. `SUSPENDED` is outside `SERVING_STATUSES`, so `EntitlementService`
stops resolving the paid plan and the workspace falls back to `DEFAULT_PLAN_CODE`.
A settled payment lifts it back to `ACTIVE`; a cancellation and an expiry are
untouched by settlement, as before.

Context:
ADR-059 closed the *purchase*: a priced plan became unobtainable without an
authoritative payment. It said nothing about *retention*. `_chase_unpaid` marked
a workspace `PAST_DUE` after seven days and nothing moved it afterwards, and
`PAST_DUE` is a serving status — the model's own comment said the platform would
"decide separately when that grace has run out", and no such decision existed.

So a workspace that simply stopped paying kept its full paid plan indefinitely,
receiving one email per invoice. That is the same product given away by a slower
route than the one ADR-059 shut, and it made the entire billing lifecycle
advisory: buying was enforced and keeping was not.

Reason:
**A new status rather than reusing `CANCELLED`.** This enum's job is to record
*who decided and why* — its docstring already separates an expiry from a
cancellation on exactly that ground, because "nobody chose it" and "the customer
chose it" want different emails. A suspension is the platform's decision, and
writing it as a cancellation would misattribute it in the audit trail, count it
as churn on a dashboard that deliberately separates cancellations from failed
payments, and — because settlement must not revive a subscription somebody chose
to end — make recovery impossible to express. A customer who paid their overdue
bill would have settled the invoice and stayed cut off.

**`SUSPENDED` is in `TERMINAL_SUBSCRIPTION_STATUSES`.** That set means "the sweep
advances this no further", which is exactly true: no period opens, no invoice is
raised, no saved card is charged. It is not a claim that the row can never move
again, and the model now says so. `Subscription.is_suspended_for_non_payment` is
the single place the recoverable case is named, and `CheckoutService` reads a
closed two-member set rather than "any status that is not active" — so adding a
sixth status later cannot silently make it recoverable.

**Anchored on `issued_at`, like the soft threshold already was.** It is the day
the customer was asked for money rather than a period boundary they never saw,
and it is written once and never rewritten — so neither threshold can move under
a workspace while it is being chased. No new column and no new query: the same
`PlatformInvoiceRepository.overdue` serves both sweeps with different windows.

**Both thresholds are configuration, and the ordering is validated.** A hard
threshold at or before the soft one would suspend a workspace in the same sweep
that first told it anything, which is the opposite of a grace period. That is
refused in every environment including `test`, because it is an ordering rather
than a credential.

**The worker changes state; the entitlement service interprets it.** There is no
plan resolution in the worker and no dunning arithmetic in `EntitlementService`.
The degradation to the default plan is the fallback that already existed for a
workspace whose subscription is not serving.

**Chase before suspend, in one sweep.** A workspace whose invoice is already past
the hard threshold the first time the loop sees it — because the worker was down —
gets both transitions, both audit rows and both notices, rather than being cut off
having never been told.

Consequences:
Migration 0037 adds two enum labels and nothing else. Its downgrade is empty for
the reason 0025, 0029, 0034 and 0036 give, plus one of its own: a row that reached
`suspended` describes a workspace the platform stopped serving, and a downgrade
would have to invent a status for it.

Idempotency has two independent guards. The status is the claim — only one pass
can find a row in `PAST_DUE` — and the outbox key is the invoice, so a workspace
that stays suspended is told once about that bill rather than once every ten
minutes.

Dunning is now complete as a lifecycle. What remains open is commercial rather
than technical: how long a suspended workspace's data is retained, and whether a
suspension should ever become a cancellation on its own.

## ADR-062 — Deployment Configuration Is Derived From Settings, Not Maintained Beside It

Date:
2026-09-01

Status:
Accepted

Decision:
`docker-compose.prod.yml` passes every setting each process actually reads, each
as `${VAR:-}` so a feature nobody enabled cannot stop the stack starting. A test
derives the expected set from `Settings.model_fields` through a single mapping of
feature prefix to service, and fails when the two diverge.

Context:
Google sign-in, transactional email and Paymob payments were implemented, tested
and documented, and none of them could be switched on by a deployment brought up
from the shipped production Compose file: it carried no `GOOGLE_*`, no `EMAIL_*`,
no `RESEND_*`, no `PAYMOB_*`, no `BILLING_PROVIDER` and no `APP_PUBLIC_URL`.

The file was not wrong when written. It enumerates its environment explicitly,
which is the right posture for production — nothing reaches a container by
accident — and that enumeration went stale across five phases while `Settings`
grew. Nothing anywhere compared the two, so the failure was invisible: the stack
came up, served traffic, and quietly had no email, no Google and no card
payments. `docker-compose.yml` was never affected, because it forwards a
developer's whole `.env` through `env_file` and therefore cannot drift.

Reason:
**Optional at interpolation, fail-closed at validation.** The infrastructure a
deployment cannot run without — the image, the database, the signing key — stays
`${VAR:?}`. A feature is different: refusing to boot over an absent Google client
secret would make an optional integration compulsory. So every feature setting is
`${VAR:-}`, and each feature's own validator refuses a *half* configured one.
`Settings` already knew which combinations are coherent; Compose does not, and
this keeps that decision where the knowledge is.

**Split by what each process reads.** The API verifies Resend's delivery webhook
and never sends, so `RESEND_API_KEY` exists only on the worker — which is what
`Settings` already documented when it declined to require that key globally.
Nothing in the worker touches OIDC, so no `GOOGLE_*` reaches it and the client
secret lives in exactly one container. Handing each process only what it uses is
the difference between one compromised container and two.

**The guard derives rather than restates.** Three hand-maintained copies of the
same list is the original bug with extra steps. `FEATURE_SETTINGS` maps a prefix
to the services that read it and the test expands it against `Settings.model_fields`,
so a field added under a mapped prefix and not wired in fails CI. The mapping is
checked in both directions: an entry naming a field that no longer exists fails
too, which is what stops the guard rotting into a test of itself. Deliberate
omissions live in `EXPECTED_ABSENT` with their reason, so an absence is either a
recorded decision or a failure.

Consequences:
Adding a setting to an existing feature now requires wiring it into the production
file or recording why not. Adding a *new* feature requires one line in
`FEATURE_SETTINGS`; until it is there the guard says nothing about it, which is
the honest limit of a mapping-based approach and the reason the mapping is small
and reviewed rather than clever.

`.env.example` is held to the same set, because the operator who has to set these
values needs to be able to discover they exist — the failure `TRUSTED_PROXY_IPS`
demonstrated in ADR-060, where an undocumented variable was also misconfigured and
the two compounded.


## ADR-063 — A Credential Is Required By The Process That Uses It

Date:
2026-09-01

Status:
Accepted

Decision:
`RESEND_WEBHOOK_SECRET` is no longer required by `Settings`. The requirement
moved to `integrations.email.require_delivery_verification`, called from
`create_app`, so a production API with email enabled still refuses to start
without it — and the worker container no longer carries it. The drift guard was
extended to enforce deliberate *absence* as well as presence, so a secret added
to a process that does not read it fails CI.

Context:
ADR-062 split the production environment by what each process reads, and got
`RESEND_API_KEY` right: only the worker sends, `build_email_provider` validates
the key in the sending process, and the API never sees it. `Settings` documents
that choice in as many words — "requiring it globally would force a credential
into a container that never uses it".

The webhook secret is the mirror image and was handled the opposite way. Only
the API serves `POST /webhooks/email`, and `settings.resend_webhook_secret` is
read in exactly one module (`api/v1/email_webhooks.py`). But the requirement sat
in the shared production validator, keyed on `EMAIL_ENABLED` — a flag the worker
must also have, because it is what tells the worker to send. Every process
builds `Settings`, so the worker had to be handed a secret it never reads simply
to boot. `docker-compose.prod.yml` said so in a comment: "the one value in this
block that is present to satisfy a check rather than to be used."

Nothing was exploitable and nothing was broken. It was an extra copy of a
credential in an extra container, which is the kind of thing that is only ever
found by going looking.

Reason:
**A validator in a shared model is a requirement on every process.** That is the
whole mechanism. `GOOGLE_*` avoids it by accident of shape — `GOOGLE_ENABLED` is
API-only, so the Google validator never fires in the worker — while email could
not, because its enabling flag is genuinely shared. So the check has to move to
the process rather than the flag being contorted to suit the check.

**The two halves are separately damaging.** `RESEND_API_KEY` can send mail as the
platform's domain; `RESEND_WEBHOOK_SECRET` decides which delivery reports are
believed. Keeping them in different containers means taking one does not hand
over the other, and there is now a test asserting no container holds both.

**The guard only checked one direction.** `EXPECTED_ABSENT` recorded decisions
and nothing verified them, so adding `RESEND_API_KEY` back to the API — or
copying a block "to keep the two the same" — would have passed every assertion
in the file. Absence is now enforced, which is what makes the least-privilege
split a property of the repository rather than of the commit that introduced it.

Consequences:
The fail-fast guarantee is unchanged in production and now belongs to the API
process; `tests/unit/test_email_configuration_is_per_process.py` asserts both
halves, including which process fails, because a test that only asked whether
*something* refuses would have passed before and after.

Paymob is deliberately left alone. Every Paymob setting genuinely reaches both
processes: the API creates intentions and verifies callbacks, the worker charges
saved cards for renewals, and both build the same provider through
`build_checkout_provider`. `PAYMOB_HMAC_SECRET` is the one value the worker
constructs but never uses, and splitting the factory to withhold it would put a
second, partial construction path into the money code to remove a secret from a
container that already holds `PAYMOB_SECRET_KEY` — a worse trade than the one it
buys.

## ADR-064 — A Dependency Outage Is a 503 Naming the Dependency, Not a 500

Date:
2026-09-02

Status:
Accepted

Decision:
`RefreshTokenStore` catches `RedisError` on all three of its operations and
raises `DependencyUnavailableError` with `details={"dependency": "redis"}`,
which the existing handler renders as a 503. The behaviour it replaces was not
insecure — it already refused — but it refused by letting the driver exception
escape to `handle_unexpected_error`, so `/auth/refresh` and `/auth/logout`
answered 500.

Context:
The security direction was never in question. Spending a refresh token *is* the
replay check: a store that cannot answer does not know whether the token has
already been presented, and issuing a fresh pair on that unknown is precisely
the case the mechanism exists to catch. `OAuthFlowStore` already made this
argument for the OAuth state (ADR-051) and already had the right shape. The
denylist did not, and the gap was only in how the refusal was reported.

Reason:
**A 500 is a statement about us, and it is the wrong one.** It says the request
was malformed by the application, so it pages the wrong people, is not
retryable by any well-behaved client, and hides a Redis outage inside the
application's error rate. A 503 with the dependency named is the same refusal
made legible.

**Silence would have been worse than either.** `revoke` had the additional
option of returning quietly, since "already revoked" is not an error there. It
would have meant answering 204 to somebody signing out of a shared machine
while their refresh token stayed usable for a fortnight — a false statement
about a security action. There is no second authoritative record for an
individual refresh token, so an honest failure is the only available answer.

**This is the opposite call to ADR-040, deliberately.** A rate limiter meters
capacity and can be approximated in-process; the denylist meters credentials
and cannot be approximated at all, because the only thing it knows is whether
one particular token has been seen before. The two policies now sit in the same
test file so the distinction is read rather than rediscovered.

Consequences:
While Redis is unreachable nobody can refresh, so every session ends within the
access-token lifetime. That cost is stated in the module rather than left to be
discovered during an incident. What is deliberately *not* coupled: signing in
does not consult the denylist and keeps working, and `/auth/logout-all` keeps
working because bulk revocation is `users.token_version` in PostgreSQL
(ADR-036) — so the lever somebody reaches for when they believe a token has
leaked is the one that does not depend on the cache.

The exception type is recorded in the log and its message is not.
`ConnectionError` from redis-py carries the address it failed to reach, and a
configured URL may carry a password; this is also the log line most likely to
be pasted into a ticket.

## ADR-065 — An Identifier That Will Not Fit Is Refused; Decoration Is Shortened

Date:
2026-09-02

Status:
Accepted

Decision:
`GoogleIdTokenVerifier._extract` bounds every claim it returns against the
column that will store it. `sub` and `email` are **refused** when they exceed
their bound; `name` is **shortened**; `picture` already degraded to `None` and
is unchanged. The four widths are imported from the models rather than written
down again, and a test asserts each pair is equal.

Context:
Only `picture` was bounded. `name` (200), `sub` (255) and `email` (320) reached
PostgreSQL unchecked, where an over-long value raises `DataError` — which is
not `IntegrityError`, so the `except` in `_enrol` did not catch it and the
login became a 500 (SEC-11). The audit named the first two; the address is a
third instance of the same class, found by tracing the claim flow rather than
by implementing the finding as written.

None of this is reachable by an ordinary Google account, because Google
constrains these fields itself. That is a statement about Google's input
validation, not about ours: an ID token is a signed assertion of whatever the
account says, and `_safe_picture` already accepted that argument once.

Reason:
**Shortening an identifier is an authentication bypass, not a tidy-up.** The
subject is what every login looks an account up by. Two subjects agreeing on
their first 255 characters would be shortened onto one stored `provider_subject`
and resolve to one account, so the second person would sign in as the first. The
same argument covers the address: at enrolment it *is* the account, and it is
what the collision check compares. For a value we cannot store whole, declining
the token is the only answer that cannot merge two people.

**Refusing decoration would be a cost paid to prevent nothing.** Nothing is
authorized by a display name, no lookup compares one, and no column is unique
on one — so shortening cannot collide anybody. Refusing would let an unusual
Google profile name lock somebody out of a product they have paid for, which is
exactly the reasoning `_safe_picture` gives for degrading rather than raising.
Shortened rather than dropped, because a name cut to fit is still a name and
`None` is an account with nobody's name on it.

**The address is measured lower-cased.** `normalise_email` stores the
lower-cased form and `str.lower()` can lengthen a string — U+0130 maps to two
characters — so measuring the value as written would let an address inside the
bound become one outside it on the way to the column.

Consequences:
The bounds are imports, not literals. Two numbers meant to be equal and written
down separately are a latent 500: the day a column changes, a copied validator
keeps passing values the column will refuse. `MAX_PICTURE_URL_LENGTH` is now
`MAX_AVATAR_URL_LENGTH` for the same reason, which removes a coincidence that
had been holding.

The update path is covered as well as enrolment. `_refresh_profile` follows
Google's name on every login, so an existing account was a second place an
unbounded claim reached a column — and the one where the damage would land on
somebody who already had a working account.

No migration: every bound is the width the schema already has.

## ADR-066 — An OAuth State Proves the Server Issued It; a Cookie Proves Who Asked

Date:
2026-09-02

Status:
Accepted

Decision:
Both Google `authorize` routes hand the browser a 256-bit secret in a
short-lived cookie and store its SHA-256 in the flow record beside the state,
nonce and PKCE verifier. Both callback routes require a cookie that hashes to
the stored digest, checked with `compare_digest` after the state is spent and
before the code is exchanged. It is the only cookie this API sets.

Context:
ADR-047 recorded a known gap and did not close it. The state is 256-bit,
single-use, ten-minute and server-side, which establishes that *this server
issued it*; because the API is cookieless there was nothing in a callback that
established *this browser asked for it*. So an attacker could complete a Google
authorization on their own account, capture the `code` and `state`, and induce a
victim's browser to post them — signing the victim into the attacker's account
(SEC-07, CWE-352). Linking was partly protected already, because the flow record
holds the initiating account, but only partly: it bounded *whose* account an
identity could land on, not *who* could cause the landing.

Reason:
**A cookie is the only thing that answers the question.** The missing property
is "this request comes from the user agent that started the flow", and a user
agent has exactly one place to keep something across a redirect it does not
control. PKCE binds the code to this *server*, not to a browser; a header cannot
survive Google's redirect; a value in the URL is not a secret because it travels
through the attacker's hands.

**What is on the browser is a secret, not a session.** No Google token, no PKCE
verifier, no nonce, no user or tenant id. Presented without a live state it is
worth nothing, and every other route ignores it — which is what leaves SEC-18's
reasoning intact: this API has no CSRF tokens because it authenticates by bearer
token, and it stays true because this cookie authorizes nothing. It is also why
introducing a cookie does not change the CORS argument in `Settings`.

**Only the digest is stored.** A reader of the OAuth keyspace in Redis should
not come away able to finish somebody's flow. SHA-256 rather than a slow hash
for `hash_invitation_token`'s reason: 256 bits of randomness leave nothing to
brute-force.

**One secret per browser, not per flow.** A fresh value on every initiation
would mean opening Google sign-in in a second tab silently broke the first, and
the refusal the person then saw would be indistinguishable from an attack.
Reusing a well-formed cookie makes concurrent flows work; the cost is that the
secret outlives one flow, which is acceptable for an `HttpOnly`, host-only value
that expires with the flow window and that anybody able to read could only read
by already owning the browser.

**The check runs after the state is spent and before the exchange.** After,
because the flow being consumed belongs to whoever presents it — in the attack
that is the attacker, so the burn costs them and not the victim, and the
victim's own flow is untouched. Before, because a callback from the wrong
browser must not cause an outbound request to Google or spend an authorization
code.

**Cleared on success, never on failure.** Success means the secret has done its
job. Clearing on failure would hand anybody who can induce one forged callback
the ability to destroy a legitimate flow running in the victim's browser — the
defence would become a denial of service against the person it defends.

Consequences:
The concurrency behaviour is stated rather than discovered: two flows started in
one browser share its secret and both are completable until one succeeds, at
which point the other must be restarted. `authorize` mints a new secret
immediately, so this is a restart and not a lockout, and it is pinned by a test.

`SameSite=Lax` is depth, not the mechanism — the binding holds whatever a
browser does with the cookie. It does require the frontend and the API to be
same-site, which is the topology `GOOGLE_REDIRECT_URI` already assumes.

The name is `__Host-wasla_oauth` where TLS terminates and `wasla_oauth`
elsewhere, because a `__Host-` cookie without `Secure` is rejected by browsers
outright: keeping the prefix over plain HTTP would mean no cookie and no local
Google sign-in, rather than a weaker one.

A flow record with no stored binding is refused rather than treated as
unbound — a check that switches itself off when its input is missing is not a
check. The cost is that flows in flight across the deploy that introduces this
are refused; they live ten minutes and the person retries.

No migration and no new configuration: the digest goes in the existing Redis
flow record, and whether the cookie is secure is derived from `ENVIRONMENT`.

## ADR-067 — Every Outbound Client Is Guarded, and the List Is a Test

Date:
2026-09-02

Status:
Accepted

Decision:
`PaymobProvider` builds its HTTP client with `build_guarded_client`, the same
constructor OpenAI, WhatsApp and Google use. `UnsafeUrlError` is caught in
`_post` and leaves as a non-retryable `ProviderError`. The claim that every
outbound client is guarded is now an assertion over every integration in
`tests/unit/test_outbound_pinning.py` rather than a sentence in a docstring.

Context:
`app/integrations/openai/client.py` already stated the rule: the guard is used
there although the URL is a constant, "so that the answer to which clients are
guarded? is all of them rather than a list that goes stale". Paymob was the
stale entry — a bare `httpx.AsyncClient` (SEC-08). Its URLs are constants from
`REGIONS`, so nothing a caller supplies reaches the destination and the exposure
needed a hijacked or poisoned resolver for `accept.paymob.com`. It was also the
one outbound request carrying `PAYMOB_SECRET_KEY`.

Reason:
**A rule with an exception is a list, and lists go stale.** The value of the
guard is not what it stops on any given day; it is that "which clients are
guarded?" has one answer. Restoring that is most of the point, and it is why the
fix is accompanied by a test that enumerates integrations rather than by a
comment.

**A guarded client and a retrying client are different concerns.** `_post`
creates payment intentions and charges saved cards. Nothing was added that
retries, and the test that says so is not decoration: an automatic second
attempt after a timeout would turn one payment request into two financial
operations. Retryability stays a *label* on `ProviderError` for a caller holding
an idempotency key to act on.

**Redirects stay off.** `GuardedTransport` judges the request it is handed, so a
client that followed redirects itself would take the second hop inside httpx —
below the guard, unresolved and unjudged. A public Paymob URL answering
`302 Location: http://169.254.169.254/` would then reach the metadata endpoint.
Paymob's API endpoints are JSON POSTs and do not redirect; if that ever changes,
the answer is a hand-written loop of guarded requests, which is what the
WhatsApp media download already does.

**A refused destination is not retryable, and that is the difference from a
timeout.** Nothing was sent, and pointing the same request at the same refused
destination produces the same answer, because the refusal is a statement about
where the request was aimed rather than about the network. Marking it retryable
would have a caller loop on its own configuration.

**`UnsafeUrlError` had to be caught.** It is deliberately not a `WaslaError`, so
uncaught it reaches the unhandled-error handler and becomes a 500 raised from
inside a route — the wrong report for "this deployment cannot safely reach its
payment provider". `GoogleOAuthClient.exchange` makes the same catch for the
same reason.

Consequences:
The regional hosts need no allow-list. `PAYMOB_REGION` selects between four
documented API hosts and the guard judges the address each resolves to at the
time of the request, which is the thing a list of names cannot do.

Nothing about the inbound side changed. Callback HMAC verification, event
deduplication, amount and currency checks and settlement are not HTTP clients
and are untouched; their tests are part of this change's regression set for
exactly that reason.

The log line for a refused destination names neither the address nor the host.
The refusal is the thing being probed for, so a log that described it would
become the oracle it exists to prevent.

---
## ADR-068 — Retryability Belongs to the Operation, Not Only to the Failure

Date:
2026-09-02

Status:
Accepted

Context:
A job on one of the three Redis queues had two outcomes: it succeeded, or it was
dead-lettered. There was no attempt count, no backoff and no classification, so
a document whose embedding call met a 502 was thrown away as decisively as a
document nobody could parse. The audit named this: "no exponential backoff and
no per-job attempt counter on the Redis queues… nothing drains or alerts on the
dead-letter list."

The obvious fix — retry everything a few times — is wrong here, and wrong in a
way that costs a customer rather than a developer. An agent turn ends by sending
a WhatsApp message, and the Cloud API takes no idempotency key; `WhatsAppClient`
already refuses to retry a timed-out send for exactly that reason. A generic
queue-level retry would undo that decision one layer up, re-running the whole
turn and sending a second answer to a question that already has one.

Decision:
Two independent judgements, and both have to say yes.

**The failure has a category.** `FailureCategory` is eleven values covering
everything the classifier recognises, and `RETRYABLE` is four of them:
`dependency_unavailable`, `provider_error`, `rate_limited`, `timeout`. Anything
else fails identically on the next attempt.

`unknown` is deliberately *not* retryable. An exception the classifier does not
recognise is one whose safety nobody has argued, and the honest response to an
unargued failure is to stop and show an operator rather than run it four more
times. Making a new failure retryable is a line in the map, not a default.

**The operation has a policy.** `IDEMPOTENT_RETRY` — five attempts, doubling
from two seconds to a ceiling of sixty, with jitter that only ever adds — is
carried by the ingestion and media workers, because re-ingesting replaces a
document's chunks and a file already read is not read again. The agent worker
carries `AGENT_RETRY` (three attempts) and narrows it to `NO_RETRY` the instant
`_TurnProgress.engaged` is set, which happens immediately before the HTTP client
is built.

That marker is the whole of this queue's retry safety in one expression. Before
it, the turn has loaded a workspace, read an allowance and looked up an agent —
all inside a transaction that rolls back, none of it visible outside the
process, all of it safe to repeat. After it, the turn may have reserved an
allowance, called the provider and sent a message, and no failure this worker
can catch distinguishes those from one that did not.

Consequences:
A transient blip on the agent queue is now retried instead of dead-lettered, and
a failure that might have sent a message still is not. The two cases are
distinguished by *where* they happened rather than by what was raised, which is
the only distinction that can be made honestly from an `except` block.

The jitter fraction is an argument rather than a `random()` call inside the
formula, so `delay_for` is a pure function and a test pins an exact delay
without patching `random` or watching a clock. `handle_failure` draws it when
the caller does not supply one.

Delays are scheduled in a Redis sorted set and promoted at the head of every
`reserve`. The `zrem` is the claim: two workers promoting the same entry at the
same instant both see it and only the one whose removal returns 1 pushes it, so
a retry cannot land on the pending list twice.

A queued entry is now a `JobEnvelope` carrying the payload as an opaque string
plus the attempt count, the original enqueue time and the last failure category.
`decode` accepts a bare payload as attempt 1, so a deploy that happens while
jobs are sitting in a queue does not strand them.

The three queues became one `ReliableQueue` with three namespaces. Retry
scheduling, attempt counting and dead-lettering are the kind of logic that goes
subtly wrong when written out three times, and everything that actually differs
between the queues is data.

---

## ADR-069 — The Worker's Metrics Travel Through Redis, Not a Second Listener

Date:
2026-09-02

Status:
Accepted

Context:
Metrics have to leave the process that produced them. For the API that is
trivial: it already serves HTTP, so each replica is its own scrape target, which
is the ordinary Prometheus model. The worker is the problem. It serves no HTTP
at all — its health probe is a *command* (`app/workers/health.py`) precisely
because of that — and yet it produces the numbers an operator most needs: job
outcomes, provider call results, everything about whether customer work is
getting done.

Decision:
The worker writes cross-process counters into Redis, one hash per metric with a
field per label combination, and the API renders them at scrape time.

Redis is already the cross-process channel for this pair: the worker writes
heartbeats there and a separate probe process reads them. Queue depths, retry
depths, dead-letter depths and heartbeat freshness are read live at scrape time
rather than published on a timer, because they are gauges of what is true now
and reading them is one command each.

Consequences:
The worker container gains no listening socket. That matters more than it
sounds: it holds the Meta access token, the OpenAI key and the Paymob secret,
and it currently accepts no inbound connection of any kind. Adding an HTTP
server to it to publish counters would trade a real reduction in attack surface
for a mechanism Redis already provides.

Counters are totals across replicas rather than per-replica. For queue work that
is the right shape — an operator asks how many agent jobs were dead-lettered,
not how many by which container.

A flushed or restored Redis resets them. A scraper handles that natively by
noticing the value fell, so nothing here has to preserve totals.

Two mechanisms exist rather than one, and the rule for which is which is
written at the top of `app/core/telemetry.py`: in-process for the API's own
request path, Redis for anything the worker produces. An HTTP request paying a
Redis round trip to be counted would be instrumentation taking part in the work.

The sink is module-level, set once per process at start-up, like the registry
beside it. The alternative was threading a Redis handle through
`WhatsAppClient`, `ResponsesClient` and every Paymob provider — none of which
has one, all of which would then take an argument used for nothing but counting.

---

## ADR-070 — The Scrape Endpoint Is Kept Off the Public Listener, Not Behind a Token

Date:
2026-09-02

Status:
Accepted

Context:
`/metrics` publishes operational shape: route names, request rates, queue
depths, provider error counts, which worker loops are alive. It carries no
customer data — `app/core/metrics.py` refuses an identifier as a label and a
test scans the whole rendered document for anything UUID-shaped — but it is
still not something to serve to the internet.

The reflex is a bearer token. Decision:

Decision:
No authentication on the endpoint. Three things stand between it and the
internet instead:

- The API container publishes no port; in `docker-compose.prod.yml` only nginx
  is reachable from outside the `internal` network.
- `nginx.conf` answers `404` for `/metrics` on the public listener rather than
  proxying it. Without that block the catch-all `location /` would forward it.
- `METRICS_ENABLED=false` removes it entirely, answering `404` — not `403`,
  because "this deployment serves no metrics" is the true answer and a `403`
  would confirm the endpoint exists.

Consequences:
A shared bearer token was rejected on its merits. Every scraper in a deployment
would hold the same one; it would have to be distributed to whatever collects
metrics; and it would protect a document that by construction carries nothing
worth stealing. A credential whose loss costs nothing is a credential that gets
treated as if losing it costs nothing, and it would then be one more secret in
the deployment inventory to rotate and to leak.

The access control is the deployment topology, which is the same access control
everything else the API serves on port 8000 already relies on. Making that
explicit — rather than leaving `/metrics` to be caught by nginx's `location /`
— is what this decision actually buys.

The endpoint is unversioned and excluded from the OpenAPI schema. A scrape path
is part of the deployment's shape rather than of the product's API, and putting
it under `/api/v1` would place it behind the public proxy's catch-all instead of
beside the paths nginx already treats specially.

`METRICS_ENABLED` is mapped in the deployment drift guard to both processes: the
API serves the exposition and the worker writes the counters it renders, so a
deployment that set it on one would silently publish half the signals.

---

## ADR-071 — Dead-Letter Replay Is a Command, and Refuses the Agent Queue

Date:
2026-09-02

Status:
Accepted

Context:
Once jobs stop being retried they have to go somewhere an operator can see, and
there has to be a way back out. The question is what that way looks like.

Decision:
`python -m app.workers.queues`, reachable through the image's `queues`
entrypoint command: `status`, `dead-letters <queue>`, `replay <queue>`. No HTTP
endpoint, and replay is never automatic.

`replay` refuses the agent queue unless `--force` is passed.

Consequences:
An HTTP endpoint for replay would need a platform role, a rate limit, an audit
trail and an answer to "what may a tenant replay" — and none of that is worth
building before anybody has needed it once. A command reachable only by whoever
can already exec into the worker container has exactly the audience this should
have.

Automatic replay is refused for the same reason automatic retry is bounded: a
loop that decided on its own to re-run dead-lettered work would turn a provider
outage into the same jobs failing round and round for ever, which is the
behaviour the attempt ceiling exists to prevent.

The agent refusal is the same idempotency argument as ADR-068, one layer up.
Ingestion and media are safe to replay; an agent turn ends in a message that
carries no idempotency key. `--force` exists because an operator who has read
the conversation may know better than the rule, and the refusal says so rather
than pretending the operation is impossible.

Replayed jobs go back as fresh first attempts. The old attempt count is what
said the job had run out of budget, and an operator replaying it has decided
that reason is gone; carrying the count forward would dead-letter it again on
the first failure without giving it the retry that was being asked for.

Dead-letter records survive the replay. A replayed job that fails again writes a
*new* record, and comparing the two is how an operator learns whether the replay
changed anything — which deleting the original would take away.

A dead-letter record carries the job type, the workspace, the attempt count, the
first and last attempt times, and a failure *category*. Never an exception
`repr`: provider error text can echo a customer's phone number or a fragment of
the request, and a dead-letter list outlives the incident and is read by whoever
is on call rather than by whoever wrote the code.

---

## ADR-072 — The Metrics Registry Is Written Here, So the Label Guard Can Be

Date:
2026-09-02

Status:
Accepted

Context:
Wasla needed a metrics layer. The default answer is `prometheus_client`: small,
ubiquitous, well tested.

Decision:
`app/core/metrics.py` implements counters, gauges, histograms and Prometheus
text exposition 0.0.4 directly, for the same reason the OpenAI integration is
written here rather than pulled in (ADR-013): the surface actually used is
small, well specified and stable.

The deciding factor was not the dependency. It was that a metrics library will
happily accept `tenant_id` as a label.

Consequences:
`_reject_unbounded` refuses an identifier-shaped label value at the moment a
sample is recorded — a UUID in any spelling, anything containing `@`, a
phone-shaped run of digits, a value longer than 96 characters. Label *names* are
fixed at declaration, so a sample carrying one the metric did not declare is
refused too.

That guard is the whole justification. A `tenant_id` label does not leak a
workspace's data; it multiplies every series by the number of workspaces until
the scraper falls over, weeks after the line was written, with nothing pointing
back at the line. The failure is silent, delayed and expensive, which is exactly
the shape that has to be made impossible in code rather than in review.

It **raises**. Every call site in the application reaches metrics through
`app.core.telemetry`, which swallows — so the guard is provable in a test and
can never fail a request. `tests/integration/test_metrics_endpoint.py` breaks
the recorder outright and asserts the request it was measuring is still served.

The exposition format is the interoperable one: an OpenTelemetry collector,
Grafana Agent, VictoriaMetrics or Prometheus itself all read it, so nothing here
commits the deployment to a particular monitoring stack.

A real bug this bought: the first `Histogram.render` summed the bucket counts a
second time, because `observe` already increments every bucket a value falls
under. It was caught by a test asserting the cumulative-and-total invariant
rather than an example, which is the test that stays true when the buckets
change.

---

## ADR-073 — A Backup Is Not a Script, It Is a Restore That Happened

Date:
2026-09-02

Status:
Accepted

Context:
The runbook said, in as many words, that there was no backup system and that
`postgres-data` was a Docker volume. The audit listed "backups and a rehearsed
restore" as blocking a real launch, with the note that a documented drill was
required rather than a unit test.

Decision:
`scripts/backup_postgres.sh` and `scripts/restore_postgres.sh`, a one-shot
`backup` Compose service behind a profile, host-scheduled by cron, and a restore
drill executed against real PostgreSQL 16 with pgvector before any of it was
committed. `docs/BACKUP.md` records the drill, including its output.

The restore script **verifies**: the schema has tables, `vector` and `pgcrypto`
came back, `alembic_version` is populated and matches `WASLA_EXPECTED_HEAD` if
set, and representative rows can be counted. The drill goes one step further and
reads the restored database through the application's own ORM.

Consequences:
The verification step is what makes this a procedure rather than a `pg_restore`
invocation. A dump that restores into a database the application cannot query is
not a backup, it is a file — and the pgvector check is the concrete version of
that: `document_chunks.embedding` is a `vector(1536)`, and a restore that lost
the extension would fail on that table and nowhere else.

The target database is always named. There is deliberately no "restore into the
configured database" path; overwriting the database `DATABASE_URL` names needs
`WASLA_RESTORE_ALLOW_PRODUCTION=yes`, an opt-in that cannot be reached by
leaving an argument off. The one thing a restore script must never do is the
destructive thing by accident.

Retention prunes only after a successful dump, and each dump is read back with
`pg_restore --list` before it is believed. A failed run cannot delete the last
good backup, and a truncated artefact that looks like a backup is worse than no
backup.

The password never reaches a command line or the output. It is exported into the
process environment and nothing echoes it, so `ps` on a shared host shows the
host, the user and the database name and no secret. This is asserted by a test
that runs the script with a password in `DATABASE_URL` and greps both streams.

**Redis is deliberately not backed up.** Queued work is a message answered late
rather than never — the message itself is in PostgreSQL. The one entry worth
thinking about is the refresh-token denylist, whose value is entirely in being
current, and `users.token_version` in PostgreSQL is the revocation mechanism
that actually survives.

**Media is not backed up, and the gap is stated rather than papered over.**
Attachments live on one host's volume; losing it loses them, and no amount of
`pg_dump` changes that. Object storage behind the existing `MediaStorage`
protocol is the fix and it is P2.

The scripts run from the *database* image, not the application one. `pg_dump`
has to match the server's major version and the image that certainly does is the
one running the server.

---
## ADR-074 — A Reservation Has a Lease, and a Crash Has a Stage

Date:
2026-09-02

Status:
Accepted

Context:
P1-C left one gap and named it: "a job reserved by a worker that dies remains
in-flight until an operator moves it." Reproduced before anything was changed —
a reserved job was still on the in-flight list after a simulated thirty days,
invisible to `pending`, `delayed` and `failed` alike, with no method on the
queue capable of recovering it.

That is not an observability problem. A stranded job is not a failure anybody
can see; it is an absence. The queue looked healthy the whole time.

The obvious fix — expire in-flight entries by age and requeue them — is wrong
here for the same reason a blanket retry was wrong in ADR-068. An agent turn
ends in a WhatsApp send that carries no idempotency key. A worker can die at any
instruction, including the one after Meta accepted the message, and a reaper
that requeued on age alone would send a customer a second answer to a question
that already had one.

Decision:
Three pieces, and the third is the one that makes the other two safe.

**A reservation, recorded in Redis.** `<namespace>:reservations` is a hash
keyed on the in-flight payload, holding the worker that took it, when, until
when, and how far it had got. An in-flight entry used to *be* the whole
reservation, which is why nothing could be asked about it.

**A lease that is renewed, not merely long.** Two minutes, refreshed by the
holding process every third of that. A lease without renewal forces a choice
between a timeout long enough to cover the longest job anybody might run — an
hour to notice a dead worker — and one short enough to notice quickly, which
gets stolen out from under a worker still using it. Renewal removes the choice:
the timeout can be short because a living process keeps saying so.

**A stage, written before the provider is engaged.** `_TurnProgress.engaged`
already existed and was in memory, which is exactly no use to a reaper looking
at a process that no longer exists. `mark_engaged` now writes that transition
to the reservation *before* the HTTP client is built, so the fact outlives the
process that knew it. Recovery then classifies:

    stage RESERVED   -> nothing left the process   -> requeue
    stage ENGAGED    -> a message may have been sent -> quarantine
    stage UNKNOWN    -> nobody recorded it        -> quarantine

on a queue that is not idempotent, and requeues in every case on one that is —
because re-ingesting a document replaces its chunks and a file already read is
not read again. That is a per-queue `idempotent` flag rather than a list the
reaper carries, so a new queue declares its own answer.

Consequences:
**A crash spends an attempt.** A recovered job returns as `next_attempt`
carrying its history, so a job that kills a worker every time exhausts its
budget rather than looping for ever, and one already on its last attempt is
quarantined rather than given a hidden extra one. Both are tested as
properties rather than examples.

**Recovery is idempotent by the mechanism the rest of the module already
uses.** `LREM` returns 1 for exactly one caller, so two reapers looking at the
same expired entry produce one outcome between them. Proving that needed a test
that holds both *at the claim* — an earlier version paused them inside the
enumeration and proved nothing, because the first through deletes the
reservation record and the second then adopts the entry instead of racing for
it.

**An in-flight entry with no reservation is adopted, not recovered.** That is
the crash in the microseconds between `BLMOVE` and the `HSET`, and giving it a
lease starts the clock so the next pass judges it by the same rule. Recovering
it on the spot would race a worker that is reserving right now.

`recovery` is a worker kind in `ALL_KINDS`. It is the only loop that does
nothing for its own queue — it reclaims what other processes were holding — so
a deployment running it nowhere has no crash recovery at all, and leaving it
out should take a deliberate act.

**What this does not detect.** A wedged event loop keeps renewing its leases,
because renewal asserts what the heartbeat asserts: the process is up and
scheduling. Every loop here is I/O-bound async, so a blocked one is a bug
rather than a state — but it is a bug this design cannot see.

---

## ADR-075 — A Backup Is Not Done Until It Has Left the Host

Date:
2026-09-02

Status:
Accepted

Context:
ADR-073 established that a backup is a restore that happened, and proved one.
What it did not establish was whether a real deployment would ever *take* those
backups, or whether they would survive the thing they exist for. The schedule
was a documented cron line rather than a shipped unit; the destination was a
directory on the host running the database; and the freshness signal was going
to be the age of the newest file in that directory.

That last one is the trap. A deployment whose `pg_dump` succeeds nightly and
whose upload has been failing for a week has plenty of dumps and no recovery
point — and "newest file in `BACKUP_DIR`" calls it healthy.

Decision:
**A run succeeds when the artifact is verified at an off-host destination**,
and not before. `backup_postgres.sh` dumps, validates with
`pg_restore --list`, uploads through `scripts/upload_backup.sh`, has the store
confirm the object's size, and only then advances the recorded last success.
Anything that stops earlier leaves the previous success where it was.

**One backend, which is not one provider.** `aws s3` with an optional
`BACKUP_S3_ENDPOINT_URL` reaches AWS, MinIO, Cloudflare R2, Wasabi, Backblaze
B2 and Ceph, so a single implementation covers every object store a deployment
is likely to choose without this repository choosing one. `BACKUP_DESTINATION=none`
is refused rather than skipped.

**A deployment that needs something else replaces the script.** A file
boundary, deliberately, rather than a `BACKUP_UPLOAD_COMMAND` string something
would have to `eval` — nothing here ever hands attacker-influenced text to a
shell.

**The freshness signal is a file, because it has to be.** The backup runs in
its own container and exits; an in-memory counter dies with it. So it writes a
small JSON status — outcome, last success, artifact name, byte count,
destination kind, failure count, failed stage — and the API mounts that
**read-only** and publishes `wasla_backup_age_seconds` from it. Nothing in the
file is a credential or a bucket name: it is exactly the sort of thing that
gets pasted into a support ticket.

**A shipped systemd timer**, not a documented cron line. `Persistent=true`, so
a host that was down at 02:17 runs the backup when it comes back rather than
skipping the day and silently doubling the recovery window.

Consequences:
The backup runs in its own image (`Dockerfile.backup`): the `pgvector` base for
a `pg_dump` that matches the server's major version, plus `awscli` for the
upload. Putting `postgresql-client` in the application image instead would add
it to every container serving traffic for the benefit of a process that runs
once a day. The **restore** script ships in that image too — on the day
somebody needs it, the application image may not build and the repository may
not be reachable, and what certainly exists is whatever was pulled to run last
night's backup. The drill found that missing.

**Only the backup container holds object-store credentials.** A compromised API
or worker must not be able to delete the database *and* every copy of it, and
the deployment drift guard now asserts both directions of that.

Two retentions with different owners. Local staging is pruned by this script
after the upload succeeds; what the object store keeps is a lifecycle rule
there, which outlives this host, survives a bug in a shell script, and does not
require giving the uploader delete permission on the bucket.

The disaster drill destroys the staging volume before restoring, so what it
proves is the remote copy. It ran against MinIO over the S3 API — the same
client and the same protocol as a real provider, and not that provider's IAM,
TLS chain or rate limits, which `docs/BACKUP.md` says plainly.

No RPO or RTO is claimed. What the schedule *implies* is written down as an
observed fact; adopting a target means measuring a restore at production scale,
and there is no production.

## ADR-076 — A File Is What Its Bytes Are, Not What Its Sender Called It

Date:
2026-09-02

Status:
Accepted

Context:
Every media type in this system arrived as a string somebody else wrote.
`file.content_type` from a browser on the upload route, and `mime_type` from
Meta's media descriptor on the download path. Both were believed, and both
decided real behaviour: which of Meta's four attachment kinds a file was sent
as, whether the media reader handed it to a vision model or a transcriber, what
went into the `mime_type` column, and what `Content-Type` it was served back
with.

Worse, the outbound check was a family prefix. `_whatsapp_kind` read
`mime_type.split("/")[0]` and mapped `image`, `audio` and `video` straight
through, so `image/svg+xml` was an image, and so was any invented
`image/x-whatever`. Nothing anywhere opened the file (SEC-09).

The exploitable shape is narrow but real: a PDF, an HTML page or an archive
uploaded as `image/jpeg` was accepted, uploaded to Meta as an image, stored as
an image and served back to a colleague with `Content-Type: image/jpeg`. It was
kept out of the High band only by `Content-Disposition: attachment` and
`nosniff`, which are the controls that stop the *browser* acting on the lie —
not controls that stop the lie.

Decision:
**The claim is a hint. The bytes are the answer.** `app/core/media_types.py`
identifies a file from a bounded prefix of its own content and returns a
canonical type, and everything downstream uses that: what Meta is told, what
the reader routes on, what lands in the database, what is served back.

**An exact allowlist, no families.** `CANONICAL_TYPES` enumerates every type
this product supports, derived from what the reader can actually read and what
a colleague can actually send. There is no wildcard left to widen, and
`image/svg+xml` — a script that a browser will execute given half a chance — is
not in it.

**A signature table, not libmagic.** `python-magic` needs a system package in
the runtime image and a second thing to patch. The supported set here is small
and fixed, so a table for exactly those formats is smaller than the dependency,
has no deployment story, and is tested exhaustively. The cost is stated rather
than hidden: an unusual-but-valid file of a supported type whose signature the
table does not recognise is refused. That is the direction to fail in.

**Containers that genuinely carry two things narrow to two.** Matroska is audio
or video with one signature, and an OLE2 compound document is Word or Excel; in
both cases detection narrows to the pair and the *claim* picks within it. That
is the only thing a claim is allowed to do, and it can never widen the set.

**A conflict is a refusal, not a correction.** Silently relabelling a file whose
declared type contradicts its content would carry on with something that is
either a broken client or an attempt, and would throw away the evidence. On the
upload route it is a 400; on the download path it is `SKIPPED`, because no
retry turns those bytes into the announced type.

**Absent is not conflicting.** A missing header and `application/octet-stream`
say the same thing, and the bytes decide alone. Where detection narrows to a
pair there is usually no honest way to choose, and the file is refused — but
text is the exception, and the exception is the point: `text/plain` and
`text/csv` take the same route through extraction, are the same class, and are
both served as an attachment that is never rendered, so the distinction has no
consequence and picking the conservative half of it costs nothing. Refusing a
plain-text attachment for want of a header somebody's client did not send would
cost a customer their file to settle a question that changes nothing.

**The cap is enforced during the read.** `fetch_media` now takes a required
`max_bytes` and streams, abandoning the body mid-chunk when it passes. A
buffered fetch learns a file was too big only once the process is holding it,
which makes the limit a description of what already happened. The upload route
reads in chunks for the same reason.

Consequences:
Detection answers "are these bytes a supported container of a known format?".
It does **not** prove the file is harmless: a valid JPEG can carry a decoder
exploit, a valid PDF can carry JavaScript, and a polyglot can be a legitimate
JPEG and a legitimate ZIP at once. Nothing here scans for malware and nothing
here should be described as if it did. What it removes is the class where a
file is processed and served as a type it is not.

Two consequences a reader should expect. Bytes that decode as text are
`text/plain`, which means an HTML file or an SVG **can** be stored — as text,
served as text, with the disposition and `nosniff` that were always there. And
the download handler serves the stored type only if it is canonical: rows
written before this ADR hold whatever a caller claimed, and echoing one back
would keep the defect alive for every file already in the store.

Meta's own format support is narrower than this table in places. A file Meta
will not carry comes back as a recorded rejection on the message rather than as
a guess made locally, which is the same posture every other send already takes.

## ADR-077 — Media Belongs in an Object Store, Signed Here

Date:
2026-09-02

Status:
Accepted

Context:
ADR-023 named the storage boundary and shipped one implementation behind it:
local disk. That was always a stated limitation rather than a design. It
requires the API and the worker to share a volume, which means one host — and
one host is a single point at which every attachment every workspace ever
received disappears, with the PostgreSQL backup carrying none of it. The audit
called local-disk media a hard single-host ceiling and, separately, a disk-full
incident waiting to happen.

Decision:
**An S3-compatible backend behind the existing `MediaStorage` protocol**, chosen
by `MEDIA_STORAGE_BACKEND`. Local disk stays for development and is still the
default; nothing about the interface, the key format or any caller changes.

**One protocol, not one provider**, following ADR-075. AWS S3, MinIO, Cloudflare
R2, Wasabi, Backblaze B2 and Ceph all speak the S3 object API, so one
implementation reaches any of them and this repository chooses none.

**The requests are signed here rather than by `boto3`.** The SDK is synchronous,
so every `put` and `get` would cross a thread boundary in an application that is
asynchronous end to end; it is a large addition to a deliberately small runtime
image; and it brings its own retry, timeout and endpoint-resolution behaviour
that would have to be pinned to match what every other client here does. The
four operations this needs — PUT, GET, DELETE and HEAD on one object — are the
simplest possible use of SigV4: no multipart, no pagination, no session tokens.
The drill against a real MinIO is what proves it, and a signing bug is not
subtle: the store answers 403 and nothing works.

**This client is deliberately not `build_guarded_client`.** That guard exists
because integration clients fetch URLs arriving in somebody else's response, and
the worker sits inside the deployment network. An object store is not in that
class: its endpoint comes from configuration and from nowhere else, and
`http://minio:9000` is the correct value for a self-hosted stack — exactly as
`DATABASE_URL` and `REDIS_URL` point at private addresses by design. Guarding it
would break the ordinary deployment while protecting against nothing.
`tests/unit/test_outbound_pinning.py` asserts the exemption and its reason
rather than leaving it in a comment.

**Fail closed, in every environment including `test`.** Selecting `s3` with an
incomplete configuration refuses to start. A silent fall back to local disk
would give the API and the worker each their own copy on their own container,
every download would be a coin toss, and nothing anywhere would say why.

**A different credential from the backups.** `MEDIA_S3_*` rather than the
`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` pair the backup container holds.
Media storage hands the API and the worker an object-store credential for the
first time, and the property that keeps ADR-075 true is that it is a *different*
bucket under a *different* key: a compromised application container still cannot
reach the copies of the database. The deployment guard asserts both directions,
including that the backup service holds no media credential — the container
whose job is deleting old files must not have every customer attachment in its
blast radius.

**Private, with no way to ask otherwise.** No ACL header is ever sent, no
presigned URL is ever produced, and a test asserts that over the module's code
with its comments and strings stripped, because the property is "never, on any
path" and the way it stops being true is a branch nothing exercises. Bytes reach
a colleague by being streamed through the authenticated API.

Consequences:
Keys are unchanged — `{tenant}/{year}/{month}/{uuid}{ext}` from the same
`build_key` — so a key written by one backend is a key the other can read, and
migrating is copying objects rather than rewriting rows. The key is validated
against the same pattern on the way out of both, because a key read back from a
database row is input whatever wrote it.

**A key prefix is a layout, not a boundary.** Tenant isolation is the scoped
repository, and nothing about object storage changes that; the isolation tests
run against both backends for exactly that reason.

Storage errors are translated at the adapter. A caller gets `StorageError`,
whose message names no infrastructure at all; an operator gets a log line with
the operation and the status code, and never the bucket, the endpoint, the key,
the credential or the store's own error body — which a misconfigured gateway
will happily echo request headers into.

The object-store tests skip without `TEST_S3_ENDPOINT_URL`, following the
convention the PostgreSQL suite already uses. `docker compose --profile
objectstore up -d minio` is enough to make them run, and they are worth running:
mocking a `boto` call proves nothing about a signature.

## ADR-078 — Retention Deletes the File and Keeps the Record

Date:
2026-09-02

Status:
Accepted

Context:
`MediaStorage.delete` existed from the day the interface was written and no
caller ever invoked it. Nothing swept the store, so it grew monotonically — on
a single host that is a disk-full incident with no warning, and it takes the API
and the worker down together because they share the volume (BUG-006). Object
storage removes the disk-full failure and does nothing about the growth; a
bucket that only ever accumulates is a bill that only ever rises, and a set of
customer photographs that is kept for ever by accident rather than by policy.

Decision:
**The file goes, the row stays.** `message_media.transcript` holds what Wasla
concluded a photograph or a voice note said. That is conversation history: it is
what the agent was shown and what a colleague reading the thread sees, and
deleting it would silently rewrite the record of a conversation. Retention
removes the copy of the original bytes and leaves a row that says plainly there
was a file and that it was removed.

**Zero is the default, and it means "keep everything".** How long a business
keeps what its customers sent it is not a decision this code can make. Any
number here would be invented, and an invented number that deletes customer data
is worse than no sweep at all. `MEDIA_RETENTION_DAYS` is what a deployment sets
when it has decided.

**The claim is committed before the object is touched.** Removing an object and
clearing the column that points at it are writes to two systems, and no
transaction spans both. Both orders fail, and they fail differently:

- delete then commit → the commit fails and the row points confidently at a file
  that is gone, indistinguishable from a broken store;
- commit then delete → the delete fails and nothing anywhere remembers the
  object exists. A permanent orphan no query can find.

So `purge_started_at` is set and committed first. Set with `storage_key` still
present means "being removed, and may or may not still be there"; a pass that
dies anywhere leaves that state, and the next pass deletes again — a no-op on an
object already gone, in both backends — and finishes. The cost is a window in
which a row says a file is going and the file is still readable, which is the
right way round: briefly serving an attachment that was due for deletion is a
smaller failure than telling somebody a file is there when it is not.

**A reconciliation pass, separate from the age query.** A claimed row can
outlive its own eligibility: raise `MEDIA_RETENTION_DAYS` after a failed sweep
and the age query stops selecting the rows it left half-done, which would strand
them for ever with their objects still in the store. Reconciliation runs first
each pass, so work already decided is finished before more is taken on.

**A poll, not a queue.** Enqueueing one job per file would put the deletion of
customer data behind the replay command, where an operator could re-run a
dead-lettered purge weeks later against a row since re-populated. The claim in
the database is a better record of intent than a job payload, it survives Redis
entirely, and resuming after a crash is a query rather than a recovery
mechanism — the same reasoning as the billing sweep (ADR-022).

Consequences:
One nullable column and one partial index (migration 0038), reversible, and a
no-op for a deployment that never sets a retention period.

A purged file answers 404 with the reason rather than failing as a storage
error. "Deleted on purpose" and "the store is unavailable" are different
sentences, a colleague acts on them differently, and only the row can tell them
apart.

**What is deliberately not built: a bucket-listing orphan sweep.** The
reverse direction — an object with no row referencing it — cannot be found
without listing the store and deciding from age, and a sweep on that rule
eventually deletes a live file whose row it failed to read. That remains true
and is now structural rather than a resolution (ADR-087).

One narrow orphan was left open here and is **closed by ADR-087**:
`MediaService.download` wrote the object and then committed the row, so a
transaction that failed after the write left an object nothing referenced. It
was invisible to every query in this module, and closing it needed a durable
intent written *before* the object. That intent turned out to be four columns on
the row that already exists rather than the second table this paragraph
anticipated, and the failure was reproduced against a real store before it was
fixed rather than remaining one nobody had observed.

`wasla_media_retention_total` carries one label with three fixed values —
`purged`, `failed`, `pending` — and no tenant, media id, filename or key
anywhere near it. `pending` is the one to alert on: a store refusing deletions
is otherwise invisible, because the rows are claimed, the sweep reports itself
as having run, and the volume simply does not shrink.

**On a versioned bucket, deleted does not mean gone.** A delete leaves a delete
marker and the previous versions remain until the bucket's own lifecycle rule
expires them. What retention guarantees is that the object is no longer
retrievable through Wasla; making it unrecoverable is a rule on the bucket, and
`docs/MEDIA.md` says so rather than letting the word "deleted" imply more than
it should.

## ADR-079 — An Approximate Index Is Three Decisions, and Two of Them Are Not the Index

**Context.** `document_chunks.embedding` had no index. Every knowledge search
computed a cosine distance for each retrievable chunk in the workspace and
sorted the lot, which the P2 audit named as the single largest scaling blocker
on the hottest path. The obvious fix is one line of DDL.

Measured on 77,000 chunks across 36 workspaces (PostgreSQL 16.14, pgvector
0.8.6), that one line makes retrieval **worse**.

**Decision.** Build an HNSW index with `vector_cosine_ops`, and set two
PostgreSQL parameters per retrieval query without which it is not usable.

**Why HNSW and not IVFFlat.** IVFFlat trains its lists against the rows present
at build time. A knowledge base is empty when a workspace signs up and fills up
over months, so an IVFFlat index built by a migration is trained on nothing and
stays that way until somebody reindexes — and "somebody reindexes" is an
operational commitment, not a design. HNSW has no training step: it is correct
on an empty table and stays correct as one fills. Defaults for `m` and
`ef_construction` were kept because nothing measured argued for changing them,
and both cost build time on the table this system writes to most.

**Why the opclass is not a detail.** `KnowledgeRepository.search` orders by
`<=>`. An index built with `vector_l2_ops` is created, occupies the same 597MB,
appears in `pg_indexes`, and is never once considered by the planner. Retrieval
stays exactly as slow as it was and nothing anywhere says so. The suite asserts
the opclass against `pg_opclass`, and the plan test fails too when it is wrong —
which is the actual proof, because the catalogue looks the same either way.

**`hnsw.iterative_scan = strict_order`, or the index answers with silence.**
pgvector indexes one column, so the tenant, knowledge-base and READY filters
are post-filters on the approximate path. By default the scan visits
`ef_search` candidates in global distance order, discards those belonging to
other workspaces, and answers with whatever survived. For a workspace holding a
small share of the corpus that is reliably nothing: a 200-chunk workspace in a
32,000-chunk table returned **zero passages out of five**. The agent was then
told, as far as it could tell truthfully, that the knowledge base had no answer
— a query that succeeded, documents that were present, and a customer told "I
do not have that information". `strict_order` makes the scan resume until it has
the rows it was asked for, and hands them back in distance order, which is what
the query's `ORDER BY` promises. `relaxed_order` is faster and does not promise
that.

**`plan_cache_mode = force_custom_plan`, or the index is abandoned after five
queries.** The retrieval statement is prepared once per pooled connection.
PostgreSQL builds custom plans for five executions, then compares a generic plan
and keeps it if it estimates cheaper. A generic plan cannot know which workspace
is asking, so it estimates the tenant filter from the average workspace, decides
a nested loop over every document is cheap, and holds that opinion for the life
of the connection. Measured through the repository on a 45,000-chunk workspace:
searches one to five took 7ms, and every search after that took 250ms. Same
query, same connection, same data — run often enough. This trap pre-dates the
index and was costing 250ms against a 190ms exact scan; with the index available
the generic plan is worse still, because the ANN scan it declines is the one
that would have taken 8ms.

Scoped to this query rather than set on the engine. Every query in this system
filters by `tenant_id` and workspace sizes vary by two orders of magnitude, so
the same trap plausibly waits elsewhere — but "plausibly" is not a measurement,
and turning off generic plans platform-wide on a hunch is how a performance fix
becomes a performance regression somewhere nobody was looking.

**What is deliberately not built: a size threshold in the repository.** The
planner's cost crossover sits at roughly 26,000 retrievable chunks in one
workspace. Below it the exact scan is chosen, and for the 200-chunk workspace
that is right by a factor of 36 — forcing the approximate path there costs 54ms
against 1.5ms, because the scan spends its whole budget discarding other
workspaces' vectors. Above it the approximate index is chosen, and the exact
scan it replaces was growing linearly with no ceiling. Between roughly 3,000 and
26,000 chunks the planner keeps the exact scan where the approximate one would
have been faster, and that is accepted: the alternative is application code
choosing between two query shapes from a row count, where a wrong threshold is a
silent retrieval-quality regression rather than a query somebody notices is slow.

**Consequences.** Retrieval on the largest measured workspace goes from 236ms to
7.9ms. Small workspaces pay 0.7ms for the one round trip that carries both
settings, and get complete results in exchange. Recall at that scale is 1.000
against the exact answer. The index is 597MB per 77,000 chunks and takes ~14
minutes to build, so migration 0039 builds it `CONCURRENTLY` in an Alembic
autocommit block — a plain `CREATE INDEX` would block document ingestion
platform-wide for the duration. A failed concurrent build leaves an invalid
index behind, so the migration drops by name before creating: a retry must build
rather than adopt something half-finished.

## ADR-080 — A Turn Gives the Connection Back Before It Waits

**Context.** One session spanned an agent turn and the provider was called
inside it, which `app/workers/ai_worker.py` disclosed in its own module
docstring rather than hiding. A pooled connection was therefore held for the
length of an inference, so the number of turns a worker process could run at
once was `pool_size + max_overflow` — 15 by default — regardless of how deep the
queue was. The P2 audit ranked it second behind the missing vector index.

The reason it was written that way is real: tools mutate rows during the round
loop, so the session was the consistency boundary for the turn, and the stated
alternative — writing through a second session — would have let a handoff commit
while the reply explaining it did not.

**Decision.** `AgentOrchestrator` ends its transaction and hands the connection
back before every provider call, and before every per-round reservation. The
primitive is `app.db.session.released`, which is a commit and says so: SQLAlchemy
returns a connection to the pool when a transaction ends, and nothing less than
ending it releases anything.

**Why committing at those points is not the failure the old comment feared.**
The feared shape was a second session writing concurrently with the first. This
is one session, committing at boundaries where what is staged is finished work:

- Before the *first* inference nothing is staged. The phase above only reads,
  and the sentiment assessment — the one thing before it that writes — releases
  and commits its own reading, which is a unit of its own with its own paid
  provider call and its own meter.
- Before a *later* inference what is staged is the completed work of the
  previous round's tools. A tool has run and either succeeded or reported its
  failure to the model; there is no half-written state at that line.
- A handoff can never be caught mid-commit, because `handoff_to_human` breaks
  the loop. No reply is ever composed after a committed handoff, which is what
  the original concern was actually about.

**What it costs, and it is a real cost.** A turn is no longer one transaction.
A turn that dies partway now leaves the work it finished instead of none of it.
That is the better direction for this queue specifically: it is `NO_RETRY` once
the provider is engaged (ADR-068), so a rolled-back lead is a lead nobody will
ask the customer for again.

**A commit ends a snapshot, so state has to be read again.** Between the
reservation and the reply the world can move, and exactly one thing that moves
changes the right answer: a colleague taking the conversation. `_taken_over`
re-reads `conversations.mode` as a scalar column before the orchestrator offers
a reply — a scalar and not a repository fetch, because the row is already in the
identity map and a `select` returning the mapped object hands back the instance
with the attributes it was loaded with, which is the stale value the re-read
exists to avoid. The reply is discarded rather than sent, and the tokens are
still metered, because the inference happened.

The other candidates were checked and need nothing. The allowance is
re-resolved every round already: `_reservation` builds a fresh
`EntitlementService` on a fresh session per round, so a subscription suspended
mid-turn is seen by the next round — falling back to the default plan's limits
rather than to a lockout, which is the existing policy and is now tested.
Agent configuration is deliberately *not* re-read: the turn was authorised under
the configuration it loaded, and swapping a system prompt between rounds of one
conversation would be worse than using a snapshot.

**The reservation moved inside the released block, and that ordering is load-
bearing.** `consume` holds an advisory lock until its transaction ends, so it
gets a session of its own — which means two sessions want connections at the
same moment. Asking for the second while the turn still holds the first needs
two connections from a pool that may have one, and on a pool of one it
deadlocks. Releasing first makes the peak one connection instead of two.

**What is deliberately not built: a separate pool for agent turns.** It was the
audit's alternative suggestion and it treats the symptom. A dedicated pool still
holds a connection across an inference; it just holds it somewhere else, and it
adds a second pool to size, monitor and get wrong.

**Consequences.** Proved rather than argued, in
`tests/integration/test_provider_session_lifetime.py`: two turns are inside the
provider call simultaneously through a pool of exactly one connection with no
overflow, and the pool's own checkout count is zero while they wait. A control
test asserts the pool really is one connection, so the proof cannot pass
vacuously. Moving the call back inside the transaction makes that test fail with
`QueuePool limit of size 1 overflow 0 reached`, and makes a unit test fail on the
commit/respond ordering — which is the cheap version of the same guard.

The WhatsApp send is left inside the transaction on purpose. It is not the same
boundary: the message row is flushed before the call so an attempt exists as a
row whatever happens next, and that is the quarantine design ADR-074 depends on.

## ADR-081 — usage_events Got an Index, Not a Rollup, and the Reason Is a Number

**Context.** The P2 audit ranked `usage_events` fourth: append-only,
unpartitioned, and the aggregate behind every entitlement check. The obvious
answers are a rollup table or time partitioning, and both are substantial.

**Decision.** Carry `quantity` and `unit` as INCLUDE columns on
`ix_usage_events_tenant_id_event_type_occurred_at`, and build neither a rollup
nor partitions. Raw events stay the only source of truth for money.

**What was measured.** 3.9 million events across 50 workspaces over nine
months, shaped from the plans the product sells — one workspace at the Business
tier's volume, a middle cohort, a long tail. Four query shapes, taken from the
repositories rather than invented:

| query | 1.3M rows | 3.9M rows |
| --- | --- | --- |
| entitlement period check | 8.8ms | 9.4ms |
| workspace dashboard totals | 20ms | 33ms |
| workspace daily series | 63ms | 66ms |
| platform `by_tenant` | 64ms | **172ms** |

**The hottest query does not have a growth problem, and that was the surprise.**
`_period_usage` sums one meter over one workspace's current billing period, and
the number of rows in that sum is bounded by *the plan limit*, not by the size
of the table: a workspace that has spent its 25,000 AI requests stops making
them. So it scanned 26,035 rows at 1.3M and 26,035 rows at 3.9M, and the
timing barely moved. The audit's framing — "at platform scale this table
dominates" — is right about the table and wrong about this query.

What it did have was a heap visit per row, to read two narrow columns it could
have been handed: 219 index buffers and 916 heap buffers to add up numbers.
INCLUDE turns it into an `Index Only Scan` with `Heap Fetches: 0` on a
committed, vacuumed table — 9.4ms to 7.1ms, with the other three shapes flat.
That matters more than 2ms sounds like, because `consume` runs this query while
holding the workspace's advisory lock, so it is time every other conversation in
that workspace spends waiting.

There is a second reason, visible only in a cold cache. On a 1.3GB table that no
longer fits in `shared_buffers`, the same 26,035-row check measured **50ms**,
because the heap pages it needed had been evicted. Row count is bounded by the
plan; page residency is bounded by nothing. An index-only scan reads a few
hundred pages of a small hot index and is indifferent to how large the table
has grown around it.

**Why not a rollup.** The one query that grows without bound is the platform
`by_tenant` aggregate: 64ms to 172ms for a 3× table, a parallel sequential scan
because a month of a nine-month table is not selective enough for the
`occurred_at` index. That is linear in total rows and arithmetic guarantees
where it ends up. But it is a SaaS-owner dashboard measured at 172ms, and a
rollup is a table, a worker, bucket boundaries, late-arrival handling, rerun
idempotency and — the part that actually costs — a second set of numbers that
can disagree with the invoice. Building that for a query nobody is waiting on
is how money ends up depending on eventually-consistent aggregation. The
trigger is recorded instead: **when the platform usage window crosses roughly
50 million rows, or the dashboard passes a second, the answer is a daily rollup
keyed `(tenant_id, day, event_type)` with `INSERT ... ON CONFLICT DO UPDATE`,
serving dashboards only, with entitlements still reading raw.**

**Why not partitioning.** Monthly partitions would prune that platform query to
one partition and would make retention a `DROP TABLE`. They would also mean
converting the highest-write table in the system in place, plus insert routing,
future-partition creation and a maintenance job that must never fall behind —
and the query benefit is the same benefit a rollup gives more cheaply. The
prompt for this phase put it well: do not partition because a table is
append-only. Nothing measured here argues for it yet.

**A defect found by doing the measuring.** PostgreSQL puts a parallel query's
shared tuple store in `/dev/shm`, Docker gives a container 64MB of it by
default, and neither Compose file set `shm_size`. Running the platform roll-up
over four million rows produced `could not resize shared memory segment ... No
space left on device` and restarted the server into crash recovery. It is now
`shm_size: 1gb` in both files. This was not a benchmark artefact — it is what
the shipped Compose file does to a production database the first time an
aggregate is big enough to be worth parallelising.

**Consequences.** One index changed, no new tables, no new worker, and usage
accounting untouched: the same rows, the same half-open windows, the same
`occurred_at` bucket. `tests/integration/test_usage_index_only.py` asserts the
index-only total equals a total read from the table with every index refused —
across three workspaces with deliberately different quantities, on both window
boundaries, and after a late event written for a day already over. Removing the
INCLUDE columns fails the schema and plan tests and leaves the correctness tests
passing, which is the right split: it is a performance regression, not a
wrong number.

The migration drops and recreates, because PostgreSQL cannot add an INCLUDE
column to an existing index, and does both `CONCURRENTLY` — usage is written on
the path that answers customers, so an index rebuild that blocks inserts there
blocks replies.

## ADR-082 — The Billing Sweep Claims Its Work, and Two Workers Divide a Cohort

**Context.** `BillingWorker.run_once` opened one session, read up to
`CLAIM_LIMIT = 200` due subscriptions with no row locks at all, did everything
to all of them, and committed at the end. The audit called it a throughput
problem — 200 per ten-minute pass is 1,200 an hour, so a first-of-the-month
cohort takes hours. It was also a correctness problem, and that is the half
worth writing down.

Two workers on that code both read the same due subscriptions and both called
`issue_for_period`, which checks for an existing invoice and finds none because
the other worker has not committed. Both insert. The second blocks on
`uq_invoices_tenant_id_period_start` until the first commits, then raises — and
an `IntegrityError` aborts the whole PostgreSQL transaction, so every later
statement in the loser's sweep fails too. One worker's pass was lost to
discovering that another worker existed. `_claim_attempt` was already better
behaved, using a savepoint, but it still *blocked* until the winner committed,
and the winner committed at the end of a pass that included Paymob requests.

**Decision.** Every phase claims its rows with `FOR UPDATE ... SKIP LOCKED`,
each claim is processed in its own transaction, and each phase drains rather
than taking one batch.

**What is claimed, per phase, and why that row.**

The **subscription** for roll-over and invoicing. It is the consistency owner
for everything a sweep does to a workspace: the invoice is unique per
`(tenant_id, period_start)`, and the period bounds and status are columns on
this row. There is one per tenant, so holding it holds the workspace.

The **invoice** for collection. An attempt belongs to an invoice, and two
workspaces' invoices are independent work that should proceed at once.

**Both** for dunning, and that pair is the subtle one. A workspace can have
several overdue invoices, so a claim on the invoice alone lets two workers take
one each and both transition the same subscription — two audit rows, two
notices, and a race on the outbox idempotency key. The transition belongs to the
subscription, so the subscription is claimed with it.

**Why `SKIP LOCKED` rather than `FOR UPDATE`.** A row another worker holds is
another worker's work. Waiting for it means waiting to discover that, and with
one lock held per claim that is a convoy: the second worker finishes the pass
having done nothing but queue. The proof is a test — with `SKIP LOCKED` removed,
the "a row another worker holds is skipped" test stops finishing and times out.

**Why the transaction is per claim.** A lock lives until the transaction ends,
so one transaction per sweep holds every claimed row for the duration of the
whole pass, including for the length of a Paymob request made on behalf of some
other workspace. It also meant one workspace's failure discarded every other
workspace's committed work. Per claim, a failure is contained to its own
workspace and the rest of the pass stands. What still commits together is what
always had to: a status change, its audit row and its notice (ADR-042).

**One lock is deliberately held across an HTTP call, and it is the invoice's
own.** `_collect_one` claims an invoice and then reaches Paymob inside that
transaction. That is not an oversight and it is not avoidable: the lock *is* the
mutual exclusion for "one attempt reaches the provider". Releasing it before the
request would leave only the payment row's `UNIQUE(tenant_id, idempotency_key)`,
which catches a duplicate after the money has moved rather than instead of it —
demonstrated, by unlocking the re-claim and watching a charge duplicate once the
key is bypassed too. What changed is the scope: the lock is held for the length
of *that invoice's* request, where before one transaction held every claimed row
for the sum of every request in the pass, including requests made on behalf of
other workspaces.

**Why the claim is taken twice.** A batch query claims ids and commits, which
releases its locks; the transaction that acts re-claims by id with the same
eligibility predicate. Acting on a row whose lock has been released is the
duplicate this design exists to prevent, and re-asking under the lock is what
makes "one attempt reaches the provider" a property of the row rather than of a
unique key that fires after the money has moved.

**Two starvation bugs found while doing this, both pre-existing.** Neither was a
concurrency problem; both were a fixed limit meeting a predicate that processing
does not clear.

Chasing an invoice moves its *subscription* to `PAST_DUE` and leaves the invoice
exactly as overdue as it was. With the status checked in the worker, a chased
invoice stayed eligible for ever and held its place at the front of every later
batch — so a deployment with more than `CLAIM_LIMIT` already-chased invoices
never reached the ones behind them. The subscription status is now a predicate
in the claim query, so a processed row leaves the set.

An invoice whose collection attempts are spent has `next_collection_at IS NULL`,
which is indistinguishable from one nobody has tried, so it too kept a slot for
ever. `MAX_COLLECTION_ATTEMPTS` is now passed into the claim query — passed in,
not decided there, so the rule still lives in `RecurringService`.

**`CLAIM_LIMIT` is now a batch size, not a ceiling.** `_drain` keeps claiming
until a phase finds nothing, so a cohort of any size finishes in one pass. A
batch returning nothing ends the phase, and that reads correctly for both
causes: no work left, or another worker holds what remains. `MAX_BATCHES` is a
guard rather than a limit anybody should reach — every claim query excludes the
state its own processing produces, so a row cannot be claimed twice in a pass,
but that property lives in a query and a future edit that breaks it should slow
a sweep down instead of spinning one for ever.

**Fairness.** Every claim is ordered — subscriptions by the oldest period end,
invoices by `period_start` or `issued_at`. A workspace cannot be perpetually
last, and the longest-outstanding money is chased first.

**Consequences.** Proved with two workers against a committing database in
`tests/integration/test_billing_sweep_concurrency.py`: a cohort of seven against
a claim limit of two is advanced exactly once with exactly one invoice per
workspace; seven collectible invoices produce seven charges at the provider and
no duplicates; a workspace with two overdue invoices gets one transition, one
audit row and one notice; a row held by somebody else is skipped and picked up
by the next sweep; and one workspace's provider failure leaves the other six
collected — with the failed attempt still on the record, because a request whose
outcome is unknown has still been made.

Removing `SKIP LOCKED` makes the skip test hang. Unlocking the per-invoice
re-claim makes one worker do everything while the other does nothing, and
unlocking it *and* bypassing the payment idempotency key produces the duplicate
charge both layers exist to prevent.

---

## ADR-083 — A Trace Says Where the Time Went, and Nothing About Whose Time It Was

**Context.** A customer's message arrives at `/webhooks/whatsapp`, is stored,
is enqueued, and is answered some minutes later by a *different process*
reading a Redis list — which reserves an allowance, calls OpenAI, writes rows,
and sends a reply through Meta. Structured logs correlate the first leg by
`request_id` and stop dead at the queue: the worker is a different process and
carries none of it. So "why did this customer wait four minutes" had no answer
that was not four separate log searches joined by a timestamp and a guess.

The audit's recommendation was OpenTelemetry across API → queue → worker →
provider. The interesting decisions are not in the SDK.

**Decision.**

**Four span kinds, written by hand, and no auto-instrumentation.** Not
`opentelemetry-instrumentation-fastapi`, not `-httpx`, not `-sqlalchemy`. Each
is one configuration flag away from exporting exactly what the rest of this
codebase spends its effort keeping out of logs: FastAPI's records the requested
path and query string — which carry conversation, lead and media identifiers —
httpx's records full request URLs (Paymob, S3, Google's token endpoint), and
SQLAlchemy's records statement text. In every case the privacy control would be
a setting, changeable by anybody, in a package this repository does not own.
Written by hand, the attribute set is an allowlist by construction, and
`ALLOWED_ATTRIBUTES` is a frozen set a test compares every produced attribute
against.

That is the same argument ADR-013 makes about the OpenAI client and ADR-072
about the metrics registry, applied to the case where the cost of the
dependency's defaults is disclosure rather than weight.

**The queue carries W3C trace context, and it is never identity.** The job
envelope gained one field holding at most `traceparent` and `tracestate`,
sanitised on the way in and again on the way out. The envelope is what a retry,
a delay, a crash recovery and a dead-letter record all preserve, so the context
survives all four for free.

What it is deliberately *not*: the job's identity, its retry budget, or its
deduplication key. Those are the payload, `attempt`, and a unique constraint in
PostgreSQL. A missing, truncated, hostile or older-release carrier means the
attempt starts its own trace and runs exactly as it would have — which is the
whole reason extraction lives in `job_span` and not in `reserve`. Tracing has
to be able to fail without work failing with it.

A retry gets a **new span in the same trace**, tagged `wasla.job_attempt`.
Reusing one span across attempts would overwrite the history of the first;
starting a new trace per attempt would sever the link to the request that
queued the work. Neither answers the question the trace exists for.

**No inbound trace context from HTTP.** Every API request starts a new trace.
Wasla's callers are a browser frontend and Meta, Paymob and Resend webhooks:
none participates in Wasla's traces, and all are outside the trust boundary.
Honouring a `traceparent` from the internet would let a stranger choose trace
identifiers, merge unrelated requests into one trace, and write up to 512 bytes
of `tracestate` into every span they produced. The propagation that matters is
internal. Gating on `trusted_proxy_ips` was considered and rejected as
worthless here: nginx forwards the client's header, so the peer address says
nothing about who wrote it.

**Span names are route templates, for the metrics reason and one more.**
`POST /leads/{lead_id}` rather than `POST /leads/8f3c…`. A backend indexes,
groups and displays by span name, so an identifier there is both a cardinality
problem and a disclosure — the second is new, and it is why this rule is worth
restating rather than inheriting. The middleware opens the span before routing
under the same `__unmatched__` placeholder the metrics middleware uses, and
renames it on the way out.

**No exception text, anywhere.** `record_exception` and
`set_status_on_exception` both default to *true* in the SDK and both put
`str(exception)` into the exported span — a provider's error body, a database
error quoting a parameter, a validation message quoting the rejected value.
Both are off on every span this application opens; a failing span carries the
exception's *class name* as its status description and no events at all.

**A provider span is recorded after the fact.** `ProviderCall` starts a clock
at the top of a client method and the outcome is decided at one of a dozen
exits inside a retry loop. A span opened at the top would have to be closed on
all of them, and "the exit that forgot" is exactly the omission that shows up
as a silently missing span. `record_span` creates and ends the span in one
statement with the recorded start time, so no path can leave one open. Nothing
in this application runs *inside* a provider call, so nothing is lost by the
span not being current while the call is in flight.

**Off by default, and refusing to start half-configured.** The opposite default
from `METRICS_ENABLED`, because metrics are served on request and cost nothing
unscraped, whereas tracing needs a destination and most deployments have none.
Disabled, no SDK is built, no exporter is imported, no thread is started, and
every span is a no-op. Enabled without `OTEL_EXPORTER_OTLP_ENDPOINT`, the
process refuses to boot — exporting into a void is the failure a deployment
discovers during the incident the traces were for.

The switch is Wasla's (`TRACING_ENABLED`) and everything else uses
OpenTelemetry's own standard variable names, so a deployment configures this
the way it configures every other OTLP producer it runs. They are declared as
`Settings` fields all the same, because the refusal above has to be able to see
them.

**Sampling is one number, `ParentBased`, and never a function of the tenant.**
Decided once per trace and inherited downstream, so the API and the worker
cannot disagree and produce a trace missing its middle. One number for the
deployment: sampling that varied by workspace, route or payment would put two
populations in one dataset with no way to tell them apart, and would make
traces a per-tenant signal — the same mistake the metrics cardinality rule
exists to prevent.

**Consequences.**

The pool metric and the database span between them make ADR-080 *visible*: in a
traced agent turn the `db.session` spans end before `provider.openai.respond`
begins, and `wasla_db_pool_checked_out` reads zero while two turns are parked
inside a provider call. A property that was previously true and only assertable
by a test is now readable by an operator.

What a trace backend's operator can see is span names, timings and ten
attributes. What they cannot see is written down in `docs/OBSERVABILITY.md` and
held by two tests that fail from opposite directions — an allowlist check that
catches any new attribute whatever it is named, and a canary scan that catches
a leak nobody predicted the field for.

The job envelope grew a nested object, and `release` removes an in-flight entry
by *exact value*. `encode` sorts nested keys along with the rest and a test
asserts an envelope re-encodes byte for byte after a round trip, because the
cost of getting that wrong is a job that can never be released.

Three packages added: `opentelemetry-api`, `-sdk`, and the OTLP/HTTP exporter.
No collector is shipped or verified here — the spans are produced and proved
against an in-memory exporter, and whether a particular backend accepts them is
a deployment question this repository has not answered.

---

## ADR-084 — Two Audiences, and Every Old Session Ends at Deploy

**Context.** SEC-14. A Wasla JWT carried `iss`, `sub`, `typ`, `jti`, `iat`,
`exp`, and — where relevant — `tid` and `ver`. It never said who was meant to
*accept* it. That is safe for exactly as long as one verifier exists, and the
audit's recommendation was to add the claim before a second one does rather
than after.

Reproducing the current behaviour turned up something the audit wording does
not imply. The verifier passed no `audience=` to PyJWT, and PyJWT's rule when
no audience is expected is that a token *carrying* `aud` is invalid. So the old
behaviour was exactly inverted from the desired one: a token with no audience
was accepted, and a token correctly addressed to this API was refused.

**Decision.**

**Two audiences, not one.** `wasla-api` for access tokens, `wasla-auth` for
refresh tokens. The two kinds have genuinely different consumers — an access
token is presented to the API's authenticated routes, a refresh token only to
`/auth/refresh` and `/auth/logout` — so one vague `wasla` for every
cryptographic purpose would add a claim without adding a separation. `typ`
already separated them; this separates them a second time *inside the library*,
on the same call that checks the signature, so a regression in the `typ`
comparison cannot turn a fortnight-long credential into an API session.

**Code constants, not settings.** Nothing outside this process verifies a Wasla
token, so an audience is a property of the protocol rather than of a
deployment. Making it configurable would create a way for two replicas of one
service to mint tokens the other refuses, in exchange for flexibility nobody
can use. `ISSUER` is a constant for the same reason and sits three lines above.

**Set in `_create_token`.** Every token this application issues passes through
one function, and the claim goes there rather than in the two public minting
functions — which differ only in their arguments to it. ADR-058 is the reason
that matters: workspace switching once had its own issuance path and quietly
dropped `ver`, and every switched session was refused as revoked. The tests
drive login, registration, refresh and workspace switching over HTTP and assert
the claim on all of them at once, rather than one caller at a time.

**Checked by PyJWT during decode, plus one strictness the library does not
impose.** `audience=` refuses a wrong or absent claim before any claim is
parsed. Beside it, `aud` must be the exact string: PyJWT accepts an array
containing the expected value, and this application has never minted one — so
an array can only come from a second issuer holding the signing key, which is
precisely the situation the claim was added ahead of.

**Every rejection still answers "The credentials are not valid."** A wrong
audience is indistinguishable from a wrong signature or a missing `jti`, so the
verifier does not become an oracle that tells somebody probing which part of a
forgery to fix next.

**Consequences.**

**This is a hard cutover.** Every access and refresh token minted by the
previous release stops working the moment this one starts, and everybody signs
in again. There is no compatibility window and deliberately none: accepting an
absent `aud` for a period would mean the verifier still cannot tell a Wasla
token from one minted for something else, which is the whole thing being fixed.
Access tokens live fifteen minutes and refresh tokens a fortnight, so the cost
is one extra sign-in per person, once. It is recorded as a test
(`test_a_token_in_the_old_format_no_longer_opens_the_api`) rather than as a
changelog line, because a changelog line is not re-read.

Google's ID tokens are unaffected: they are verified in
`app/integrations/google/oidc.py` against Google's published keys with
`audience` set to the configured client id, and share no key material with
this.

---

## ADR-085 — A Latency Histogram With No Outcome Label, and a Pool Metric That Says Whose Pool

**Context.** P1 gave every provider call a counter keyed by outcome, which says
whether OpenAI is up and nothing about whether an agent turn takes forty
seconds. P2-B made database connection lifetime the scaling boundary for agent
turns and left pool occupancy unobservable. Both gaps are one metric each; both
metrics have a shape that is easy to get subtly wrong.

**Decision.**

**`wasla_provider_request_duration_seconds` has no `outcome` label, and records
failures.** Those two facts are one decision. A provider that timed out after
twenty seconds is the most important latency this metric can hold, and a
histogram of successes alone reports a system getting *faster* as it breaks.
Once failures are included, splitting by outcome would quadruple the series to
answer a question `wasla_provider_requests_total` already answers — so "how
slow" and "how often does it fail" come from the metric shaped for each.

**Buckets from the configured timeouts, not from the HTTP set.** 50 ms to 60 s.
The request histogram starts at 5 ms because an in-process handler can finish
in one; no call across the internet will, so three of its ten buckets would be
permanently empty. Ten buckets is this module's ceiling and spending three of
them on nothing is a real cost.

**The duration is the whole operation, retries included**, because that is what
the work waited on. `ProviderCall` starts its clock at the top of each client's
request method so no exit can forget to time itself — there are sixteen exits
across four clients, and "the one that forgot" is invisible until a dashboard
has a hole in it.

Two calls are counted and deliberately not timed. `whatsapp`/`inbound_webhook`
is Meta calling us. The email worker's suppressed-recipient and
unrenderable-template exits are counted because an operator reads them as
provider outcomes, but neither made a call, and timing a decision not to send
would put a microsecond in the same distribution as a fifteen-second timeout.

**Written to Redis non-cumulatively.** A Prometheus histogram wants buckets
that each include everything below them; written directly that is eleven
`HINCRBY`s beside every provider call. The bucket an observation lands in is
incremented instead — two commands — and the scrape accumulates. Identical
exposition, one fifth the write cost, on the path a provider call runs on. A
bound Redis holds that this release no longer declares is *dropped* at scrape
time rather than folded into a neighbour: silently moving observations between
buckets makes a quantile computed across a bucket change look like an answer.

**`wasla_db_pool_*` carries `process_role`, and the only value is `api`.**
`/metrics` is served by the API and `AsyncEngine.pool` is a process-local
object: the API can see its own pool and has no way at all to see the worker's.
Unlabelled, `wasla_db_pool_checked_out` reads as "the deployment's pool", which
is the one thing it is not. The label makes the worker's absence visible rather
than implied, and leaves room to publish it later without renaming a metric a
dashboard depends on.

Publishing the worker's pool through Redis was considered and rejected: a pool
is a *level*, and ADR-069's counter channel carries totals. A stale level is
worse than a missing one, because it looks current.

**`pool.overflow()` is not published.** SQLAlchemy defines it as
`open_connections - pool_size`, so it reads `-5` on a cold pool of five, and an
operator alerting on "overflow above zero" would be alerting on warmth. What
saturation needs is `checked_out` against `size + max_overflow`, and all three
of those are published. `max_overflow` comes from `Settings` rather than the
pool, because `QueuePool` keeps it private and the deployment is where the
number was decided.

**Consequences.**

The pool gauge is proved against the pool it names rather than against itself:
`tests/integration/test_provider_session_lifetime.py` now renders the
exposition while two agent turns are parked inside a provider call and asserts
the published number is what `checkedout()` reports at that instant — which is
also the executed proof that ADR-080 still holds. A second test holds a
connection open and watches the gauge move, so a value hard-coded to zero
cannot satisfy the first.

Only the API's pool is observable. A worker running out of connections shows up
as queue depth and job latency rather than directly, and that is written down
in `docs/OBSERVABILITY.md` rather than left for somebody to infer from an
absent series.

---

## ADR-086 — The Test Suite Is Type-Checked Too, and the Fakes Are Named

**Context.** CI ran `mypy app` — 231 modules, 51,045 lines. The code that
calls those modules is mostly somewhere else: 197 test modules and 68,760
lines, more than the application itself, where every stand-in, every fixture
and every helper calls an application API and is believed to describe it.

`pyproject.toml` carried
`[[tool.mypy.overrides]] module = ["tests.*"] disallow_untyped_defs = false`,
which is the conventional exemption, and it had the conventional effect: a test
could pass `None` to a parameter typed `uuid.UUID`, a stub could return a
narrower type than the repository it stands for, and nothing said so.

Turning the check on produced 2,107 errors.

**Decision.**

**No `tests.*` override at all, rather than the sanctioned one.** This was
settled by measurement, not preference: restoring
`disallow_untyped_defs = false` removes 47 of 739 remaining errors. The other
692 come from `disallow_incomplete_defs`, `disallow_untyped_calls` and
`check_untyped_defs`, which the override does not touch. The exemption everyone
reaches for buys 6% of the work and costs the whole guarantee, so the suite is
checked under the same strict settings as `app` with no per-module relaxation.

**One named conversion function per stand-in family, in `tests/fakes.py`,
instead of `cast(Any, ...)` at the call site.** A fake WhatsApp client handed to
a service typed against the real one needs *something* at the boundary. Scatter
`cast(Any, ...)` and there are a hundred of them, each individually invisible
and collectively a hole; the type checker is then being asked to ignore exactly
the seam that most needs checking. Twenty-one functions — `as_session`,
`as_whatsapp`, `as_embeddings`, `as_messaging` and so on — each carry a
docstring saying which protocol the stand-in is claiming to satisfy. They are
greppable, countable, and reviewable as a list, which a cast never is.

`as_table` is the exception that proves the shape is right: it is a real
narrowing, `assert isinstance(clause, Table)`, and during this work it failed
loudly on ten model tests where a rewrite had wrapped the class instead of its
`__table__`. A cast would have passed all ten and left the tests asserting
against the wrong object.

**CI runs `mypy app tests`.** Proved by mutation rather than asserted: a test
helper calling `AuthService.register(workspace_slug=12345)` fails
`mypy app tests` with an `arg-type` error and passes `mypy app` with
"Success: no issues found". The old command could not see the seam it was
supposed to be guarding.

**Consequences.** The typing pass found five tests asserting something weaker
than their names claimed — a lead "entered by a person" whose actor was `NULL`
and therefore exempt from the foreign key; a stub subscription repository that
could not represent a workspace without a subscription; a stub media reader
that could not represent a missing file; a subclass taken through a variable,
which made its base `Any` and hid every inherited return type; and a
parametrised delay whose call had been rewritten to match a wrong annotation.
None of these was a type error anyone had been shown. They are the argument for
the change, and they are why the exemption was worth removing rather than
narrowing.

The cost is real: annotating 175 files is a large diff, and future test code
must be annotated to land. That is the intended trade. A test suite is a claim
about how the application is called, and an unchecked claim is a comment.

---

## ADR-087 — The Object's Key Is Committed Before the Object Exists

**Context.** ADR-078 built media retention and stated, in the same breath, the
one failure it could not close:

> One narrow orphan remains open and is stated rather than papered over:
> `MediaService.download` writes the object and then commits the row, so a
> transaction that fails after the write leaves an object nothing references.

That is the whole of P2-D. It was reproduced against a real PostgreSQL and a
real MinIO before anything was changed:

```
PUT succeeded, transaction rolled back.
EVIDENCE 1: the object is readable from MinIO after the rollback.
EVIDENCE 2: the media row is status='pending' storage_key=None
            - it never heard of the object.
EVIDENCE 3: retention's two queries return 0 due / 0 unfinished rows,
            none of them this object. Nothing in PostgreSQL will ever
            delete it.
```

The object is real, the bucket bills for it, and no query in the system can find
it — because every query starts from a row carrying a key, and the transaction
that would have written that key rolled back.

**Decision.**

**Write the reference, then create the object.** Three phases, and the commit in
the middle is the whole design:

```
TX1   allocate the object key, record what is about to be written,
      state PENDING                                            -> COMMIT
--    write the object. No transaction, no connection held, no row lock.
TX2   re-read under a row lock, confirm it is still ours,
      state STORED                                             -> COMMIT
```

Both orders of the pair fail. Only one of them fails recoverably:

- object then commit → nothing anywhere remembers the object. Unfindable.
- commit then object → a row names an object that may not be there. Askable.

Retention already made the same choice in the other direction (ADR-078): claim
the purge, commit, *then* delete. This is that argument applied to the write.

**No bucket-listing orphan sweep, now or later.** The tempting alternative is to
enumerate the store, subtract the keys the database knows, and delete the
remainder. It is unsafe in a way that does not surface until it has destroyed
something: a PostgreSQL failure, a lagging replica, a query that timed out, a
row a process could not read for any reason — each makes a live attachment look
like an orphan, and the rule says delete it. Deletion by *absence of evidence*
cannot be made safe. `tests/unit/test_no_bucket_listing.py` makes this
structural rather than a promise: the storage protocol has four operations, each
taking one `key: str`, neither implementation has a method returning more than
one object, and no module under `app/` mentions the S3 listing API in code. A
sweep of that shape cannot be written without first changing that file, which is
where somebody has to argue for it.

**`storage_state`, because the old encoding cannot express the new state.**
`(storage_key, purge_started_at)` described the lifecycle completely while a key
could only exist at the instant its object did:

```
key NULL,  purge NULL    never downloaded
key set,   purge NULL    stored
key set,   purge set     being purged
key NULL,  purge set     purged
```

Writing the key first adds a fifth possibility — key set, object not yet proved
to exist — which is indistinguishable from "stored" in that encoding, and every
consumer would be guessing. So the state becomes a column with six values
(`absent`, `pending`, `stored`, `purging`, `purged`, `mismatched`) and a check
constraint saying what each is allowed to look like, which is what lets a
consumer trust `storage_state` instead of re-deriving the lifecycle from which
columns happen to be null.

**The existing row owns the intent; there is no second table.** A
`media_storage_intents` table would be a second row per file with its own
lifecycle, its own foreign key, its own orphans, and a join on every read — to
carry four columns that belong to the file and have no meaning apart from it.
`MessageMedia` is already one row per message, already tenant-scoped, and
already the thing every consumer loads. `content_hash` and `byte_size` already
existed and now describe what *will* be written rather than only what was, so
there is one hash column rather than an expectation and a truth that always
agree.

**`UNIQUE(storage_key)`.** Not against collision — a UUID settles that — but
against two rows coming to reference one object, which would leave
reconciliation with two owners and no way to choose. NULL repeats freely, which
is what every row that never had a file and every purged row needs.

**Allocation happens under a row lock.** The queue can deliver the same media
job twice. Two attempts each allocating a key would each write an object, of
which only one could end up on the row — recreating the orphan through the front
door. `SELECT ... FOR UPDATE` on the media row serialises the allocation: the
second attempt waits, re-reads, finds the committed key, and writes its bytes
there. The lock covers a `SELECT` and an `UPDATE` and no network call.

**Verification is a recomputed SHA-256, never a validator the store supplies.**
S3 defines the ETag as opaque; it is the MD5 of the body only for a single-part
unencrypted upload, and SSE-KMS or a different provider makes every object look
wrong. `x-amz-meta-sha256` would be a claim travelling with the object rather
than a fact about it. So reconciliation HEADs the key — which is what
distinguishes absent from unreachable — and then reads the object back and
hashes it. That costs one GET bounded by `MEDIA_MAX_BYTES`, on a path that runs
only for interrupted writes, which on a healthy deployment is never.

**Four answers from the store, not two.**

```
present + contents match    -> finalise
present + contents differ   -> quarantine. Do not serve it, do not delete it:
                               deleting destroys the only evidence of how a
                               foreign object reached our key.
absent                      -> abandon. Nothing is there, the row owns no
                               object, and a later attempt may allocate afresh.
unreachable                 -> decide nothing. Not "absent".
```

The fourth is why `MediaStorage.exists` raises rather than returning False, and
why **403 was removed from the not-found set**. S3 answers 403 for a missing key
when the caller lacks `s3:ListBucket` — and also for a wrong secret, an expired
credential and a tightened policy, indistinguishably. Wasla's own bucket
credential can always read its own bucket, so a 403 is far more likely to be the
credential; reading it as "the object is gone" would let a rotated secret
abandon every upload in flight.

**Unconditional PUT.** `If-None-Match: *` would refuse a write over an existing
object, and the S3-compatible stores this reaches do not agree on it: AWS added
it in late 2024, recent MinIO honours it, and R2, Wasabi, B2 and Ceph each
answer differently or ignore it. A guarantee that silently does nothing on half
the providers is worse than none, so replacement is *detected* — by the same
hash check — rather than prevented.

**A separate loop, not the retention sweep.** They are neighbours and were
nearly one. Two things kept them apart. Their periods differ by two orders of
magnitude: retention is a date and sweeps daily, while an unfinished upload is
an attachment a colleague cannot open, measured in minutes. And they move in
opposite directions — retention removes objects that exist, this adopts objects
that may — so sharing a pass would put "delete the file" and "the file is fine"
behind one decision, which is the kind of ambiguity that eventually deletes
something.

**It owns `storage_state` and nothing else.** `MediaStatus` is the media queue's
column, with its own bounded retries and dead-letter list. Two states, two
owners; a reconciler that also moved `MediaStatus` would be racing the worker
for it. The queue remains the primary recovery for an inbound file — a
redelivered job resumes the same intent and writes to the same key — and this
loop is the backstop for when the queue has given up, and the *only* recovery
for an outbound attachment, whose bytes arrived in a request body that no longer
exists.

**Retention's eligibility narrows to `STORED`.** It used to select every row
with a key, which now includes uploads in flight — rows that are as old as their
message the moment they exist. Deleting one of those would be retention
destroying a file nobody had finished writing.

**The conversation gate lock moved.** `MediaWorker` took it before the download
and held it across a Meta fetch, an object write and an inference. The intent
commit ends that transaction, so the lock had to move anyway — and it moved to
where it is both correct and cheap: immediately before the count it exists to
serialise, in the transaction that commits the row's final state. The
intermediate states are safe to leave unlocked because every one of them
(`PENDING`, `DOWNLOADING`, `STORED`) is unresolved, so a sibling counting
mid-download sees work outstanding and correctly declines to release the reply.
This is ADR-080's principle applied where it had been missed.

**Consequences.** One migration (0041): a column, a timestamp, a unique
constraint, a check constraint, one new partial index and one re-cut. The
backfill is a total mapping from the pair every existing row already carries;
**no existing row can be `pending`**, because the state did not exist and a row
with a key got it from a transaction that committed after its object was
written. A purged row maps to `purged` and never to `absent`, which is the P2-A
distinction this must not undo.

The outbound path now commits mid-request. That is a change in transaction
boundary and not in ordering: Meta is still called first, the store second, and
only for a send that succeeded — so it cannot produce a second message. What it
does produce is a durable record of a send that has already happened, which
today is lost along with everything else if the request fails from that point
on.

Storage is metered by whoever finalises, including reconciliation, because the
transition out of `PENDING` happens exactly once under a row lock. A recovered
object is therefore billed once rather than not at all.

`wasla_media_upload_reconciliation_total` carries one label with six fixed
values — `finalized`, `missing`, `mismatched`, `unreachable`, `pending`,
`quarantined` — and no tenant, media id, object key, filename, hash or bucket
anywhere near it. `quarantined` is expected to be zero always rather than
usually: anything above it is an object in the bucket that Wasla wrote a key for
and did not write the contents of, and it needs a person.

What is proved rather than argued: a real child process is killed with
`SIGKILL`/`TerminateProcess` between its object write and its finalisation
(`tests/integration/test_media_crash_recovery.py`), and the replacement pass
recovers the exact object and writes no second one.
