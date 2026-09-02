# Wasla Roadmap

Status legend:

- `[ ]` Not started (Planned)
- `[x]` Completed (Implemented)
- `[~]` In progress
- `[!]` Blocked

This file is updated as part of every logical change. Phases follow the implementation order defined in `claude.md`.

**Current position:** Phases 0 through 13 are complete, and the pipeline that verifies them has been repaired — see *Phase 5.5* below, which came first because none of the claims in this file were being checked while it was broken. A customer message now travels the whole path: the webhook stores and projects it, enqueues one job per conversation, and a worker runs the agent — memory window, tool loop, handoff check — and sends the reply through the messaging service. Workspaces configure their own agents and tool grants over the API, upload documents, and their agents answer from them through tenant-scoped pgvector search. Agents capture leads from those conversations, and schedule follow-ups that a customer's reply cancels. The workers have a process of their own: one container runs the media, agent, ingestion, follow-up and campaign loops together, selectable by `WORKER_KINDS`, and stops cleanly on SIGTERM. As of phase 9 a customer can send a photograph, a voice note or a PDF and be answered about what is actually in it: the media worker reads the file before any agent is asked to reply, and a business can send attachments back. Phase 10 classifies how every customer message reads *before* the agent composes anything, raises the conversation's priority when it is bad, and hands an angry customer to a person instead of answering them. Phase 11 mirrors Meta's approved templates into a registry the platform can check in a transaction, and builds broadcasts on top of it — targeted only at people who already wrote to the business, paced by a rate limit stored on the row, and stopped for anyone who says stop. Phase 12 meters every path that consumes something — messages in both directions, agent turns and the sentiment call behind them, retrieval, media, stored bytes, leads and campaign traffic — with each meter staged in the same transaction as the work it measures, so a rolled-back turn is not billed. It reports those figures back over `/usage` and, derived from the domain tables rather than from a second event stream, `/analytics`; the one thing the domain does not record, a handoff and who decided it, is now a row of its own. The platform owner has a first read-only view across every workspace. Phase 13 puts a plan behind every workspace: limits are stored data with a closed vocabulary, checked against row counts and against the period's own usage, and enforced where somebody chooses rather than on the path a customer's message arrives by. A period that closes is invoiced by the same sweep that rolls it over, amounts copied so a repricing cannot rewrite last month, and the payment provider is one method with an honest manual implementation. Phase 14 (production hardening) is next.

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

**COMPLETE.** Verified 2026-08-23 against PostgreSQL 16: 1121 tests passed, 0 failed, 0 skipped; migrations `0014` and `0015` each upgrade, `alembic check` reports no drift, downgrade one step and to base, and reapply clean; Ruff, Black and MyPy clean.

Also verified in containers, against a database reset to empty first: the image builds, the API container applies `0014` and `0015` on start, and the whole path was driven over real HTTP. A workspace was registered, a lead created through `POST /leads`, and the `lead_created` row appeared in `usage_events` in the same commit as the lead — then came back through `GET /usage` (`leads_created: 1`), `GET /analytics` (`leads.created: 1`) and `GET /platform/tenants`, where the workspace's counter matched its own. Authorization was checked on the way: `401` unauthenticated, `403` on `/platform/*` for a workspace **owner**, and `200` only once that user was granted a platform role.

One trap this run exposed, recorded because it will catch the next person: running the test suite and Alembic against the same database leaves it in a state CI never produces. The suite drops every table it built from the models but does not touch `alembic_version`, so a later `alembic upgrade head` sees itself at head against an empty schema and does nothing. The database has to be reset before a container run means anything.

Deferred by decision, not unfinished:

- [ ] Voice *minutes*, as distinct from a count of recordings. `gpt-4o-mini-transcribe` and its siblings answer in plain JSON with no duration, and the verbose format that carries one is refused by those models. A recording transcribed is a fact; seconds inferred from a compressed byte count would be a fabricated number in a bill. `UsageUnit.SECOND` is declared and unwritten, waiting for a provider that reports it
- [ ] `API_REQUEST` metering. The meter exists and nothing writes it: a row per HTTP request on the largest table in the schema buys nothing until a plan prices requests, and what a plan actually limits is the work behind a request rather than the request
- [ ] Revenue, MRR, ARR, churn and estimated AI cost on the platform overview. The first four are questions about subscriptions, and there are none until Phase 13. The last needs per-model prices stored nowhere; token counts are real and a cost derived from invented prices would not be. A plausible zero on a dashboard is worse than an absent field, because somebody eventually believes it
- [ ] Tenant administration from the platform layer — creating, suspending or deleting a workspace. Read-only on purpose: those are the actions that most need an audit trail, and there is no audit log until Phase 14
- [ ] Usage retention and rollups. Nothing sweeps old rows, and nothing should until a billing period is closed and its figures are stored where a sweep cannot change them (Phase 13). A rollup belongs beside the rows, never instead of them
- [ ] Timezone-aware days. The daily series buckets by UTC, so a workspace elsewhere sees a boundary that is not its midnight — honest until a workspace can state its timezone, since guessing one from a phone number would silently move every figure
- [ ] Agent-level performance, as distinct from workspace-level. Every figure here is the workspace's; comparing two agents needs the agent on the conversation, which is decided per turn rather than stored

## Phase 13 — Plans, subscriptions, billing

- [x] Plan model with configurable limits, and the documented catalogue seeded (migration `0016`, ADR-029)
- [x] Subscription model, one per workspace, enforced by a unique index
- [x] Entitlement service: resource limits counted from rows, period limits from `usage_events` over the billing period
- [x] Subscription lifecycle (trial, change of plan, cancellation, resume, period roll-over)
- [x] A new workspace starts on the default plan at registration, and a signup survives a catalogue that has no such plan
- [x] Billing worker: trials end, pending cancellations take effect, periods roll over (`WORKER_KINDS=billing`)
- [x] Limits enforced where somebody chooses, never on the inbound path (ADR-030)
- [x] Billing APIs (catalogue, subscription, entitlements; owners commit the company, members read)
- [x] Provider-agnostic billing abstraction (`PaymentProvider`, one method; `ManualProvider` — ADR-031)
- [x] Invoices and payment records (migration `0017`), issued per period and never edited
- [x] Invoice APIs (a workspace reads its own; only platform staff record a payment or void one)
- [x] The sweep issues an invoice for each period it closes, and never for a trial

**COMPLETE.** Verified 2026-08-23 against PostgreSQL 16: 1248 tests passed, 0 failed, 0 skipped; migrations `0016` and `0017` upgrade from an empty database, `alembic check` reports no drift, downgrade one step and to base, and reapply clean; Ruff, Black and MyPy clean.

Deferred by decision, not unfinished:

- [ ] Refunds, credits, proration and tax. Each is a decision about money nobody has made, and a system that guesses produces numbers a customer is asked to pay. Changing plan mid-period therefore moves no money either way
- [ ] Per-unit overage pricing. Usage lines on an invoice carry a quantity and a zero amount for exactly this reason; when a price exists the lines gain amounts and nothing else changes
- [ ] A real payment provider. `PaymentProvider` is one method and `ManualProvider` implements it honestly — it records that an invoice is awaiting payment and never claims to have collected (ADR-031)
- [ ] MRR and ARR on the platform overview. Collected revenue is a fact and is available; those are projections needing decisions nobody has made — whether a trial counts, what a past-due subscription is worth, how an annual plan is spread
- [x] Dunning: chasing a `past_due` subscription, and deciding when grace runs out. Completed in Phase 23 (ADR-061) - `SUSPENDED` ends the grace and stops the paid plan resolving. What remains is retention policy for a suspended workspace, which is commercial rather than code
- [ ] Plan administration over the API. Plans are edited in the database today, which is acceptable while only platform staff can reach them and becomes a real requirement the moment they are editable by anyone else (ADR-029)
## Phase 14 — Production hardening

- [x] Rate limiting: authentication by client address, workspace traffic and campaigns by workspace, the WhatsApp webhook deliberately unlimited, failing open on a Redis outage (ADR-032)
- [x] Audit logging: append-only, labels copied so an entry outlives the account it names, platform actions recorded in the workspace's own trail, readable at `/audit-logs` and `/platform/audit-logs` (migration `0018`, ADR-033)
- [x] CORS and secure headers (CORS configurable; Nginx security headers in place)
- [x] Request size and timeout limits, enforced by the application rather than only by nginx: an oversized body is refused before it is read, a handler that overruns answers 504, and the WhatsApp webhook is exempt from the timeout but not from the body cap
- [x] Retry policies for external calls. Built with the clients rather than as a hardening pass, and verified here rather than assumed: every external client bounds its attempts and waits between them, both client factories bound their timeout, and the WhatsApp send path retries *by failure mode* — a connect error is retried because nothing reached Meta, a timeout or a 5xx is not because the message may already have been delivered and a duplicate cannot be taken back. `tests/unit/test_retry_policies.py` keeps a new integration from shipping without a policy
- [x] Dependency advisories cleared and a floor put under them (Starlette declared directly — ADR-017)
- [x] Per-workspace credential encryption at rest (migration `0020`, ADR-034, superseding ADR-009). AES-256-GCM under a key ring, the workspace id bound in as authenticated data so a ciphertext cannot be moved between rows, write-only over the API, and no fallback to the platform token when a stored credential cannot be read
- [x] A real liveness probe for the worker. Each configured loop publishes a heartbeat to Redis with a short expiry, refreshed on a timer, and `scripts/entrypoint.sh worker-health` exits non-zero unless **every** loop this container runs has beaten recently. Verified in a container: all six keys present, `docker ps` reports **healthy** for the first time in the project's life, deleting one key makes the probe exit 1, and the next beat restores it. What it proves is the process is up and its event loop is scheduling; what it does not prove is that a loop is making progress — that is the in-flight reaper, which reads the same keys
- [~] Performance review and indexing pass. The analytics windows are done (migration `0019`): every tenant figure is "this workspace, this window", and four tables had a tenant index carrying no time, so PostgreSQL found the workspace's rows and discarded most of them by filter — waste proportional to how long a workspace had existed, which made the dashboard slowest for the longest-paying customers. Measured on 50 workspaces and 50,000 messages: **772 buffers and 850 rows discarded → 6 buffers, no heap fetches, an index-only scan**. `tests/unit/test_analytics_indexes.py` is the guard against adding the next windowed query the same way. Still open: the approximate vector index deferred from phase 6, and a review of the campaign and follow-up sweeps under load
- [x] Speed up the PostgreSQL-backed suite. The schema is now built once per session and each test runs in a transaction that is rolled back. Measured on the same machine and the same 451 tests: **2507s → 94s**, a 27x reduction. Isolation is unchanged and is now itself covered by `tests/integration/test_fixture_isolation.py`.

**COMPLETE**, with one item left deliberately in progress: the indexing pass closed the analytics gap it was opened for, and the vector index it also mentions was always Phase 6's deferral (it needs representative data to be built against, so it belongs with a real corpus rather than with a hardening sweep).

Verified 2026-08-23 against PostgreSQL 16 and in containers: migrations `0018`, `0019` and `0020` apply from an empty database, `alembic check` reports no drift, and the schema downgrades to base and reapplies clean; the image builds; the API answers; and the worker container reports **healthy** for the first time since it existed, with all six heartbeat keys in Redis, the probe exiting 1 when one is deleted and recovering on the next beat.

Deferred by decision, not unfinished:

- [ ] Keys in a secret manager rather than in configuration. `CREDENTIAL_ENCRYPTION_KEYS` sits in the environment of every API and worker container, which is the exposure `JWT_SECRET` already has — a real limit, and not a new class of one
- [ ] Automated key rotation. Prepending a key works and old credentials keep decrypting; `needs_rotation` finds the stragglers and nothing rewrites them yet
- [ ] The in-flight reaper. It wants the heartbeat that now exists, and needs a visibility timeout to tell a slow job from a dead one — guessing wrong sends a customer two replies
- [ ] Dunning, retention sweeps and the approximate vector index, each recorded in the phase that deferred it

## Phase 15 — Delivery

- [x] Container registry publishing. Every commit on `main` is built and pushed to GitHub Container Registry as `sha-<commit>`, using the workflow's own token rather than a registry secret (ADR-035)
- [x] Image provenance. The runtime image carries OCI labels naming its commit, version and build time, and the revision is an environment variable inside the container — so `docker inspect` answers "what is running" without the pipeline's help
- [x] Deploy workflow, gated on CI *concluding successfully* rather than on the push, building the commit CI verified rather than the branch head, deploying a digest rather than a moving tag, running migrations as their own step before anything serves, and checking readiness afterwards. With no deployment target configured it fails loudly instead of reporting a green tick for a release that touched nothing
- [x] Container vulnerability scanning. The published image is scanned after the push (ADR-035 explains why after); pull requests scan a locally built image, failing on fixable CRITICAL/HIGH findings and reporting unfixable ones without failing
- [x] The workflows are under test (`tests/unit/test_delivery_pipeline.py`). YAML is the one part of this repository nothing else checks — not imported, not typed — and a mistake in it surfaces as a broken release rather than a red build. The tests assert rules, not contents
- [x] Production TLS documentation and an nginx configuration that can actually obtain a certificate: the ACME challenge path is served *before* the redirect to HTTPS, which is the deadlock a first issuance otherwise hits. The TLS server block is complete and commented out, because `nginx -t` fails on a certificate path that does not exist — an operator who enables it early gets a proxy that will not start rather than one silently serving plaintext
- [x] Production compose carries the settings phases 13 and 14 added (`CREDENTIAL_ENCRYPTION_KEYS`, `DEFAULT_PLAN_CODE`, the rate-limit switches, the body and timeout caps), with the encryption keys reaching the worker as well as the API — they must match, or a credential written by one is unreadable by the other
- [x] Operational runbook ([docs/RUNBOOK.md](docs/RUNBOOK.md)): triage order, the symptoms this system actually produces, and the procedures — deploy a digest, roll back without running migrations, find what is running, rotate each secret, add worker capacity, take a number offline

**COMPLETE.** Verified 2026-08-23 against PostgreSQL 16 and in containers: 1373 tests passed, 0 failed, 0 skipped (the whole suite, database-backed tests included, in 195s); Ruff, Black and MyPy clean; migrations `0001`-`0020` apply from an empty database, `alembic check` reports no drift, and the schema downgrades to base and reapplies clean with no drift after. The runtime image builds with real build arguments and `docker inspect` reads back the commit, version and build time it was given; the container boots, reports **healthy**, answers `/health/live` and `/health/ready` with PostgreSQL and Redis up, and `WASLA_BUILD_REVISION` inside the process matches the OCI label. `nginx -t` passes on the compose network. `docker-compose.prod.yml` validates, and the encryption key ring and default plan reach the worker as well as the API while all three application services resolve to one pinned digest. The workflow guards were checked by breaking each invariant in turn - removing the CI-success gate, building the branch head instead of the verified commit, and swapping host-key pinning for `StrictHostKeyChecking=no` - and confirming a test goes red for each.

What has **not** been verified, because it cannot be here: the deploy job has never run against a host. It is written, its shape is tested, and it fails closed when no target is configured.

Deferred by decision, not unfinished:

- [ ] A production deployment. The pipeline is written and none of it has run against a real host, because no host exists. The deploy job is gated on secrets that are not set, so it fails rather than pretending; both `docs/DEPLOYMENT.md` and `docs/RUNBOOK.md` say so plainly rather than reading as though something is live
- [ ] Backups. `postgres-data` is a Docker volume and nothing dumps it. Before real traffic: scheduled `pg_dump`, a *tested* restore, and a stated recovery objective. Writing the procedure without a system to test it against would produce a document nobody can trust
- [ ] Alerting. The runbook lists the log events worth waking somebody for; no aggregator is configured to act on them, and configuring one is a decision about which service, made once there is something to page about
- [ ] Zero-downtime releases. `up -d --wait` stops the old container before the new one is healthy, so a deploy is a short outage. Blue/green needs a second upstream and a proxy that can switch between them — worth doing, and not worth guessing at before there is traffic to protect
- [ ] Signed images and SBOM attestation. The build emits provenance; signing (cosign) and an attested SBOM are the next step and want a key-management decision this project has already deferred once (ADR-034)

## Phase 16 — Account lifecycle and session revocation

Opened by the authentication review, which found that `users.is_active` was checked on every request and written by exactly one line — `is_active=True` at creation — so the check guarded a column no code path could change. With no member-removal API, no password reset and no password change, a leaked refresh token could not be revoked for one person at all; the only lever was rotating `JWT_SECRET`, which signs out every user of every tenant and is an outage rather than a revocation.

- [x] `users.token_version` (migration `0021`), stamped into every access and refresh token as a `ver` claim and compared against the row on use (ADR-034 is unrelated; this is ADR-036). The access-token check rides the user row `get_current_user` already loads to verify `is_active`, so revocation costs no extra query and takes effect on the next request rather than waiting out the fifteen-minute access lifetime
- [x] Self-service revocation: `POST /auth/logout-all` ends every session including the calling one, because exempting the caller would leave the session an attacker is most likely to be holding
- [x] `POST /auth/password` — an authenticated password *change*, proving the current password, ending every session on success. Not a reset; see below
- [x] Platform account lifecycle: `POST /platform/users/{id}/disable` and `/enable`, both audited with the resulting version so a revocation is provable afterwards. Platform-authorized rather than workspace-authorized, because an account is a global identity and a tenant administrator able to suspend one could evict somebody from workspaces that administrator has nothing to do with
- [x] Re-enabling bumps the version as well as restoring the account. Without it a disable/enable cycle would resurrect exactly the credentials the disable existed to kill — a token minted before the suspension is still signed and may still be inside its lifetime
- [x] A token carrying no `ver` claim is refused, so applying `0021` signs out every open session once. Treating an unversioned token as current would leave precisely the tokens this mechanism exists to revoke permanently exempt from it
- [x] Four new audit actions (`user_disabled`, `user_enabled`, `user_sessions_revoked`, `password_changed`), recorded with no tenant so they appear in the platform trail
- [x] `tests/integration/test_account_lifecycle.py` — 20 adversarial tests: a leaked refresh token after revocation, after a password change, before a disable and across a disable/enable cycle; an unversioned token; a stale version; cross-user token substitution; a refresh racing a revocation; workspace isolation unaffected; a workspace owner refused the platform routes and platform staff allowed them; an administrator refused their own disable

**COMPLETE.** Verified 2026-08-23 against PostgreSQL 16: 1428 tests passed, 0 failed, 0 skipped; Ruff, Black and MyPy clean (`mypy app`, 168 files); migration `0021` applies from an empty database, `alembic check` reports no drift, and the schema downgrades to base and reapplies clean.

**Password reset is deferred, not forgotten.** It serves somebody who *cannot* sign in, so its one-time token has to reach an address that person controls — delivery is the security control, and without it there is no proof of ownership. This repository has no email capability of any kind: no SMTP, no provider client, no queue. The shortcut the invitation flow takes (returning the token through the API) is sound there because the caller is an authenticated administrator already trusted to hold it, and catastrophic here because the request is unauthenticated by necessity — anyone could ask for a token for any address and read it out of the response. What a reset will need when email exists is written down in `docs/SECURITY.md` so it is not redesigned under pressure.

Deferred by decision, not unfinished:

- [ ] Per-workspace member removal and suspension. `memberships` has no `status` column and there is no `/members` router. This is now the **largest** remaining gap in access control: platform staff can disable a whole account and a person can end their own sessions, but a workspace owner still cannot withdraw one colleague's access to one workspace
- [x] Password reset — **shipped in Phase 18** once email delivery existed (ADR-042), built to the list `docs/SECURITY.md` wrote down in advance
- [ ] Refresh-token family revocation on reuse. Reuse is detected and the presented token refused; the chain a thief already established is not torn down automatically. Bumping the version does tear it down, so the lever exists and is simply not pulled by the reuse path yet
- [ ] Per-session revocation. `token_version` is per user, so signing one device out while leaving another alone is not expressible. That needs a row per refresh token — a write on every rotation, a cleanup job, and a new failure mode — for a device-management surface this product does not have (ADR-036)
- [ ] Rate-limiting `POST /auth/logout`, which is unauthenticated and unlimited. Revoking a token you hold is legitimate; so is revoking one you stole

## Phase 17 — Final security audit

A repository-wide adversarial review that trusted no previous report, including
this file. Findings were verified behaviourally — against a production-configured
application and a running container — rather than by reading the code that was
supposed to implement them.

- [x] **Validation errors no longer echo the submitted value.** Confirmed live in
      production configuration before the fix: an over-length password came back
      in full in the 422 body. `input`, `url` and `ctx` are stripped; `loc`,
      `type` and `msg` remain, because those are what make an error actionable
- [x] **Security headers set by the application**, not only by nginx — CSP, HSTS,
      `nosniff`, `DENY`, `no-referrer`, `no-store`. HSTS only over real HTTPS,
      and the forwarded protocol believed only from a trusted peer
- [x] **SSRF guard on the one URL this application does not construct.** Every
      redirect hop validated, judged by resolved address rather than hostname,
      IPv4-mapped forms handled. Corrects the earlier claim that the fetch
      carries a bearer token across redirects — httpx strips it, and a test now
      pins that
- [x] **A tighter body cap on the webhook** (1 MB), applied as a `min` so
      lowering the general cap can never loosen it. An existing test caught that
      regression during the work
- [x] **Production refuses `CORS_ORIGINS=*`**, which Starlette answers by echoing
      any origin when credentials are allowed
- [x] **Container hardening**: `cap_drop: ALL` and `no-new-privileges` on the
      three application services
- [x] 56 new tests, including a mutation check — a guard whose removal the suite
      cannot detect is weak evidence

**COMPLETE.** Verified 2026-08-23: 1484 tests pass, 0 failed, 0 skipped; Ruff,
Black and MyPy clean; migrations apply from empty, downgrade to base and reapply
with `alembic check` reporting no drift; the runtime image builds, the container
reports **healthy** and runs as uid 1001, all five security headers were observed
over real HTTP, and production was observed refusing to boot with docs enabled
and with a CORS wildcard.

Verified sound and left alone: credential encryption at rest (AAD binding,
unknown key, corrupted ciphertext and malformed envelope all refused
behaviourally), logging (no secret reaches a log line, a schema or audit
metadata), and CI token permissions.

Deferred by decision, not unfinished:

- [x] Pin GitHub Actions by SHA rather than by mutable tag — done; the tag is
      kept as a trailing comment so a human can still read the version
- [x] Redis authentication (W-05) — `REDIS_PASSWORD` required in production, and
      the healthcheck authenticates so a bad password fails it rather than
      reporting a server that refuses every real client
- [x] WhatsApp number ownership verification (W-02 / M-01) — ADR-037. Proven
      against the Graph API with the workspace's own credential, before anything
      is written; the platform token is deliberately not a route to it
- [x] Per-workspace member removal (W-03a) — ADR-038. A membership status,
      enforced at the one place every workspace route already resolves
- [x] Refresh-token reuse teardown (W-10) — ADR-039. Atomic spend, and losing the
      race invalidates the account's whole token estate
- [x] `JWT_ALGORITHM` constrained to the HMAC family, so `none` and the
      asymmetric families cannot be configured
- [x] DNS rebinding on outbound fetches — ADR-040. Reproduced (the validator
      allowed a URL and the connection returned a loopback service's body), then
      closed by pinning every outbound connection to a validated address
- [x] Redis fail-open on the authentication limiter — ADR-040. Capacity limits
      still fail open; credential limits fall back to a bounded process-local
      counter. Failing closed was rejected as attacker-triggerable
- [x] `POST /auth/logout` rate-limited, and deliberately still unauthenticated
- [x] Re-verify a number already held — ADR-041. Rows claimed before ADR-037 had
      no way to gain proof except release-and-reclaim, which frees the number
      platform-wide in between
- [ ] Re-verify number ownership on a *schedule*. The mechanism now exists
      (ADR-041); only the trigger is missing, so a number that moves at Meta
      after the fact still goes unnoticed
- [ ] Streaming size cap on media download: the limit is enforced once the body
      is in memory rather than while it arrives
- [ ] Registration account enumeration (W-12) — accepted, not fixed. Needs the
      same email delivery channel password reset is blocked on; see ADR-040 for
      why merging the conflict messages would be theatre

## Phase 18 — Email infrastructure

Wasla could not send email at all. Four things were blocked on that: invitations
minted a token nobody could deliver, password reset was deferred in
`docs/SECURITY.md` for exactly that reason, account security changes told the
audit trail but never the person, and billing events reached nobody. ADR-042
records the architecture.

- [x] Provider abstraction (`EmailProvider`) with a Resend adapter spoken to
      over plain HTTPS — no SDK, no supply-chain entry for one JSON POST. The
      API key exists only in an `Authorization` header; provider error bodies
      are truncated before they reach a log or a row, because provider errors
      quote the credentialed request back
- [x] Transactional outbox (`email_messages`, migration `0026`). The action that
      decides an email should exist writes the row on its own session, so the
      two commit or roll back together. The idempotency key is a unique
      constraint, so racing callers produce one row rather than two
- [x] Email worker as one `WORKER_KINDS` entry, claiming with
      `FOR UPDATE SKIP LOCKED`, exponential backoff with jitter, and recovery of
      rows a killed worker left `sending`
- [x] Delivery is **at-least-once**, and the window is **one message**: the
      claim commits before any network call and each message is delivered in its
      own transaction. The first implementation committed the whole sweep at the
      end, so a worker killed on the fiftieth message re-sent the forty-nine
      before it
- [x] Resend delivery-event webhook, Svix-verified: HMAC over the exact bytes,
      a replay window in both directions, constant-time comparison, and multiple
      signatures so a secret can be rotated without dropping deliveries
- [x] Bounce and complaint suppression. **The address suppressed is the one our
      own row recorded, never one from the payload** — a verified delivery proves
      who sent the request, not that its contents are true
- [x] Password reset (migration `0027`), built to the list `docs/SECURITY.md`
      wrote down before it: hashed single-use token, 30-minute expiry,
      supersession, a constant response, a session bump, and the token never
      logged nor returned
- [x] Invitation, password-change, sessions-revoked, account-disabled and
      account-enabled notices, all queued transactionally and all addressed to
      the row's own `email` rather than anything from the request
- [x] Billing notices wired to the domain events that already existed: invoice
      issued and trial expired from the billing sweep, subscription cancelled
      from the subscription service. No business logic was invented to give a
      template a caller
- [x] Production fails closed on a half-configured setup: missing sender, a
      sender that is not an address, a missing or dangerously-schemed
      `APP_PUBLIC_URL`, the fake provider, or a missing `RESEND_WEBHOOK_SECRET`
- [x] 220 email tests — provider failure classes, header-injection refusals,
      template escaping and link construction, outbox idempotency and recovery,
      webhook forgery and replay, suppression, password reset replay and
      enumeration, transactional rollback, and cross-workspace isolation

**Corrections to the work as first committed.** The nine commits this phase
inherited had never been run through the verification gate, and it found real
defects rather than only style ones:

- `alembic check` **failed**: migration `0026` declared server defaults the
  model did not, so autogenerate saw permanent drift. The model now declares
  them, matching the convention the rest of `app/db/models/` follows
- The worker committed one transaction per **sweep**, so a crash re-sent the
  whole claimed batch. Split into claim-then-per-message
- Suppression compared addresses case-sensitively while nothing normalised the
  recipient on the way in. Both ends now normalise
- Production did not require `RESEND_WEBHOOK_SECRET`, so a deployment could run
  with the delivery endpoint answering 503 to every bounce — recording no
  suppressions until the sending domain was what failed
- `EMAIL_FROM` was checked for presence but not shape, and a sender the provider
  rejects fails *every* row permanently
- `APP_PUBLIC_URL` was checked for an `https://` prefix in production only,
  leaving `javascript:` and `data:` acceptable elsewhere
- The three billing templates and `enqueue_for_tenant_owners` were dead code
- ADR-042 was referenced fourteen times in the code and had never been written
- Ruff (7), Black (10 files) and MyPy (2) all failed

**Verified 2026-08-24 against PostgreSQL 16** — see the Phase 18 report for the
exact figures, including what was *not* verified.

**Not verified: real Resend delivery.** No message has been handed to Resend, no
`RESEND_API_KEY` has been used, and no delivery event has arrived from Resend's
own infrastructure. Everything above the webhook boundary is exercised against a
mock transport and the fake provider. The first production send still has to
walk the checklist in `docs/EMAIL.md` with real credentials.

Deferred by decision, not unfinished:

- [x] ~~Email verification~~ — **built** (ADR-043), and revisited on a different
      trigger than the one ADR-042 named. No capability came to depend on an
      unverified address, and none does now; what changed is that account
      squatting was undetectable, and a verified-at column is what lets the
      question be asked at all. It still grants nothing
- [ ] Templates whose domain events do not exist yet: invitation accepted,
      membership revoked, role change, subscription started, trial *ending*,
      payment succeeded, failed and pending
- [ ] Redis degradation of the reset limiter specifically. The limiter degrades
      rather than disappears (ADR-040); that path through the reset endpoints is
      not separately covered

## Phase 19 — Email verification

Six-digit codes over the existing outbox (ADR-043). `users.email_verified_at`,
one live challenge per account enforced by a partial unique index, Argon2
verifiers, a per-challenge attempt cap and two authenticated endpoints under
`/auth/email/verification`. Registration issues the first code in the same
transaction as the account.

Found and fixed while finishing work started earlier on this branch:

- **The router had no `route_class=CommittingRoute`** — the only one in
  `app/api/v1/` without it. Inclusion does not confer it, so verification would
  have answered `200` with a timestamp while the write was rolled back on the
  way out. `app/api/route.py` claimed in a docstring that every router carries
  the class; nothing checked, and now a test walks the whole tree
- **The attempt ceiling was off by one.** An attempt is counted before the code
  is compared, so the consuming UPDATE's `attempts < max` rejected the correct
  code on the last permitted try and filed it as a lost race — a cap of five
  that allowed four, failing the person who typed carefully after mistyping
- **A rejected challenge reported the wrong reason.** Any challenge with one
  failed attempt behind it was recorded as `attempts_exhausted`, so an address
  change on a mistyped challenge read as brute force in the trail
- **The TTL and attempt settings did not exist.** The bounds and the default
  were there and nothing read a setting, so `EMAIL_VERIFICATION_TTL_SECONDS`
  was inert
- **Registration queued nothing**, so a new account had no code until it asked
- **A rate-limited attempt left no audit row.** Closing it needed the service to
  commit the entry itself: the refusal raises, and an exception discards the
  transaction the entry was staged in
- **`/auth/me` did not report verification state**, so a client could only find
  out by mailing somebody a code
- **Every failed attempt rolled back**, so the per-challenge attempt cap did not
  exist in a deployment. `confirm` raises, an exception unwinds the request's
  transaction, and the increment and audit entry went with it. Found by driving
  a container over HTTP - seven wrong codes left `attempts` at zero - and
  invisible to every session-scoped test, because those drive the service on a
  session nobody rolls back

**Verified 2026-08-27 against PostgreSQL 16 and Docker.** Figures in the phase
report, including what was *not* verified.

**Verified: real Resend delivery.** One verification code was mailed through
Resend to a real mailbox on 2026-08-27 and reported `delivered` by Resend's
API; the delivered code verified the account over HTTP, a replay was refused,
and an aged challenge was refused as expired. The outbox context was cleared on
send, and neither the credential nor the code reached a log.

**Not verified: the delivery webhook.** No event has arrived from Resend's
infrastructure - that needs a publicly reachable URL this environment does not
have - so suppression and bounce handling are still exercised only against
synthesised payloads.

Deferred by decision:

- [ ] No email-change flow exists to integrate with. Challenges already record
      the address they were issued for and both the check and the consuming
      UPDATE compare it to the current one, so the flow cannot be written in a
      way that lets an old code verify a new address — but the flow itself is
      not built, and `email_verified_at = NULL` on change is a rule written down
      rather than enforced by code that exists
- [ ] Nothing sweeps dead challenges. They are small and carry no usable secret
      once superseded; a cleanup job would be operational work this repository
      does not otherwise have
- [ ] Invitation acceptance does not mark an address verified, though redeeming
      a token delivered to it is the same proof. Left alone rather than added
      speculatively — it is a product decision, not a security gap

## Phase 20 — Paymob payments

Card payments through Paymob's Intention API and Unified Checkout (ADR-044),
behind a second provider protocol so the billing domain cannot see which
processor is behind it. `BILLING_PROVIDER` defaults to `manual`, so a
deployment that configures nothing bills exactly as before.

Built to the current official documentation, read 2026-08-27. The details an
older tutorial gets wrong, and which are pinned by tests: the API host and the
checkout host differ; test and live share a base URL and the keys decide the
mode; `special_reference` returns as `order.merchant_order_id`; and the HMAC
covers twenty named fields in a documented order, verified against the vendor's
own published worked example rather than against our own function.

Two billing defects found while reading the domain and fixed first, because
taking payments makes both worse:

- **A cancelled subscription kept granting its plan.** `SERVING_STATUSES` and
  `Subscription.is_serving` existed from the first billing commit and neither
  was read, so cancelling was a way to keep the entitlements and stop the
  invoices
- **A private plan could be self-selected** by posting its code to
  `start` or `change_plan`. `GET /billing/plans` filtered the catalogue and
  nothing enforced it as a rule

Deferred by decision:

- [ ] **PayTabs.** Not started. The provider boundary is where it goes

**Not verified: anything against a live Paymob account.** No intention has been
created, no callback has arrived from Paymob's infrastructure, and no card has
been charged. The HTTP boundary is exercised against a mock transport. Every
claim about behaviour is a claim about code checked against published
documentation.

## Phase 21 — The rest of the payment lifecycle

Completes phase 20 (ADR-045). Refunds, a renewal that can actually be paid,
dunning when one is not, and explicit state machines so a callback is checked
against what we already believe rather than applied because it was signed.

One defect found in phase 20's own work and fixed here:

- **`payment_events` recorded every callback as `applied`,** whatever was
  decided. An event naming an unknown payment, or reporting an amount that
  disagreed with the invoice, was refused and then filed as a success — so the
  one table an operator reads to find out why a payment never landed said that
  it had

Built:

- **Payment and invoice state machines.** `refunded → succeeded` and
  `failed → succeeded` are refused, so a signed-but-late callback cannot settle
  an invoice twice or erase a decline. A paid invoice cannot be settled again
- **Composite provider event ids.** `{transaction}:{state}`, because one
  transaction produces several callbacks — `pending` then `success`, and a
  refund notification that arrives on the *parent* transaction. Keying on the
  transaction alone filed every later callback as a duplicate of the first
- **Refunds**, request-then-confirm. The amount is the payment's own unreturned
  balance and is never accepted from a caller; `refunded_amount` is written only
  by a verified callback, which is also where a refund issued from Paymob's
  dashboard arrives
- **Checkout for an outstanding invoice**, which is what makes a renewal
  collectible rather than merely recorded
- **Checkout idempotency**, `UNIQUE(tenant_id, idempotency_key)`. A repeat is
  refused rather than replayed: the response carries a one-use URL that is
  deliberately never stored
- **Dunning.** A renewal unpaid for seven days from `issued_at` moves the
  workspace to `past_due` — still served, audited, and put right by the
  settling callback
- **`GET /billing/payments/{id}`**, so a client can ask the server what
  happened instead of believing the browser redirect
- **Configuration hardening.** Mismatched key modes and test keys in production
  are refused at boot; duplicate and non-positive integration ids are refused
- **Credential redaction** in provider error text, because Paymob quotes the
  request back and truncation bounds how much returns, not what

Deferred by decision:

- [ ] **Automatic card debits.** Paymob's Subscription API needs a MOTO
      integration id enabled per merchant, an API key for their older
      auth-token flow, and bills on a fixed number of days where Wasla bills on
      calendar months. The remaining dependency is merchant configuration, not
      missing code — see ADR-045
- [ ] **Partial refunds and credit notes.** A refund returns a payment's
      remaining balance; there is no way to render "half of March" on an
      invoice, and inventing one to match a provider capability would be
      building a concept the model cannot show
- [ ] **Void.** Available at the provider seam and through Paymob's dashboard.
      Choosing between void and refund needs error semantics this integration
      has never seen

**Not verified: anything against a live Paymob account.** No intention has been
created, no callback has arrived from Paymob's infrastructure, no card has been
charged and no refund has been issued. The HTTP boundary is exercised against
`httpx.MockTransport` and the signature against the vendor's published worked
example.

## Phase 22 — Audit remediation, batch 0

The seven defects a repository-wide audit found at the **seams between
subsystems**, each of which was individually well built and individually well
tested. Every one of them lived in a gap that no subsystem owned, which is why
the suite was green over all of them.

- [x] **An invitation can no longer claim an existing account** (ADR-057).
      `accept` set a password whenever the account had none, on the reasoning
      that passwordlessness meant "created by an earlier invitation". Google
      sign-in creates passwordless accounts on purpose, so anybody who could
      register could invite a Google user's address, redeem the invitation with
      a chosen password, and sign in as them. A password is now written only on
      the branch that creates the account
- [x] **The raw invitation token no longer crosses the API boundary**
      (ADR-057). It was returned by `POST /invitations` because there was no
      mail delivery; there is now, and the schema's own docstring said the field
      would go when there was
- [x] **`POST /auth/password/set`** (ADR-057) — the legitimate route the hole
      above was standing in for. Authenticated, self-only, refused for an
      account that already has a password, and reusing `change_password`'s
      revocation, audit and notification policy exactly. A Google-first account
      can now set a password and afterwards disconnect Google, which is what
      `unlink`'s refusal has always instructed
- [x] **Workspace switching returns a usable token** (ADR-058).
      `select_workspace` omitted `token_version`, so `POST /auth/workspace`
      answered 200 with a token every later request refused as revoked.
      Multi-workspace switching did not work at all. Every access token is now
      minted by one function
- [x] **A priced plan is granted only by settlement** (ADR-059). `change_plan`
      moved a workspace onto any public plan for free, so the whole checkout
      pipeline was optional decoration. Self-service is now refused with 402 for
      a priced plan, and `CheckoutService._settle` applies the invoice's plan
      when a signed callback says it is paid
- [x] **A trusted proxy is an address, not a name** (ADR-060). The shipped
      compose file set `TRUSTED_PROXY_IPS=nginx`, which can never equal a peer
      address, so every client on the internet shared one authentication
      rate-limit bucket and HSTS was never emitted. Entries are now parsed as
      addresses or CIDR networks, a hostname is refused at startup, and the
      compose file gives nginx a fixed address on an explicit subnet
- [x] **`.claude/` is ignored, and secrets are scanned in the working tree.**
      The directory was untracked rather than ignored, so `git add -A` would
      have staged a full checkout including its own `.env`. Nothing was ever
      committed. `gitleaks` now runs as a pre-commit hook over the staged diff,
      not only over history in CI

Cross-subsystem regression tests, which is the half that was missing rather than
the fixes:

- [x] `tests/integration/test_identity_seam.py` — invitations against Google
      accounts, over HTTP against real rows, including the exploit reproduced
      with the token taken from the outbox so the test holds even if the
      response leak returns
- [x] `tests/integration/test_workspace_switching.py` — switches, then *spends*
      the token on `/auth/me` and on a workspace-scoped route
- [x] `tests/integration/test_paid_plan_settlement.py` — the free upgrade
      refused, a verified callback granting the plan, and declined, forged,
      mismatched, replayed and cross-tenant callbacks granting nothing
- [x] `tests/unit/test_trusted_proxies.py` — addresses, networks, IPv6, and a
      hostname refused at startup

Still open, and deliberately not addressed here:

- [x] **Dunning.** Closed in Phase 23 by ADR-061: `SUSPENDED` is where the
      grace ends, and it is outside `SERVING_STATUSES`
- [ ] **A browser-bound OAuth state.** The login-CSRF residual disclosed in
      ADR-047 is unchanged
- [ ] **Recovering a Google-first account that has already lost Google access.**
      `POST /auth/password/set` needs a live session, and a password reset is
      declined for an account with no password hash, so somebody who never set
      a password and can no longer sign in with Google needs support to
      re-establish the identity (ADR-057)
- [ ] **Redis failure on `/auth/refresh`** answers 500 rather than 503
- [ ] **The Paymob client** does not use `build_guarded_client`, which every
      other integration does
- [ ] **Media MIME types** are the caller's claim, with no magic-byte check
- [x] **Compose files carry no `GOOGLE_*`, `PAYMOB_*`, `EMAIL_*` or
      `APP_PUBLIC_URL`**. Closed in Phase 23 by ADR-062, with a drift guard so
      it cannot recur silently

## Phase 23 — Commercial enforcement and deployment readiness

The two items Phase 22 recorded as open and most load-bearing: a billing
lifecycle that ended nowhere, and a shipped deployment that could not switch on
the features the code already had.

- [x] **A workspace that stops paying stops being served** (ADR-061).
      `PAST_DUE` was a serving status and nothing moved a subscription past it,
      so retention was unenforced even after ADR-059 made the purchase safe.
      `SubscriptionStatus.SUSPENDED` is where the grace ends: outside
      `SERVING_STATUSES`, so `EntitlementService` falls the workspace back to
      the default plan rather than locking it out
- [x] **A settled payment lifts a suspension** — and only a suspension. The
      recoverable set is two closed members, so a cancellation and an expiry
      stay where the customer left them
- [x] **Both dunning thresholds are configuration**, anchored on `issued_at`,
      with the hard one validated to be strictly later than the soft one in
      every environment
- [x] **Migration `0037`** adds the `suspended` status and the
      `subscription_suspended` audit action. Two enum labels, no column, no
      data change
- [x] **Production Compose passes what each process reads** (ADR-062). Google,
      email, Resend, Paymob, `APP_PUBLIC_URL`, `BILLING_PROVIDER` and the
      dunning thresholds, split so the Resend key reaches only the worker and
      no Google credential reaches it at all
- [x] **A drift guard derived from `Settings`** rather than a fourth copy of
      the same list, checked in both directions and proven to fail when a
      required setting is removed from the Compose representation

Tests added:

- [x] `tests/integration/test_dunning_lifecycle.py` — the lifecycle asserted at
      the *entitlement* level rather than on a status column, because a test
      that only read the label would pass against the bug. Soft threshold still
      serving, hard threshold degrading to the default plan, recovery from both
      `PAST_DUE` and `SUSPENDED`, no revival of a cancelled or expired
      subscription, idempotency by status and by outbox key, tenant isolation,
      and the boundary just before, exactly at and just after the threshold —
      all against a fixed clock
- [x] `tests/integration/test_deployment_configuration.py` — the drift guard,
      plus assertions that no feature setting is mandatory at interpolation
      time and that no credential is written literally into the shipped file
- [x] `tests/unit/test_config.py` — Google sign-in disabled, incomplete and
      complete, which had no coverage at all, and the dunning ordering rule

Still open, and deliberately untouched here:

- [ ] **SEC-07** — a browser-bound OAuth state
- [ ] **SEC-08** — the Paymob client does not use `build_guarded_client`
- [ ] **SEC-10** — Redis failure on `/auth/refresh` answers 500 rather than 503
- [ ] **SEC-11** — Google `name` and `sub` claims are unbounded against their
      columns
- [ ] **Retention policy for a suspended workspace.** How long its data is kept,
      and whether a suspension should ever become a cancellation on its own, are
      commercial decisions rather than missing code (ADR-061)
- [ ] Observability: metrics, tracing, error tracking, alerting
- [ ] Queue retry with backoff, attempt counts and dead-letter monitoring
- [ ] Object storage and a media retention sweep; an ANN vector index

## Phase 24 — Verifying Phase 23 rather than trusting it

Phase 23 claimed two things it had not actually demonstrated. Both turned out to
be nearly right, and the gap between "nearly" and "proved" is what this phase
closed.

- [x] **A suspended customer can reach a payment page.** `_settle` supporting
      `SUSPENDED -> ACTIVE` shows the transition is *possible*; it does not show
      it is *reachable*, and every existing recovery test inserted the pending
      payment row directly — a state only this system can create. Since
      `SUSPENDED` is a member of `TERMINAL_SUBSCRIPTION_STATUSES`, one generic
      `if subscription.is_terminal` on the way to checkout would have made the
      suspension permanent while every test still passed. Traced: no such guard
      exists on `CheckoutService.start`, `_priced_plan`, `_collectible_invoice`,
      `_open_invoice`, the billing routes or workspace resolution, and dunning
      writes only `subscriptions.status` — never the tenant or the membership —
      so authentication is unaffected. **No production code change was needed**;
      the reachability is now asserted rather than inferred
- [x] **The webhook secret reached a process that never reads it.**
      `RESEND_WEBHOOK_SECRET` is read in exactly one module,
      `api/v1/email_webhooks.py`, but was required by the shared `Settings`
      validator — which every process builds — so the worker had to be handed it
      to boot at all. `docker-compose.prod.yml` said so in a comment, and
      `docs/DEPLOYMENT.md` already documented the opposite as the intent. Closed
      by ADR-063: the requirement moved to
      `integrations.email.require_delivery_verification`, called from
      `create_app`, mirroring what `build_email_provider` already did for
      `RESEND_API_KEY`. Each half of the Resend configuration now reaches
      exactly one container
- [x] **The drift guard only checked one direction.** `EXPECTED_ABSENT` recorded
      decisions and nothing verified them, so adding a credential to a process
      that does not need it passed every assertion in the file. Absence is now
      enforced too
- [x] **Settings accounting reconciled.** Twenty-four settings across the four
      optional integrations, not twenty-one — the earlier figure was the count
      the API carries, and Paymob has six settings rather than five. Derived
      mechanically from `Settings.model_fields` against both Compose files and
      `.env.example`; no setting was missing from either process or from the
      example file, so the discrepancy was arithmetic in the write-up and not a
      configuration defect

Tests added:

- [x] `tests/integration/test_dunning_lifecycle.py` — a ninth section walking
      the reachable recovery path over HTTP: suspension by the real sweep, then
      the owner's own requests through `GET /billing/subscription`,
      `POST /billing/checkout` by invoice id *and* by plan code, the verified
      Paymob callback, and paid entitlements returning. With the counterweights:
      starting a checkout and not paying leaves the workspace cut off, and
      `POST /billing/subscription/plan` still answers 402 for the paid plan and
      409 for the free one, so recovery opens no free door. Cancelled and
      expired subscriptions pay a *fresh* checkout and still do not revive —
      the stronger form of the old-callback test. Verified by injecting the
      hypothetical `is_terminal` guard into `CheckoutService.start`: five of the
      new tests fail and no pre-existing test does, which is what says the gap
      was real
- [x] `tests/unit/test_email_configuration_is_per_process.py` — which process
      fails, not merely that something does, since a test asking only the latter
      passed before the split and after it. Includes a subprocess check that
      `app.workers.runner` never imports `app.main`, because that import is what
      would silently drag the API's startup checks back into the worker
- [x] `tests/integration/test_deployment_configuration.py` — every
      `EXPECTED_ABSENT` entry asserted genuinely absent, and neither container
      holding both halves of the Resend configuration

Reviewed and deliberately left alone:

- [ ] **`TERMINAL_SUBSCRIPTION_STATUSES` keeps its name.** It means "the sweep
      advances this no further", which is documented on the constant, on
      `is_terminal`, and pinned by `tests/unit/test_billing_models.py`. Every
      one of its four readers wants that meaning; the one place the other
      question is asked has its own helper, `is_suspended_for_non_payment`, and
      settlement uses a closed `_RECOVERABLE_STATUSES` rather than "not
      terminal". Renaming would be churn across correct code
- [ ] **All six `PAYMOB_*` settings stay on both processes.** The API creates
      intentions and verifies callbacks, the worker charges saved cards for
      renewals, and both build the same provider through
      `build_checkout_provider`. `PAYMOB_HMAC_SECRET` is the one value the
      worker constructs but never uses; withholding it would need a second,
      partial construction path in the money code to remove a secret from a
      container that already holds `PAYMOB_SECRET_KEY` (ADR-063)

## Phase 25 — Operations: retries, dead letters, metrics and backups

The audit's summary of this area was one sentence: "there is nothing that would
tell anyone an outage had started — no metric, no alert, no error tracker, and
no visibility into the queues where the symptom would first appear. Discovery
would be a customer complaint." Alongside it, "there is no backup or restore
procedure, because there is no backup system."

Four things closed, in the order they depend on each other.

### Queue reliability

- [x] **A failure has a category, and the category is a bounded vocabulary.**
      `FailureCategory` is eleven values; `RETRYABLE` is four of them. They are
      used as dead-letter fields and metric labels, which is why they are an
      enum rather than an exception message — provider prose can echo a
      customer's phone number. `unknown` is deliberately not retryable: an
      exception nobody has classified is one whose safety nobody has argued
- [x] **Retryability is also a property of the operation, not only of the
      failure** (ADR-068). Ingestion and media carry `IDEMPOTENT_RETRY` because
      re-running one changes nothing anybody sees. The agent worker carries
      `AGENT_RETRY` and narrows to `NO_RETRY` the instant `_TurnProgress`
      records that the turn engaged the provider — after which no failure it can
      catch is distinguishable from one that already sent a customer a reply
- [x] **Bounded exponential backoff with additive jitter**, scheduled in a
      Redis sorted set and promoted at the head of every reserve. The `zrem` is
      the claim, so two workers promoting at once cannot queue the same retry
      twice. `delay_for` is a pure function of the attempt and a jitter
      fraction the caller supplies, so tests pin exact delays without patching
      `random` or watching a clock
- [x] **A queued entry became a `JobEnvelope`** carrying the attempt count, the
      original enqueue time and the last failure category. `decode` accepts a
      bare payload as attempt 1, so a deploy while jobs are in flight strands
      nothing. The three near-identical queues became one `ReliableQueue`
- [x] The queue tests were given a fake that keeps real lists and sorted sets.
      The old one answered every `lrem` with 1, and dead-letter deduplication is
      *defined* as the second `lrem` removing nothing — so it could not have
      told the working implementation from the broken one

### Dead letters

- [x] **`fail()` became `dead_letter()` with a record**: job type, workspace,
      attempt count, first and last attempt times, failure category and when it
      was dead-lettered. Deliberately no exception text, no message content, no
      credential — the list outlives the incident and is read by whoever is on
      call
- [x] **Deduplicated by the reservation.** Removing the entry from the in-flight
      list is what proves the caller still holds the job, so calling the path
      twice writes one record. Capped at 1,000 per queue, trimmed from the old
      end
- [x] **An operator command, not an endpoint** (ADR-071):
      `python -m app.workers.queues status | dead-letters | replay`, reachable
      through the image's `queues` entrypoint. Replay refuses the agent queue
      without `--force`, re-queues as fresh first attempts, and leaves the
      records in place so a second failure can be compared with the first

### Metrics

- [x] **A registry written here rather than pulled in** (ADR-072), because the
      point is the guard: `_reject_unbounded` refuses a UUID, an email address,
      a phone-shaped run of digits or anything longer than 96 characters *as a
      label value*, at the moment a sample is recorded. Label names are fixed at
      declaration. It raises; `app/core/telemetry.py` swallows, so the guard is
      testable and can never fail a request
- [x] **`GET /metrics`** in Prometheus text 0.0.4: HTTP rate, latency and
      in-flight by route template; dependency readiness; queue pending,
      in-flight, delayed, dead-lettered and oldest-job age; worker heartbeats;
      job outcomes; provider calls for OpenAI, WhatsApp, Paymob and email
- [x] **The worker's numbers travel through Redis** (ADR-069) rather than
      through a second HTTP listener on the container that holds the Meta
      token, the OpenAI key and the Paymob secret
- [x] **Kept off the public listener** (ADR-070) rather than behind a shared
      token: the API publishes no port, `nginx.conf` answers 404 for the path,
      and `METRICS_ENABLED=false` removes it
- [x] `docs/OBSERVABILITY.md` — the catalogue, the cardinality rules, the retry
      safety matrix, and concrete alert expressions. Stated as recommendations,
      because no Alertmanager exists in this stack and claiming otherwise would
      be the drift this repository keeps catching itself in
- [x] **No error-monitoring vendor was added**, and the reasoning is written
      down rather than left as an omission: it would receive stack frames from
      the process holding every credential and every conversation, and the
      redaction that keeps those out of the logs is written against this
      application's log records, not against an SDK's automatic context capture

### Backups

- [x] `scripts/backup_postgres.sh` and `scripts/restore_postgres.sh`, a one-shot
      `backup` Compose service behind a profile, and host cron. The password
      never reaches a command line or the output; each dump is read back with
      `pg_restore --list` before it is believed; retention prunes only after a
      success, so a failed run cannot delete the last good backup
- [x] **The restore verifies** (ADR-073): schema, `vector` and `pgcrypto`,
      `alembic_version` against `WASLA_EXPECTED_HEAD`, and representative rows.
      The target is always named — restoring over the configured database needs
      `WASLA_RESTORE_ALLOW_PRODUCTION=yes`
- [x] **A drill was executed**, not written down and left: 149,666-byte dump of
      a schema at head 0037 with a real 1536-dimension embedding, restored into
      a database created by the script, verified by the script and then read
      through the application's own ORM. Every failure case exercised too —
      bad credentials, truncated dump, garbage file, production target,
      existing target, wrong head. `docs/BACKUP.md` records all of it

### Found while working

- [x] **A pre-existing flake, diagnosed and fixed.**
      `test_a_summary_names_the_counters_a_dashboard_asks_for` failed roughly
      one run in three at `f31c34d`, and never on CI. It recorded usage at
      `datetime.now(UTC)` and then summarised with a default window ending at
      `datetime.now(UTC)`; the window is half-open, and this host's clock has a
      granularity of a few milliseconds, so the two reads could land in the same
      tick and `occurred_at == until` excluded every event. Reproduced (three
      full-suite runs, then twelve runs of the file alone), the assertion
      captured — `totals=()` — and closed by recording a minute in the past. The
      boundary itself keeps its own test
- [x] **A bug the mutation probes found in this phase's own work.** Every test
      of the agent retry boundary stubbed `_handle` and set `progress.engaged`
      by hand, so replacing `progress.engaged = True` with `False` in the real
      code passed the entire file. Closed with a structural test that reads
      `_handle`'s AST and asserts the marker is set, is set to `True`, and is
      set *before* the HTTP client is built

Deliberately not done here:

- [ ] **OpenTelemetry tracing.** Metrics answer "is something wrong"; tracing
      answers "where in the chain", and the second is only worth its weight once
      the first exists. P2
- [ ] **Provider latency histograms.** Provider calls are counted by outcome,
      not timed. The time is in the log line, and for AI in `usage_events`
- [ ] **Off-host backup copies and media backup.** The script writes to a
      directory; getting it elsewhere and encrypted is a deployment decision,
      and attachments need object storage before they can be backed up at all
- [ ] **An in-flight reaper.** A job reserved by a worker that died still sits
      in the in-flight list until an operator moves it. The runbook says so and
      says why moving one is a judgement rather than a sweep
## Phase 26 — Closing P1: crash recovery and disaster-resistant backups

P1-C shipped retries, dead letters, metrics and a verified restore, and left two
things unproven that it named honestly. This phase closed both, and both were
reproduced against the code before anything was written.

### A worker crash no longer strands work

- [x] **Reproduced first.** A reserved job was still on the in-flight list after
      a simulated thirty days, invisible to `pending`, `delayed` and `failed`,
      with no method on the queue capable of recovering it. Not an
      observability problem: a stranded job is an absence, and the queue looked
      healthy the whole time
- [x] **A reservation is now a record** - `<namespace>:reservations`, holding
      the worker, when it took the job, until when, and how far it had got
- [x] **The lease is renewed, not merely long** (ADR-074). A holder extends its
      leases every third of `QUEUE_VISIBILITY_TIMEOUT_SECONDS`, which removes
      the choice between a timeout long enough to cover the longest job anybody
      might run and one short enough to notice a death
- [x] **The engagement stage is written to Redis before the HTTP client is
      built.** `_TurnProgress.engaged` was in memory, which is exactly no use
      to a reaper looking at a process that no longer exists
- [x] **Recovery classifies rather than guesses.** `reserved` requeues,
      `engaged` on a non-idempotent queue quarantines as `uncertain_delivery`,
      and `unknown` is treated as unsafe there. Idempotent queues declare
      themselves so and are recovered at any stage
- [x] **A crash spends an attempt**, so a job that kills a worker every time
      runs out of budget instead of looping; one already on its last attempt is
      quarantined rather than given a hidden extra one
- [x] **`recovery` is a worker kind in `ALL_KINDS`.** It is the only loop that
      does nothing for its own queue, so a deployment running it nowhere has no
      crash recovery - which should take a deliberate act
- [x] Drilled with real containers: a process reserved a job through the
      production path, was `SIGKILL`ed (exit 137), and a replacement worker
      reclaimed it exactly once, attempt 1 -> 2, original enqueue time
      preserved. Then the agent variant: reserved, marked engaged, killed -
      quarantined, `agent:jobs:delayed` stayed at zero, and
      `queues replay agent` refused without `--force`

### A host failure no longer destroys every copy

- [x] **A run succeeds only when the artifact is verified off-host** (ADR-075).
      Dump, validate, upload, confirm the object's size at the destination, and
      only then advance the recorded last success
- [x] **One backend that is not one provider.** `aws s3` with an optional
      endpoint URL reaches AWS, MinIO, R2, Wasabi, B2 and Ceph.
      `BACKUP_DESTINATION=none` is refused rather than skipped, and a
      deployment needing something else replaces the script - a file boundary
      rather than a string something has to `eval`
- [x] **A shipped systemd timer**, `Persistent=true`, so a host that was down
      at 02:17 backs up when it returns rather than silently doubling the
      recovery window
- [x] **The freshness signal is a durable status file**, because the process
      that knows the answer has exited by the time anybody scrapes.
      `wasla_backup_age_seconds` is the age of the last *durable* backup, which
      is what makes the alert mean something: "newest file in `BACKUP_DIR`"
      would call a deployment healthy whose uploads had failed for a week
- [x] **Only the backup container holds object-store credentials**, asserted in
      both directions by the deployment drift guard. A compromised API must not
      be able to delete the database and every copy of it
- [x] **The restore script ships in the backup image.** The drill found it
      missing: on the day somebody needs it, the application image may not
      build and the repository may not be reachable
- [x] Drilled against MinIO with the staging volume **destroyed** before the
      restore, so what it proves is the remote copy. Fetched onto an empty
      volume, restored into a fresh database, verified schema, `pgvector`,
      migration head and rows, then read through the application's own ORM

### Found while working

- [x] **A label inconsistency, caught by a test written for it.** The recovery
      counter first reported `queue="agent:jobs"` while every depth gauge
      reported `queue="agent"`, which would have split one queue into two
      unrelated series. One `label` per queue now, and a test that the registry
      and the queues agree
- [x] **Two mutation probes that passed and should not have.** The
      concurrent-reaper test ran its reapers sequentially, so the second never
      reached the claim; and the renewal test used one queue object as both
      worker and reaper, so the worker's held set was emptied as a side effect.
      Both rewritten, both now catch their probe

Deliberately not done here:

- [ ] **A wedged event loop is still invisible.** Renewal and the heartbeat
      assert the same thing, so a loop blocked by a synchronous call keeps
      renewing. Every loop is I/O-bound async, so that is a bug rather than a
      state - but one this design cannot detect
- [ ] **No RPO or RTO adopted.** What the schedule implies is written down;
      what anybody has committed to is nothing, and adopting a target means
      measuring a restore at production scale
- [ ] **Media is still not backed up.** It needs object storage first, which
      is P2
