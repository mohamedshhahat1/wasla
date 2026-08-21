# Wasla Project Memory & Documentation Protocol

This is a permanent project rule.

You must maintain the project's documentation and project-memory files throughout the entire development lifecycle.

The following files are mandatory:

- README.md
- ARCHITECTURE.md
- TASKS.md
- DECISIONS.md

These files must always reflect the current state of the project.

==================================================
1. INITIAL SETUP
==================================================

If any of the following files do not exist:

- README.md
- ARCHITECTURE.md
- TASKS.md
- DECISIONS.md

Create them.

Before creating them, inspect the entire existing repository and Git history so the files describe the actual current state rather than an imagined project.

Do not create duplicate documentation files with similar purposes.

==================================================
2. README.md
==================================================

README.md is the primary public project documentation.

Keep it updated whenever changes affect:

- Project purpose
- Features
- Architecture overview
- Installation
- Environment variables
- Running locally
- Docker
- Database setup
- Migrations
- Workers
- Testing
- API usage
- WhatsApp configuration
- OpenAI configuration
- RAG
- Production deployment
- CI/CD
- Security
- Troubleshooting

Do NOT rewrite README.md unnecessarily.

Only update the sections affected by the change.

Keep it concise and accurate.

Never document functionality that does not actually exist.

==================================================
STEP 2.5 — DOCUMENTATION STRUCTURE
==================================================

Create a dedicated docs/ directory for detailed project documentation.

Create:

docs/
├── PRODUCT.md
├── SAAS.md
├── AUTH.md
├── WHATSAPP.md
├── AI_AGENTS.md
├── RAG.md
├── CRM.md
├── BILLING.md
├── ANALYTICS.md
├── API.md
├── DEPLOYMENT.md
└── SECURITY.md

These files are documentation foundations.

Do not document functionality as implemented unless it actually exists.

For future/unimplemented features, clearly label the documentation as:

Status: Planned

The documentation must remain synchronized with the implementation.

Do not duplicate the entire contents of ARCHITECTURE.md or README.md inside docs/.

Each document should have a clear scope and be updated when its corresponding subsystem changes.

==================================================
3. ARCHITECTURE.md
==================================================

ARCHITECTURE.md is the technical source of truth for the current system architecture.

Update it whenever an architectural change occurs.

Examples:

- New service
- New module
- New integration
- New database subsystem
- New worker
- New queue
- New external provider
- New authentication strategy
- New authorization layer
- New tenancy strategy
- New AI architecture
- New RAG architecture
- New billing architecture
- New deployment architecture
- New data flow
- New background processing flow

Document:

- System overview
- Project structure
- Application layers
- Request flow
- WhatsApp webhook flow
- AI Agent flow
- RAG flow
- Human handoff flow
- CRM/Lead flow
- Background jobs
- Redis usage
- Database architecture
- Multi-tenancy
- SaaS Owner architecture
- Authentication
- Authorization
- Billing
- Usage tracking
- CI/CD
- Production deployment

Use diagrams where useful.

For example:

WhatsApp
    ↓
Webhook
    ↓
Tenant Resolver
    ↓
Message Service
    ↓
Redis Queue
    ↓
Agent Orchestrator
    ├── Memory
    ├── RAG
    ├── Tools
    └── OpenAI Responses API
    ↓
WhatsApp Client

Keep architecture documentation aligned with the actual implementation.

==================================================
4. TASKS.md
==================================================

TASKS.md is the implementation roadmap and current project state.

Every task must have a status.

Use:

- [ ] Not started
- [~] In progress
- [x] Completed
- [!] Blocked

Organize tasks by phases/features.

Example:

# Wasla Roadmap

## Phase 0 — Foundation

- [x] Project structure
- [x] Docker
- [x] PostgreSQL
- [x] Redis
- [x] Configuration
- [x] Logging
- [x] CI

## Phase 1 — Multi-Tenancy

- [x] Tenant model
- [x] Tenant isolation
- [x] Tenant repository
- [ ] Tenant onboarding

## Phase 2 — Authentication

- [x] User model
- [x] RBAC
- [ ] Refresh tokens
- [ ] Password reset

## Phase 3 — SaaS Owner

- [x] Platform Owner role
- [x] Tenant listing
- [ ] Tenant suspension
- [ ] Platform usage
- [ ] Platform analytics

Update TASKS.md immediately after completing or changing the status of a task.

If a new feature is discovered during implementation:

1. Add it to TASKS.md.
2. Put it in the correct phase.
3. Mark it appropriately.
4. Implement it only when appropriate for the current roadmap.

Do not silently introduce major features without recording them.

==================================================
5. DECISIONS.md
==================================================

DECISIONS.md contains important architectural decisions.

Do NOT add an entry for trivial implementation details.

Create a decision entry when choosing something that could significantly affect future development.

Examples:

- Multi-tenancy strategy
- Database architecture
- Authentication architecture
- RBAC model
- Queue architecture
- AI provider/API
- RAG/vector database
- Storage strategy
- Billing architecture
- WhatsApp integration strategy
- Background processing architecture
- Deployment strategy
- Caching strategy

Use this format:

# Architecture Decisions

## ADR-001 — Multi-Tenancy Strategy

Date:
YYYY-MM-DD

Status:
Accepted

Decision:
Use shared PostgreSQL infrastructure with tenant_id isolation.

Context:
Wasla is a multi-tenant SaaS platform.

Reason:
Provides a simpler initial operational model while allowing future scaling.

Consequences:
All tenant-owned resources must enforce tenant isolation.

==================================================
6. WHEN TO UPDATE DOCUMENTATION
==================================================

Documentation updates are part of the feature itself.

Whenever you implement a feature:

1. Determine whether README.md is affected.
2. Determine whether ARCHITECTURE.md is affected.
3. Update TASKS.md.
4. Determine whether DECISIONS.md requires a new ADR.

Do not postpone documentation until the end of the project.

Do not wait for me to ask.

==================================================
7. DOCUMENTATION MUST MATCH CODE
==================================================

The documentation must never describe functionality that does not exist.

For example:

If documentation says:

"Wasla supports voice transcription."

Then the implementation must actually support it.

If a feature is planned but not implemented, write:

"Planned"

or keep it in TASKS.md.

Do not represent planned features as completed.

==================================================
8. CODE CHANGE → DOCUMENTATION CHECK
==================================================

For EVERY logical feature, perform this checklist:

[ ] Code implemented
[ ] Tests added/updated
[ ] TASKS.md updated
[ ] README.md reviewed
[ ] ARCHITECTURE.md reviewed
[ ] DECISIONS.md reviewed
[ ] Git diff reviewed

Documentation changes must be included in the same logical Git commit as the feature when they describe that feature.

Example:

feat(platform): add SaaS owner tenant management

The commit may contain:

- platform service
- platform API
- authorization changes
- schemas
- tests
- TASKS.md
- ARCHITECTURE.md
- README.md

Do NOT create separate commits such as:

docs: update tasks

if the documentation belongs directly to the feature being implemented.

==================================================
9. GIT HISTORY
==================================================

Documentation follows the same Logical / Atomic Commit policy as code.

One logical change = one commit.

Do NOT create a commit for every documentation file.

If a feature changes:

- code
- tests
- architecture
- tasks
- README

all related changes should normally be included in the same feature commit.

Example:

feat(ai): add agent orchestration

Not:

feat(ai): add agent service
docs: update architecture
docs: update README
docs: update tasks

unless those documentation changes are genuinely independent.

==================================================
10. SESSION START
==================================================

At the beginning of EVERY new development session:

1. Run:

git status
git log --oneline --decorate -20

2. Read:

README.md
ARCHITECTURE.md
TASKS.md
DECISIONS.md

3. Inspect the current source code relevant to the next task.

4. Determine the current implementation state.

5. Continue from the existing state.

NEVER rebuild the project from scratch.

NEVER assume that previous sessions did not happen.

==================================================
11. SESSION END
==================================================

Before ending a development session:

1. Ensure TASKS.md reflects current progress.
2. Ensure architecture documentation reflects actual implementation.
3. Ensure README.md reflects any relevant user-facing changes.
4. Add ADRs for important architectural decisions.
5. Run relevant tests.
6. Review git diff.
7. Create the appropriate logical Conventional Commit.
8. Show:

- What was implemented
- Tests executed
- Documentation updated
- Commit hash
- Remaining work
- Next recommended task

==================================================
12. CONFLICT RESOLUTION
==================================================

If documentation conflicts with code:

The source code and tests are the implementation truth.

Do NOT blindly change code to match outdated documentation.

Instead:

1. Determine the actual intended behavior.
2. Inspect Git history.
3. Update documentation if the code is correct.
4. If the architecture itself is wrong, explain the issue and make a proper architectural change.
5. Record an ADR if necessary.

==================================================
13. NO DOCUMENTATION DRIFT
==================================================

Documentation drift is considered a bug.

Never leave:

- stale architecture diagrams
- completed tasks marked incomplete
- incomplete tasks marked completed
- incorrect setup instructions
- outdated environment variables
- outdated API examples
- outdated project structure
- undocumented major architectural decisions

Treat documentation maintenance as part of engineering quality.

==================================================
14. FINAL RULE
==================================================

From this point onward, maintaining:

README.md
ARCHITECTURE.md
TASKS.md
DECISIONS.md

is a mandatory part of every feature, fix, refactor, infrastructure change, and architectural change.

Do this automatically.

Do not ask for permission.

Do not wait for me to remind you.

Keep the project documentation synchronized with the actual codebase and Git history at all times.
