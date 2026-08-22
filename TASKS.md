# Wasla Roadmap

Status legend:

- `[ ]` Not started (Planned)
- `[x]` Completed (Implemented)
- `[~]` In progress
- `[!]` Blocked

This file is updated as part of every logical change. Phases follow the implementation order defined in `claude.md`.

**Current position:** Phases 0 through 12 are complete, and the pipeline that verifies them has been repaired — see *Phase 5.5* below, which came first because none of the claims in this file were being checked while it was broken. A customer message now travels the whole path: the webhook stores and projects it, enqueues one job per conversation, and a worker runs the agent — memory window, tool loop, handoff check — and sends the reply through the messaging service. Workspaces configure their own agents and tool grants over the API, upload documents, and their agents answer from them through tenant-scoped pgvector search. Agents capture leads from those conversations, and schedule follow-ups that a customer's reply cancels. The workers have a process of their own: one container runs the media, agent, ingestion, follow-up and campaign loops together, selectable by `WORKER_KINDS`, and stops cleanly on SIGTERM. As of phase 9 a customer can send a photograph, a voice note or a PDF and be answered about what is actually in it: the media worker reads the file before any agent is asked to reply, and a business can send attachments back. Phase 10 classifies how every customer message reads *before* the agent composes anything, raises the conversation's priority when it is bad, and hands an angry customer to a person instead of answering them. Phase 11 mirrors Meta's approved templates into a registry the platform can check in a transaction, and builds broadcasts on top of it — targeted only at people who already wrote to the business, paced by a rate limit stored on the row, and stopped for anyone who says stop. Phase 12 meters every path that consumes something — messages in both directions, agent turns and the sentiment call behind them, retrieval, media, stored bytes, leads and campaign traffic — with each meter staged in the same transaction as the work it measures, so a rolled-back turn is not billed. It reports those figures back over `/usage` and, derived from the domain tables rather than from a second event stream, `/analytics`; the one thing the domain does not record, a handoff and who decided it, is now a row of its own. The platform owner has a first read-only view across every workspace. Phase 13 (plans, subscriptions, billing) is next.

## Phase 0 — Foundation

- [x] Engineering rules captured (`claude.md`)
- [x] Documentation protocol captured (`Documentation_Protocol.md`)
- [x] Project memory files (README, ARCHITECTURE, TASKS, DECISIONS)
- [x] `docs/` documentation structure
- [x] Python project definition (`pyproject.toml`, pinned dependency ranges)
- [x] Configuration management (Pydantic Settings, `.env.example`, production guard rails)
- [x] Structured logging with request IDs and secret redaction
- [x] Centralised error handling and domain exceptions
- [x] Dependency injection wiring
- [x] FastAPI application factory and minimal app
- [x] Health endpoints (`/health`, `/health/live`, `/health/ready`)
- [x] Database foundation (async SQLAlchemy 2.0 engine, session, declarative base)
- [x] Alembic migration tooling
- [x] Initial migration enabling `pgcrypto` and `vector`
- [x] Redis client and health check
- [x] Testing infrastructure (pytest, pytest-asyncio, httpx client, fixtures)
- [x] Initial test suite for the foundation
- [x] Dockerfile and `.dockerignore`
- [x] Docker Compose (API, PostgreSQL + pgvector, Redis)
- [x] Production Docker Compose
- [x] Nginx reverse proxy example
- [x] GitHub Actions CI pipeline
- [x] Pre-commit configuration
- [x] Security workflow (dependency and secret scanning)

## Phase 1 — Database and tenancy foundation

- [x] Base model mixins (UUID keys, timestamps, soft delete, tenant scope)
- [x] Tenant model
- [x] User model (global identity)
- [x] Membership model with `UNIQUE(user_id, tenant_id)`
- [x] Tenant invitation model
- [x] Role definitions (platform and tenant scopes)
- [x] Initial domain migration (`0002`)
- [x] Tenant and user repositories with enforced isolation
- [x] Tenant isolation tests

## Phase 2 — Authentication and authorization

- [x] Password hashing (Argon2id, with rehash-on-login detection)
- [x] Token issuance (access and refresh, typed, with per-token identifiers)
- [x] Invitation token generation (hash-only storage)
- [x] Registration and login endpoints
- [x] Refresh token rotation and revocation (Redis)
- [x] Current-user dependency
- [x] Active workspace resolution and switching
- [x] Membership-scoped RBAC dependencies
- [x] Platform role authorization layer
- [x] Invitation issuing, revocation and acceptance
- [x] RBAC and cross-tenant access tests (PostgreSQL-backed)

## Phase 3 — WhatsApp integration

- [x] WhatsApp account model
- [x] WhatsApp event model with `UNIQUE(tenant_id, event_id)` (migration `0003`)
- [x] Webhook verification endpoint
- [x] Meta signature verification
- [x] Payload parser
- [x] Tenant resolution from `phone_number_id`
- [x] Inbound event persistence
- [x] Idempotency on WhatsApp event IDs
- [x] Webhook integration tests
- [x] Model and repository tests (PostgreSQL-backed, including model/migration parity)
- [x] Account connection API (connect, list, disable, enable)
- [x] Outbound client (text, media, location, buttons, lists, templates, read receipts)
- [x] Outbound retry policy and error mapping
- [x] Delivery status projection onto messages (completed in Phase 4, once messages existed)

## Phase 4 — Conversations

- [x] Contact model and repository
- [x] Conversation model and repository
- [x] Message model and repository
- [x] Conversation tables migration (`0004`)
- [x] Inbound event projection into conversations and messages
- [x] Delivery status projection onto messages (never regresses a status)
- [x] Customer profile names captured from the webhook `contacts` block
- [x] Conversation mode (AI / HUMAN) with handoff reason
- [x] Assignment and ownership tracking (membership verified, not trusted)
- [x] Outbound send API (with 24-hour service window enforcement)
- [x] Conversation and message APIs
- [x] Conversation projection tests (PostgreSQL-backed: replay, status ordering, isolation)
- [x] Dedicated `template` message kind, with `template_name` and `template_language` columns and no body (migration `0006`)
- [x] Cursor pagination for conversation and message collections (keyset on `(last_message_at, id)` and `(created_at, id)`)

## Phase 5 — AI agents

- [x] Agent and agent-tool models (migration `0005`)
- [x] OpenAI Responses API integration layer (HTTP-level, no SDK — ADR-014)
- [x] Agent configuration service
- [x] Agent configuration API (CRUD, default promotion, tool grants, available tools)
- [x] Token-aware conversation memory (message and token budgets, oldest turns dropped)
- [x] Tool registry and argument validation (`request_human_handoff` implemented)
- [x] Agent orchestrator (bounded tool-calling loop; decides a reply, never sends it)
- [x] Agent job queue (pending, in-flight and failed lists — ADR-015)
- [x] AI worker (reserves a job, runs the agent, sends through the messaging service)
- [x] Enqueue from the inbound webhook path (one job per conversation, never AI on the request)
- [x] Unit tests with a mocked provider (memory, registry, orchestrator, queue)
- [x] Agent model and migration parity tests (metadata parity in the unit suite, constraints and isolation against PostgreSQL)
- [x] Worker process entrypoint and container service (delivered with Phase 8)
- [ ] In-flight reaper for jobs abandoned by a dead worker (still open — see Phase 8)

## Phase 5.5 — Pipeline repair

Unplanned, and recorded because it changed what every other status in this file is worth. CI had been red on `main` since `185545a`, and the test job never ran a single test.

- [x] Make `tests/`, `tests/unit/` and `tests/integration/` packages (two modules shared a basename, so collection aborted and *nothing* ran)
- [x] Pin the developer toolchain to exact versions (ADR-016)
- [x] Fix Ruff findings under the pinned version (line length, import order, PEP 695 generics in the repository base)
- [x] Reformat under the pinned Black
- [x] Clear all 25 MyPy errors (`__table_args__` override type, `tenant_id` declared directly rather than through `declared_attr.directive`, redis-py's sync/async command union, a shadowed membership variable)
- [x] Fix the `204` routes, which FastAPI 0.116.2 refused to build under postponed annotations — the application would not start
- [x] Fix the invitation token bug this uncovered: `issue()` treated the `(token, hash)` pair as the token, hashed the tuple, and raised at runtime
- [x] Cover invitation issuing with tests that need no database, since nothing executable covered the path that broke
- [ ] Re-run the PostgreSQL-backed suite, migrations and `alembic check` — not verifiable in this environment (no PostgreSQL, no Docker)

## Phase 6 — Knowledge base and RAG

**COMPLETE.** Verified 2026-08-22 against PostgreSQL 16 with pgvector: 451 tests passed, 0 failed, 0 skipped; migration `0007` upgrades, `alembic check` reports no drift, downgrades to base, upgrades again and checks clean; Ruff, Black and MyPy clean; the application factory builds.

The four unchecked items below are **deferred by decision, not unfinished**. Two are scheduled into later phases and two are recorded omissions with reasons; none of them is required for a workspace to upload documents and have its agents answer from them.

- [x] pgvector enablement (migration `0001`)
- [x] Knowledge base, document, and chunk models (migration `0007`)
- [x] OpenAI embeddings client (HTTP-level, batched, width-checked)
- [x] Structure-aware chunking with overlap
- [x] Ingestion pipeline (extract, chunk, embed, store), idempotent by content hash
- [x] Ingestion queue and worker, separate from the agent queue (ADR-019)
- [x] Document lifecycle (`pending`, `processing`, `ready`, `failed`) with the failure reason on the row
- [x] Re-ingestion endpoint, so a failure is recoverable once its cause is fixed
- [x] Tenant-filtered vector search (cosine distance, `READY` documents only)
- [x] Retrieval service with a distance threshold and an explicit empty answer
- [x] `search_knowledge` tool, wired through the orchestrator
- [x] Knowledge admin API with role separation (members read, admins write)
- [x] Cross-tenant retrieval tests (PostgreSQL + pgvector)
- [x] Grounding tests: the agent invokes the tool, the passages reach its context, and an empty result is stated rather than silent
- [ ] *Deferred:* PDF extraction. Refused explicitly today with a message telling the uploader to submit the text instead, rather than accepted and silently indexing nothing. Needs a parser dependency, which was not added to a supply chain that had just been cleaned of advisories (ADR-017).
- [ ] *Deferred:* approximate vector index (ivfflat or hnsw). These have to be built against representative data to be worth anything — ivfflat wants its list count chosen from the row count, and one built on an empty table produces a bad plan that survives until someone reindexes. Exact search is correct at every size and fast at the sizes a new workspace has. Belongs to the Phase 14 performance pass.
- [ ] *Deferred to Phase 8:* sweeper for documents stranded `PENDING` by a queue outage. `DocumentRepository.list_pending` exists for it; it belongs with the worker service.
- [ ] *Deferred to Phase 12:* RAG usage metering, with the rest of the usage subsystem.

## Phase 7 — CRM and leads

**COMPLETE.** Verified 2026-08-22 against PostgreSQL 16: 588 tests passed, 0 failed, 0 skipped in 81.06s; migration `0008` upgrades, `alembic check` reports no drift, downgrades to base, upgrades again and checks clean; Ruff, Black and MyPy clean; the image builds, starts, answers `/health/live` and `/health/ready`, and serves `/api/v1/leads`.

- [x] Lead, note and activity models (migration `0008`)
- [x] Lead, note and activity repositories, tenant-scoped
- [x] Lead extraction from conversations, idempotent on the contact
- [x] One open lead per customer, enforced by a partial unique index (ADR-020)
- [x] Human-entered fields protected from AI overwrite (ADR-021)
- [x] Lead lifecycle statuses with an explicit transition graph
- [x] Scoring, clamped to its bounds
- [x] Assignment through the existing membership system
- [x] Internal notes
- [x] Append-only activity timeline
- [x] Search, filtering, tags and keyset pagination
- [x] Statistics endpoint, aggregated in one query
- [x] `record_lead_details` tool for agents
- [x] Administration API with role boundaries

Deferred by decision, not unfinished:

- [ ] Lead scoring *rules* — the field and its bounds exist; deciding what earns a score needs the qualification signals Phase 10 introduces
- [ ] Lead deletion or merge — no route exists on purpose; merging two histories is a product decision, and nothing yet creates the duplicates that would need it
- [ ] Budget unit parsing (`"500k"`) — refused rather than guessed, because reading it wrong reprioritises a real pipeline silently

## Phase 8 — Follow-ups

- [x] Follow-up model (migration `0009`)
- [x] Scheduling service, rescheduling rather than stacking nudges
- [x] One pending follow-up per conversation, enforced by a partial unique index
- [x] Follow-up worker, polling with `FOR UPDATE SKIP LOCKED` (ADR-022)
- [x] Cancellation on customer reply, on the inbound webhook path
- [x] Messaging-window and template compliance, with `SKIPPED` distinct from `FAILED`
- [x] Retry with backoff and a bounded attempt count
- [x] `schedule_follow_up` tool for agents
- [x] Follow-up API
- [x] Worker service in Docker Compose (local and production)
- [x] Worker process entrypoint running every loop, selectable by `WORKER_KINDS`
- [x] Graceful shutdown on SIGTERM

**COMPLETE.** Verified 2026-08-22 against PostgreSQL 16: 664 tests passed, 0 failed, 0 skipped; migration `0009` upgrades, `alembic check` reports no drift, downgrades to base, upgrades again and checks clean; Ruff, Black and MyPy clean; the image builds, the worker container starts, survives its blocking reserves, claims a due follow-up and resolves it, and shuts down cleanly on SIGTERM.

Deferred by decision, not unfinished:

- [ ] In-flight reaper for jobs abandoned by a dead worker — needs a heartbeat or a visibility timeout to tell a slow job from a dead one; guessing wrong sends a customer two replies
- [x] Template approval checking — delivered in Phase 11. The registry answers it, and a follow-up asks twice: at scheduling and again before the send

## Phase 9 — Media

- [x] Media download and storage abstraction (`MediaStorage` protocol, `LocalMediaStorage`, ADR-023)
- [x] Media descriptors parsed from the webhook; captions become the message body
- [x] `message_media` table and migration `0010`
- [x] Image understanding (Responses API image input, reusing the existing client)
- [x] Voice transcription (`TranscriptionClient`)
- [x] Document handling (PDF and plain text extraction; also unblocks knowledge base PDF ingestion)
- [x] Media worker, its own queue, and `WORKER_KINDS=media`
- [x] Conversation gate: two attachments produce one agent job
- [x] Transcripts rendered into agent memory, distinct from customer captions
- [x] Outbound attachments (upload to Meta, send, serve stored files back)

Deferred, and recorded rather than forgotten:

- [ ] OCR for scanned documents — reported as unreadable for now
- [ ] Video understanding — downloaded and stored, but skipped as unreadable
- [ ] Streaming rather than whole-file reads
- [ ] Retention sweep for stored files — belongs with the object-store implementation

## Phase 10 — Sentiment and escalation

- [x] Sentiment analysis service (`SentimentAnalyzer`, schema-constrained output, ADR-024)
- [x] Priority and intent storage (`message_sentiments` and the conversation's current reading, migration `0011`)
- [x] Automatic handoff rules, per agent, above a confidence floor
- [x] Assessment before the reply rather than after it, inside the agent turn
- [x] Priority raised by a reading and lowered only by a person
- [x] Inbox priority filter and the manual priority endpoint
- [x] Voice notes classified; image descriptions deliberately not
- [x] Structured output on the Responses client (`text.format`, strict)

**COMPLETE.** Verified 2026-08-22 against PostgreSQL 16: see the phase report. Migration `0011` upgrades, `alembic check` reports no drift, downgrades to `0010` and to base, and reapplies clean; Ruff, Black and MyPy clean; the image builds and the worker container runs.

Deferred by decision, not unfinished:

- [ ] Escalation analytics events — there is no analytics event table until Phase 12, and `message_sentiments` already carries the timestamped rows those counts will read. Adding a second write now would mean migrating two shapes later
- [ ] A holding message when a conversation escalates — the customer gets silence until a person arrives. Phase 11 removed the obstacle (there is now a registry of approved templates), but the remaining question is not technical: what a business says to a customer it has just decided is angry is the sentence most likely to make things worse, and it must be that workspace's own words. It is agent configuration and belongs with escalation, not with campaigns
- [ ] Conversation-level mood, as distinct from message-level — a customer whose tone curdles over ten polite messages is judged one message at a time
- [ ] Intent as a closed vocabulary — suggested in the prompt so reports group, but not enforced, because the intents a business has not thought of yet are the ones worth seeing
- [ ] Sentiment on outbound messages — nothing reads how the business itself sounds

## Phase 11 — Campaigns and templates

- [x] WhatsApp template registry mirrored from Meta, with sync (migration `0012`)
- [x] Statuses and categories that fail closed on anything Meta introduces later
- [x] Template approval checked by follow-ups — closes the Phase 8 deferral
- [x] Campaign and recipient models, one row per person (migration `0013`)
- [x] Audience built from conversations only, never an uploaded list (ADR-025)
- [x] Marketing opt-out on contacts, honoured in audiences and re-checked at send time
- [x] A customer's stop word honoured on the inbound path
- [x] Campaign lifecycle: draft, scheduled, running, paused, completed, cancelled, failed
- [x] Rate-limited sending, paced by a timestamp on the row rather than a sleep (ADR-026)
- [x] Campaign worker, `WORKER_KINDS=campaign`, claiming with `FOR UPDATE SKIP LOCKED`
- [x] Delivery and failure statistics, with delivery read from the message rows
- [x] Campaign, template and opt-out APIs with role separation

**COMPLETE.** Verified 2026-08-23 against PostgreSQL 16 and in containers; see the phase report for the figures. Migrations `0012` and `0013` each upgrade, `alembic check` reports no drift, downgrade one step and to base, and reapply clean; Ruff, Black and MyPy clean; the image builds, the worker container runs all five loops, and a campaign was composed, targeted, scheduled and stopped through real HTTP against real PostgreSQL.

Two defects the container run found and no test could see, both now covered by tests that fail without the fix:

- `POST /api/v1/campaigns` and `POST /api/v1/follow-ups` both answered **500**. A route returns the row the service just staged and the request commits afterwards, so the primary key default and the server-default timestamps were still null when the response was built. The follow-up route had been broken this way since Phase 8; every endpoint test used a stub service returning a fully populated model, which is exactly what hid it
- A missing WhatsApp credential looped a campaign forever without exhausting anyone's attempts, staging a message row per recipient per sweep

Deferred by decision, not unfinished:

- [ ] Per-recipient personalisation — a campaign's template variables are one list for the whole send. Filling `{{1}}` with each customer's name needs a source of per-recipient facts that nothing here has yet
- [ ] A per-*number* rate limit, as distinct from the per-campaign one. Two campaigns on one number can exceed either one's rate; a shared budget needs a shared counter, and a workspace that starts two simultaneous broadcasts has made a decision this system can surface but should not silently override
- [ ] Audience import — refused on purpose (ADR-025). Adding one means answering the consent question, not writing a CSV parser
- [ ] Campaign analytics events — counts come from the recipient and message rows; the analytics event table arrives in Phase 12, and writing a second shape now would mean migrating two later
- [ ] Retention for completed campaigns — recipient rows are the record of who was written to and when, so sweeping them is a product decision rather than a cleanup job
- [ ] Per-category consent — an opt-out is currently all-or-nothing. Splitting it needs a vocabulary a business can actually explain to a customer
- [ ] Understanding a sentence like "please take me off your list" — the stop-word matcher reads whole messages only. The alternative is a model call on every inbound message to decide something a person can record in one click

## Phase 12 — Analytics and usage

- [x] Usage event model and recorder (migration `0014`, ADR-027)
- [x] Aggregation services (named counters and a daily series, over a half-open window)
- [x] Metering wired into every path that consumes something: inbound and outbound messages, conversations opened, agent turns and the sentiment call, retrieval, media reads and transcriptions, stored bytes, leads captured, campaign messages
- [x] Analytics event model, recording handoffs and who decided them (migration `0015`, ADR-028)
- [x] Tenant analytics APIs (`GET /analytics`, `GET /analytics/conversations/{id}/events`, `GET /usage`, `GET /usage/daily`)
- [x] Platform analytics APIs (`GET /platform/overview`, `GET /platform/tenants`, in `app/platform/`)

**COMPLETE.** Verified 2026-08-23 against PostgreSQL 16: 1121 tests passed, 0 failed, 0 skipped; migrations `0014` and `0015` each upgrade, `alembic check` reports no drift, downgrade one step and to base, and reapply clean; Ruff, Black and MyPy clean; the application factory builds and serves the four tenant routes and the two platform ones.

Deferred by decision, not unfinished:

- [ ] Voice *minutes*, as distinct from a count of recordings. `gpt-4o-mini-transcribe` and its siblings answer in plain JSON with no duration, and the verbose format that carries one is refused by those models. A recording transcribed is a fact; seconds inferred from a compressed byte count would be a fabricated number in a bill. `UsageUnit.SECOND` is declared and unwritten, waiting for a provider that reports it
- [ ] `API_REQUEST` metering. The meter exists and nothing writes it: a row per HTTP request on the largest table in the schema buys nothing until a plan prices requests, and what a plan actually limits is the work behind a request rather than the request
- [ ] Revenue, MRR, ARR, churn and estimated AI cost on the platform overview. The first four are questions about subscriptions, and there are none until Phase 13. The last needs per-model prices stored nowhere; token counts are real and a cost derived from invented prices would not be. A plausible zero on a dashboard is worse than an absent field, because somebody eventually believes it
- [ ] Tenant administration from the platform layer — creating, suspending or deleting a workspace. Read-only on purpose: those are the actions that most need an audit trail, and there is no audit log until Phase 14
- [ ] Usage retention and rollups. Nothing sweeps old rows, and nothing should until a billing period is closed and its figures are stored where a sweep cannot change them (Phase 13). A rollup belongs beside the rows, never instead of them
- [ ] Timezone-aware days. The daily series buckets by UTC, so a workspace elsewhere sees a boundary that is not its midnight — honest until a workspace can state its timezone, since guessing one from a phone number would silently move every figure
- [ ] Agent-level performance, as distinct from workspace-level. Every figure here is the workspace's; comparing two agents needs the agent on the conversation, which is decided per turn rather than stored

## Phase 13 — Plans, subscriptions, billing

- [ ] Plan model with configurable limits
- [ ] Subscription lifecycle
- [ ] Entitlement enforcement service
- [ ] Provider-agnostic billing abstraction
- [ ] Invoices and payment records

## Phase 14 — Production hardening

- [ ] Rate limiting
- [ ] Audit logging
- [x] CORS and secure headers (CORS configurable; Nginx security headers in place)
- [ ] Request size and timeout limits
- [ ] Retry policies for external calls
- [x] Dependency advisories cleared and a floor put under them (Starlette declared directly — ADR-017)
- [ ] Per-workspace credential encryption at rest
- [ ] A real liveness probe for the worker. The image's HEALTHCHECK curls the API, and the worker serves no HTTP, so it inherited a check it could never pass and reported unhealthy for its whole life — found by reading the health column during the phase 10 container run, not by any test. Both compose files now disable it explicitly rather than fake it; a genuine probe needs the loops to publish a heartbeat, which is also what the phase 8 in-flight reaper wants
- [ ] Performance review and indexing pass
- [x] Speed up the PostgreSQL-backed suite. The schema is now built once per session and each test runs in a transaction that is rolled back. Measured on the same machine and the same 451 tests: **2507s → 94s**, a 27x reduction. Isolation is unchanged and is now itself covered by `tests/integration/test_fixture_isolation.py`.

## Phase 15 — Delivery

- [ ] Deploy workflow
- [ ] Container registry publishing
- [ ] Production TLS documentation
- [ ] Operational runbook
