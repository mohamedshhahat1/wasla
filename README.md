# Wasla

**AI employees for WhatsApp.** Wasla is a multi-tenant, AI-powered customer engagement platform built around the WhatsApp Business Cloud API. Businesses connect their WhatsApp numbers, configure specialised AI Agents, manage conversations, capture and qualify leads, answer questions from their own knowledge base (RAG), run campaigns, hand off to human agents, and monitor usage and analytics.

Wasla is not a chatbot demo. It is designed from day one as a scalable, secure, production-ready SaaS platform.

## Project status

**Phase 0 — Foundation is complete.** Phase 1 (database and tenancy foundation) is next. The table below reflects the actual state of the code, not the roadmap.

| Area | Status |
| --- | --- |
| Engineering rules (`claude.md`) | Implemented |
| Documentation protocol (`Documentation_Protocol.md`) | Implemented |
| Project memory (`README` / `ARCHITECTURE` / `TASKS` / `DECISIONS`, `docs/`) | Implemented |
| Application foundation (FastAPI, config, logging, errors, DB, Redis, health, Docker, CI) | Implemented |
| Domain models and migrations beyond extension enablement | Planned |
| Multi-tenancy, auth, WhatsApp, AI agents, RAG, CRM, billing | Planned |
| Background workers and deployment automation | Planned |

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

Planned for later phases:

- **AI:** OpenAI Responses API, OpenAI embeddings
- **Messaging:** WhatsApp Business Cloud API (Meta Graph API) via httpx

## Documentation map

| Document | Scope |
| --- | --- |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Technical source of truth for system architecture |
| [TASKS.md](TASKS.md) | Implementation roadmap and current task status |
| [DECISIONS.md](DECISIONS.md) | Architecture decision records (ADRs) |
| [docs/PRODUCT.md](docs/PRODUCT.md) | Product scope, personas, customer journey |
| [docs/SAAS.md](docs/SAAS.md) | Multi-tenancy, workspaces, platform owner, plans |
| [docs/AUTH.md](docs/AUTH.md) | Authentication, memberships, RBAC |
| [docs/WHATSAPP.md](docs/WHATSAPP.md) | WhatsApp Cloud API integration and webhooks |
| [docs/AI_AGENTS.md](docs/AI_AGENTS.md) | AI agent configuration, orchestration, tools |
| [docs/RAG.md](docs/RAG.md) | Knowledge base, ingestion, tenant-scoped retrieval |
| [docs/CRM.md](docs/CRM.md) | Contacts, leads, follow-ups, human handoff |
| [docs/BILLING.md](docs/BILLING.md) | Plans, subscriptions, entitlements, invoicing |
| [docs/ANALYTICS.md](docs/ANALYTICS.md) | Analytics events, usage tracking, dashboards |
| [docs/API.md](docs/API.md) | API conventions and endpoint catalogue |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | Docker, Nginx, CI/CD, production deployment |
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

The suite injects fake infrastructure, so no PostgreSQL, Redis, OpenAI or Meta credentials are required to run it.

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
