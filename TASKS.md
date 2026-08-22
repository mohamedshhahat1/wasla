# Wasla Roadmap

Status legend:

- `[ ]` Not started (Planned)
- `[x]` Completed (Implemented)
- `[~]` In progress
- `[!]` Blocked

This file is updated as part of every logical change. Phases follow the implementation order defined in `claude.md`.

**Current position:** Phases 0 through 8 are complete, and the pipeline that verifies them has been repaired — see *Phase 5.5* below, which came first because none of the claims in this file were being checked while it was broken. A customer message now travels the whole path: the webhook stores and projects it, enqueues one job per conversation, and a worker runs the agent — memory window, tool loop, handoff check — and sends the reply through the messaging service. Workspaces configure their own agents and tool grants over the API, upload documents, and their agents answer from them through tenant-scoped pgvector search. Agents capture leads from those conversations, and schedule follow-ups that a customer's reply cancels. As of this phase the workers finally have a process of their own: one container runs the agent, ingestion and follow-up loops together, selectable by `WORKER_KINDS`, and stops cleanly on SIGTERM. Phase 9 (media) is next.

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
- [x] Worker process entrypoint running all three loops, selectable by `WORKER_KINDS`
- [x] Graceful shutdown on SIGTERM

**COMPLETE.** Verified 2026-08-22 against PostgreSQL 16: 664 tests passed, 0 failed, 0 skipped; migration `0009` upgrades, `alembic check` reports no drift, downgrades to base, upgrades again and checks clean; Ruff, Black and MyPy clean; the image builds, the worker container starts, survives its blocking reserves, claims a due follow-up and resolves it, and shuts down cleanly on SIGTERM.

Deferred by decision, not unfinished:

- [ ] In-flight reaper for jobs abandoned by a dead worker — needs a heartbeat or a visibility timeout to tell a slow job from a dead one; guessing wrong sends a customer two replies
- [ ] Template approval checking — there is no template registry until Phase 11, so `template_name` is free text and nothing can confirm Meta has approved it before the send

## Phase 9 — Media

- [ ] Media download and storage abstraction
- [ ] Image understanding
- [ ] Voice transcription
- [ ] Document handling
- [ ] Media worker

## Phase 10 — Sentiment and escalation

- [ ] Sentiment analysis service
- [ ] Priority and intent storage
- [ ] Automatic handoff rules
- [ ] Escalation analytics events

## Phase 11 — Campaigns and templates

- [ ] WhatsApp template model and sync
- [ ] Campaign model and audience selection
- [ ] Scheduling and rate-limited sending
- [ ] Delivery and failure statistics

## Phase 12 — Analytics and usage

- [ ] Usage event model and recorder
- [ ] Aggregation services
- [ ] Analytics event model
- [ ] Tenant analytics APIs
- [ ] Platform analytics APIs

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
- [ ] Performance review and indexing pass
- [x] Speed up the PostgreSQL-backed suite. The schema is now built once per session and each test runs in a transaction that is rolled back. Measured on the same machine and the same 451 tests: **2507s → 94s**, a 27x reduction. Isolation is unchanged and is now itself covered by `tests/integration/test_fixture_isolation.py`.

## Phase 15 — Delivery

- [ ] Deploy workflow
- [ ] Container registry publishing
- [ ] Production TLS documentation
- [ ] Operational runbook
