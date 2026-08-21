# Wasla

**AI employees for WhatsApp.** Wasla is a multi-tenant, AI-powered customer engagement platform built around the WhatsApp Business Cloud API. Businesses connect their WhatsApp numbers, configure specialised AI Agents, manage conversations, capture and qualify leads, answer questions from their own knowledge base (RAG), run campaigns, hand off to human agents, and monitor usage and analytics.

Wasla is not a chatbot demo. It is designed from day one as a scalable, secure, production-ready SaaS platform.

## Project status

This repository is at **Phase 0 — Foundation**. The table below reflects the actual state of the code, not the roadmap.

| Area | Status |
| --- | --- |
| Engineering rules (`claude.md`) | Implemented |
| Documentation protocol (`Documentation_Protocol.md`) | Implemented |
| Project memory (`README` / `ARCHITECTURE` / `TASKS` / `DECISIONS`, `docs/`) | Implemented |
| Application foundation (FastAPI, config, logging, DB, Redis, Docker, CI) | In Progress |
| Multi-tenancy, auth, WhatsApp, AI agents, RAG, CRM, billing | Planned |

Status vocabulary used across all documentation: **Implemented**, **In Progress**, **Planned**, **Blocked**.

See [TASKS.md](TASKS.md) for the phase-by-phase roadmap and the current status of every task.

## Planned technology stack

- **Runtime:** Python 3.12+, FastAPI, Uvicorn
- **Data:** PostgreSQL, SQLAlchemy 2.0 (async), Alembic, pgvector
- **Cache / queues:** Redis
- **Config / validation:** Pydantic v2, Pydantic Settings
- **AI:** OpenAI Responses API, OpenAI embeddings
- **Messaging:** WhatsApp Business Cloud API (Meta Graph API) via httpx
- **Quality:** Ruff, Black, MyPy, pytest, pytest-asyncio
- **Delivery:** Docker, Docker Compose, Nginx, GitHub Actions

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

**Status: In Progress.** Setup, environment variables, database, migrations, Docker, workers, and test instructions are added to this section as the foundation lands in Phase 0. Nothing runnable is committed yet, so no commands are documented here.

## Engineering rules

- [`claude.md`](claude.md) is the authoritative source for engineering, architecture, stack, security, SaaS, multi-tenancy, AI, WhatsApp, testing, Git, and development rules.
- [`Documentation_Protocol.md`](Documentation_Protocol.md) is the authoritative source for how documentation and project memory are maintained.

Documentation updates are part of every feature. Documentation drift is treated as a bug.
