# Wasla

**AI employees for WhatsApp.** Wasla is a multi-tenant, AI-powered customer engagement platform built around the WhatsApp Business Cloud API. Businesses connect their WhatsApp numbers, configure specialised AI Agents, manage conversations, capture and qualify leads, answer questions from their own knowledge base (RAG), run campaigns, hand off to human agents, and monitor usage and analytics.

Wasla is not a chatbot demo. It is designed from day one as a scalable, secure, production-ready SaaS platform.

## Project status

**Phases 0 through 15 are complete.** The table below reflects the actual state of the code, not the roadmap. One caveat is worth stating up front: the delivery pipeline builds, scans and publishes images and can deploy one, but it has never run against a real host — no production deployment exists yet, and nothing here pretends otherwise.

| Area | Status |
| --- | --- |
| Engineering rules (`claude.md`) | Implemented |
| Documentation protocol (`Documentation_Protocol.md`) | Implemented |
| Project memory (`README` / `ARCHITECTURE` / `TASKS` / `DECISIONS`, `docs/`) | Implemented |
| Application foundation (FastAPI, config, logging, errors, DB, Redis, health, Docker, CI) | Implemented |
| Domain models and migrations (`0001`–`0046`) | Implemented |
| Multi-tenancy, authentication, workspace RBAC, invitations | Implemented |
| WhatsApp Cloud API (webhook, signatures, idempotency, outbound client) | Implemented |
| Conversations, inbox, human handoff, templates, cursor paging | Implemented |
| AI agents (configuration, memory, tools, orchestrator, queue) | Implemented |
| Knowledge base and RAG (ingestion, pgvector retrieval, `search_knowledge`) | Implemented |
| CRM and leads (capture, lifecycle, assignment, notes, activity timeline, `record_lead_details`) | Implemented |
| Follow-ups (scheduling, cancel-on-reply, window and template compliance, polling worker) | Implemented |
| Media (download, storage, vision, transcription, documents, outbound attachments) | Implemented |
| Sentiment, priority and automatic escalation to a human | Implemented |
| Campaigns and templates (approved-template registry, audiences, rate-limited sending, opt-out) | Implemented |
| Usage metering, and tenant analytics derived from the domain tables | Implemented |
| Platform owner view across every workspace (read-only) | Implemented |
| Plans, subscriptions, entitlements enforced against usage, invoices and payment records | Implemented |
| Dunning: an unpaid renewal becomes `past_due`, then `suspended`, and the paid plan stops applying | Implemented |
| Transactional email (outbox, Resend provider, delivery webhooks, bounce suppression) | Implemented |
| Password reset, invitation delivery and account security notices | Implemented |
| Google sign-in (OIDC, PKCE, identity linking, profile name and picture) | Implemented |
| A message actually delivered by Resend (DNS, webhook and reputation are the operator's) | Planned |
| Card payments through Paymob: checkout, signed callbacks, refunds, saved-card renewals | Implemented |
| A payment taken from a live Paymob merchant account | Planned |
| Production hardening: rate limits, audit trail, request limits, encrypted workspace credentials, worker liveness | Implemented |
| Worker process (media, agent, ingestion, follow-up, campaign, billing and email loops in one container) | Implemented |
| Container publishing, image provenance and vulnerability scanning | Implemented |
| Deployment automation (CI-gated, digest-pinned, migrate-then-serve, readiness-checked) | Implemented |
| A production deployment it has actually run against | Planned |
| Production TLS (documented and configured; certificates are the operator's) | Implemented |
| Operational runbook | Implemented |
| Queue reliability: bounded retries with backoff, attempt counts, dead-letter records and operator replay | Implemented |
| Crash recovery: leased reservations, renewal, an automatic reaper, and quarantine for deliveries a crash left uncertain | Implemented |
| Operational metrics (`/metrics`): HTTP, dependencies, queue depth and age, dead letters, worker heartbeats, provider outcomes | Implemented |
| PostgreSQL backup on a systemd timer, uploaded to off-host object storage, with a durable last-success signal | Implemented |
| Disaster restore from the off-host copy, verified by drill after the local copy was destroyed | Implemented |
| Media backup and a configured alerting stack | Planned |
| Distributed tracing through the API and into the workers, with a privacy allowlist | Implemented |
| An error-monitoring provider and zero-downtime releases | Planned |

Status vocabulary used across all documentation: **Implemented**, **In Progress**, **Planned**, **Blocked**.

See [TASKS.md](TASKS.md) for the phase-by-phase roadmap and the current status of every task.

## Technology stack

In use today:

- **Runtime:** Python 3.12, FastAPI, Uvicorn
- **Data:** PostgreSQL 16 with pgvector, SQLAlchemy 2.0 (async), Alembic
- **Cache / queues:** Redis 7
- **Config / validation:** Pydantic v2, Pydantic Settings
- **Quality:** Ruff, Black, MyPy, pytest, pytest-asyncio, pre-commit
- **Delivery:** Docker, Docker Compose, Nginx, GitHub Actions

Also in use:

- **AI:** OpenAI Responses API, OpenAI embeddings, vision and transcription
- **Messaging:** WhatsApp Business Cloud API (Meta Graph API) via httpx
- **Payments:** Paymob Intention API and Unified Checkout
- **Email:** Resend, with Svix-signed delivery webhooks
- **Identity:** Google OpenID Connect (PKCE, nonce, JWKS)

## Documentation map

| Document | Scope |
| --- | --- |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Technical source of truth for system architecture |
| [TASKS.md](TASKS.md) | Implementation roadmap and current task status |
| [DECISIONS.md](DECISIONS.md) | Architecture decision records (ADRs) |
| [docs/PRODUCT.md](docs/PRODUCT.md) | Product scope, personas, customer journey |
| [docs/SAAS.md](docs/SAAS.md) | Multi-tenancy, workspaces, platform owner, plans |
| [docs/AUTH.md](docs/AUTH.md) | Authentication, memberships, RBAC |
| [docs/GOOGLE_OAUTH.md](docs/GOOGLE_OAUTH.md) | Google sign-in: OIDC verification, identity linking, profile data |
| [docs/WHATSAPP.md](docs/WHATSAPP.md) | WhatsApp Cloud API integration and webhooks |
| [docs/AI_AGENTS.md](docs/AI_AGENTS.md) | AI agent configuration, orchestration, tools |
| [docs/RAG.md](docs/RAG.md) | Knowledge base, ingestion, tenant-scoped retrieval |
| [docs/CRM.md](docs/CRM.md) | Contacts, leads, follow-ups, human handoff |
| [docs/MEDIA.md](docs/MEDIA.md) | Attachments: storage, understanding, sending |
| [docs/SENTIMENT.md](docs/SENTIMENT.md) | Sentiment, priority, automatic escalation |
| [docs/CAMPAIGNS.md](docs/CAMPAIGNS.md) | Templates, campaigns, audiences, marketing opt-out |
| [docs/BILLING.md](docs/BILLING.md) | Plans, subscriptions, entitlements, invoicing |
| [docs/ANALYTICS.md](docs/ANALYTICS.md) | Analytics events, usage tracking, dashboards |
| [docs/EMAIL.md](docs/EMAIL.md) | Transactional email: outbox, provider, webhooks, suppression |
| [docs/API.md](docs/API.md) | API conventions and endpoint catalogue |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | Docker, Nginx, TLS, CI/CD, production deployment |
| [docs/RUNBOOK.md](docs/RUNBOOK.md) | Operating it: triage, symptoms, procedures, what to watch |
| [docs/OBSERVABILITY.md](docs/OBSERVABILITY.md) | Metrics catalogue, the scrape endpoint, alert expressions |
| [docs/BACKUP.md](docs/BACKUP.md) | PostgreSQL backup, retention, restore procedure and drill |
| [docs/SECURITY.md](docs/SECURITY.md) | Security model, secrets, isolation, auditing |

## Local development

**Status: Implemented.**

### Requirements

Either Docker with Compose v2.24 or newer, or Python 3.12 with local PostgreSQL 16 (pgvector) and Redis 7.

### Run with Docker

```bash
cp .env.example .env   # optional: Compose already carries working local defaults
docker compose up --build
```

- API: http://localhost:8000
- Interactive docs: http://localhost:8000/docs
- Migrations are applied on startup (`RUN_MIGRATIONS=true` in `docker-compose.yml`)

There is no worker service yet; background workers arrive in Phase 8.

### Verify the service

```bash
curl http://localhost:8000/health         # identity and status, no dependency checks
curl http://localhost:8000/health/live    # liveness, never touches PostgreSQL or Redis
curl http://localhost:8000/health/ready   # readiness, checks PostgreSQL and Redis
```

`/health/ready` returns `503` with a per-dependency breakdown when PostgreSQL or Redis is unreachable. `/health/live` intentionally stays green in that case, because the process itself is healthy and restarting it would not help.

### Run without Docker

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
alembic upgrade head
uvicorn app.main:app --reload
```

### Tests and quality gates

```bash
pytest                 # unit and integration tests
pytest --cov=app       # with coverage
ruff check .
black --check .
mypy app
pre-commit install     # run the same gates on every commit
```

The suite injects fake infrastructure, so no Redis, OpenAI or Meta credentials are required to run it. Tests that need a database skip unless `TEST_DATABASE_URL` (or `DATABASE_URL`) points at PostgreSQL with `pgvector`; `docker compose up -d postgres` is enough to make them run — and they are worth running, since the isolation and retrieval guarantees are only meaningful against a real database.

The media suites want an object store the same way: they skip unless `TEST_S3_ENDPOINT_URL`, `TEST_S3_BUCKET`, `TEST_S3_ACCESS_KEY_ID` and `TEST_S3_SECRET_ACCESS_KEY` point at one, and `docker compose --profile objectstore up -d minio` is enough. Run them: what they prove is that the SigV4 signing in `app/core/object_store.py` satisfies a real store, and mocking an SDK call cannot establish that — a signature is either accepted or it is a 403. CI runs them against MinIO and fails if it finds them skipped.

Those tests build the schema **once per session** and roll each test back afterwards, so the whole suite takes about a minute and a half rather than the forty it took when every test dropped and recreated the schema. `tests/integration/test_fixture_isolation.py` covers the isolation itself, including that a test which calls `commit()` still cannot leak into the next one.

Install with `pip install -e ".[dev]"` rather than installing the tools yourself: Ruff, Black and MyPy are pinned to exact versions (ADR-016), and a different version will report findings CI does not, or miss ones it does. Those pins are kept honest by a weekly `pip-audit` run in the security workflow — run `pip-audit` locally before changing any dependency bound.

### Migrations

```bash
alembic upgrade head                                   # apply
alembic downgrade -1                                   # revert one revision
alembic revision --autogenerate -m "add tenant model"  # generate from model changes
alembic check                                          # fail if models drift from migrations
```

### Configuration

Every setting is environment-driven and documented in [.env.example](.env.example). In `production` the application refuses to start with a placeholder or short `JWT_SECRET`, or with `DEBUG` enabled.

## Engineering rules

- [`claude.md`](claude.md) is the authoritative source for engineering, architecture, stack, security, SaaS, multi-tenancy, AI, WhatsApp, testing, Git, and development rules.
- [`Documentation_Protocol.md`](Documentation_Protocol.md) is the authoritative source for how documentation and project memory are maintained.

Documentation updates are part of every feature. Documentation drift is treated as a bug.
