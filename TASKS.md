# Wasla Roadmap

Status legend:

- `[ ]` Not started (Planned)
- `[x]` Completed (Implemented)
- `[~]` In progress
- `[!]` Blocked

This file is updated as part of every logical change. Phases follow the implementation order defined in `claude.md`.

**Current position:** Phases 0 through 5 are complete. A customer message now travels the whole path: the webhook stores and projects it, enqueues one job per conversation, and a worker runs the agent — memory window, tool loop, handoff check — and sends the reply through the messaging service. Workspaces configure their own agents and tool grants over the API. What the worker still lacks is a process of its own to run in, which belongs with the Phase 8 worker service. Phase 6 (knowledge base and RAG) is next.

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
- [ ] Dedicated `template` message kind (templates are recorded as text; needs a migration)
- [ ] Cursor pagination for conversation and message collections

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
- [ ] Agent model and migration parity tests (PostgreSQL-backed)
- [ ] Worker process entrypoint and container service (with the Phase 8 worker)
- [ ] In-flight reaper for jobs abandoned by a dead worker (with the Phase 8 worker)

## Phase 6 — Knowledge base and RAG

- [x] pgvector enablement (migration `0001`)
- [ ] Knowledge base, document, and chunk models
- [ ] Ingestion pipeline (extract, chunk, embed, store)
- [ ] Tenant-filtered vector search
- [ ] `search_knowledge` tool
- [ ] Cross-tenant retrieval tests

## Phase 7 — CRM and leads

- [ ] Lead model and repository
- [ ] Lead extraction from conversations
- [ ] Lead lifecycle statuses and scoring
- [ ] Assignment and notes
- [ ] Lead tools for agents

## Phase 8 — Follow-ups

- [ ] Follow-up model
- [ ] Scheduling service
- [ ] Follow-up worker
- [ ] Worker service in Docker Compose (local and production)
- [ ] Cancellation on customer reply
- [ ] Messaging-window and template compliance

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
- [ ] Per-workspace credential encryption at rest
- [ ] Performance review and indexing pass

## Phase 15 — Delivery

- [ ] Deploy workflow
- [ ] Container registry publishing
- [ ] Production TLS documentation
- [ ] Operational runbook
