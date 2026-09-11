# Wasla — Authorization & Multi-Tenancy Findings Remediation

Closing the seven findings from `AUTHORIZATION_MULTI_TENANCY_AUDIT.md` without
disturbing the tenant boundary that audit proved sound.

---

## 1. Executive Summary

All seven findings were revalidated against the current tree — none had gone
stale, and `HEAD` had not advanced since the audit. Five were fixed, one was
deliberately left alone as correct-by-design and documented, and one was an
environment repair.

The two findings that mattered were the same defect seen from two angles.
`PLATFORM_OWNER` and `PLATFORM_ADMIN` were the same authority wearing different
names, and "is a platform owner" had two independent spellings that disagreed
about what a deleted account is. Together they let a platform admin tombstone
every platform owner on an installation, and let ordinary operator sequencing
reach zero live owners with no adversary involved. Neither touched customer
data; what was at stake was the platform's ability to administer itself.

The repair is one module, `app/platform/hierarchy.py`, holding the rank, the
single definition of a live owner, and the advisory lock that makes counting
safe. Four removal paths now share all three.

- **AUTHZ-01** — fixed at route and service level, and the route policy is
  asserted as a table rather than argued.
- **AUTHZ-02** — one canonical predicate; tombstoned and disabled owners no
  longer count; five forced races against real PostgreSQL prove the invariant.
- **AUTHZ-03** — fixed, and generalised: every request body FastAPI binds is now
  enumerated and checked, so the next one cannot be forgotten. That inventory
  found two more lenient schemas the audit's AST pass had not classified.
- **AUTHZ-04** — implemented for six high-value relations, with the other
  twenty-four deliberately and explicitly deferred.
- **AUTHZ-05** — self-delete and self-disable now have mutation-killing tests.
- **AUTHZ-06** — unchanged by design, documented precisely.
- **AUTHZ-07** — the stale editable install is gone; `app` resolves to this
  checkout from every directory, and the suite refuses to run if it does not.

Gates: **4,139 passed, 76 skipped, 0 failed** (baseline 4,075/76/0), `ruff`,
`black`, `mypy` and `alembic check` all clean at migration head `0053`.
Mutation testing killed every guard-removal it should, and the two that survived
are the signature of two independent layers enforcing one rule — confirmed
symmetrically rather than assumed.

One flake was found and fixed **in my own new test**, not in the code under
test, and it is reported in §16 rather than quietly repaired.

**Verdict: AUTHORIZATION FINDINGS CLOSED WITH EXTERNAL DEPLOYMENT CHECKS.**

---

## 2. Repository State

| | |
|---|---|
| Initial branch | `worktree-billing-google-auth` |
| Initial HEAD | `69db6e4e97000a7b0b9a3d72e1e477ba234b4a24` |
| Final HEAD | `c469e32` plus this report's commit (8 added in total) |
| Migration head | `0053` (was `0052`) |
| Working tree at start | clean but for two untracked audit reports |
| Working tree at end | clean but for the same two untracked audit reports |
| Stashes | none, before or after |
| Python `app` import source | `E:\wasla\app\__init__.py` |

`HEAD` matched the audited revision exactly, so every finding was revalidated
against the code the audit actually read. No commit was amended, reset or
squashed; every commit is additive.

The audited HEAD's own untracked files (`AUTHORIZATION_MULTI_TENANCY_AUDIT.md`,
`FINAL_AUTH_ACCOUNT_SECURITY_AUDIT.md`) were left untracked and unmodified.

---

## 3. Findings Revalidation

| Finding | Still applicable? | Fix | Proof | Status |
|---|---|---|---|---|
| **AUTHZ-01** admin destroys owner | Yes — reproduced | Rank the roles; target-aware route dependency + independent service guard | Live HTTP: admin→disable owner `403`, admin→delete owner `403`, row untouched. Mutations M1, M2a, M2d killed | **Closed** |
| **AUTHZ-02** guard counts tombstones | Yes — reproduced | One canonical `live_platform_role`; advisory lock around count-and-mutate; tombstone clears the role | 5 forced races, real PostgreSQL; mutations M3, M4, M9, M10 killed | **Closed** |
| **AUTHZ-03** three agent schemas accept extras | Yes | `extra="forbid"` on all three, plus a whole-router inventory test | 11 assertions over 53 body models; mutation M7 killed | **Closed** |
| **AUTHZ-04** relations application-enforced only | Yes | Composite FKs on 6 of 30 relations; 24 explicitly deferred | 13 raw-SQL probes, each refusal paired with a control; mutation M8 killed | **Closed (6 done, 24 deferred with reasons)** |
| **AUTHZ-05** no self-protection test | Yes | 4 parametrised tests (2 roles × 2 verbs) asserting status, row state and token version | Mutations M5, M6 killed | **Closed** |
| **AUTHZ-06** in-flight request completes | Yes | **No code change** — correct by design | Existing tests kept; semantics documented in `docs/AUTHORIZATION.md` §6.18 | **Closed as correct** |
| **AUTHZ-07** editable install shadows the tree | Yes — still pointing at `D:\Wasla\wasla` | Uninstalled and reinstalled from `E:\wasla`; collection-time guard in `tests/conftest.py` | `app.__file__` from four directories; guard derived from the conftest's own path | **Closed** |

---

## 4. Platform Role Policy

```
PLATFORM_OWNER > PLATFORM_ADMIN
```

**A `PLATFORM_ADMIN` may not:** disable a platform owner, delete a platform
owner, revoke or grant the `PLATFORM_OWNER` role, or remove the last live owner.

**A `PLATFORM_ADMIN` may:** everything else it could before — read the estate,
settle and void invoices, suspend and restore workspaces, repair ownership, and
disable, enable or delete any account that is not a platform owner.

**A `PLATFORM_OWNER` may** act on any platform account, including another owner,
provided at least one live owner remains afterwards.

**Neither may act on its own account** through the platform API (`422`).
Self-closure is `DELETE /auth/me`, which demands a password first.

**Tenant roles are untouched.** `TENANT_OWNER`, `TENANT_ADMIN` and `MEMBER`
imply no platform authority, and platform authority implies no membership. Both
directions remain tested and were re-verified after the change (§15).

Recorded as **ADR-099**.

---

## 5. AUTHZ-01 Remediation

**Two layers, for different reasons.** `PlatformAccountTargetDep` resolves the
target and applies the hierarchy before the route body; `AccountService.delete`
and `.disable` check again. Not belt-and-braces for its own sake: a dependency
protects the routes it is attached to, and a service inherits nothing from it —
which is precisely how this reached HTTP, with one layer holding the only guard
and the other assuming it existed.

**Why not `PlatformOwnerDep`.** The obvious fix — putting the destructive routes
behind the owner-only dependency that already existed and guarded nothing —
repairs the boundary by deleting the job. Shutting down an abusive or
compromised customer account *now* is ordinary platform administration. The
distinction that matters is not who is calling, it is who is being called about.

**`enable` deliberately keeps plain staff authority.** It is the only account
route that *restores* authority, so it cannot be the path by which an
installation loses its owners — and it is the way back if they are ever
suspended with nobody senior awake.

### Live HTTP results

Real Uvicorn process on a real socket, real Redis, schema built by `alembic
upgrade head`, database state re-read after every call.

| Check | Expected | Actual | DB after |
|---|---|---|---|
| admin disables an owner | 403 | **403** | `active=True deleted=False role=platform_owner` |
| admin deletes an owner | 403 | **403** | `active=True deleted=False role=platform_owner` |
| admin reads the estate | 200 | **200** | — |
| admin disables an ordinary account | 200 | **200** | — |
| admin re-enables it | 200 | **200** | — |
| owner deletes itself | 422 | **422** | untouched |
| owner disables itself | 422 | **422** | untouched |
| admin deletes itself | 422 | **422** | untouched |
| owner deletes an ordinary account | 200 | **200** | `active=False deleted=True role=None` |
| owner deletes an admin | 200 | **200** | `active=False deleted=True role=None` |
| owner deletes another owner (one remains) | 200 | **200** | `active=False deleted=True role=None`; live owners 2 → 1 |
| owner deletes itself as the last owner | 422 | **422** | untouched |
| admin deletes the last owner | 403 | **403** | live owners still 1 |
| platform owner reads `/conversations` | 403 | **403** | — |
| tenant owner reads `/platform/tenants` | 403 | **403** | — |

**15/15.** Live platform owners: 2 at start, 1 at end, never 0.

Note the tombstones read `role=None` — the new semantics, with the role the
account held preserved in the audit entry's `previous_platform_role`.

---

## 6. AUTHZ-02 Remediation

### The canonical definition

```python
def live_platform_role(role: PlatformRole) -> ColumnElement[bool]:
    return (User.platform_role == role) & User.deleted_at.is_(None) & User.is_active.is_(True)
```

One function, in `app/platform/hierarchy.py`, used by every last-owner question:
`PlatformRoleService.owners()`, `.staff()` (via `live_platform_staff`),
`.revoke`, `.grant`, `AccountService.delete`, `AccountService.disable`, and the
route dependency.

**That single definition is the actual repair.** The bug was never a wrong
`WHERE` clause somebody could have caught in review — it was that "is a platform
owner" was spelled out independently in the account lifecycle and in the role
lifecycle, and the two spellings disagreed about what a deleted account is. A
reviewer reading either one in isolation would have found nothing wrong with it.

### Every path that can reach zero owners

| Path | Guarded | Test |
|---|---|---|
| `DELETE /platform/users/{id}` | yes | hierarchy + concurrency |
| `POST /platform/users/{id}/disable` | yes | hierarchy + concurrency |
| `python -m app.platform.roles revoke` | yes | role lifecycle + concurrency |
| `python -m app.platform.roles grant <owner> platform_admin` | yes | role lifecycle + concurrency |

The last is the one worth naming: a *grant* that is really a demotion, under a
subcommand whose name suggests it only ever adds. The invariant follows the
shape of the act, not the subcommand.

### CLI hardening (§12)

`grant` additionally refuses a **deleted** account outright, and a **disabled**
one until it is re-enabled. Writing authority onto a row that can never
authenticate again produces an audit entry saying somebody was made a platform
owner, with no owner at the end of it.

### A note on reachability, stated rather than glossed

**Over HTTP, the last-live-owner guard on `delete` is unreachable
sequentially**, and this is a consequence of the hierarchy rather than a gap. To
act on an owner you must be an owner; if the target is the *last* live owner,
you are the target, and the self-guard refuses first with a different message. A
test that deleted the last owner as itself and asserted `422` would pass with
the last-owner guard deleted entirely.

So the sequential refusal is proved where a second actor genuinely exists — the
operator commands — and the HTTP refusal is proved under concurrency. The test
that covers the HTTP side says this in its docstring, because the next person to
extend it needs to know which guard is answering.

---

## 7. Platform Owner Concurrency

Real PostgreSQL, real committed transactions, two independent connections.
Serialisation is **forced**, not hoped for: a patched `lock_platform_owners`
holds the leading contender inside the critical section while the trailing one
is launched and queues behind the same lock, then releases it. The loser's
answer is therefore determined by the winner's committed write, which is only
possible if the two were ordered.

Both contenders authenticate *before* the race, so the trailing request is one
the server has already admitted — which makes its refusal the guard's, not a
revoked token's.

| Race | Contenders | Expected | Actual | Zero-owner possible? |
|---|---|---|---|---|
| owner delete × owner delete | 2 | one protected | `delete:200` / `delete:422` → 1 live owner | **No** |
| delete × CLI revoke | 2 | invariant preserved | `delete:200` / `revoke:refused` → 1 live owner | **No** |
| disable × CLI revoke | 2 | invariant preserved | `disable:200` / `revoke:refused` → 1 live owner | **No** |
| revoke × revoke | 2 | invariant preserved | `revoke:applied` / `revoke:refused` → 1 live owner | **No** |
| revoke × demote (`grant … admin`) | 2 | invariant preserved | `revoke:applied` / `demote:refused` → 1 live owner | **No** |

In every race **exactly one** removal landed and **exactly one** was refused.
Both halves are asserted: a guard that refused *both* would keep the count at 1
and break the product, so "removed nobody" fails as loudly as "removed both".

The `delete:422` in the first row is the decisive detail — the trailing request
was admitted and then refused by the invariant, not rejected at authentication.

### Locking design

`pg_advisory_xact_lock(0x57415302, 1)` — a singleton, because the protected
resource is "the set of platform owners", of which there is one. An advisory
lock rather than a row lock because the invariant is about a *set* and no row
represents it; the specific race is two transactions each locking the row it is
about to remove, which serialises nothing because they are different rows.

This mirrors the established pattern: `WorkspaceService` takes
`TenantRepository.lock` (a `FOR UPDATE` on the tenant row) for the tenant owner
set, and a `pg_advisory_xact_lock` for per-account workspace creation where
there is no row yet. The platform case is the second shape.

---

## 8. AUTHZ-03 Schema Hardening

`AgentCreate`, `AgentUpdate` and `ToolGrantRequest` now carry
`model_config = ConfigDict(extra="forbid")`. No field semantics changed.

**The regression does not test three schemas.** The finding was that six
existing tests assert `extra="forbid"` by name on six chosen models — coverage
that says nothing about the seventh. So
`tests/integration/test_request_schema_strictness.py` enumerates **every** body
model FastAPI binds, resolved from the dependency graph the way
`test_route_authorization.py` resolves guards: 53 models across 138 routes.

Each is checked twice — structurally (`model_config`) and behaviourally, by
constructing it with each of `tenant_id`, `workspace_id`, `platform_role`,
`role`, `is_admin`, `user_id`, `owner_id`, `created_by` and a nonsense field,
and requiring an `extra_forbidden` error naming that field. A model that
*declares* the field is skipped, since e.g. `user_id` on a conversation
assignment is the request.

It carries a control asserting the walker found at least 40 models — an
inventory that silently came back empty would satisfy every other assertion in
the file, and `_routes` missing `_IncludedRouter` returns 7 routes out of 138.

**The inventory found two the audit's AST pass had not classified:**
`PasswordResetRequestPayload` and `PasswordResetConfirmPayload`, lenient for no
stated reason while their siblings in `app/schemas/auth.py` go through a base
that forbids extras. Neither ever read an undeclared field, so this changes no
behaviour — only what a misspelling does. Fixed.

**One exemption, and it is principled:** the model FastAPI assembles for the
media upload's multipart signature. Nobody wrote it, there is nothing in
`app/schemas` to harden, unknown form parts are refused by the multipart parser
anyway, and it is recognised by *where it was built* (`not
model.__module__.startswith("app.")`) so the exemption cannot be borrowed by a
model of ours.

---

## 9. AUTHZ-04 Database Defence in Depth

The schema has **30** tenant-to-tenant parent/child relations. Six are now
structurally enforced; 24 are deliberately not.

### Pre-migration data audit

The local development database was empty (the test session drops its schema at
teardown), so there was no production-like data to sweep — stated plainly rather
than reported as "zero mismatches found". The audit is instead encoded twice:
migration `0053` runs the six counting queries **before** it constrains
anything and refuses with the offending counts, and
`test_tenant_relational_integrity.py` runs the same six against a populated
schema so the code that guards a deployment at three in the morning has been
executed at least once. All six returned 0.

### Implemented

| Child | Column | Parent | Nullable | On delete | Why this one |
|---|---|---|---|---|---|
| `conversations` | `contact_id` | `contacts` | no | CASCADE | whose conversation this is |
| `conversations` | `account_id` | `whatsapp_accounts` | no | CASCADE | which number it arrived on |
| `messages` | `conversation_id` | `conversations` | no | CASCADE | the transcript itself |
| `documents` | `knowledge_base_id` | `knowledge_bases` | no | CASCADE | the corpus a document joins |
| `document_chunks` | `document_id` | `documents` | no | CASCADE | what a retrieval actually reads |
| `document_chunks` | `knowledge_base_id` | `knowledge_bases` | no | CASCADE | how a retrieval scopes itself |

The sixth goes beyond the brief's five on purpose: `KnowledgeRepository.search`
filters chunks by `knowledge_base_id` directly, which makes that column a
scoping field in its own right rather than a denormalised convenience. Pinning
only `document_id` would leave the row half-anchored.

Parents gained `UNIQUE (tenant_id, id)` — not a uniqueness claim on a table
whose primary key is `id`, but the target a composite key requires. The
single-column keys were **dropped** rather than kept alongside: the composite
already implies them, and keeping both is the same check paid twice on every
insert into the two largest tables in the schema.

### Deferred, with the reason

The other 24 relations — `usage_events`, `analytics_events`,
`campaign_recipients`, `lead_activities`, `lead_notes`, `follow_ups`,
`message_media`, `message_sentiments`, `agent_tools`, `invoices`, `payments`,
`whatsapp_events`, `whatsapp_templates`, `campaigns` and the rest. A mismatch in
any of them is an inconsistency — a wrong number on a dashboard — rather than a
confidentiality event. Adding constraints mechanically would buy write-path cost
on the highest-volume tables in the system for that.

This is **deferred defence-in-depth work, not an open vulnerability.** No API
path produces a crossed row in any of the 30, which is what the audit
established and what this section does not weaken.

Recorded as **ADR-100**; the table and the reasoning are in
`docs/AUTHORIZATION.md` §6.19, so the next person to ask "why only six" finds an
answer rather than inferring an oversight.

---

## 10. AUTHZ-05 Test Gap

Four tests, parametrised over both platform roles and both verbs. Each asserts
more than the status code, because the finding was a guard that a mutation
survived silently:

- status `422` and error code `validation_error`;
- `is_active` still true, `deleted_at` still null;
- `token_version` **unchanged** — a "refusal" that had already revoked every
  session would otherwise pass;
- and the session that made the attempt still works (`GET /auth/me` → 200).

| Mutation | Detector | Killed? |
|---|---|---|
| M5 — remove the `user_id == actor.id` delete guard | `test_platform_staff_cannot_act_on_their_own_account` | **Yes** |
| M6 — remove the self-disable guard | same | **Yes** |

---

## 11. AUTHZ-06 In-Flight Revocation

**Unchanged by design.** No lock was added, no isolation level raised, no
existing test altered.

The semantics are now written down, in `docs/AUTHORIZATION.md` §6.18:

> Revocation is effective for every request whose authorization check begins
> after the revocation commits. It is not retroactive for a request already
> admitted.

A request whose membership check passed before a concurrent revocation committed
still completes; the next request is refused. This is ordinary read-committed
behaviour, and the request's authorization was genuinely valid at the moment it
was taken. Making the finding disappear would have meant a global lock on every
request or a raised isolation level — real cost, for a report line.

---

## 12. AUTHZ-07 Environment Fix

`pip uninstall wasla` removed the editable install pointing at `D:\Wasla\wasla`
(a directory that no longer exists), and `pip install -e .` repointed it at this
checkout. No `.pth` file was hand-edited.

```
E:\wasla            → E:\wasla\app\__init__.py
E:\                 → E:\wasla\app\__init__.py
C:\                 → E:\wasla\app\__init__.py
%TEMP%\claude\...   → E:\wasla\app\__init__.py
```

Before the repair, three of those four raised `ModuleNotFoundError` — the stale
`.pth` pointed at a deleted tree, which is a *louder* failure than the audit met
and would have become silent again the moment anything was cloned back to `D:`.

`pip list` now reports `wasla 0.1.0 E:\wasla`.

**The guard that belongs in the repository** (§21) is in `tests/conftest.py`: a
collection-time check that `app.__file__` lies inside this working tree, with
the expected root derived from the conftest's own location rather than written
down. A hardcoded `E:\wasla` would be the same bug wearing a different hat —
passing on the machine it was written on and misleading everywhere else. It
guards dev and audit tooling only; nothing in `app/` references it.

---

## 13. Platform Route Matrix

All eleven routes. Two changed.

| Route | Platform admin | Platform owner | Guard |
|---|---|---|---|
| `GET /platform/overview` | yes | yes | `PlatformStaffDep` |
| `GET /platform/tenants` | yes | yes | `PlatformStaffDep` |
| `GET /platform/audit-logs` | yes | yes | `PlatformStaffDep` |
| `POST /platform/invoices/{id}/payments` | yes | yes | `PlatformStaffDep` |
| `POST /platform/invoices/{id}/void` | yes | yes | `PlatformStaffDep` |
| `POST /platform/tenants/{id}/suspend` | yes | yes | `PlatformStaffDep` |
| `POST /platform/tenants/{id}/restore` | yes | yes | `PlatformStaffDep` |
| `POST /platform/tenants/{id}/ownership` | yes | yes | `PlatformStaffDep` |
| `POST /platform/users/{id}/enable` | yes | yes | `PlatformStaffDep` |
| `POST /platform/users/{id}/disable` | **not against an owner** | yes, guarded | `PlatformAccountTargetDep` |
| `DELETE /platform/users/{id}` | **not against an owner** | yes, guarded | `PlatformAccountTargetDep` |

**Nine staff routes, two owner-guarded destructive routes.** No read-only route
became owner-only.

This table is executable:
`test_each_platform_route_carries_the_guard_the_policy_says` resolves it from
the dependency graph and compares. It asserts **both** directions — a route
added to this router without an entry fails, and so does a router-wide
owner-only dependency, which would otherwise have passed every refusal test in
the suite while quietly breaking platform administration.

---

## 14. Platform User Lifecycle Matrix

| Target state | Admin | Owner |
|---|---|---|
| normal user, active | disable / enable / delete — allowed | allowed |
| normal user, disabled | enable / delete — allowed | allowed |
| platform admin, active | disable / enable / delete — allowed | allowed |
| platform admin, disabled | allowed | allowed |
| **platform owner, active** | **403 on disable and delete**; enable allowed | allowed while another live owner remains |
| **platform owner, disabled** | **403 on delete**; enable allowed | allowed while another live owner remains |
| deleted user | `404` — no resurrection | `404` |
| tombstoned owner | not counted live; holds no role after deletion | same |
| inactive owner | not counted live | same |
| self | `422` on disable and delete | `422` |
| last live owner | `403` (hierarchy) | `422` (invariant) |

Role transitions, via the operator command only:

| Transition | Result |
|---|---|
| grant any role to a live account | allowed |
| grant any role to a **deleted** account | refused — "permanently deleted" |
| grant any role to a **disabled** account | refused — "re-enable the account" |
| revoke an owner while another live owner remains | allowed |
| revoke the last live owner | refused |
| revoke the last live owner past a **tombstoned** owner | refused (the AUTHZ-02 reproduction) |
| demote the last live owner via `grant … platform_admin` | refused |
| demote an owner while another remains | allowed |

A *disabled* owner is deliberately still protected from an admin, though it does
not count toward the live total. The two predicates answer different questions:
"can this account exercise ownership now" (counting) and "is this an owner's
account" (the hierarchy). Reusing the narrow one for the hierarchy would let
disable-then-delete walk around the guard the moment anybody else disabled them.

---

## 15. Tenant Isolation Regression

A focused re-run, not the whole 1,500-line audit. Every customer-to-customer
control still passes, with own-object controls passing beside them.

| Suite | Result |
|---|---|
| `test_tenant_isolation.py` | pass |
| `test_authorization.py` | pass |
| `test_route_authorization.py` | pass |
| `test_billing_authorization.py` | pass |
| `test_media_isolation.py` | pass (8 object-store cases skipped, no MinIO) |
| `test_knowledge_rag.py` | pass — RAG marker isolation preserved |
| `test_trace_isolation.py` | pass |

Covering: foreign signed `tid` denied; foreign conversation, contact, lead,
document and audit-log access denied; foreign billing resources denied; RAG
retrieval isolation intact; workers refusing a mismatched tenant/object pair.

**Tenant RBAC re-verified** and unchanged: member cannot invite; admin can
invite member and admin but not owner; owner can invite owner; member and admin
cannot read billing while owner can; member cannot read the audit log while
admin can.

**No tenant-role behaviour changed.** `PlatformAccountTargetDep` resolves a
`User`, never a workspace, so platform authority still confers no membership —
re-checked explicitly over live HTTP (a platform owner reading `/conversations`
gets `403`).

---

## 16. Mutation Results

Each mutation removes exactly one guard, runs its detector, and is restored in a
`finally` with a byte-for-byte equality assertion. `git status` was clean
afterwards; **no mutation remains**.

| Mutation | Expected detector | Killed? |
|---|---|---|
| M1 — hierarchy refusal removed (admin may act on owner) | `test_platform_hierarchy.py` | **Yes** |
| M2a — routes unwired back to `PlatformStaffDep` | route policy test | **Yes** |
| M2b — dependency body neutered, service intact | — | **No, by design** |
| M2c — service guard removed, dependency intact | — | **No, by design** |
| M2d — shared policy function neutered (both layers) | `test_platform_hierarchy.py` | **Yes** |
| M3 — live-owner predicate drops the lifecycle filters | role lifecycle + hierarchy | **Yes** |
| M4 — advisory lock removed (check-then-act restored) | concurrency suite | **Yes** |
| M5 — self-delete guard removed | hierarchy | **Yes** |
| M6 — self-disable guard removed | hierarchy | **Yes** |
| M7 — agent schemas accept extras | schema strictness | **Yes** |
| M8 — composite FK on `messages` reduced to single-column | relational integrity | **Yes** |
| M9 — tombstone keeps its platform role | hierarchy | **Yes** |
| M10 — CLI stops guarding the demotion inside `grant` | role lifecycle | **Yes** |

**On M2b and M2c.** These are not coverage gaps; they are what two independent
layers enforcing one rule look like. Removing either alone leaves the other
producing the identical `403`, so no behavioural test can distinguish them — by
construction. The symmetry was confirmed from both sides rather than assumed,
and the layers are covered where they can be: the route *wiring* structurally
(M2a), the shared rule behaviourally (M1, M2d).

### One flake, found in my own test

`delete x delete` signed both contenders in from *inside* the raced coroutines,
so the trailing login had to complete within the hand-off window. It does on an
idle machine and did not under the full suite: the leader committed, the
trailing actor's account was tombstoned, and its in-flight login returned `401`.
**One failure in 4,215, with nothing wrong in the code under test.**

Reported rather than quietly fixed, because a race test that fails for a reason
unrelated to the race is the thing most likely to be re-disabled later. Both
contenders now authenticate before the hand-off begins — which also sharpens the
claim, since the trailing request is then demonstrably one the server had
already admitted. M4 still kills the file afterwards, so the restructure cost no
detection.

---

## 17. Database / Migration Results

| Check | Result |
|---|---|
| `alembic heads` | `0053 (head)` — single head |
| `alembic check` | `No new upgrade operations detected` |
| Fresh `upgrade head` on an empty database | clean, 53 migrations |
| `downgrade 0052` across the new migration | clean; single-column FKs restored, unique constraints dropped |
| Re-`upgrade head` | clean |
| `alembic check` after the round trip | clean |
| Full suite against a **migration-built** schema | 165 pre-existing billing failures, byte-for-byte identical to the audited baseline — see below |

### The migration-built run, stated plainly

Because this change alters the schema, the whole suite was also run with
`WASLA_TEST_SCHEMA=migrations` — a database built by `alembic upgrade head`
rather than from the mapped metadata. It reports **165 failures**, and none of
them is mine.

All 165 are in billing modules — `test_paymob_checkout` (32),
`test_billing_worker` (29), `test_dunning_lifecycle` (28),
`test_paymob_refunds` (23), `test_invoicing` (20), `test_billing_endpoints`
(16), `test_entitlements` (12), `test_refund_entitlements` (5). Not one touches
conversations, messages, documents, chunks, platform roles or request schemas.
The cause is visible in the failing names: the migrations seed a plan catalogue
that the billing suite assumes absent (`test_the_seeded_catalogue_is_not_visible_to_this_suite`
is itself among them).

Reasoning about that is not proof, so it was measured. A git worktree was
created at the audited baseline `69db6e4`, given its own database, and run the
same way:

| Run | Tests | Failures | Skipped |
|---|---|---|---|
| Baseline `69db6e4`, migration-built | 4,151 | **165** | 73 |
| This branch `c469e32`, migration-built | 4,215 | **165** | 73 |

The two failure *sets* were diffed and are **byte-for-byte identical**. This is
a pre-existing property of running the full suite against a migration-built
schema, unchanged by migration 0053, and it is reported here rather than
omitted because a clean `alembic check` alone would not have surfaced it.

The worktree and every scratch database were removed afterwards.

**Direct SQL controls**, 13 assertions:

- **Negative** — for each of the six relations, a child in workspace A naming
  workspace B's parent is refused by PostgreSQL, with the error asserted to
  mention a foreign key (so a not-null or unique failure cannot be mistaken for
  the constraint holding).
- **Positive** — the same six statements with a same-workspace parent are
  accepted. Without this half, a constraint that rejected everything would pass.
- **Sweep** — the migration's own six counting queries, run against a populated
  schema; all zero.

Each workspace fixture seeds a *spare* contact and number, because
`conversations` carries `UNIQUE (tenant_id, contact_id, account_id)` and a probe
reusing the seeded pair would be refused by that instead — a negative test
failing on the wrong constraint proves nothing.

No old migration was edited. No production-like data was deleted or rewritten.

---

## 18. Static / Test Gates

| Gate | Result |
|---|---|
| `ruff check .` | All checks passed |
| `black --check .` | 541 files unchanged |
| `mypy app` | Success: no issues found in 246 source files |
| `alembic check` | No new upgrade operations detected |
| `alembic heads` | `0053 (head)` |
| **`pytest`** | **4,139 passed · 76 skipped · 0 failed · 0 errors** (687s) |

Baseline was 4,075 passed / 76 skipped / 0 failed. **+64 tests, no new skips.**

Skip reasons — all four environmental opt-ins, identical in kind and count to
the baseline:

| Count | Reason |
|---|---|
| 62 | No object store configured; set `TEST_S3_ENDPOINT_URL` |
| 11 | No `OPENAI_API_KEY`; real-provider tests are opt-in |
| 3 | Schema parity needs `WASLA_TEST_SCHEMA=migrations` |

The three schema-parity skips were additionally exercised against a
migration-built database (§17), since this change alters the schema — and that
run surfaced a pre-existing condition worth reading before trusting it.

New test files: `test_platform_hierarchy.py` (22),
`test_platform_owner_concurrency.py` (6), `test_platform_role_lifecycle.py`
(11), `test_request_schema_strictness.py` (11),
`test_tenant_relational_integrity.py` (13), plus one added to
`test_orphan_workspace.py`.

---

## 19. Remaining Product Decisions

Not defects. Recorded, unchanged, for product review.

1. **Should a `MEMBER` reassign conversations?** They can today, along with
   sending messages and creating leads. The audit flagged it; **this remediation
   changed nothing**, because no ADR or requirement says it should. The
   suggested future policy — member may assign to *self*, admin and owner may
   assign anyone — needs a source-of-truth decision first.
2. **Should ownership transfer leave the previous owner an admin?** It does,
   deliberately. Unchanged.
3. **Should a workspace have several owners?** It may. Unchanged.
4. **Should `PLATFORM_ADMIN` be able to delete another `PLATFORM_ADMIN`?**
   **Now decided: yes**, and tested. Peer-level platform administration is
   ordinary; only the rank boundary is protected. Stated explicitly here because
   the brief asked for it not to stay implicit.
5. **Should platform staff ever read tenant conversations?** They cannot. If
   that capability is added it must be a separate, explicit, audited feature —
   the `PlatformAccessAudit` groundwork exists. This remediation did **not**
   make `PlatformAccountTargetDep` imply any tenant access; it resolves a `User`
   and never a workspace.
6. **Should deleting a workspace purge its data?** It does not. Unchanged.

---

## 20. Remaining External Verification

Genuine deployment facts, not verifiable from this machine. None is claimed
fixed.

| Item | Verified locally | Still external |
|---|---|---|
| Object storage | Key construction, no presigned URLs, API-mediated download | The production S3/MinIO **bucket policy** — that it is not publicly listable or readable |
| Reverse proxy | `nginx/` example reviewed | That the deployed proxy does not bypass the app or expose `/metrics` publicly |
| Alerting | Metrics and audit events emitted | Prometheus scrape config, alert rules, Alertmanager receivers and **actual Slack delivery** |
| Database hardening | Application isolation proven; six relations now structurally enforced | Whether production uses a restricted role. There is still no PostgreSQL RLS |
| Provider accounts | Webhook HMAC and tenant routing proven with real signatures | That real Meta/Paymob/Resend credentials belong to the intended accounts |

**Browser scope.** No platform UI exists in this tree — platform-role controls
are API-only — so real HTTP against a live Uvicorn process is the strongest
available proof, and §5 is it. No throwaway UI was built for cosmetic
verification.

---

## 21. Final Authorization Score

| Dimension | Before | After | Basis for the change |
|---|---|---|---|
| Tenant isolation | 10 / 10 | **10 / 10** | Unchanged and re-verified |
| Object-level authorization | 10 / 10 | **10 / 10** | Unchanged |
| Membership enforcement | 10 / 10 | **10 / 10** | Unchanged |
| Tenant RBAC | 9 / 10 | **9 / 10** | Untouched by design |
| Owner safety (tenant) | 9 / 10 | **9 / 10** | Unchanged |
| **Platform hierarchy** | 6 / 10 | **10 / 10** | Ranked, enforced at two layers, executable route matrix, invariant centralised, concurrency-proven |
| Worker / background isolation | 9 / 10 | **9 / 10** | Unchanged |
| RAG isolation | 10 / 10 | **10 / 10** | Now also structural — chunks cannot name a foreign document or knowledge base |
| Storage | 8 / 10 | **8 / 10** | Bucket policy still external |
| Billing authorization | 10 / 10 | **10 / 10** | Unchanged |
| Auditability | 9 / 10 | **10 / 10** | `previous_platform_role` recorded; refusals leave no misleading entry |
| Concurrency | 9 / 10 | **10 / 10** | Platform owner set now under the same discipline as the tenant owner set |
| Testing | 9 / 10 | **10 / 10** | +64 tests; 11/11 meaningful mutations killed; the AUTHZ-05 gap closed; a self-inflicted flake found and fixed |
| Operations | 7 / 10 | **9 / 10** | Shadowing install removed and guarded against; operator runbook states the hierarchy and its refusals |
| Database integrity | — | **9 / 10** | Six high-value relations structural; 24 deferred with reasons; no RLS |

---

## 22. Final Verdict

> ### AUTHORIZATION FINDINGS CLOSED WITH EXTERNAL DEPLOYMENT CHECKS

Every finding in the ledger is closed. Five were fixed and proved, one is
correct by design and now documented as such, one was an environment repair with
a guard so it cannot recur silently.

The qualifier is **not** an open authorization defect. It is the five items in
§20, none of which can be verified from a development machine: the object-store
bucket policy, the deployed reverse proxy, live alert delivery, the production
database role, and provider account ownership. They were external before this
work and remain external; nothing here claims otherwise.

Two things are worth stating plainly about scope:

**AUTHZ-04 is 6 of 30 relations by deliberate choice**, not by exhaustion. The
remaining 24 are defence-in-depth work with a stated cost/benefit argument, not
latent vulnerabilities — no API path produces a crossed row in any of the 30,
and that finding is unchanged.

**The `MEMBER` conversation-assignment question (§19.1) was left alone.** It is a
product decision with no source-of-truth backing, and changing RBAC behaviour
inside a findings remediation would have been the wrong place to decide it.

Wasla's customer-to-customer tenant isolation is exactly as strong as the audit
found it — re-verified after every change. What is different is that the
platform-staff hierarchy is now internally consistent, cannot be collapsed to
zero live owners through any supported operation or any race between two of
them, and is legible to the next developer and the next operator from a table
rather than from the source.
