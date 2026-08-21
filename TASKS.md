# Wasla Roadmap

Status legend:

- `[ ]` Not started (Planned)
- `[x]` Completed (Implemented)
- `[~]` In progress
- `[!]` Blocked

This file is updated as part of every logical change. Phases follow the implementation order defined in `claude.md`.

**Current position:** Phase 0 is complete. Phase 1 (database and tenancy foundation) is next.

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
- [ ] Tenant model
- [ ] User model (global identity)
- [ ] Membership model with `UNIQUE(user_id, tenant_id)`
- [ ] Tenant invitation model
- [ ] Role definitions (platform and tenant scopes)
- [ ] Initial domain migration
- [ ] Tenant and user repositories with enforced isolation
- [ ] Tenant isolation tests

## Phase 2 — Authentication and authorization

- [ ] Password hashing
- [ ] Login and token issuance
- [ ] Refresh tokens and revocation
- [ ] Current-user dependency
- [ ] Active workspace resolution and switching
- [ ] Membership-scoped RBAC dependencies
- [ ] Platform owner authorization layer
- [ ] Invitation acceptance flow
- [ ] RBAC and cross-tenant access tests

## Phase 3 — WhatsApp integration

- [ ] WhatsApp account model
- [ ] Webhook verification endpoint
- [ ] Meta signature verification
- [ ] Payload parser
- [ ] Tenant resolution from `phone_number_id`
- [ ] Inbound message persistence
- [ ] Idempotency on WhatsApp event IDs
- [ ] Outbound client (text, media, location, buttons, lists, templates)
- [ ] Delivery status and read receipts
- [ ] Webhook integration tests

## Phase 4 — Conversations

- [ ] Contact model and repository
- [ ] Conversation model and repository
- [ ] Message model and repository
- [ ] Conversation mode (AI / HUMAN)
- [ ] Assignment and ownership tracking
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
- [ ] Performance review and indexing pass

## Phase 15 — Delivery

- [ ] Deploy workflow
- [ ] Container registry publishing
- [ ] Production TLS documentation
- [ ] Operational runbook
