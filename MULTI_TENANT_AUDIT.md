# Wasla — Multi-Tenant Isolation Audit

**Audit date:** 2026-08-23
**Audited tree:** `7fa9b0e` (`worktree-security-audit`, Phase 15)
**Scope:** every model, repository, service, route, dependency, worker, background job and platform endpoint
**Method:** read-only inspection of the whole repository, followed by a new adversarial test suite executed against real PostgreSQL rows over HTTP

This audit answers one question, and only that question:

> **Can a user belonging to Workspace A ever read, modify, delete, trigger, or indirectly influence data belonging to Workspace B?**

It is deliberately narrower than [SECURITY_AUDIT.md](SECURITY_AUDIT.md), which covers authentication, transport, deployment and dependency risk. Where a finding here overlaps one recorded there, the earlier identifier is cited rather than renamed.

---

## A. The answer

**Not through the HTTP API.** Across 95 operations on 78 paths — 51 of which take an identifier in the path — no route was found that reaches another workspace's rows, and a 52-operation adversarial matrix confirms it dynamically rather than by reading alone.

**Yes, through one non-API path.** A WhatsApp number can be claimed without proving ownership of it, and that claim is the authority that maps inbound customer messages to a workspace. This is a genuine cross-tenant capture path that no repository filter can catch, because by the time the filter runs the row already says the traffic belongs to the attacker. It is recorded as **M-01** below and as W-02 in the earlier audit.

Two further weaknesses affect the tenancy model without themselves crossing it: access cannot be withdrawn once granted (**M-02**), and a queue that carries tenant authority sits on an unauthenticated Redis (**M-03**).

| Layer | Verdict |
|---|---|
| Database schema | **Sound.** 27 of 30 tables carry `tenant_id`; the three that do not are global by design |
| Repositories | **Sound.** The tenant predicate is applied in one place and cannot be omitted by accident |
| Services | **Sound.** Every service is constructed with a tenant id from the token; none accepts one as an argument |
| Routes | **Sound.** No route reads a tenant identifier from path, query or body |
| Client-supplied identifiers | **Sound.** Every foreign-key reference in a request body is re-resolved through a scoped repository |
| Workers and queues | **Sound in application flow.** Tenant is taken from the claimed row or used to build a scoped repository |
| Vector retrieval | **Sound.** Doubly filtered — on the chunk and on its document |
| Platform routes | **Sound.** Guarded by a user-level role that no membership can confer |
| WhatsApp number ownership | **Broken.** See M-01 |
| Membership lifecycle | **Incomplete.** See M-02 |
| Queue transport | **Unauthenticated.** See M-03 |

### What "verified" means here

Claims below are marked **[dynamic]** where a test executed against PostgreSQL proves them, and **[static]** where they rest on reading the code. Every **[dynamic]** claim corresponds to a test in `tests/integration/test_tenant_isolation.py`, which passes in full (10 tests, 0 failures).

---

## B. Inventory of tenant-scoped entities

30 mapped tables. **27 carry `tenant_id`:**

`agent_tools`, `agents`, `analytics_events`, `audit_logs`, `campaign_recipients`, `campaigns`, `contacts`, `conversations`, `document_chunks`, `documents`, `follow_ups`, `invoices`, `knowledge_bases`, `lead_activities`, `lead_notes`, `leads`, `memberships`, `message_media`, `message_sentiments`, `messages`, `payments`, `subscriptions`, `tenant_invitations`, `usage_events`, `whatsapp_accounts`, `whatsapp_events`, `whatsapp_templates`

**Three do not, each correctly:**

| Table | Why it has no `tenant_id` |
|---|---|
| `tenants` | It *is* the tenant |
| `users` | A global identity. A person may belong to several workspaces; what they may do in one is decided by `memberships`, never by this table (claude.md §7) |
| `plans` | A catalogue. A plan belongs to nobody, and pricing is not workspace data |

`audit_logs.tenant_id` is **nullable** by design: a platform-level action belongs to no workspace, and forcing one would either invent an attribution or lose the entry. Workspace-scoped reads filter on it, so a null-tenant entry is invisible to every workspace. Verified in the listing test.

Three schema-level observations worth recording:

- **`TenantScopedMixin` contributes the column, the foreign key and the index**, but a subclass declaring its own `__table_args__` *replaces* the mixin's rather than extending it — silently dropping the tenant index. The models that do this restate the index by hand, and `tests/unit/test_whatsapp_models.py` asserts that every table with a `tenant_id` column has its index. The trap is real and already guarded.
- **`whatsapp_accounts.phone_number_id` is unique platform-wide, not per workspace.** This is load-bearing: it is what makes inbound tenant resolution unambiguous. It is also what makes M-01 a squatting vector.
- **`whatsapp_events` keys idempotency on `UNIQUE(tenant_id, event_id)`.** Scoping it by tenant is correct — two workspaces must not collide on Meta's identifiers — though see W-11 in the earlier audit for the check-then-insert race, which is a correctness issue rather than an isolation one.

---

## C. How isolation is enforced

Four gates, each of which would have to fail independently.

### 1. The tenant is read from the token, never from request input

`app/api/dependencies.py` opens with the invariant and the code keeps it:

> The tenant a request acts on is read from the signed access token, never from a path, query or body parameter. There is therefore no request field a caller could forge to aim a route at another workspace's data.

No route takes a `tenant_id`. Confirmed by inspection of all 95 operations.

### 2. The claim is re-verified against the database on every request

`get_active_workspace` does not trust the token's `tenant_id`. It re-reads the membership through a scoped repository and re-reads the tenant's status:

```python
memberships = MembershipRepository(session, tenant_id=tenant_id)
membership = await memberships.require_for_user(current_user.user.id)
tenant = await TenantRepository(session).get_by_id(tenant_id)
if tenant is None or not tenant.is_active:
    raise PermissionDeniedError("This workspace is not available.")
```

A token naming a workspace the user never joined is refused — **[dynamic]**, including for a token signed with the real key. Suspension takes effect on the next request rather than at token expiry — **[dynamic]**.

Token *issuance* is equally careful: `AuthService._resolve_workspace` matches a client-supplied `workspace_slug` against the user's **own** membership list, so a slug naming someone else's workspace raises `TenantIsolationError` rather than minting a token for it. Refresh tokens carry no tenant at all, so a refresh re-resolves rather than replaying a stale claim.

### 3. The tenant predicate lives in exactly one place

`TenantScopedRepository` supplies the tenant id at construction, exposes it read-only, and applies it in `_select()`:

```python
def _select(self) -> Select[tuple[ModelT]]:
    return select(self.model).where(self._tenant_filter())
```

`_tenant_filter()` is an `@abstractmethod`. A subclass that forgets it **cannot be instantiated** — the failure is a `TypeError` at construction, not a silently unfiltered query. This is the single most valuable decision in the codebase's isolation design.

`_require()` raises `TenantIsolationError` on a miss, which answers **404 with the generic not-found message**. Whether the row exists in another workspace is deliberately not checked, because that answer is itself information.

### 4. Services are constructed with a tenant, and never accept one

Every workspace-scoped service is built in a dependency provider from `workspace.tenant.id`:

```python
def get_lead_service(session: SessionDep, workspace: ActiveWorkspaceDep) -> LeadService:
    """Workspace-scoped, so no route can pass a tenant id of its own choosing."""
    return LeadService(session=session, tenant_id=workspace.tenant.id)
```

No service method takes a `tenant_id` parameter. A route therefore has no way to express the wrong workspace even by mistake.

---

## D. Endpoint verification

All 51 identifier-taking operations resolve their target through a workspace-scoped service, which resolves through a scoped repository. Spot-checked in full and then proven behaviourally.

**[dynamic]** The adversarial matrix runs 52 operations — every `{id}` route plus the body-referencing creates — as a **`TENANT_OWNER` of a different workspace**, holding a genuine signed token and the victim's exact UUIDs. Every one answers **404**. No response body contains the victim's planted canary string.

Coverage by domain: conversations, messages, media, conversation analytics, contacts and opt-out, leads with notes/activity/status/score/assignment, agents and tool grants, knowledge bases and documents, campaigns with audience/schedule/pause/cancel/statistics/recipients, templates and sync, follow-ups, WhatsApp numbers, invitations, invoices and payments.

Three properties beyond "it returns 404":

- **Listings leak by inclusion, not by lookup** — a missing predicate on a collection route produces a longer list rather than an error, so the 11 collection endpoints are asserted separately. None returns another workspace's rows, ids, or tenant id. **[dynamic]**
- **Nested routes are checked in both directions.** `GET /conversations/{id}/media/{id}` is the shape where a handler scopes the parent and trusts the child. Every parent/child crossing is refused; the handler compares `media.conversation_id` after a scoped lookup, and answers not-found rather than naming the mismatch. **[dynamic]**
- **There is no 403/404 oracle.** A route answering 404 for a random UUID and 403 for a real foreign one would confirm the real one exists. Seven routes are asserted to return an identical status **and identical message** for both. **[dynamic]**

The controls matter as much as the refusals: a suite that only asserts 404 passes equally well against an endpoint broken for everyone. Sixteen routes are therefore also fetched successfully by their owner, and the platform 403s are paired with a test granting the platform role and observing 200. **[dynamic]**

---

## E. The unscoped repositories

Thirteen classes extend `BaseRepository` rather than `TenantScopedRepository`. Each was examined; each is deliberate, and most are documented in place.

| Class | Reads across tenants because | Guard |
|---|---|---|
| `TenantRepository` | It is how a tenant is found at all | Callers must check membership; `get_active_workspace` does |
| `UserRepository` | Identity precedes any workspace | Authorization comes from `memberships`, never this table |
| `PlanRepository` | A plan belongs to nobody | Read-only catalogue |
| `UserMembershipRepository` | Answers "which workspaces may this person open?" | Always keyed by the authenticated user's own id |
| `InvitationTokenRepository` | Acceptance happens before any workspace is known | Matches on an unguessable token hash only; contains exactly one query |
| `WhatsAppAccountDirectory` | Inbound webhooks must resolve a tenant from `phone_number_id` | The deliberate resolution point — and the reason M-01 matters |
| `DueCampaignClaim`, `DueFollowUpClaim` | Workers sweep due work across all workspaces | Tenant is taken **from the claimed row**, which is the safest available pattern |
| `PlatformAuditLogRepository`, `PlatformSubscriptionRepository`, `PlatformInvoiceRepository`, `PlatformUsageRepository` | The platform view | `PlatformStaffDep`, a role on the user that no membership can confer |
| `ConversationMediaGate` | Serialises concurrent media jobs on one conversation | Takes a row lock and returns nothing — see M-04 |

`TenantMetricsRepository` is not a repository subclass at all: it spans several models, so it applies the predicate to each query itself. Every one of its ten queries filters the driving table on `self._tenant_id`, and every join is on a primary key, so no join widens the result. The tenant id is fixed at construction and no method accepts one.

---

## F. Client-supplied identifiers

The classic cross-tenant injection is not the path parameter — it is the UUID in a request body, where a tenant filter on the *object being modified* cannot help. Every such field was traced.

| Field | Route | Validation |
|---|---|---|
| `assigned_to_id` | conversation + lead assignment, lead create | `MembershipRepository.require_for_user` — scoped, so an outsider 404s |
| `contact_id` | lead create, opt-out | `ContactRepository.require_by_id` — scoped |
| `conversation_id` | lead create, follow-up create | `ConversationRepository.require_by_id` — scoped |
| `lead_id` | follow-up create | Scoped lookup |
| `account_id` | campaign create, audience preview, template sync | `WhatsAppAccountRepository.require_by_id` — scoped |
| `template_id` | campaign create | Scoped, **and** cross-checked: `template.account_id != account_id` is rejected |
| `knowledge_base_id` | document submit, search | Scoped |
| `contact_ids[]` | campaign audience | **Intersection only** — see below |

Two of these deserve their reasoning recorded.

**Campaign audiences cannot be poisoned by id.** `AudienceRepository` builds the eligible population from `self._select()` — already tenant-filtered — joined to conversations on the sending number, excluding opt-outs. A caller's `contact_ids` is applied as `Contact.id.in_(...)`, an *intersection filter over that population*, never a source of rows. Supplying a thousand of another workspace's contact ids therefore selects nothing. The opt-out predicate is part of the base population rather than an option, so no filter combination turns it off.

**Assignment to an outsider is refused.** This is the one case where the object legitimately belongs to the caller and only the body identifier is foreign. Both conversation and lead assignment re-resolve the assignee through the scoped membership repository. **[dynamic]** — asserted for conversation assignment, lead assignment, and lead creation.

---

## G. Workers, jobs and Redis

Background work is where tenant context is most easily lost, because there is no request and no token.

**Jobs carry `tenant_id` explicitly.** `AgentJob` and `MediaJob` both encode it, and both reject a payload missing or malforming it (`MalformedJobError`, dead-lettered rather than retried).

**The tenant is then used to build scoped repositories, not to bypass them.** `AgentWorker._handle` constructs `EntitlementService`, `AgentRepository`, `SentimentService`, `AgentOrchestrator`, `UsageRecorder` and `MessagingService` all with `job.tenant_id`. A `conversation_id` belonging to another workspace therefore fails at `ConversationRepository.require_by_id`, and a foreign `agent_id` returns `None` and falls back to the workspace default — logged, and explicitly commented as covering "retired, or from another workspace".

**Sweep workers take the tenant from the row.** `campaign.tenant_id`, `follow_up.tenant_id`, `subscription.tenant_id`. There is no job payload to forge because there is no payload.

**Tools inherit the orchestrator's tenant.** `ToolContext.tenant_id` comes from `self._tenant_id`, and every tool builds its service from it. The model chooses tool *arguments*, never the tenant.

**Webhook ingestion never infers a tenant from the customer.** `WhatsAppIngestionService` resolves `phone_number_id → WhatsAppAccount → account.tenant_id` and caches its repositories keyed by tenant id. The customer's own number is never consulted, exactly as claude.md §15 requires.

**Redis keys.** Queue namespaces (`agent:jobs:*`, media, ingestion) are **global, not per tenant** — a single ordered work list per queue, which is the correct design for a shared worker pool, and safe because the tenant travels inside the payload and is validated against the database on use. Rate-limit keys are namespaced by policy and by identity (client address, or `str(tenant.id)` for workspace policies), so no workspace consumes another's budget. Refresh-token revocation is keyed by token id, which is tenant-independent by nature. **No cache holds tenant-owned rows**, so there is no cache key that could serve one workspace's data to another.

The residual risk here is transport, not design: see **M-03**.

---

## H. Findings

### M-01 — A WhatsApp number can be claimed without proving ownership · **High**

**Location:** `app/api/v1/whatsapp.py`, `app/services/whatsapp_account_service.py`, `app/repositories/whatsapp_repository.py`
**Cross-tenant:** **Yes — inbound message capture**
**Also recorded as:** W-02 in [SECURITY_AUDIT.md](SECURITY_AUDIT.md)

`POST /whatsapp/accounts` accepts `phone_number_id`, `waba_id` and `display_phone_number` as free-form strings from any `TENANT_ADMIN` and stores them after a uniqueness check only. Nothing asks Meta whether the calling workspace controls that number.

That row is then the tenant-resolution authority for every inbound message. This is the one path where isolation cannot be enforced downstream: every repository filter runs *after* the row has already asserted which workspace the traffic belongs to. A `phone_number_id` is not a secret — it is visible to anyone who has integrated with that business.

Consequences, in descending order of severity: **capture** (where the platform's Meta app is subscribed to the number's WABA and the legitimate business has not yet connected it, inbound conversations resolve to the attacker, and the attacker's agent replies to the victim's customers), **squatting** (the platform-wide unique constraint permanently blocks the real owner), and **enumeration** (the `ConflictError` discloses whether a number is already onboarded).

**Fix.** Verify the claim against Meta before persisting — `GET /{phone_number_id}` with the supplied token, requiring the returned id to match and the number to belong to the declared `waba_id`. Better, onboard only through Embedded Signup, where the token exchange proves control. Failing either, add a `verification_status` column and refuse to route inbound traffic to an unverified account. Audit and rate-limit connection attempts.

**Not covered by the new suite**, deliberately: the defect is that the row is created *legitimately*, so there is no cross-tenant request to make. A regression test belongs with the fix and should assert that an unverifiable number is refused.

### M-02 — Membership can be granted but never withdrawn · **High**

**Location:** `app/db/models/membership.py`, `app/repositories/membership_repository.py`, absence of a `/members` router
**Cross-tenant:** No — but it makes the tenancy model one-way
**Also recorded as:** W-03a

`Membership` has no `status` column. `MembershipRepository` exposes `get_for_user`, `require_for_user`, `list_members` and `add_member` — no remove, no suspend, no role change — and `list_members()` is not reachable from any endpoint.

`get_active_workspace` deliberately re-reads the membership on every request so that withdrawing access takes effect at once. Nothing can perform the withdrawal. The available responses are direct SQL, or `users.is_active = false`, which is also unexposed and evicts the person from *every* workspace — unacceptable for a contractor working across several.

**Fix.** Add `memberships.status` with a migration, filter it in `MembershipRepository._select()` so a suspension propagates everywhere at once, add `GET/PATCH/DELETE /api/v1/members` behind `TenantAdminDep`, forbid removing or demoting the last `TENANT_OWNER`, and audit every change.

### M-03 — Redis is unauthenticated, and jobs carry tenant authority · **High**

**Location:** `docker-compose.yml`, `docker-compose.prod.yml`, `app/workers/queue.py`
**Cross-tenant:** Yes, for anyone who can reach Redis
**Also recorded as:** W-05

A job payload names its own `tenant_id`. The worker validates that the `conversation_id` belongs to it, so a *mismatched* pair is refused — but a payload naming a tenant and one of that tenant's own conversations is indistinguishable from a real one. Anyone able to write to Redis can therefore make an agent answer any conversation on the platform.

The application-side check is sound; the exposure is that nothing authenticates the transport.

**Fix.** `requirepass` or a Redis ACL, network isolation, and — if jobs must cross a trust boundary — a MAC over the payload.

### M-04 — `ConversationMediaGate.lock()` takes an unscoped row lock · **Low**

**Location:** `app/repositories/media_repository.py`

```python
async def lock(self, conversation_id: uuid.UUID) -> None:
    await self._session.execute(
        select(Conversation.id).where(Conversation.id == conversation_id).with_for_update()
    )
```

No tenant predicate. It leaks nothing — it selects only `id`, returns nothing, and the docstring is explicit that the ordering rather than the row is the point. The reachable input is `media.conversation_id`, read moments earlier through a scoped repository, so in application flow the id is always in-tenant.

It is recorded because it is the one place a conversation is addressed by id with no tenant filter, and because it would become a cross-tenant row lock the moment a caller passed an id from elsewhere. Adding `Conversation.tenant_id == self._tenant_id` costs nothing and removes the question.

### M-05 — `POST /invitations/accept` requires authentication and cannot be reached · **Medium**

**Also recorded as:** W-03b. An availability defect rather than an isolation one, but it is the *entrance* to the membership lifecycle whose exit M-02 describes: together, workspace membership has neither. The router-level `_WORKSPACE_LIMIT` guard resolves `ActiveWorkspaceDep` for every route on the invitations router, including the one documented as unauthenticated on purpose. The existing test passes only because it requests a fixture that overrides `get_active_workspace`.

### Informational — hand-written tenant predicates

Three places apply the tenant filter by hand rather than through `_select()`: `DocumentChunkRepository.search()` (which filters both the chunk and its document — correct, and the stricter of the two), `DocumentChunkRepository.count_for_document` / `clear_for_document`, and every query in `TenantMetricsRepository`. All are correct today. They are noted because the guarantee elsewhere is structural — a forgotten filter is a `TypeError` — whereas here it is a matter of remembering, and the failure mode is a silently widened result rather than an error.

---

## I. What this audit did not verify

Stated plainly, so the coverage is not overread.

- **M-01 is unproven in either direction.** Confirming capture requires a Meta app subscribed to a WABA the platform does not control. The squatting and enumeration consequences follow from the schema and are certain; the capture consequence depends on subscription state.
- **No concurrency testing.** The adversarial suite is sequential. Check-then-insert races (W-11) are correctness defects that could plausibly produce a duplicate or a 500, but no scenario was found in which either crosses a tenant boundary.
- **The worker path is verified by reading, not by executing.** The new suite drives HTTP. Worker isolation rests on static tracing plus the existing worker tests, which use fakes.
- **Soft deletion is not filtered in `UserRepository`.** A soft-deleted user's `deleted_at` is not consulted at authentication; `is_active` is. Recorded in the earlier audit; not a cross-tenant issue.
- **The database enforces no row-level security.** Isolation is entirely an application property. This is a reasonable choice for a single-schema multi-tenant design, but it means a raw SQL path — a migration, a console, a future reporting job — is outside every guarantee in this document.

---

## J. Remediation order

1. **M-01** — verify number ownership against Meta before persisting, or gate inbound routing on a verification status. This is the only finding here that crosses a tenant boundary through normal use.
2. **M-03** — authenticate Redis. Configuration, not code.
3. **M-02** — `memberships.status`, the member-management API, last-owner protection, audit entries.
4. **M-05** — move `invitations.router` out of `WORKSPACE_ROUTERS` and apply limits per route; fix the neutralised test.
5. **M-04** — add the tenant predicate to the media gate lock.

---

## K. The regression suite

`tests/integration/test_tenant_isolation.py` — 10 tests, all passing against PostgreSQL 16.

It exists because the coverage that looked like isolation testing was not. Endpoint tests override the service dependency with stubs, which pins routing, shapes and roles but proves nothing about isolation — a stub returns what it was told to regardless of who asked. `test_authorization.py` proves the filter works when a scoped repository is constructed correctly, but cannot prove that every route constructs one. The gap was between them: no test drove a real request from one workspace against another's real rows.

Three choices keep a pass meaningful rather than accidental:

- **The attacker is a `TENANT_OWNER`,** so a refusal can never be attributed to a role check that would have refused anybody.
- **Entitlements are stubbed permissive,** so a plan limit answering 403 cannot be mistaken for isolation while the row remains reachable.
- **Refusals are asserted as 404, never 403,** so the suite fails if a future handler starts distinguishing "not yours" from "does not exist".

What it will catch: a new route that forgets its scoped service; a repository that loses its predicate; a handler that answers 403 and opens an oracle; a listing that widens; a body identifier accepted without re-resolution; a nested route that trusts its child id.

What it will not catch: M-01, which needs no cross-tenant request; anything in the worker path; and anything reaching the database other than through a route.

---

## Resolution status — updated 2026-08-23 (`worktree-security-audit`)

This report is a record of what was true when it was written and is not rewritten
in place. What has since changed:

| Finding | Status | Where |
|---|---|---|
| **M-01 / W-02** — a WhatsApp number could be claimed with no proof of ownership | **Closed** | ADR-037. The connect request carries a Meta access token; the claim is verified against the Graph API for that exact `phone_number_id` before anything is written, and the business account, display number and verified name come from Meta's answer rather than the request. The platform credential is deliberately not a route to it. `tests/unit/test_number_ownership.py`, `tests/integration/test_whatsapp_ownership.py` |
| **M-02 / W-03a** — no member removal, suspension or role change | **Closed for removal and readmission** | ADR-038. `memberships.status`, enforced in `get_active_workspace`. Role *change* on an existing membership is still only expressible through remove-and-reinstate. `tests/integration/test_membership_revocation.py` |
| **W-05** — Redis unauthenticated | **Closed for the production stack** | `REDIS_PASSWORD` is required by `docker-compose.prod.yml`, and the healthcheck authenticates. Job signing remains unbuilt and unneeded while the transport is authenticated |
| **W-10** — refresh-token reuse detected but the family not revoked | **Closed** | ADR-039. Spending is a single atomic `SET NX`; losing that race raises `users.token_version` and audits it, committed before the refusal is raised. `tests/integration/test_refresh_reuse.py` |
| GitHub Actions pinned by mutable tag | **Closed** | Every `uses:` is pinned to a commit SHA with the tag kept as a trailing comment |
| `JWT_ALGORITHM` unvalidated | **Closed** | Constrained to `{HS256, HS384, HS512}` at startup, so `none` and the asymmetric families cannot be configured. `tests/unit/test_config.py` |
| **W-12** — registration discloses whether an address or slug is taken | Open | Deliberate: the alternative is an unhelpful signup flow, and the disclosure is bounded by the client-address limit |
| **W-14** — the limiter fails open on a Redis error | Open, deliberate | ADR-032 |

Found while closing the above, and fixed here rather than deferred:

- **Revoked memberships and released numbers still consumed plan capacity.**
  `TEAM_MEMBERS` counted every membership row, so removing a colleague on a
  two-seat plan would have consumed the seat permanently. `WHATSAPP_NUMBERS`
  excluded `disabled` but not `released`.
- **`TemplateService.sync` read a workspace's templates with the platform
  credential**, against a `waba_id` the workspace had typed in. Both halves are
  fixed: the id is now Meta's own answer, and the sync resolves the credential
  the same way a send does.
- **The review's own dependency-graph walker read the wrong tree.** Run as a
  script file from a scratch directory, `import app` resolved to the installed
  package in the main checkout rather than to the worktree under review. It
  reported 98 operations against a tree containing 106, and every route added in
  this change was silently absent. Re-run with `PYTHONPATH` pinned and
  cross-checked against `app.openapi()`.

The full current position is in [docs/AUTHORIZATION.md](docs/AUTHORIZATION.md) §6
and §7.
