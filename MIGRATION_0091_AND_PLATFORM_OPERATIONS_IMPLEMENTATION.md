# Migration 0091 And Platform Entitlement Operations — Implementation Report

Stage: MIG-0091 + PLAT-G1..G8 (ADR-132). Branch `platform-entitlement-ops-20261008`,
worktree `E:\wasla-platform-ops`, 2026-10-08/09. Local commits only; nothing
pushed, merged or deployed.

## 1. Executive Summary

Two bounded gaps left after the entitlements stage are closed.

**Part A — refused downgrades.** 0091's downgrade dropped its index
`CONCURRENTLY` in an autocommit block, which commits every downgrade step above
it. Reproduced: a downgrade from head that 0090 refused stopped stamped `0091`
without `ix_conversations_tenant_id_channel_last_message_at`; `upgrade head`
reached head without it; `db_preflight verify` printed ok. The survey found
the same pattern in **0075, 0078, 0079 and 0081** (a refusal at 0069 left the
stamp at 0075 and lost eleven indexes, `uq_users_email_lower` among them). All
five downgrades now drop in the run's transaction; every refused downgrade
now leaves the database at its starting head with every index valid.
`db_preflight verify` now compares the database with every table, index and
named constraint the models declare and names each one missing.

**Part B — platform operations.** Staff can now, through audited API calls:
withdraw a platform grant (capacity shortfalls go through `boundary()` with
the 7-day grace; nothing is disabled by the withdrawal itself); list every
workspace's capacity reductions, read one, and read a workspace's history;
read a workspace's channel capacity and connections exactly as the workspace
does (same code, same schemas, no secret); filter subscriptions by plan
version, products by code/search, purchases by channel type, the audit log by
target with a stable cursor; read the channel vocabulary with `operable`; see
`topup_eligible` on `/features`.

Evidence: migration tests that failed before the fix and pass after; APP-E2E
R-1..R-10 **10/10 PASS** against uvicorn + a migration-built database;
invariants E01..E18 at 0; the SQL oracle 168/168 (suite) and 12/12 (E2E) with
0 mismatches; new mutations **20/21 killed**, the survivor (M-P08) proven a
redundant guard; the entitlements matrix still **46/46**; ruff, black and mypy
clean; model-built 6,782 passed / 0 failed and migration-built 6,786 / 0.

**Verdict: MIGRATION 0091 AND PLATFORM OPERATIONS CLOSED WITH EXTERNAL ACTIONS REMAINING.**

## 2. Starting Repository State

| Item | Value |
| --- | --- |
| PLATFORM_OPS_BASE_HEAD | `a7fc8b5` (entitlements report; code head `b85b717`; `43751f1` is an ancestor) |
| Base branch / worktree | `entitlements-channel-capacity-20261002`, `E:\wasla-entitlements`, clean tracked tree |
| Migration head at base | `0098` (single) |
| Ahead / behind `origin/worktree-billing-google-auth` | 78 ahead / 0 behind (origin still at `354db53`, lacks `aa11098`) |
| New branch / worktree | `platform-entitlement-ops-20261008`, `E:\wasla-platform-ops` (clean before the first change) |
| Helper worktrees (detached) | `E:\wasla-plat-mut` (mutations), `E:\wasla-plat-lane` (full suites) |
| `app.__file__` | `E:\wasla-platform-ops\app\__init__.py` (and each helper worktree's own) |
| Python / Docker / Compose | 3.12.7 / 29.8.0 / v5.5.1 |
| PostgreSQL / Redis | 16.15 (`pgvector/pgvector:pg16`) / 7.4.11 |
| Containers (this stage only) | `wasla-plat-pg` 56261, `wasla-plat-redis` 56271, `wasla-plat-redis2` 56272, `wasla-plat-minio` 59261 |
| Databases | `wasla_plat_models`, `wasla_plat_migrations`, `wasla_plat_alembic`, `wasla_plat_mut`, `wasla_plat_e2e`, `wasla_plat_probe` |

The user's untracked documents in `E:\wasla` were not touched; `E:\wasla` and
`E:\wasla-entitlements` were only read. The `wasla-ent-*` containers were not
used.

## 3. Baseline Reproductions

On `wasla_plat_alembic` / `wasla_plat_probe`, migration-built at `a7fc8b5`:

| Gap | Reproduction | Observed before |
| --- | --- | --- |
| MIG-0091 | head; tenant + connection with `sends_per_minute = 10`; `alembic downgrade 0089` | `RuntimeError` from 0090; `alembic_version = 0091`; index **absent**; `upgrade head` → `0098`, index **still absent**; `db_preflight verify` → `ok`, exit 0 |
| Survey (0075..0081) | head; a stored card; `alembic downgrade 0068` | 0069 refuses; stamp **0075**; indexes of 0075, 0078, 0079, 0081, 0091 gone |
| New tests on the unfixed code | `test_migration_downgrade_atomicity.py` | **3 failed**: `'0091' == '0098'` (0090 refusal), `'0091' == '0098'` (0081 refusal), `'0075' == '0098'` (0069 refusal) |
| PLAT-G1 | grant `channel_connections +1` | no route withdraws a grant; only SQL |
| PLAT-G2 | reductions in two workspaces | no cross-workspace list; the summary shows `latest()` only |
| PLAT-G3 | staff token on `GET /billing/channel-capacity` | refused: no membership; the summary has totals only |
| PLAT-G4..G8 | `plan_version_id`, `/channel-types`, `target_type`/`target_id`/cursor, `code`/`search`, `channel_type` on purchases | unknown query parameters are ignored (unfiltered results); `/channel-types` 404 |

## 4. Decisions Applied (PO-01..PO-07)

| Decision | Applied |
| --- | --- |
| PO-01 | 0091's downgrade: `SET LOCAL lock_timeout = '15s'` + `DROP INDEX IF EXISTS` in the run's transaction. Upgrade unchanged. **Extended** by the Phase 3 survey to 0075, 0078, 0079 and 0081 (same edit, downgrade only) — see §7; this goes beyond the brief's "only 0091" and is recorded as an explicit exception in ADR-132. |
| PO-02 | `db_preflight verify` reports missing tables, indexes, unique constraints, primary keys, check constraints and foreign keys declared by `Base.metadata`, by convention name; invalid indexes as before. Exit 1, names only. `MIGRATION_ONLY_INDEXES` is empty. Columns are not compared (a missing column fails loudly at once). |
| PO-03 | `POST /platform/billing/topup-purchases/{id}/withdraw`, staff; body `tenant_id`, `reason`, `expected_revision`. `tenant_id` added so "another workspace's purchase id → 404" is meaningful on a route that is not tenant-scoped. New status `withdrawn`; `withdrawn_at/by`, `withdrawal_reason`; audit `billing_topup_grant_withdrawn`. No shorten-to-a-date operation. |
| PO-04 | Reduction queue, detail and history — reads only. Staff actions on a reduction: **deferred product decision**. |
| PO-05 | Platform capacity and connection reads call the extracted `app/services/channel_capacity_view.py`, the same functions the tenant routes call; same schemas. |
| PO-06 | All five filters plus `/channel-types` and `topup_eligible`; additive. `/plans/{id}/versions` and custom offers stay unpaginated (accepted residual). |
| PO-07 | Nothing charges, disables, releases or deletes by hand, or bypasses the guard. A withdrawal does not apply or discard an owner's pre-selection (it is not the term boundary the pre-selection was made for), so it never disables in the request. |

## 5. MIG-0091 Fix and Reasoning

Each fixed downgrade (0075, 0078, 0079, 0081, 0091) now reads:

```python
op.execute("SET LOCAL lock_timeout = '15s'")
op.execute(f"DROP INDEX IF EXISTS {INDEX}")
```

Why not keep `CONCURRENTLY`: a downgrade runs with the application stopped
(RUNBOOK deploy rule), so the brief `ACCESS EXCLUSIVE` lock on `conversations`
for a catalogue change costs nothing; `CONCURRENTLY` cannot run in a
transaction, and an autocommit block is exactly what made the run non-atomic.
Refusing "before any autocommit step" would require each migration to know
every lower refusal — not maintainable. Same choice as 0094 in `9695ec0`.

The lock wait is bounded at 15 s; past it the whole run fails and changes
nothing. Upgrades are byte-for-byte unchanged, so a database at head is
unaffected; docstrings say what changed and why.

`test_plan_price_migration` no longer steps below 0091 first: its 0081 refusal
is made from head again and asserts the stamp is still head (the workaround is
gone, the original intent restored).

Results (`test_migration_downgrade_atomicity.py`, 5 tests, plus the existing
migration suites — 12 passed after the fix, `test_entitlement_migrations`,
`test_plan_price_migration`, `test_omnichannel_migrations`,
`test_migration_recovery`, `test_billing_migrations`,
`test_payment_token_migration`):

| Refused at | Before (stamp / indexes) | After |
| --- | --- | --- |
| 0090 (sending allowance) | 0091 / 0091's index gone | head (`0100`) / every index valid |
| 0081 (price) | 0091 / 0091's gone | head / valid |
| 0069 (stored card) | 0075 / 0075, 0078, 0079, 0081, 0091 gone | head / valid |
| 0099 (a withdrawal exists) | — (new) | head / 0100's index back too |
| Successful 0100 → 0090 → head | — | index dropped once, rebuilt `CONCURRENTLY` |

## 6. db_preflight

`verification_problems` = unvalidated constraints + invalid indexes + disabled
triggers (unchanged) + **`missing_problems`**: every table, index and named
constraint of `Base.metadata`, named as the convention names it
(`PGDialect().identifier_preparer.format_constraint`, which also applies the
63-byte truncation), compared with `pg_class` / `pg_index` / `pg_constraint`
in `current_schema()`. Constraint-backed indexes are reported once, as the
constraint. Objects the models do not declare are not reported (the parity
suite owns that direction). Unique and check constraints are compared by name:
the catalogue-parity suite proves the names identical, so it is exact and
noiseless — 0 problems on a fresh migration-built head and on a model-built
schema.

| Test (`test_db_preflight_declared.py`, CLI as a subprocess) | Result |
| --- | --- |
| clean on a fresh head | exit 0, `db_preflight verify: ok` |
| 0091 index dropped by hand (asserted absent in `pg_indexes` first) | exit 1, `index missing: conversations.ix_conversations_tenant_id_channel_last_message_at` |
| 0091 index marked invalid | exit 1, `index invalid: conversations.…` |
| a check constraint dropped | exit 1, `check constraint missing: topup_purchases.ck_topup_purchases_grant_is_not_a_sale` |
| the pre-fix damage rebuilt by hand, then `upgrade head` | exit 1, names the index |

On the real damaged databases from §3: the 0091 case prints exactly the one
index; the 0075 case prints 7 tables, 18 indexes and 51 constraints (20 check, 21 foreign key, 10 unique) missing: that database sits stamped 0075 with the schema of the steps below it, so against the head models most of the list is the later migrations' objects - the point is that verify is no longer ok on it.
Model-built (`test_a_migrated_schema_passes_verification` in the model lane)
and migration-built (gates, §16) both read `ok`.

## 7. Migration Downgrade Survey

Every `autocommit_block()` / `CONCURRENTLY` reachable from a `downgrade()`
(AST walk over `alembic/versions/`, helpers followed). Downgrades that can
refuse: 0069, 0071-0074, 0079, 0081, 0082, 0084-0090, 0092-0098, 0099.

| Migration | Autocommit step in downgrade | Can a lower migration refuse after it? | Inconsistent stamp + schema? | Action |
| --- | --- | --- | --- | --- |
| 0039 | drops the ANN index `CONCURRENTLY` | no (nothing below refuses) | no | safe as is |
| 0040 | drops and rebuilds the covering index `CONCURRENTLY` | no | no | safe as is |
| 0075 | drops four purge indexes | yes (0069-0074) | **yes, reproduced** (stamp 0075) | **fixed** |
| 0078 | drops two unique indexes | yes (0069-0074) | yes | **fixed** |
| 0079 | drops `ix_whatsapp_events_redactable` (after its own refusal check) | yes | yes | **fixed** |
| 0081 | drops five indexes (after its refusal check) | yes (0079, 0069-0074) | yes | **fixed** |
| 0084 | none (helper mentions `CONCURRENTLY` in a docstring only) | — | — | safe |
| 0091 | drops the inbox index | yes (0090, 0089, …) | **yes, reproduced** | **fixed** |
| 0094 | none since `9695ec0` | — | — | already fixed |
| 0099 / 0100 (new) | none | — | — | one transaction by design |

Totals: 7 migrations with an autocommit step in a downgrade; **5 fixed**,
**2 safe** (with the reason), **0 residual**. `ALTER TYPE … ADD VALUE` in
upgrades stays in autocommit blocks; enum labels cannot be dropped and stay.

## 8. Grant Withdrawal (PLAT-G1)

`TopupAdmin.withdraw_grant`: 404 unless the purchase exists **and** belongs to
`tenant_id`; then the workspace's advisory lock on the grant's key
(`hold_limit_lock`), then the row lock; 409 for a stale revision, a paid
purchase, a grant already withdrawn, expired, cancelled or past `expires_at`.
Status `granted → withdrawn` through `TOPUP_TRANSITIONS` (terminal); snapshot
untouched (the trigger still freezes key, quantity, channel, expiry);
`withdrawn_at`, `withdrawn_by`, `withdrawal_reason`, `ended_at` recorded. For
`channel_connections` → `ChannelCapacityReductions.boundary(cause=grant_withdrawn,
topup_purchase_id=…)`. Audit entry with before/after effective limit, reduction
id, actor role, reason, request id. Metric `wasla_platform_grant_withdrawals_total{key}`.
Response: purchase, `effective_limit_before/after`, `reduction`.

| Test (`test_grant_withdrawal.py`, real route) | Result |
| --- | --- |
| stops counting at once | limit 2 → 1, the guard then refuses a 2nd connection; `GET /topup-purchases/{id}` shows withdrawn fields |
| still fits → nothing opened | `reduction` null, no reduction row, active unchanged |
| no longer fits → reduction with grace | `grant_withdrawn`, names the grant, grace exactly 7 days, **0 disabled** at withdrawal; worker at grace −1 s: nothing; +1 s: the 2 newest disabled, the oldest kept, `resolved_automatically`, **0 released, 0 deleted** |
| owner resolves during the grace | `resolved_by_owner`, cause preserved |
| typed grant | Messenger slot gone, Instagram's untouched; target `(1, {instagram: 1})` |
| AI-turn grant | limit 5 → 2, 4 recorded `ai_turn` events stay 4, no reduction |
| next AI turn after withdrawal (real `AgentWorker`) | 2 turns charged on the grant; withdrawn; 3rd turn handed off `AI_QUOTA_EXHAUSTED`, **no provider call**, still 2 charges |
| paid purchase | 409 "refunded instead", still granted |
| second withdrawal | both stale and current revision → 409; revision, time and reason unchanged; 1 audit entry |
| another workspace / unknown id | 404 / 404, still granted, no audit |
| tenant roles (member, admin, owner; verified addresses) | 403 `permission_denied` |
| audit entry | actor, `platform_admin`, reason, before/after status and effective limit (2 → 1), reduction id, request id |

Withdrawals in the suites and E2E: every withdrawal of a slot grant that no
longer fit opened exactly one reduction; disables happened only after the
grace (R-2: 1) or by an owner's choice (R-3: 1); released 0, deleted 0.

## 9. Capacity-Reduction Reads (PLAT-G2)

`CapacityReductionQueue` (`app/platform/capacity_reduction_queue.py`):
`list_reductions` (status[], cause[], tenant, grace window; order
`grace_ends_at` asc nulls last, `id`), `get` (adds kept/disabled, and while open
the owner's pre-selection and the fallback preview from the same
`ChannelCapacityReductions.fallback_preview` the owner page uses), `history`
(newest first; 404 for no such workspace). `active_now` is one grouped count
per page; `would_be_disabled_count` is the preview's disable count while open,
else the recorded disables. Tests in `test_platform_entitlement_reads.py`:
order across workspaces, every filter (status, cause, tenant, both window
edges, bad status 422), detail equal to the tenant page's preview, history
order and paging. Staff actions on a reduction: **DEFERRED PRODUCT DECISION**.

## 10. Platform Channel Reads (PLAT-G3)

`read_channel_capacity` and `list_channel_connections` moved out of the tenant
routes into `app/services/channel_capacity_view.py`; both the tenant routes and
`GET /platform/billing/tenants/{id}/channel-capacity` / `.../channel-connections`
call them. Tests compare the tenant and platform responses from **real ASGI
requests**: equal, field by field (with typed slots, an open reduction and a
fallback preview present, and with `?channel=`). A WhatsApp number connected
with a sealed token: neither the plaintext token, its tail nor the stored
ciphertext appears in any platform body, and no `access_token`, `token`,
`secret`, `credential` or `verification_code` key.

## 11. Filters and Vocabulary (PLAT-G4..G8)

| Gap | Implementation | Test (`test_platform_entitlement_filters.py` unless noted) |
| --- | --- | --- |
| G4 | `plan_version_id` on `/subscriptions` | finds the starter subscriber, excludes the pro one; combines with `tenant_id`; unknown id → empty |
| G5 | `GET /channel-types` → `channel`, `operable` (adapter registered), `state`; `/features` `topup_eligible` | only WhatsApp operable in the default registry; a synthetic paused Instagram adapter flips it (`test_platform_entitlement_reads.py`); exactly the seven keys eligible |
| G6 | `target_type`, `target_id`, `before_occurred_at` + `before_id` (together, else 422); keyset on `(occurred_at, id)`; index by 0100 | 7 entries with one timestamp, paged 3 at a time: 7 seen, 0 repeated, ordered |
| G7 | `code` (exact, any case), `search` (code or name, `ILIKE`, `%`/`_` literal, 1-50 chars) | code / upper-case code / near-miss / search on code / on name / literal `%` and `_` / 51 chars → 422 |
| G8 | `channel_type` on `/topup-purchases` | narrows to the typed grant; combines with `source`; `fax` → 422 |

Accepted residual: `/plans/{id}/versions` and per-tenant custom offers stay
unpaginated (small lists).

## 12. Tenant Isolation and Roles

All seven new routes take `PlatformStaffDep`. Tests: each refuses member,
tenant admin and tenant owner with 403 `permission_denied` (users with verified
addresses, so the refusal is for the role); unknown tenant/reduction ids are
404; a grant of another workspace is 404 on withdrawal. The route-policy table
(`test_platform_hierarchy.py`) pins all seven to `staff`; the access-audit
coverage test (`test_platform_access_audit.py`) lists the six reads. No tenant
route changed behaviour; the only visible change to a tenant is that
`TopupPurchaseRead.status` can now carry `withdrawn`.

## 13. Concurrency Results

`test_grant_withdrawal_concurrency.py`, separate committed connections, gates
that force each order (each side holds the workspace's capacity lock until the
other is provably blocked on it):

| Race | Attempts | Decided | Created / refused | Max active at a commit vs capacity in force | Outcome |
| --- | --- | --- | --- | --- | --- |
| connection first | 2 racers | 2 | 1 created | 2 ≤ 2 | then reduction `grant_withdrawn`, 7-day grace, 0 disabled |
| withdrawal first | 2 racers | 2 | 1 refused | — | limit 1, 1 active, no reduction |
| two staff, same grant | 2 | 2 | 1 withdrawn, 1 conflict | — | revision 2, 1 audit entry |

Three consecutive runs: 3/3 passed each time. Existing races (capacity, AI
holds, two billing workers, owner vs fallback) pass in both lanes.

## 14. Invariants and SQL Oracle

New read-only checks in `scripts/omnichannel_invariants.py verify`:
**E16** withdrawn grant still counting, **E17** withdrawn grant without its
audit entry, **E18** `grant_withdrawn` reduction not naming a withdrawn
platform grant of its workspace. Each proved by injection in
`test_entitlement_invariants.py` (E16 lifts the CHECK inside the rolled-back
transaction; E17's injected grant carries its creation entry, so only a
withdrawal entry explains it — strengthened after M-P09b). E01's
"explained" set: a withdrawal opens its reduction in its own transaction, so
the existing open-reduction clause covers `grant_withdrawn`; the comment says so.

The SQL oracle's population gains withdrawn channel-slot grants (general and
typed) and AI-turn grants in force and withdrawn (asserted present):
**168 comparisons, 0 mismatches**. On the E2E database: verify exit 0, 45
checks all 0 (E01..E18 among them); oracle 12 comparisons, 0 mismatches.
Fresh-head gate: `verify` ok.

## 15. Observability

`wasla_channel_capacity_reductions_total{cause="grant_withdrawn", resolution}`
(closed domain extended) and `wasla_platform_grant_withdrawals_total{key}`
(key ∈ the seven top-up keys, else `other`). No alert added (each withdrawal is
a deliberate audited staff act), so no Prometheus rule changed.

## 16. Migrations and Alembic Gates

| Migration | Change | Online |
| --- | --- | --- |
| 0075, 0078, 0079, 0081, 0091 (edit) | downgrade only: in-transaction `DROP INDEX IF EXISTS`, 15 s lock wait | downgrades run with the app stopped |
| 0099 | labels `withdrawn`, `grant_withdrawn`, `billing_topup_grant_withdrawn` (autocommit, first); `topup_purchases.withdrawn_at/withdrawn_by/withdrawal_reason`; `channel_capacity_reductions.topup_purchase_id` (FK `fk_channel_capacity_reductions_topup_purchase`, named: the convention's name is 64 bytes); CHECKs `withdrawn_is_a_grant`, `withdrawal_recorded`, `withdrawn_grant_named` | nullable columns; constraints `NOT VALID` then `VALIDATE`; 15 s `lock_timeout`; downgrade refuses while any withdrawal exists, else one transaction |
| 0100 | `ix_audit_logs_target_type_target_id_occurred_at` | `CONCURRENTLY`, INVALID leftover rebuilt; downgrade in-transaction |

Gates on fresh `wasla_plat_alembic` (`evidence/alembic_gates.txt`):
`alembic heads` → `0100 (head)`; empty → head **100 steps, 12 s**;
`alembic check` → no operations; catalogue **0 NOT VALID / 0 invalid
indexes / 0 disabled triggers**; `db_preflight verify` ok; invariants ok;
head → 0091: 9 steps, stamp `0091`, index present; re-upgrade 9 steps, check
clean, catalogue 0/0/0; head → 0089 with the 0090 refusal seeded → refused,
stamp **0100**, 0091 index valid; preflight ok. (The ledger then counts 1
`whatsapp_connection_without_its_number` — the hand-inserted seed row itself.)
Model / migration parity: in the migration-built lane (§21).

## 17. API Compatibility

Additive: 7 operations (`POST …/topup-purchases/{id}/withdraw`; `GET
…/capacity-reductions`, `…/{id}`, `…/tenants/{id}/capacity-reductions`,
`…/channel-capacity`, `…/channel-connections`, `…/channel-types`); new
parameters on `/subscriptions`, `/topups`, `/topup-purchases`,
`/platform/audit-logs`; `/features` gains `topup_eligible`;
`PlatformTopupPurchaseRead` gains `withdrawn_at`, `withdrawn_by`,
`withdrawal_reason`. Documented operation count **202 → 209**
(`test_documentation_truth` passes); README migration range `0001`–`0100`.

## 18. Mutation Matrix

Runner `mutate2.py` (strict: KILLED only with a named killer failing; control
first; sha256 restore check). Worktree `E:\wasla-plat-mut`, model-built
`wasla_plat_mut`. Control at `ff3fe05`: **PASS** (21 killer tests, 24 passed);
at `c02af43`: PASS.

| ID | Mutation | Verdict | Killed by — first assertion |
| --- | --- | --- | --- |
| M-P01 | 0091 downgrade `CONCURRENTLY` in autocommit again | KILLED | `…refused_below_0091…` — `'0091' == '0100'` |
| M-P01b | 0075 downgrade likewise (survey) | KILLED | `…refused_at_0069…` — `'0075' == '0100'` |
| M-P02 | preflight ignores missing indexes | KILLED | `…index_the_model_declares…` — exit `0 == 1` |
| M-P03 | preflight ignores invalid indexes | KILLED | `…reports_an_invalid_index` — `0 == 1` |
| M-P04 | withdrawn keeps counting (`withdrawn` in the counted statuses) | KILLED | `…stops_counting_at_once` — `(2, 2) == (2, 1)` |
| M-P04b | E16 blind | KILLED | `test_e16…` — `{} == {e16: 1}` |
| M-P05 | withdrawal skips `boundary()` | KILLED | `…opens_a_reduction_with_a_grace` — `None is not None` |
| M-P06 | withdrawal disables the newest at once | KILLED | same test — `resolved_automatically == pending_selection` |
| M-P07 | paid purchase withdrawable | KILLED | `…paid_purchase…` — 500 (CHECK) instead of 409 |
| M-P08 | withdrawal without the up-front workspace lock | **SURVIVED — redundant guard** | see below |
| M-P09 | no audit entry | KILLED | `…audited_with_reason…` — no entry |
| M-P09b | E17 blind | SURVIVED at `ff3fe05` (**test gap**) → test fixed `c02af43` → **KILLED** | `test_e17…` — `{} == {e17: 1}` |
| M-P09c | E18 blind | KILLED | `test_e18…` — `{} == {e18: 1}` |
| M-P10 | withdrawal deletes recorded usage | KILLED | `…keeps_recorded_usage` — `(2, 0) == (2, 4)` |
| M-P11 | reduction list drops the tenant filter | KILLED | `…filters_by_status_cause_tenant…` |
| M-P12 | platform capacity read omits typed slots | KILLED | `…same_channel_capacity…` |
| M-P13 | platform connections read exposes the sealed token | KILLED | `…never_expose_credentials` — `['v2.b4fa…'] == []` |
| M-P14 | a tenant role reaches `/capacity-reductions` | KILLED | `…403_on_every_new_platform_route[member]` |
| M-P15 | another workspace's grant accepted | KILLED | `…another_workspaces_grant_is_404` — `(200, 404)` |
| M-P16 | cursor compares `occurred_at` only | KILLED | `…stable_cursor` — "no entry skipped" |
| M-P17 | `/channel-types` marks everything operable | KILLED | `…marks_whatsapp_operable_only` |

**Total 21; killed 20; survived 1 (redundant guard); all restored byte for byte.**

**M-P08, proven redundant.** For a capacity key, the only decision a
withdrawal makes about connections is `boundary()`, which takes the same
`hold_limit_lock(tenant, channel_connections)` before reading anything; a
connection holding that lock commits before the boundary judges, and one
arriving later sees the withdrawal committed (both orders are exercised by the
race tests and pass with or without the up-front lock). For a usage key, a
concurrent AI hold reads committed purchases under READ COMMITTED: it sees the
grant either still counting (equivalent to the turn before the withdrawal) or
withdrawn (after it) — never a half state, since status and limit are one row.
Two withdrawals of one grant serialize on the row lock (`FOR UPDATE`) and the
revision. The up-front lock is kept for ordering clarity, as the brief asks;
removing it changes no observable outcome.

**Entitlements matrix regression (M-E01..M-E32, 46 mutants) at `ff3fe05`:
46 / 46 KILLED, all restored.** Its first control run FAILED one test
(`test_ten_overlapping_turns_against_three_charge_three_and_hand_off_seven`,
207 s cold run while the database was busy with the preceding batch); the test
then passed 3/3 alone and the full control re-run PASSED (43 passed in 39 s).
Recorded as a load-induced harness timeout, not a code regression.

## 19. Test Non-Vacuity

- MIG-0091: downgrades go through alembic's command API on migration-built
  databases; the refusal text is asserted (`carry their own sending
  allowance`, `re-price`, `protected payment tokens`, `topup_purchases withdrawn
  by staff: 1`); the tests failed before the fix with the stamps above.
- PO-02: the index is asserted absent from `pg_indexes` (or invalid) before the
  CLI runs; the CLI's exact stderr is asserted; the declared set is asserted to
  contain the 0091 index.
- PLAT-G1: the reduction is opened by the real `boundary()` through the real
  route and resolved by `BillingWorker.run_once`; the AI handoff by the real
  `AgentWorker` with the provider call count asserted.
- PLAT-G3: both responses come from real ASGI requests; the token is asserted
  stored (sealed) and the number asserted present in the body before scanning.
- Roles: refused users have verified addresses and the error code is asserted
  (`permission_denied`) — this caught a vacuous version of the test during
  development (it had passed on `email_verification_required`).
- Races: every racer's decision recorded; the order asserted.
- Oracle: withdrawn grants of both kinds asserted present in the population.

## 20. Model-Built Test Results

`WASLA_TEST_SCHEMA` unset (schema from the models), database `wasla_plat_models`,
worktree `E:\wasla-plat-lane` at `c02af43`, run alone after the migration-built
lane, 2026-10-09 01:22:21-01:58:52 UTC (0:36:15):

**collected 6,800 - passed 6,782, failed 0, skipped 18, xfailed 0, xpassed 0**

Baseline at `b85b717`: 6,749 collected, 6,731 passed, 18 skipped. +51 tests,
0 new skips; the 18 are the same set:

| Skips | Where | Why |
| --- | --- | --- |
| 11 | `tests/real_provider/test_openai_contract.py` | real OpenAI is opt-in; no key, and the brief forbids it |
| 4 | `tests/integration/test_schema_parity.py` | parity only against a migration-built database (runs in §21) |
| 1 | `tests/unit/test_local_storage_cleanup.py` | needs `WASLA_TEST_TMPFS` |
| 1 | `tests/integration/test_ai_invariants.py` | sweeps only a run that kept the AI suites' data |
| 1 | `tests/integration/test_tool_invariants.py` | sweeps only a run that kept the tool suites' data |

**First run, recorded and not counted.** The first pair of lanes at the same
commit (2026-10-08 21:28-22:44 UTC) gave 61 failed / 6,717 passed / 22 skipped
(model) and 61 / 6,721 / 18 (migrations), the same 61 in both: 48 were
`StorageError: The file store is unavailable` - this stage's MinIO container
had no `wasla-media` bucket - and the rest were the same media suites' knock-on
assertions plus 4 platform-analytics counts thrown off by workspaces those
failing tests left behind; 4 extra skips were the Google OAuth races, which
need `TEST_REDIS_URL`. After creating the bucket the affected files passed
alone (159 passed), `TEST_REDIS_URL` was pointed at this stage's second Redis,
and both lanes were re-run in full - the figures above. No code changed between
the runs.

## 21. Migration-Built Test Results

`WASLA_TEST_SCHEMA=migrations` (fresh `alembic upgrade head`, 100 steps),
database `wasla_plat_migrations`, same worktree and commit, run alone before the
model-built lane, 2026-10-09 00:44:53-01:22:21 UTC (0:37:28):

**collected 6,800 - passed 6,786, failed 0, skipped 14, xfailed 0, xpassed 0**

Baseline at `b85b717`: 6,749 collected, 6,735 passed, 14 skipped. The schema
parity tests ran and pass: models and migrations (0099 and 0100 included) build
the same catalogue, constraint by constraint, enum labels in order. Skips, the
baseline's set: 11 real-provider (no key), 1 `WASLA_TEST_TMPFS`, 1 AI and 1
tool invariant sweep (no kept data). Every new fixture that makes a workspace
gives it a subscription and a plan.

## 22. Ruff / Black / MyPy

At `c02af43`: `ruff check .` → All checks passed; `black --check .` → 897
files unchanged; `mypy app tests` → 0 errors in 788 files; `mypy scripts` → 0
errors. One targeted `# type: ignore[no-untyped-call]` on `PGDialect()` in
`db_preflight.py`, following the existing use in
`tests/unit/test_upload_recovery_worker.py`.

## 23. Runtime Verification (APP-E2E)

Real API under uvicorn on 127.0.0.1, real login/registration, real
`BillingWorker` and `AgentWorker`; PostgreSQL `wasla_plat_e2e` built by
`alembic upgrade head` from empty (**100 steps**); Redis `wasla-plat-redis2`;
Meta, OpenAI and Paymob faked at the httpx transport. Time moved only in this
database, only for reduction graces. Run 2026-10-08 21:01–21:02 UTC at
`ff3fe05`: **10 / 10 PASS** (`app_e2e/evidence.jsonl` in the session scratch
folder).

| # | Scenario | Result |
| --- | --- | --- |
| R-1 | grant +1, 2nd number 201, withdraw | 200; limit 2 → 1; platform capacity `over_limit`, active 2; reduction `grant_withdrawn`, grace 2026-10-08 21:01:14 → 10-15 21:01:14 (7 days); 0 disabled; 0 provider calls |
| R-2 | grace passed (moved in DB), `BillingWorker.run_once` | 1 handled; newest disabled, oldest kept, **0 released, 0 deleted**; queue row `resolved_automatically`, `would_be_disabled_count` 1 |
| R-3 | second workspace, owner selects in the grace | selection 200, the other disabled; history `resolved_by_owner` |
| R-4 | two open reductions | `?status=pending_selection` orders Sooner Co (10-13) before Later Co (10-15) |
| R-5 | platform vs tenant reads | channel-capacity (908 bytes) and channel-connections (523 bytes) equal field by field; no token |
| R-6 | AI-turn grant: 2 turns, withdraw, 3rd turn | limit 2 → 1; 3rd handed off `AI_QUOTA_EXHAUSTED`, no provider call; charges 2 before and after |
| R-7 | filters | `plan_version_id` 4 rows all that version; operable `[whatsapp]`; `code=CHANNEL-CONNECTION-1` → `channel-connection-1`; `search=instagram` → `instagram-connection-1`; `channel_type=whatsapp` 1 row; the typed grant's audit history paged once by the cursor |
| R-8 | refused downgrades on copies | copy: refused by **0099** (withdrawals) → stamp 0100, 0091 index valid, preflight ok; copy without withdrawals: refused by **0098** (subscribers on placeholder versions) → same; fresh migration-built head with the 0090 seed: refused by **0090** → stamp 0100, index valid, preflight ok |
| R-9 | invariants and oracle on this database | verify exit 0, 45 checks all 0 (E01..E18); oracle 12 comparisons, 0 mismatches |
| R-10 | secrets in logs, evidence, `pg_dump --data-only` | 9 secrets over 52,863 + 5,010 + 89,665 bytes: **0 found** |

R-8 note: on a populated copy the first migration to refuse is 0099 or 0098,
before 0090 is reached; both still prove the run all-or-nothing across the
fixed downgrades. The 0090 refusal itself needs a schema those migrations let
through, so it ran on a fresh migration-built head.

Owner notices (ENT-15 emails) were not configured in this harness; they are
unchanged by this stage and proven by
`test_owners_are_told_ahead_at_the_boundary_and_two_days_before`.

## 24. Documentation Changes

- `DECISIONS.md`: **ADR-132** (PO-01..PO-07, the downgrade-edit exception,
  E16-E18); ADR-131 amended (`grant_withdrawn` as a fifth cause).
- `docs/RUNBOOK.md`: "A refused downgrade leaves the database where it started
  (MIG-0091)" replaces the old warning — before/after table, detecting damage
  with `db_preflight verify`, repairing it; "Platform entitlement operations
  (0099-0100)" — migrations, downgrades, *Withdrawing a grant*, *Reading the
  reduction queue*; reading a company's channels through the API; E01-E18.
- `docs/BILLING.md`: `withdrawn` status, withdrawing a grant, the cause.
- `docs/BILLING_OPERATIONS.md`: the reduction queue, withdrawing a grant, new
  filters, what staff still cannot do.
- `docs/API.md`: the 7 routes, new parameters, the audit-log cursor; 209
  operations. `docs/AUTHORIZATION.md` §6.20. `docs/OBSERVABILITY.md`: the new
  metric and cause. `README.md`: migrations to 0100.

## 25. Decisions Ledger

| Item | Status |
| --- | --- |
| MIG-0091 | **CLOSED** |
| PO-02 | **CLOSED** |
| Survey: 0075, 0078, 0079, 0081 | **CLOSED** (fixed) |
| Survey: 0039, 0040 | **CLOSED BY DESIGN** (nothing below them refuses) |
| Survey: 0094 | **CLOSED** (`9695ec0`, before this stage) |
| PLAT-G1 | **CLOSED** |
| PLAT-G2 | **CLOSED** for reads; staff actions on a reduction **DEFERRED PRODUCT DECISION** |
| PLAT-G3 | **CLOSED** |
| PLAT-G4 | **CLOSED** |
| PLAT-G5 | **CLOSED** |
| PLAT-G6 | **CLOSED** |
| PLAT-G7 | **CLOSED** for `/topups`; unpaginated version and offer lists **ACCEPTED RESIDUAL** |
| PLAT-G8 | **CLOSED** |
| Shortening a grant to a future date | **DEFERRED PRODUCT DECISION** (not in scope; withdraw-now only) |

## 26. Admin-Frontend Readiness

All under `/api/v1/platform/billing` unless noted; every one platform staff,
audited.

| Staff screen | Routes | SQL / script / CLI still needed |
| --- | --- | --- |
| Plan catalogue | `GET/POST /plans`, `GET/PATCH /plans/{id}`, `…/activate`, `…/deactivate`, `DELETE /plans/{id}` (owner), `GET /features` (`topup_eligible`), `GET /channel-types` | none |
| Versions and preview | `GET/POST /plans/{id}/versions`, `POST …/versions/preview`, `POST …/migrations`, `GET /plan-versions/{id}`, `GET/POST /plan-versions/{id}/prices`, `GET /prices/{id}`, `POST /prices/{id}/retire`, `GET /subscriptions?plan_version_id=` | none |
| Custom plans and offers | `POST /tenants/{id}/custom-plan/preview`, `POST /tenants/{id}/custom-plan`, `GET/POST /tenants/{id}/custom-offers`, `POST /custom-offers/{id}/cancel` | none |
| Top-up products | `GET /topups?code=&search=&channel_type=…`, `POST /topups`, `GET/PATCH /topups/{id}`, activate / deactivate, `DELETE` (owner) | none |
| Grants | `POST /tenants/{id}/topups/grant`, `GET /topup-purchases?source=platform_grant&channel_type=…`, `GET /topup-purchases/{id}`, **`POST /topup-purchases/{id}/withdraw`**, `POST …/refund-review` | none |
| Reduction queue | **`GET /capacity-reductions`**, **`GET /capacity-reductions/{id}`**, **`GET /tenants/{id}/capacity-reductions`** | none (acting on a reduction is deliberately not offered) |
| Workspace channels | **`GET /tenants/{id}/channel-capacity`**, **`GET /tenants/{id}/channel-connections`**, `GET /tenants/{id}/summary` | none |
| Audit history | `GET /api/v1/platform/audit-logs?target_type=&target_id=&before_occurred_at=&before_id=` | none |

No entitlement operation a staff member performs needs SQL, a script or a CLI.

## 27. Remaining External Actions

Not performed; for the operator:

1. Review, then push and merge in order: **billing → omnichannel remediation →
   entitlements → this branch** (origin still lacks `aa11098`).
2. On any database ever downgraded across 0075, 0078, 0079, 0081 or 0091 and
   refused below, run `python -m scripts.db_preflight verify`; if it names a
   missing index, follow the RUNBOOK recovery.
3. Open entitlement items from the previous report: real prices on the six
   channel top-ups, the placeholder plan values, the capacity-reduction email
   wording, Paymob Live, real-provider verification per adapter.
4. Decide whether staff may act on a reduction (extend, shorten, resolve on the
   owner's behalf), and whether a grant may be shortened to a date.
5. Update `FRONTEND_MASTER_PLAN.md` (untracked in `E:\wasla`) with the staff
   screens of §26.

## 28. Final Verdict

**MIGRATION 0091 AND PLATFORM OPERATIONS CLOSED WITH EXTERNAL ACTIONS REMAINING**

Every condition of §38 holds: refused downgrades below 0091 (and below every
fixed migration) leave the database at its starting head with every index;
`db_preflight verify` names a missing or invalid index, proven on the damaged
state; the survey is complete with every inconsistent pattern fixed; grants
are withdrawn through the API and capacity shortfalls go through `boundary()`
with the grace, never disabling, releasing or deleting by themselves; staff
read the reduction queue and history and a workspace's channels exactly as it
does, without secrets; G4..G8 closed or accepted residual with a reason;
E16..E18 at 0, E01..E15 and the oracle at 0; every new route refuses tenant
roles and other workspaces' ids; all meaningful mutations killed (M-P08 proven
redundant) and the entitlements matrix 46/46; both suites green, reported
separately; no real provider contacted; nothing pushed, merged or deployed.

## Required Final Runtime Matrix

| Scenario | Before | After | Verdict |
| --- | --- | --- | --- |
| Downgrade refused at 0090 | stamp 0091, index gone, preflight ok | stamp at head (0100), index present and valid | PASS |
| Damaged database (pre-fix state) | preflight ok | preflight names the missing index, exit 1 | PASS |
| Withdraw a grant that still fits | SQL only | 200, stops counting, nothing opened | PASS |
| Withdraw a grant that no longer fits | SQL only | reduction `grant_withdrawn`, 7-day grace, 0 disabled | PASS |
| Grace passes after a withdrawal | — | newest disabled, oldest kept, 0 released, 0 deleted | PASS |
| Withdraw an AI-turn grant | SQL only | limit lowered, usage kept, next turn handed off, no provider call | PASS |
| Withdraw a paid purchase | — | 409 | PASS |
| Open reductions across workspaces | one summary per workspace | one list, ordered by grace end | PASS |
| Staff read a workspace's channels | totals only | same as the tenant sees, no secrets | PASS |
| Subscribers of one plan version | client-side filtering | `plan_version_id` filter | PASS |
| Channel vocabulary for a form | OpenAPI enum or hard-coded | `GET /channel-types` with `operable` | PASS |
| A product's change history | client-side matching | target filter with a stable cursor | PASS |

## Required Numbers

- MIG-0091: refused at 0090 — before stamp 0091 / index absent (and absent
  after `upgrade head`); after stamp 0100 / present, valid. Refused at 0081 —
  before 0091 / absent; after 0100 / present. Refused at 0069 — before 0075 /
  0075's, 0078's, 0079's, 0081's and 0091's indexes absent; after 0100 / all present.
- Survey: 7 migrations with an autocommit step in a downgrade; 5 fixed, 2 safe, 0 residual.
- Withdrawals (E2E): 5 grants withdrawn (4 slot, 1 AI-turn); 4 reductions
  opened; 1 disable after a grace, 1 by an owner; released 0; deleted 0.
- Races: 3 races, 6 racers, 6 decided; 1 created, 1 refused, 1 withdrawn + 1
  conflict; max active at a commit 2 against a capacity of 2.
- Invariants: E01..E18 all 0 (E2E, fresh head); oracle 168 comparisons / 0
  mismatches (suite), 12 / 0 (E2E).
- Mutations: 21 new — 20 killed, 1 survived (redundant guard), 21 restored;
  entitlements matrix 46 / 46 killed, 46 restored.
- Test lanes: model-built 6,800 collected / 6,782 passed / 0 failed / 18 skipped / 0 xfailed / 0 xpassed; migration-built 6,800 / 6,786 / 0 / 14 / 0 / 0.
- New platform operations: 7; documented operation count 209.
