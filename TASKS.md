# Wasla Roadmap

Status legend:

- `[ ]` Not started (Planned)
- `[x]` Completed (Implemented)
- `[~]` In progress
- `[!]` Blocked

This file is updated as part of every logical change. Phases follow the implementation order defined in `claude.md`.

**Current position:** Phases 0, 1, 2 and 3 are complete. The one Phase 3 item still open is the projection of delivery statuses onto message rows, which cannot be built before the message model exists. Phase 4 (conversations) is next.

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
- [x] Account connection API (connect, list, disable, enable)
- [x] Outbound client (text, media, location, buttons, lists, templates, read receipts)
- [x] Outbound retry policy and error mapping
- [~] Delivery status projection onto messages (statuses are stored as events; the projection needs the phase 4 message model)

## Phase 4 — Conversations

- [ ] Contact model and repository
- [ ] Conversation model and repository
- [ ] Message model and repository
- [ ] Inbound event projection into conversations and messages
- [ ] Delivery status projection onto messages
- [ ] Conversation mode (AI / HUMAN)
- [ ] Assignment and ownership tracking
- [ ] Outbound send API (with 24-hour service window enforcement)
- [ ] Conversation and message APIs

## Phase 5 — AI agents

- [ ] Agent and agent-tool models
- [ ] OpenAI Responses API integration layer
- [ ] Agent configuration service
- [ ] Agent orchestrator
- [ ] Token-aware conversation memory
- [ ] Tool registry and argument validation
- [ ] AI worker
- [ ] Orchestrator unit tests with mocked providers

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
