# Wasla PostgreSQL Database Findings Remediation

| | |
|---|---|
| Audit | `DATABASE_AUDIT.md` (branch `database-audit-20260926`, commit `0b26ac2`) |
| Audit source | `worktree-billing-google-auth` @ `11cf44b9db405cbdc1f6019e650c26d17d78e448`, Alembic head `0073` |
| Remediation branch | `database-findings-remediation` (worktree `E:\wasla-database-remediation`) |
| REMEDIATION_BASE_HEAD | `11cf44b9db405cbdc1f6019e650c26d17d78e448` |
| Final code HEAD | `e15965f` (the report commit follows it) |
| Alembic | `0073` → **`0080`**, single head |
| Verdict | **DATABASE FINDINGS CLOSED WITH DEPLOYMENT VERIFICATION DEFERRED** |
| Score | 62 / 100 → **90 / 100** (measured; see "Score / readiness reassessment") |
| Merged / pushed | **No / no.** Production database untouched. Paymob Live not used. |

---

## 1. Executive summary

Every actionable finding of the audit was reproduced, fixed, covered by a permanent
test, and challenged by deliberately reintroducing it.

- **DB-001 (CRITICAL) is closed.** All money is settled through one primitive that
  locks payment → invoice → subscription → offer/top-up and decides on the re-read
  rows. The loser of a race is refused and held with a `duplicate_payment` incident.
  Deferred commit-time triggers (0074) make over-collection, and unexplained
  collected money, impossible even through direct SQL. There are 9 race scenarios ×
  10 real-connection runs each, with barriers and no sleeps, plus 10 runs with the
  application lock removed. Result: 0 double settlements, 0 over-collected invoices,
  0 deadlocks.
- **DB-002 (HIGH) is closed.** Four concurrently built indexes (0075) serve the
  purge's foreign-key actions. At the audit's 200k-message scale, deleting one
  workspace's 10,000 messages dropped from 6,507 ms to 417 ms. The real
  `WorkspacePurgeService` dropped from 8.5 s to 0.57 s, still correct: 27,006 rows
  purged, every financial row retained, other workspaces untouched.
- **All 8 MEDIUM findings are closed.** For DB-009, the repository side is closed; the
  production PITR verification is deferred to a deployment. The 12 LOW findings are
  closed, one of them (DB-021) as documented product policy. Of the 5 INFO findings,
  4 are closed and DB-022 is recorded as accepted architecture (ADR-115).
- **Direct SQL, as the runtime role:** every financial cross-tenant binding and every
  settled-history rewrite the audit's own injection suite attempts is now refused
  by PostgreSQL. The audit's 67 independent invariants, plus 17 new ones, show
  **0 violations** on a populated head database, on a populated 0073 database
  upgraded to 0080, on a restored copy, and after a large real purge.
- **Mutation campaign:** 33 pytest mutants plus 2 promtool mutants. Every meaningful
  mutant is killed. The campaign first exposed two gaps in this remediation's own
  tests (DBM-017b, DBM-025); both were fixed and the mutants re-run killed. One
  mutant is equivalent by design (DBM-006a).

**What remains is outside the repository.** A deployment must show its own WAL
archive restoring to a moment (the mechanism is drilled locally). A person holding
Paymob **Test** keys must repeat the hosted-checkout, custom-offer and replay
scenarios against the TX-split checkout (see "Remaining deployment verification"). Neither Test keys nor a way to
enter a card existed in this environment. Nothing here claims either was done.

---

## 2. Starting repository state

| Check | Result |
|---|---|
| `git fetch origin --prune` | done; canonical unchanged |
| `worktree-billing-google-auth` local | `11cf44b9db405cbdc1f6019e650c26d17d78e448` |
| `origin/worktree-billing-google-auth` | `11cf44b9db405cbdc1f6019e650c26d17d78e448` (no divergence) |
| Canonical tracked tree | clean; the nine untracked user reports (`AI_FINDINGS_REMEDIATION.md` … `WORKERS_QUEUES_FINDINGS_REMEDIATION.md`) were not touched |
| Remediation worktree | `E:\wasla-database-remediation`, branch `database-findings-remediation`, created at `11cf44b` |
| Isolated PostgreSQL | container `wasla_db_remed` (pgvector/pg16, PostgreSQL 16.15), port 55433, `max_connections=300`, synthetic credentials |
| Databases used | `wasla_db_remed_models`, `_migrations`, `_lane_a`, `_lane_b`, `_races`, `_challenge` (audit dataset at head), `_perfhead` (200k messages at head), `_upgrade` (populated 0073 → head), `_roundtrip`, `_restore_ns`, `_dbr020*`, `_perf`, `_purge*`, `_pitr`; never the developer DB or `wasla_e2e2` |
| Redis | databases 9–12 of the local Redis, never DB 0 |

## 3. Audit findings baseline

Audit verdict **NOT READY, 62/100**: CRITICAL 1, HIGH 1, MEDIUM 8, LOW 12, INFO 5.

Frozen reproductions were taken before production code changed, on the audit's
conditions, and kept as scratch evidence rather than committed:

| Finding | Reproduction at `11cf44b` |
|---|---|
| DB-001 | Two hosted pages paid concurrently (`B8`, barrier and no barrier, 3 runs each): `outcomes = applied, applied`, 2 succeeded payments, net 198.00 against `amount_due = amount_paid = 99.00`, **0 incidents**, 2 `payment_recorded` audits. The same with legacy manual ×2 (`B3`, `B3b`), callback vs legacy manual (`B4`), callback vs locked manual (`B6`). Sequential control: `applied, refused`, 1 incident. |
| DB-003 | `B2`/`B2b` (two transactions on one payment) and `B6b`: `asyncpg DeadlockDetectedError` (40P01). Server log: payments ↔ subscriptions cycle. |
| DB-017 | `B5` (manual ×2 with one free-text reference in two workspaces): raw `IntegrityError`, HTTP 500. |
| DB-002 | `EXPLAIN (ANALYZE)` of the purge sequence at 200k messages: `DELETE messages` 6,507 ms, of which `fk_campaign_recipients_message_id_messages` 5,259 ms over 10,000 calls and `fk_follow_ups_message_id_messages` 901 ms. |
| DB-004/005/012/018/025/006 | Audit injection suite `inject.sql`, 75 statements rolled back: X01, X05, X13, X14, Y12, Y13, Y21–Y28, Y34, Y39–Y41, Y44, Y45, Y50 **ACCEPTED**. |
| DB-006 | The new `test_database_ledger_privileges.py`, run against the base schema and privileges: 9 of 9 failed (runtime role could UPDATE and DELETE `audit_logs` and delete incidents). |
| DB-007, 008, 011, 013–016, 019–021, 023–027 | As recorded by the audit (settings `0`, transaction spanning Paymob, `payload NOT NULL` and no sweep, 201 statements for 200 members, …). Each fix below is additionally proved by a mutant that reintroduces the defect and is killed (see "Mutation results"). |

## 4. Architecture decisions

Recorded as **ADR-115** in `DECISIONS.md`.

1. **One settlement lock order**: payment → invoice → subscription → offer/top-up
   ledger. `InvoiceSettlement.lock` takes each row `FOR NO KEY UPDATE` with
   `populate_existing`. Every path uses it: Paymob callback, reconciliation, MOTO
   renewal, hosted renewal, custom offer, top-up, operator manual payment, the legacy
   `POST /platform/invoices/{id}/payments`, and void. `settle()` re-takes it
   defensively. There is one money-settlement primitive: the legacy route is a
   caller of it, not a second path.
2. **Backstop, not a unique index.** Invoices may legitimately hold several partial
   payments, so `UNIQUE(invoice_id) WHERE succeeded` would be wrong. Instead:
   - `payments.applied_at` records which money an invoice counts;
   - deferred constraint triggers require `invoices.amount_paid` to equal the net of
     applied payments, locking the invoice before summing;
   - every collected but unapplied payment must be held by an incident;
   - the existing `amount_paid <= amount_due` CHECK caps the total.

   Together: accepted money + held money = everything a provider collected, whether
   through the app or direct SQL. Refund and void keep working because they lower
   `amount_paid` together with the payment's `refunded_amount`.
3. **Manual references (DB-017).** A processor's own transaction id stays in
   `provider_reference` and stays globally unique, which is Paymob idempotency. An
   operator's free-text reference for money outside any processor goes to
   `payments.manual_reference`, unique per workspace and method. Such a payment's
   `provider_reference` is `manual:<payment id>`. Expected conflicts are translated
   by SQLSTATE only: `23505` → 409, and `40P01`/`40001`/`55P03`/`57014` → 503 with
   `Retry-After`. No SQL or constraint name reaches a customer.
4. **No transaction across a provider call (DB-008).** Checkout runs TX1 (commit
   invoice + pending attempt), then Paymob with the session released, then TX2
   (re-lock, bind order/page). A failed or ambiguous call leaves the attempt
   `PENDING` for hosted reconciliation and frees its idempotency key.
5. **Timeouts (DB-007).** Every application session gets `statement_timeout 30 s`,
   `lock_timeout 5 s` and `idle_in_transaction_session_timeout 60 s`, each
   configurable. The purge raises its own bounds with `SET LOCAL`. Alembic and
   `pg_dump` connections are not bounded by application settings.
6. **Evidence is append-only (DB-006).** The runtime role has SELECT/INSERT on
   `audit_logs`. On `billing_incidents` it has no DELETE and may UPDATE only the
   resolution columns. A trigger freezes incident evidence for every role.
7. **RLS not adopted (DB-022).** The system has a single application role. The
   defence is tenant repositories, composite tenant keys (now on every financial
   relation), billing triggers and runtime-role privileges.
8. **Tombstones (DB-021)** remain the existing policy, now written down and tested.
9. **Purge stays one transaction.** At the audit scale it now takes 0.57 s for
   27,006 rows. Batching would add a partial-purge state to reason about for no
   measured benefit; the purge's own `SET LOCAL` bounds cap it.
10. **Write amplification (DB-020)** is reduced by `fillfactor = 90`, not by moving
    inbox logic into the sequence trigger. The measurement is in section 19.

## 5. Migration map

| Rev | Purpose | Classes (online safety) | Downgrade |
|---|---|---|---|
| 0074 | DB-001/017 settlement backstop | column add (metadata); CHECK; partial unique index on the new, all-NULL `manual_reference` (built in the transaction: a SHARE lock for one scan of `payments`, a ledger-sized table); 2 deferred constraint triggers; **data backfill** of `applied_at`, then refuse if the ledger does not reconcile | drops triggers/columns |
| 0075 | DB-002 purge FK indexes | 4 indexes **CONCURRENTLY** in an autocommit block; INVALID leftovers dropped and rebuilt | drops concurrently |
| 0076 | DB-006 incident evidence | trigger (metadata) | drops trigger |
| 0077 | DB-004/005 financial integrity | 5 composite-target unique indexes **CONCURRENTLY**, attached as constraints; 10 composite FKs and CHECKs **NOT VALID → VALIDATE**; old single-column FKs dropped after; 2 BEFORE UPDATE + 2 deferred constraint triggers; preflight refuses and changes nothing | reverses |
| 0078 | DB-012/018/025 | 2 CHECKs **NOT VALID → VALIDATE**; 2 unique indexes **CONCURRENTLY**; preflight refuses | reverses |
| 0079 | DB-011 payload retention | `payload` DROP NOT NULL, column add, CHECK **NOT VALID → VALIDATE**, partial index **CONCURRENTLY** | refuses once anything is redacted |
| 0080 | DB-024 + DB-020 | `ALTER FUNCTION … SET search_path` ×10; `ALTER TABLE conversations SET (fillfactor=90)` (SHARE UPDATE EXCLUSIVE, writes continue) | resets |

None takes a long ACCESS EXCLUSIVE lock on a large table; the large message and
recipient tables are only ever indexed concurrently. Adding a NOT VALID FK takes
SHARE ROW EXCLUSIVE briefly with no scan, and VALIDATE takes SHARE UPDATE EXCLUSIVE.
No downtime is required. 0071/0072/0073 were changed only in their **downgrades**
(DB-019); upgrade semantics are untouched.

---

## 6. DB-001 remediation — concurrent double settlement (CRITICAL) — CLOSED

*Commit `3cd4184`.*
**Fix:** decisions 1–2 above. Money that arrives for a paid invoice becomes
`REFUSED` with a `duplicate_payment` incident, or `topup_duplicate_payment` on a
top-up invoice. Its payment row keeps `provider_reference` for operator refund
review.

**Permanent tests:**
- `test_settlement_concurrency.py`: 9 scenarios × `RUNS = 10`, real independent
  connections, `asyncio.Barrier` on even runs:
  - two hosted pages on one renewal;
  - two pages opened by the real checkout;
  - legacy manual ×2;
  - callback vs legacy manual;
  - callback vs operator manual;
  - callback vs reconciliation;
  - two transactions on one payment;
  - two pages of one custom offer;
  - two attempts on one top-up.

  Each run must end with: no racer failed (no deadlock, no 500); exactly one
  `applied`; one paid invoice with `amount_paid = price`; one applied payment; 0
  unreconciled, over-collected or unexplained-held; held = price × losers; **accepted
  + held = collected**; one `payment_recorded`; one offer activation; one top-up
  grant; one subscription restoration.
- `test_the_database_refuses_a_settlement_that_skipped_the_lock` removes the
  application lock and repeats the audit's reproduction 10 times. The backstop
  refuses the second commit (SQLSTATE 23000) in at least one run, and no run counts
  money twice.
- `test_settlement_backstop.py` (7) covers direct-SQL refusals, manual references and
  held refunds. `test_database_error_translation.py` (7) covers SQLSTATE → 409/503.

**Evidence at final HEAD:** see "Concurrency evidence".

## 7. DB-002 remediation — purge scaling (HIGH) — CLOSED

*Commit `ac19c7c`, migration 0075.* Indexes: `campaign_recipients(message_id) WHERE
message_id IS NOT NULL`, `campaign_recipients(conversation_id) WHERE … IS NOT NULL`,
`follow_ups(message_id) WHERE … IS NOT NULL` and `agent_turns(conversation_id)`. The
purge also deletes `campaign_recipients` and `follow_ups` before their parents.

Permanent tests in `test_workspace_purge_scale.py` (6) check that:
- each RI action's generic plan reads its index;
- every cascading or SET NULL key into `messages`/`conversations` is indexed;
- the delete order holds.

`test_workspace_purge.py` (9) checks correctness. Evidence: see "Purge performance before/after".

## 8. DB-003 remediation — lock-order deadlock (MEDIUM) — CLOSED

The same lock order as DB-001, with no reverse order anywhere. The audit's deadlock
scenarios are `two_transactions_on_one_payment` and `callback_vs_operator_manual`.
Across 90 locked race runs per lane at the final HEAD, the PostgreSQL deadlock
counter did not move (see "Concurrency evidence"). A bounded 40P01 retry was not added. The design does not
deadlock, and a deadlock that did occur would answer 503 with `Retry-After` (DB-017)
and fire `DatabaseDeadlocks`.

## 9. DB-004 remediation — financial composite tenant keys (MEDIUM) — CLOSED

*Commit `0186ca1`, migration 0077.* New composite keys:
- `payments(tenant_id, invoice_id)` → `invoices`, RESTRICT
- `payments(tenant_id, payment_method_id)` → `payment_methods`, SET NULL on the column only
- `invoices(tenant_id, subscription_id)` → `subscriptions`, SET NULL on the column
- `topup_purchases(tenant_id, invoice_id)` → `invoices`
- `topup_purchases(tenant_id, payment_id)` → `payments`
- `billing_incidents(tenant_id, invoice_id)` → `invoices`
- `billing_incidents(tenant_id, payment_id)` → `payments`
- `billing_adjustments(tenant_id, invoice_id)` → `invoices`
- `billing_adjustments(tenant_id, subscription_id)` → `subscriptions`
- `subscriptions(plan_id, plan_version_id)` → `plan_versions`, RESTRICT

A new CHECK requires an incident that names money to name a workspace. Rollout was
concurrent unique targets, then NOT VALID, then VALIDATE, then dropping the
superseded single-column keys. Preflight counted 0 violating rows on the populated
0073 database. Tests: `test_financial_integrity.py`, 5 cross-tenant tests.

## 10. DB-005 remediation — settled history immutable (MEDIUM) — CLOSED

*Commit `0186ca1`.* New constraints:
- `ck_invoices_paid_is_dated`, `ck_payments_collected_is_processed`;
- `invoices_history_immutable` and `payments_history_immutable` (BEFORE UPDATE,
  multi-row safe). Once an invoice is paid or void, its workspace, purpose, plan,
  version, offer, amount due, currency, lines and period are fixed. The only exits
  from `paid` are the documented reversals, which lower `amount_paid`. A void stays
  void. Collected money keeps its amount, currency, invoice and provider identity,
  and `refunded_amount` only rises;
- deferred `topup_purchases_grant_paid` (purchased → granted only on a paid invoice)
  and `custom_plan_offers_active_paid` (active only with a paid invoice of its own).

Refund, void and incident resolution still work: `test_a_refund_reopens_…`,
`test_a_goodwill_refund_…` and `test_an_operators_full_refund_voids_…`. All functions
pin `search_path` and leak no secret in their messages.

## 11. DB-006 remediation — append-only audit trail (MEDIUM) — CLOSED

*Commit `06d6162`, migration 0076, `scripts/provision_runtime_db_role.py`.* See
decision 6. The emergency authority remains with the migration identity.
`test_database_ledger_privileges.py` (9) checks, as the provisioned runtime role:
- INSERT and SELECT on `audit_logs` are allowed;
- UPDATE and DELETE on `audit_logs` are refused with 42501;
- DELETE on `billing_incidents` is refused with 42501;
- rewriting an incident's evidence is refused with 42501 for the runtime role and
  23000 for the owner;
- resolution works; a resolved incident stays resolved.

## 12. DB-007 remediation — timeouts (MEDIUM) — CLOSED

*Commit `06d6162`.* See decision 5. `test_database_timeouts.py` (6) checks, each
against generous one-sided bounds:
- the defaults reach every session;
- a blocked lock wait ends within its bound;
- an idle transaction is terminated;
- a runaway statement is cancelled;
- the purge raises its bounds for its transaction only;
- migration connections are unbounded.

Normal billing queries measured orders of magnitude under the bounds; the slowest hot
query was 45 ms (see "Query-plan evidence").

## 13. DB-008 remediation — checkout transaction boundary (MEDIUM) — CLOSED (Paymob Test re-run deferred)

*Commit `3ec0caa`.* See decision 4. Idempotency during the split:

| Case | Behaviour |
|---|---|
| crash after TX1, before Paymob | a committed `PENDING` attempt with no order id; hosted reconciliation asks Paymob about its reference; nothing else references it |
| Paymob succeeds, TX2 fails | the attempt is still `PENDING` with our reference; reconciliation finds the order by merchant order id and settles through the normal path |
| network timeout, ambiguous | the attempt is kept `PENDING` with a readable failure reason and its idempotency key released; the retry is a new attempt, and an old page paid later is reconciled and, for a paid invoice, held as a duplicate |
| two Accept & Pay | the offer is re-locked after the call; a decline or withdrawal that landed meanwhile answers 409; existing idempotency and settlement refusals handle the rest |

`test_checkout_transaction_boundary.py` (4) observes from a second connection
*during* the provider call. The attempt is committed, no session of ours is in a
transaction, and the offer row can be locked. A failed call keeps the attempt and
frees the retry, and a withdrawal during the call is refused. The provider under test
is the real `PaymobProvider` over an `httpx` mock transport. Real Paymob Test:
see "Remaining deployment verification".

## 14. DB-009 remediation — PITR / RPO / RTO (MEDIUM) — CLOSED in the repository; DEPLOYMENT VERIFICATION REQUIRED

*Commit `6ea586e`.* `docs/BACKUP.md` "Recovery objectives":

| Objective | Target |
|---|---|
| RPO | ≤ 5 min |
| RTO | ≤ 2 h up to 50 GB |
| WAL/PITR window | ≥ 7 days |
| Base backups | daily |
| Logical dump | daily, kept |
| Ownership | on-call runs the restore; the platform owner picks the target moment |
| Alerts | `BackupStale` 36 h, `BackupStatusMissing` |

The document gives both the self-hosted method (archive_command plus base backups,
WAL-G/pgBackRest) and the managed-provider equivalent. It warns against enabling
`archive_mode` without a working `archive_command`, which is why compose does not
enable it. It states that until a deployment has proved its own archive, the RPO is
the dump's ~24 h.

**Local PITR drill: PASS** (`scripts/pitr_drill.sh`, production image, head 0080):
data A, base backup, data B, target `2026-09-27 04:37:15.301768+00`, data C written
and A deleted, 5 WAL segments archived, server destroyed, recovery from base plus
archive. Result: `pitr-a,pitr-b`, head 0080, 0 unvalidated constraints; C absent,
and A's deletion not replayed. **Production PITR: NOT VERIFIED** (see "Remaining
deployment verification").

## 15. DB-010 remediation — pgvector prerequisite (LOW) — CLOSED

`scripts/db_preflight.py prerequisites` now runs before `alembic upgrade head` in
the `migrate` command. `restore_postgres.sh` makes the extensions present, or stops
with instructions, before `pg_restore`, then skips the dump's own extension entries.
`docs/BACKUP.md` "Restore prerequisites" covers the self-hosted superuser case and the
managed allow-list case.

| Proof | Result |
|---|---|
| `test_database_preflight.py`, non-superuser without pgvector | refused, and the check changes nothing |
| same, with pgvector pre-installed by a superuser | passes; `CREATE EXTENSION IF NOT EXISTS vector` is a no-op for the owner |
| real restore as `restore_owner LOGIN CREATEDB NOSUPERUSER`, no pgvector | **FAILED early**, with the instruction text, before `pg_restore` ran (the audit's failure mode, now explained) |
| the same restore after a superuser pre-installed the extensions (template1, removed afterwards) | **restored and verified**, head 0080 |
| control: the **base** `restore_postgres.sh` (`11cf44b`) under that same prerequisite | **FAILED**: `pg_restore` stopped at the dump's `COMMENT ON EXTENSION pgcrypto` (only the extension's owner may comment on it), which the new script skips once the extensions are present |

## 16. DB-011 remediation — raw webhook payload retention (MEDIUM) — CLOSED

*Commit `0205261`, migration 0079.* Processed events older than
`WHATSAPP_EVENT_PAYLOAD_RETENTION_DAYS` (default **30**, documented in
`.env.example` and `docs/WHATSAPP.md`) have their payload set to SQL NULL, with
`payload_redacted_at`. The row (id, event id, state, timestamps, workspace) stays,
so replays still deduplicate. Received and failed events keep their payload. A CHECK
forbids any other way for a payload to vanish. Metric:
`wasla_webhook_payload_retention_total{outcome=redacted|pending}`.

`test_webhook_payload_retention.py` (7) checks:
- new events keep their payload; old processed ones are redacted with identity intact;
- a redacted event still deduplicates Meta's retry;
- other workspaces age out alike; unprocessed events are untouched;
- the sweep reads its backlog by index, and batches drain;
- **the running worker redacts on every pass** (added after DBM-017b).

## 17. DB-012 remediation — period ordering (LOW) — CLOSED

*Commit `a368c0a`, migration 0078.*
- `ck_subscriptions_period_ordered`: `ended_at IS NOT NULL OR current_period_end >
  current_period_start`.
- `ck_invoices_period_ordered`: `period_end >= period_start`.

**Justified deviation:** an *ended* subscription is exempt. Ending one sets the
period end to the moment service stopped, which with renewals billed in advance can
precede or equal the start. Rejecting it would reject a legitimate state. Invoices
without a period are not affected, because the columns are NOT NULL and equality is
allowed.

## 18. DB-013 / DB-014 / DB-016 / DB-026 — CLOSED

*Commit `c7dcae1`.*

**DB-013.** Every timestamp `ORDER BY` has an `id` tie-breaker. That covers the billing
timeline, invoice timeline, offer history, WhatsApp events, member list, payment
methods, the sweeps and more. `tests/unit/test_deterministic_ordering.py` refuses any
new timestamp-only `order_by` in `app/`. Business priority orders are unchanged.

**DB-014.** `UserRepository.get_many` / `TenantRepository.get_many` (IN queries,
consistent with the no-relationships architecture). `test_roster_statement_counts.py`:
**200 members → 2 statements** (was 201); the workspace switcher is 2 statements.

**DB-016.** `app/db/advisory_locks.py` declares workspace creation `0x57415301`,
platform owners `0x57415302` and password reset `0x57415303`, now distinct. The unused
entitlement constant is removed; its one-key lock is a separate key space. The
misleading comments are corrected. `test_advisory_lock_namespaces.py` refuses
duplicates and literals declared elsewhere.

**DB-026.** `Plan.limit_for` docs now say: 0 is zero, missing or `null` is unlimited,
malformed (`"5"`, `5.5`, negative, boolean) is unlimited. Behaviour is unchanged
(ADR-113) and asserted by `tests/unit/test_limit_semantics.py` (8).

## 19. DB-015 / DB-017 / DB-018 / DB-019 / DB-020 / DB-024 / DB-025 / DB-027 — CLOSED

**DB-015** (`9886a3b`). Types whose production order is their history declare it
(`DATABASE_LABEL_ORDER`), and `create_all` builds them in that order. The
subscription count sorts in Python by `SubscriptionStatus`, not by enum order.
`test_enum_label_order.py` compares label **and** `enumsortorder` in both lanes. The
new catalog parity test compares enum order among everything else.

**DB-017** (`3cd4184`). Decision 3. `test_manual_references_repeat_across_workspaces_not_within_one`
and `test_a_processor_transaction_recorded_by_hand_stays_globally_unique` cover it.

**DB-018** (`a368c0a`). `uq_payment_methods_one_active_default` (partial unique,
built concurrently; preflight refuses existing duplicates, and the runbook says to
keep the newest and demote the rest). The card save locks the workspace's active cards
before deciding "first". A save that loses the unique race keeps its card as a
non-default. `make_default` clears and flushes before setting. `default_method()` is
deterministic. Races: 10 concurrent first saves × 10 runs, and 6 concurrent
`make_default` × 10 runs, always exactly one default. A revoked default makes room
for its replacement without a collision (the revoked-card case in the uniqueness test).

**DB-019** (`9886a3b`). The 0071/0072/0073 downgrades count and refuse:
- 0071: incidents, adjustments, plan-version migrations, versions after the first,
  scheduled changes;
- 0072: top-up invoices, purchases and grants, products, custom plans;
- 0073: all offers, and invoices naming an offer.

The runbook sections "Downgrading past the billing migrations" and "A migration
stopped half-way" document the autocommit-block partial state and its recovery
(confirm objects, `alembic stamp`, continue, verify). `test_migration_recovery.py`
reproduces a committed-but-unstamped 0073 (rerun fails "already exists"), recovers by
stamp, walks every guard refusal and ends at head with the gate clean.

**DB-020** (`9886a3b`, 0080). Measured with pgbench, 20,000 single-message
transactions over 500 conversations, each an insert plus the inbox touch:

| | fillfactor 100 (before) | fillfactor 90 (after) |
|---|---|---|
| conversation updates | 40,000 | 40,000 |
| HOT updates | 18,397 (46.0 %) | 19,721 (49.3 %) |
| sequence bumps that were HOT | 92.0 % | **98.6 %** |
| non-HOT sequence bumps | 1,603 | **279** (−83 %) |
| heap / index bytes | 647,168 / 2,023,424 | 565,248 / **1,884,160** (−7 % index) |

The correctness-critical sequence bump is kept; it touches no indexed column. The one
remaining index-touching write per message is the indexed `last_message_at` move,
which the inbox order needs. HOT can reach at most 50 % of updates by construction.
The audit's "0 HOT" came from a single-transaction workload; a realistic
multi-transaction load was already 46 %.

**DB-024** (0080). Ten pre-audit functions are pinned to `search_path = public,
pg_catalog`; the bodies are unchanged and SECURITY INVOKER is kept. New functions were
created pinned. `test_function_search_path.py` finds every own function in the
catalog.

**DB-025** (0078). `uq_users_email_lower` (unique `lower(email)`, tombstones
included, concurrent). 0 duplicates in preflight.

**DB-027** (`9886a3b`). `db_preflight verify` fails on any NOT VALID constraint,
INVALID or not-ready index, or disabled trigger. It runs in:
- CI (`tests` job, after the round trip);
- the `migrate` command, i.e. every deployment;
- `restore_postgres.sh`, as equivalent SQL.

## 20. DB-021 / DB-022 / DB-023

**DB-021 — PRODUCT POLICY DOCUMENTED** (`adc017f`). `docs/AUTH.md` states that
deletion is tombstoning, not erasure. Email and name are retained intentionally, the
`user_deleted` audit entry carries the address, and re-registration with the same
address in any case is not supported. Erasure would be a product and legal decision
with its own migration. `test_deletion_is_a_tombstone_not_an_erasure` checks one row,
the same id, address and name, `deleted_at` set, one `user_deleted` entry, and no
second account from `Tombstone@Example.com`.

**DB-022 — ACCEPTED ARCHITECTURE** (ADR-115 §5).

**DB-023 — CLOSED** (`6ea586e`). The API scrape publishes the following through its
own pool, with no exporter, no second credential and no extra connections:
- `wasla_db_connections{state}`, `wasla_db_max_connections`;
- `wasla_db_lock_waiting_sessions`, `wasla_db_oldest_transaction_age_seconds`;
- `wasla_db_deadlocks_total` (counter), `wasla_db_size_bytes`;
- `wasla_db_dead_tuples{table}` (5 fixed tables).

Production PostgreSQL logs statements over 1 s, lock waits and long autovacuums.

Alerts, each with firing and clearing promtool cases and a runbook entry:
`BackupStale` (the runbook's 36 h), `BackupStatusMissing`, `DatabaseDeadlocks`,
`DatabaseConnectionsNearLimit`, `DatabaseLockWaits`, `DatabaseLongTransaction` and
`DatabaseDeadTuplesHigh`. `test_database_health_metrics.py` (3) includes a real
session waiting on a lock being counted.

---

## Findings matrix DB-001 → DB-027

| ID | Sev | Status | Fix commit | Permanent proof | Reintroduction killed by |
|---|---|---|---|---|---|
| DB-001 | CRIT | **CLOSED** | 3cd4184 | 90 race runs + 10 no-lock runs; backstop tests | DBM-001…007 |
| DB-002 | HIGH | **CLOSED** | ac19c7c | purge scale plan tests; real purge | DBM-013 |
| DB-003 | MED | **CLOSED** | 3cd4184 | race matrix, 0 deadlocks | DBM-003 |
| DB-004 | MED | **CLOSED** | 0186ca1 | 5 cross-tenant tests; injections refused | DBM-008 |
| DB-005 | MED | **CLOSED** | 0186ca1 | history/grant/activation tests | DBM-009, 010, 011 |
| DB-006 | MED | **CLOSED** | 06d6162 | runtime role matrix | DBM-014 |
| DB-007 | MED | **CLOSED** | 06d6162 | timeout tests | DBM-015 |
| DB-008 | MED | **CLOSED** (real Paymob Test re-run deferred; see "Remaining deployment verification") | 3ec0caa | out-of-band observation during provider call | DBM-016 |
| DB-009 | MED | **CLOSED (repository) / DEPLOYMENT VERIFICATION REQUIRED** | 6ea586e | local PITR drill PASS | — (external) |
| DB-010 | LOW | **CLOSED** | 9886a3b, 6ea586e | non-superuser tests; real restore | DBM-026 |
| DB-011 | MED | **CLOSED** | 0205261, e15965f | retention tests | DBM-017, 017b |
| DB-012 | LOW | **CLOSED** | a368c0a | period tests; Y12, Y13 refused | DBM-029p |
| DB-013 | LOW | **CLOSED** | c7dcae1 | source-scanning ordering test | DBM-020 |
| DB-014 | LOW | **CLOSED** | c7dcae1 | statement counts | DBM-021 |
| DB-015 | LOW | **CLOSED** | 9886a3b | label+sortorder, catalog parity | DBM-018 |
| DB-016 | LOW | **CLOSED** | c7dcae1 | namespace test | DBM-019 |
| DB-017 | LOW | **CLOSED** | 3cd4184 | reference + translation tests | DBM-030r |
| DB-018 | LOW | **CLOSED** | a368c0a | uniqueness + 2 race tests | DBM-012 |
| DB-019 | LOW | **CLOSED** | 9886a3b | recovery + guard test | DBM-027 |
| DB-020 | LOW | **CLOSED (materially reduced, measured)** | 9886a3b, e15965f | fillfactor test | DBM-025, 025b |
| DB-021 | LOW | **PRODUCT POLICY DOCUMENTED** | adc017f | tombstone test | — |
| DB-022 | INFO | **ACCEPTED ARCHITECTURE** | adc017f | ADR-115 | — |
| DB-023 | LOW | **CLOSED** | 6ea586e | metrics tests; promtool | DBM-028, DBM-P1, DBM-P2 |
| DB-024 | INFO | **CLOSED** | 9886a3b | catalog search_path test | DBM-024 |
| DB-025 | INFO | **CLOSED** | a368c0a | case-variant test | DBM-022 |
| DB-026 | INFO | **CLOSED** | c7dcae1 | limit semantics test | — (documentation) |
| DB-027 | INFO | **CLOSED** | 9886a3b | gate test; CI/migrate/restore | DBM-023 |

## Direct-SQL challenge results

The audit's own `inject.sql` (75 statements, each in a subtransaction, all rolled
back) was run on the audit dataset rebuilt at head 0080. Deferred triggers were
forced `IMMEDIATE` so a rolled-back probe is still judged, and the run was made **as
the provisioned runtime role**, as the audit did. TEMP was granted to that role on
the scratch DB for the probe only and revoked afterwards.

| Probe | Baseline | Now | Refused by |
|---|---|---|---|
| X01 A payment → B invoice | ACCEPTED | **REFUSED** | 23503 `fk_payments_tenant_invoice` |
| X05 A top-up → B invoice | ACCEPTED | **REFUSED** | 23503 `fk_topup_purchases_tenant_invoice` |
| X13 A incident → B payment | ACCEPTED | **REFUSED** | 23503 `fk_billing_incidents_tenant_payment` |
| X14 A payment on B's saved card | ACCEPTED | **REFUSED** | 23503 `fk_payments_tenant_payment_method` |
| Y45 subscription → foreign plan version | ACCEPTED | **REFUSED** | 23503 `fk_subscriptions_plan_version_of_plan` |
| Y21 rewrite paid invoice amount | ACCEPTED | **REFUSED** | 23000 `invoices_history_immutable` |
| Y22 re-pin paid invoice version | ACCEPTED | **REFUSED** | 23000 `invoices_history_immutable` |
| Y23 rewrite paid invoice lines | ACCEPTED | **REFUSED** | 23000 `invoices_history_immutable` |
| Y24 rewrite succeeded payment amount | ACCEPTED | **REFUSED** | 23000 `payments_history_immutable` |
| Y25 reopen paid invoice | ACCEPTED | **REFUSED** | 23000 `invoices_collection_reconciles` (amount_paid must equal applied money) |
| Y26 paid without paid_at | ACCEPTED | **REFUSED** | 23000 `invoices_history_immutable` ("a paid invoice keeps when it was paid"); `ck_invoices_paid_is_dated` refuses it on any other row |
| Y27 succeeded without processed_at | ACCEPTED | **REFUSED** | 23000 `payments_history_immutable`; `ck_payments_collected_is_processed` refuses it on insert |
| Y35 grant unpaid top-up | REFUSED (by an unrelated unique) | **REFUSED** | 23000 `topup_purchases_grant_paid` |
| Y34 activate declined offer | ACCEPTED | **REFUSED** | 23000 `custom_plan_offers_active_paid` |
| Y28 two active default cards | ACCEPTED | **REFUSED** | 23505 `uq_payment_methods_one_active_default` |
| Y12 reversed subscription period | ACCEPTED | **REFUSED** | 23514 `ck_subscriptions_period_ordered` |
| Y13 reversed invoice period | ACCEPTED | **REFUSED** | 23000 `invoices_history_immutable` (probe targets a paid invoice; `ck_invoices_period_ordered` refuses it on an open one, `test_an_invoice_period_cannot_run_backwards`) |
| Y44 case-variant duplicate email | ACCEPTED | **REFUSED** | 23505 `uq_users_email_lower` |
| Y50 second succeeded payment on paid invoice | ACCEPTED | **REFUSED** | 23000 `payments_collection_reconciles` |
| Y39 delete audit row (runtime) | ACCEPTED | **REFUSED** | 42501 permission denied |
| Y40 rewrite audit row (runtime) | ACCEPTED | **REFUSED** | 42501 permission denied |
| Y41 delete billing incident (runtime) | ACCEPTED | **REFUSED** | 42501 permission denied |

**Totals as runtime role:** 75 probes. **0 financial** and 0 audit-trail probes are
accepted. Still accepted, and unchanged:
- **X10–X12, X15–X20, X23–X25** (12 non-financial cross-workspace bindings:
  campaigns, media, leads, follow-ups, tools, analytics, templates, events). The
  audit classified these as an ADR-100 application-enforced choice outside DB-004;
  the audit found no writer that can produce them.
- **Y29** (plaintext-shaped card token; noted as INFO evidence under DB-018, whose
  remediation is the default-card index; every stored token is sealed, invariant DB-F11 = 0).
- **Y30** (negative `collection_attempts`; no finding).

These are recorded as residual INFO, not as regressions.

## Concurrency evidence

Recorded by `test_settlement_concurrency.py` itself (`WASLA_RACE_EVIDENCE`) at
`e15965f`, in both schema lanes. Each run is two real, independent connections
settling one invoice, released together by an `asyncio.Barrier` on even runs.
`backstop_without_lock` is the audit's reproduction with the application lock
removed; its `EXC` outcomes are the database refusing the second commit with
SQLSTATE 23000, which is the intended result.

**Model-built schema** (final whole-suite run, `wasla_db_remed_models`)

| Scenario | Runs (barrier) | Outcomes | Applied | Held/refused | Duplicate incidents | Over-collected | Unreconciled | Accepted + held = collected |
|---|---|---|---|---|---|---|---|---|
| two_hosted_pages_one_renewal | 10 (5) | applied 10, refused 10 | 10 | 990.00 | 10 | 0 | 0 | yes |
| two_pages_opened_by_the_real_checkout | 10 (5) | applied 10, refused 10 | 10 | 990.00 | 10 | 0 | 0 | yes |
| legacy_manual_twice | 10 (5) | applied 10, refused:ConflictError 10 | 10 | 0 | 0 | 0 | 0 | yes |
| callback_vs_legacy_manual | 10 (5) | applied 10, refused 5, refused:ConflictError 5 | 10 | 495.00 | 5 | 0 | 0 | yes |
| callback_vs_operator_manual | 10 (5) | applied 10, refused 10 | 10 | 990.00 | 10 | 0 | 0 | yes |
| callback_vs_reconciliation | 10 (5) | applied 10, reconciled:still_pending 5, refused 5 | 10 | 990.00 | 10 | 0 | 0 | yes |
| two_transactions_on_one_payment | 10 (5) | applied 10, refused 10 | 10 | 0 | 10 | 0 | 0 | yes |
| two_pages_of_one_custom_offer | 10 (5) | applied 10, refused 10 | 10 | 990.00 | 10 | 0 | 0 | yes |
| two_attempts_on_one_topup | 10 (5) | applied 10, refused 10 | 10 | 990.00 | 10 | 0 | 0 | yes |
| backstop_without_lock | 10 (10) | EXC 10, applied 10 | 10 | 0 | 0 | 0 | 0 | yes |

**Migration-built schema** (targeted run, `wasla_db_remed_lane_a`)

| Scenario | Runs (barrier) | Outcomes | Applied | Held/refused | Duplicate incidents | Over-collected | Unreconciled | Accepted + held = collected |
|---|---|---|---|---|---|---|---|---|
| two_hosted_pages_one_renewal | 10 (5) | applied 10, refused 10 | 10 | 990.00 | 10 | 0 | 0 | yes |
| two_pages_opened_by_the_real_checkout | 10 (5) | applied 10, refused 10 | 10 | 990.00 | 10 | 0 | 0 | yes |
| legacy_manual_twice | 10 (5) | applied 10, refused:ConflictError 10 | 10 | 0 | 0 | 0 | 0 | yes |
| callback_vs_legacy_manual | 10 (5) | applied 10, refused 6, refused:ConflictError 4 | 10 | 594.00 | 6 | 0 | 0 | yes |
| callback_vs_operator_manual | 10 (5) | applied 10, refused 10 | 10 | 990.00 | 10 | 0 | 0 | yes |
| callback_vs_reconciliation | 10 (5) | applied 10, reconciled:still_pending 4, refused 6 | 10 | 990.00 | 10 | 0 | 0 | yes |
| two_transactions_on_one_payment | 10 (5) | applied 10, refused 10 | 10 | 0 | 10 | 0 | 0 | yes |
| two_pages_of_one_custom_offer | 10 (5) | applied 10, refused 10 | 10 | 990.00 | 10 | 0 | 0 | yes |
| two_attempts_on_one_topup | 10 (5) | applied 10, refused 10 | 10 | 990.00 | 10 | 0 | 0 | yes |
| backstop_without_lock | 10 (10) | EXC 10, applied 10 | 10 | 0 | 0 | 0 | 0 | yes |

| Totals (both lanes) | |
|---|---|
| Race scenarios | 9 (+ the no-lock backstop) |
| Runs | 200 (100 per lane: 90 locked + 10 without the lock) |
| Concurrent settlement attempts on distinct payments or transactions | 400 |
| Settlements applied | 200 (exactly one per run) |
| Double settlements | **0** |
| Over-collected invoices | **0** |
| Unreconciled invoices | **0** |
| Runs where accepted + held ≠ collected | **0** |
| Duplicate-payment incidents raised for held money | 151 (unexplained held money in every run: 0) |
| Racers failing with an error instead of a refusal (locked scenarios) | **0** |
| Deadlocks (PostgreSQL `pg_stat_database.deadlocks`) during the final runs | **0**: `wasla_db_remed_lane_a` 0 → 0; cluster total 5 → 5 across the whole model-built suite |

**Every deadlock the server logged in this remediation, attributed** from the
PostgreSQL log (`log_lock_waits`, `deadlock_timeout=1s`):

| When (UTC) | Count | What |
|---|---|---|
| 2026-09-26 17:54–17:55 | 3 | baseline reproduction at `11cf44b`: `UPDATE subscriptions` ↔ `UPDATE payments` / `UPDATE invoices` - the audit's DB-003 cycle |
| 2026-09-26 20:48 | 1 | test harness: `DROP SCHEMA public CASCADE` (schema reset) against a session a killed earlier run left inserting into `email_messages`; not settlement |
| 2026-09-27 05:05, 05:14 | 4 | the mutation campaigns (two runs), on `lane_b`: two per run from the lock mutants (DBM-001…003: lock removed, stale read, order reversed) deadlocking on `subscriptions`/`invoices` `FOR NO KEY UPDATE` - the kill working as intended |
| final runs at `e15965f` | **0** | |

The audit's baseline for the same shapes: B8 ×6 and B3/B4/B6 all `applied, applied`,
net 198.00 on a 99.00 invoice, 0 incidents; B2/B2b/B6b deadlocked (40P01).

Other races (permanent): default card, 10 concurrent first saves × 10 runs and 6
concurrent `make_default` × 10 runs, one default every time; top-up grant,
`test_topup_concurrency.py`, granted once.

## Financial invariant ledger

The audit's **67** independent SQL checks (`invariants.sql`) plus **17** new ones
(`DBR-01…17`) give 84 checks, 0 violations on every populated database:

| Database | Checks | Violations |
|---|---|---|
| audit dataset built at head (`_challenge`) | 84 | **0** |
| audit dataset at 0073 upgraded to 0080 (`_upgrade`; 265/265 payments backfilled `applied_at`) | 84 | **0** |
| 200k-message dataset at head, after a real purge (`_perfhead`) | 84 | **0** |
| restored copy of `_challenge` | 84 | 0 financial; `DB-T16` = 5, identical in the source (the audit entitlement script's `edge-*` fixtures, inserted without owners after the first ledger run) |

The new checks cover:
- invoice period order;
- paid ⇒ `paid_at`, collected ⇒ `processed_at`;
- `amount_paid` = applied net, and unapplied collected money held by an incident;
- `amount_paid ≤ amount_due`;
- granted purchase ⇒ paid invoice, active offer ⇒ paid invoice;
- top-up ↔ payment, payment ↔ card, adjustment ↔ invoice/subscription and
  invoice ↔ subscription tenant coherence;
- one account per address ignoring case;
- live period order;
- no NOT VALID constraint; every own function pinned;
- a redacted payload has a redaction time.

## Tenant isolation matrix

| Relation family | Before | After |
|---|---|---|
| payments ↔ invoices / cards | app only (X01, X14 accepted) | **composite FKs** (refused) |
| invoices ↔ subscriptions | app only | **composite FK** |
| top-ups ↔ invoices / payments | app only (X05) | **composite FKs** |
| incidents ↔ invoices / payments | app only (X13) | **composite FKs** + CHECK |
| adjustments ↔ invoices / subscriptions | app only | **composite FKs** |
| subscription ↔ plan version | app only (Y45) | **composite FK** |
| custom plans / versions / products / offers | triggers | triggers (unchanged) |
| messaging, knowledge | composite FKs | unchanged |
| campaigns, AI/tools, WhatsApp templates/events, lead assignee | app only (ADR-100) | unchanged; residual INFO |

## Purge performance before/after

200k-message audit dataset (20 workspaces; the target holds 10,000 messages, 500
conversations and 500 recipients):

| Measure | Before (0073) | After (0080) | Ratio |
|---|---|---|---|
| `fk_campaign_recipients_message_id_messages` | 5,259 ms / 10,000 calls | 46 ms | **114×** |
| `fk_follow_ups_message_id_messages` | 901 ms | 48 ms | 19× |
| `fk_campaign_recipients_conversation_id_conversations` | 266 ms / 500 | 3.1 ms | 86× |
| `fk_agent_turns_tenant_conversation` | 125 ms / 500 | 9.9 ms | 13× |
| `DELETE messages` statement | 6,507 ms | 417 ms | 16× |
| real `WorkspacePurgeService.purge` | 8.5 s (audit scale, before DB-002) | **0.567 s** (26,506 deleted) | 15× |

The after timings were taken while the mutation campaign loaded the same server.

**Plans (primary gate):** each RI action statement is an **Index Scan**:
`ix_campaign_recipients_message_id`, `ix_campaign_recipients_conversation_id`,
`ix_follow_ups_message_id`, and a Bitmap Index Scan on
`ix_agent_turns_conversation_id`. No sequential RI lookup per deleted message
remains; the sequential scans left in the purge are on tables of 0–1 rows.

**Correctness:** 27 PURGED tables at 0 for the target and 12 RETAINED tables
unchanged: invoices 62, payments 62, subscription 1, custom plan 1, offers 2, top-up
purchases 3, audit 2,500, memberships 200. Every other workspace is unchanged except
the designed +1 tenant-less `workspace_purged` audit row. 0 unclassified tables.
Invariants after: 0.

## Checkout transaction evidence

Section 13. From a second connection while `create_checkout` is in flight: the
attempt row is visible (committed), `pg_stat_activity` shows no session of ours
`idle in transaction`, and `SELECT … FROM custom_plan_offers … FOR UPDATE NOWAIT`
succeeds. DBM-016 (the provider call inside the transaction again) fails the first
of these.

## Runtime role matrix

| Operation (as provisioned runtime role) | Before | After |
|---|---|---|
| INSERT / SELECT `audit_logs` | allowed | allowed |
| UPDATE / DELETE / TRUNCATE `audit_logs` | **allowed** | refused 42501 |
| DELETE `billing_incidents` | **allowed** | refused 42501 |
| UPDATE incident evidence columns | **allowed** | refused 42501 (column privilege) and 23000 (trigger, any role) |
| UPDATE incident resolution columns | allowed | allowed |
| DDL, TEMP, CREATE, extensions, `session_replication_role` | refused | refused (unchanged) |
| sessions | unbounded | 30 s / 5 s / 60 s bounds |

## Retention behavior

Section 16. Webhook payloads: 30-day default, processed events only, identity kept,
dedup intact, batched, measured. User rows: tombstone policy (DB-021). Workspace data:
purge (DB-002).

## Backup/restore/PITR evidence

| Step | Result |
|---|---|
| Backup (`backup_postgres.sh` as the runtime role) | dump staged and read back, 9,462,977 bytes; upload stage refused with `BACKUP_DESTINATION=none` as designed (status `failure`/`upload`, `last_success_at` unchanged); off-host upload was proved by earlier drills |
| Restore, non-superuser, no pgvector | **refused early with instructions** |
| Restore, non-superuser, pgvector present | **PASS**: 49 tables, pgcrypto+vector, head 0080 (`WASLA_EXPECTED_HEAD` matched), rows, "constraints validated, indexes valid, triggers enabled" |
| Row counts + financial and content hashes (invoices, payments, top-ups, messages, embeddings, audit) | 56 fingerprint lines, **0 differences** |
| Triggers / constraints / indexes / functions | 18 / 279 / 266 / 16 in both |
| Catalog diff source ↔ restored | `schemas identical` |
| Invariants on restored | as source |
| `alembic current` / `alembic check` on restored | `0080 (head)` / no new operations |
| PITR local drill | **PASS** (section 14) |
| Production PITR | **NOT VERIFIED** |

## Query-plan evidence

The audit's 22 hot query shapes (`explain.sql`) on the 200k-message dataset at head,
after `VACUUM ANALYZE`, with the audit's thresholds (<50 ms healthy):

| Query | Baseline | Now | Plan |
|---|---|---|---|
| Q01 inbox | 0.62 ms | 1.52 ms | same index |
| Q02 messages by sequence | 0.59 | 0.21 | same |
| Q03 usage totals (every AI turn) | 0.89 | 0.80 | same index-only |
| Q04 usage all meters | 3.91 | 2.98 | same |
| Q07 billing timeline | 2.04 | 5.63 | same index |
| Q12 status by wamid | 0.05 | 0.18 | same |
| **Q18 vector search** | 14.9 | **16.0** | same (tenant-filtered, exact path) |
| Q19 analytics handoffs | 1.12 | 0.40 | **better**: index-only on the covering index |
| **Q20 platform usage** | 36.8 | **45.4** | same parallel seq scan |
| Q21 **member roster** | 0.19 | 0.51 | same; the service now issues **2 statements for 200 members** (was 201) |
| all others | ≤ 0.83 | ≤ 1.13 | same |

All 22 queries remain healthy with the same plans. The differences are single-digit
milliseconds on a server shared with other runs: **no meaningful regression**.

## Mutation results

Worktree `E:\wasla-dbr-mut` at `e15965f`. Each mutant is one or more exact-once
source edits. Its killer tests run against a model-built schema on
`wasla_db_remed_lane_b`. Every file is restored with `git checkout`, and the runner
asserts `git status --porcelain` is empty before and after each mutant (byte-for-byte
restoration). An unmutated control run of all killer tests passed first (104 tests).

| ID | Mutation | Outcome | Killed by |
|---|---|---|---|
| DBM-001 | remove the invoice FOR UPDATE | **KILLED** | `test_settlement_concurrency.py::test_two_settlements_of_one_invoice_apply_exactly_once[callback_vs_operator_manual]` |
| DBM-002 | decide on the stale invoice (no re-read under the lock) | **KILLED** | `test_settlement_concurrency.py::test_two_settlements_of_one_invoice_apply_exactly_once[legacy_manual_twice]` |
| DBM-003 | reverse the lock order: subscription before payment and invoice | **KILLED** | `test_settlement_concurrency.py::test_two_settlements_of_one_invoice_apply_exactly_once[callback_vs_operator_manual]` |
| DBM-004 | let a second succeeded payment apply | **KILLED** | `test_settlement_backstop.py::test_refunding_held_money_leaves_the_paid_invoice_alone` |
| DBM-005 | refuse the duplicate without raising an incident | **KILLED** | `test_settlement_concurrency.py::test_two_settlements_of_one_invoice_apply_exactly_once[two_hosted_pages_one_renewal]` |
| DBM-006 | restore the legacy unlocked manual settlement (no pre-lock, no re-lock) | **KILLED** | `test_settlement_concurrency.py::test_two_settlements_of_one_invoice_apply_exactly_once[legacy_manual_twice]` |
| DBM-006a | manual path only: drop the pre-lock, keep settle's re-lock | EQUIVALENT | — |
| DBM-007 | remove the over-collection database backstop | **KILLED** | `test_settlement_concurrency.py::test_the_database_refuses_a_settlement_that_skipped_the_lock` |
| DBM-008 | remove the payment -> invoice tenant composite key | **KILLED** | `test_financial_integrity.py::test_a_payment_cannot_collect_another_workspaces_invoice` |
| DBM-009 | allow a paid invoice's terms to be rewritten | **KILLED** | `test_financial_integrity.py::test_a_paid_invoice_keeps_its_terms[amount_due = 1.00]` |
| DBM-010 | grant a purchased top-up on an unpaid invoice | **KILLED** | `test_financial_integrity.py::test_a_topup_is_not_granted_on_an_unpaid_invoice` |
| DBM-011 | activate an offer without a paid invoice | **KILLED** | `test_financial_integrity.py::test_a_declined_offer_cannot_be_made_active` |
| DBM-012 | allow two active default cards | **KILLED** | `test_period_card_email_invariants.py::test_a_workspace_has_one_active_default_card` |
| DBM-013 | remove one purge foreign-key index (campaign_recipients.message_id) | **KILLED** | `test_workspace_purge_scale.py::test_each_purge_foreign_key_action_reads_an_index[ix_campaign_recipients_message_id]` |
| DBM-014 | leave UPDATE/DELETE on audit_logs to the runtime role | **KILLED** | `test_database_ledger_privileges.py::test_the_runtime_role_appends_to_the_audit_trail_and_nothing_else` |
| DBM-015 | remove the runtime lock_timeout | **KILLED** | `test_database_timeouts.py::test_the_default_bounds_reach_every_application_session` |
| DBM-016 | hold the checkout transaction across the provider call | **KILLED** | `test_checkout_transaction_boundary.py::test_the_provider_is_asked_with_no_transaction_open` |
| DBM-017 | keep the raw payload when 'redacting' | **KILLED** | `test_webhook_payload_retention.py::test_only_old_processed_payloads_are_cleared` |
| DBM-017b | the retention worker never redacts | **KILLED** | `test_webhook_payload_retention.py::test_the_running_worker_redacts_on_every_pass` |
| DBM-018 | build subscription_status in class order (enum-order parity) | **KILLED** | `test_enum_label_order.py::test_the_two_drifted_types_keep_their_production_order` |
| DBM-019 | reuse the platform-owner advisory namespace for password resets | **KILLED** | `test_advisory_lock_namespaces.py::test_every_namespace_is_distinct` |
| DBM-020 | drop the id tie-breaker from the billing timeline | **KILLED** | `test_deterministic_ordering.py::test_every_timestamp_ordering_has_a_tie_breaker` |
| DBM-021 | reintroduce the per-member user lookup | **KILLED** | `test_roster_statement_counts.py::test_the_roster_is_two_statements_for_two_hundred_members` |
| DBM-022 | remove the lower(email) unique index | **KILLED** | `test_period_card_email_invariants.py::test_an_address_belongs_to_one_account_whatever_its_case[False]` |
| DBM-023 | disable the post-migration constraint validation gate | **KILLED** | `test_database_preflight.py::test_an_unvalidated_constraint_an_invalid_index_and_a_disabled_trigger_fail` |
| DBM-024 | unpin one trigger function's search_path | **KILLED** | `test_function_search_path.py::test_every_function_pins_its_search_path` |
| DBM-025 | drop the conversations fillfactor | **KILLED** | `test_function_search_path.py::test_conversations_leave_room_for_hot_updates` |
| DBM-025b | never set the conversations fillfactor | **KILLED** | `test_function_search_path.py::test_conversations_leave_room_for_hot_updates` |
| DBM-026 | the pgvector prerequisite check always passes | **KILLED** | `test_database_preflight.py::test_a_non_superuser_without_pgvector_is_refused_before_migrating` |
| DBM-027 | drop the 0073 downgrade guard | **KILLED** | `test_migration_recovery.py::test_a_half_applied_migration_recovers_and_downgrades_keep_the_ledger` |
| DBM-028 | stop counting sessions waiting on locks | **KILLED** | `test_database_health_metrics.py::test_a_session_waiting_on_a_lock_is_counted` |
| DBM-029p | weaken the subscription period CHECK to nothing | **KILLED** | `test_period_card_email_invariants.py::test_a_subscription_period_cannot_run_backwards` |
| DBM-030r | store an operator's free-text reference as the global provider reference | **KILLED** | `test_settlement_backstop.py::test_manual_references_repeat_across_workspaces_not_within_one` |
| DBM-P1 | `BackupStale` threshold 36 h → 72 h (promtool) | **KILLED** | `alerts_test.yml`: "a backup older than 36 hours fires after fifteen minutes" |
| DBM-P2 | `BackupStatusMissing` made unable to fire (promtool) | **KILLED** | `alerts_test.yml`: "a missing backup age fires after two hours" |

| | |
|---|---|
| Applied | **35** (33 pytest + 2 promtool) |
| Killed | **34** |
| Equivalent | **1** (DBM-006a) |
| Survived | **0** |
| Meaningful survivors | **0** |

Notes:

- **DBM-006a is equivalent by design.** Without the manual path's pre-lock,
  `settle()` still takes the settlement locks and re-reads before deciding, so the
  refusal and the ledger are identical; only a doomed payment row is flushed before
  the 409 rolls it back. Removing both locks (DBM-006, the legacy path restored) is
  killed.
- **The first campaign found two real gaps, since fixed** (`e15965f`).
  **DBM-017b** survived because the retention loop's call to the redaction was never
  exercised; a test now runs `run_forever`. **DBM-025** survived because the
  fillfactor test compared the table with the model's own constant; it now requires
  the literal 90. Both were re-run and killed.
- **DBM-018** was first written as deleting the declaration, which killed only by an
  import error. It was redefined as the meaningful change, the declared order set to
  class order with the mechanism intact, and is killed by the production-order test.
- The audit's named mutants map as follows: DBM-001…012 → DBM-001…012;
  DBM-013 purge index; DBM-014 audit UPDATE; DBM-015 lock_timeout; DBM-016 TX across
  provider; DBM-017 payload retention; DBM-018 enum order; DBM-019 advisory
  namespace; DBM-020 ORDER BY id; DBM-021 N+1; DBM-022 lower(email); DBM-023
  validation gate. DBM-024 onwards, DBM-029p, DBM-030r and DBM-P1/P2 are additional.

## Whole test gates

All at `e15965f`, in the verification worktree `E:\wasla-dbr-verify`, whose
tracked files were checked hash-for-hash against the commit.

| Lane | Command | Database | Collected | Passed | Failed | Skipped | Duration |
|---|---|---|---|---|---|---|---|
| **Model-built, whole `tests/`** | `pytest -q -rs tests` | `wasla_db_remed_models` | 5,973 | **5,840** | **0** | 133 | 23 m 59 s |
| **Migration-built, integration + e2e** | `pytest -q -rs tests/integration tests/e2e` with CI's two `--deselect`s | `wasla_db_remed_migrations` | 2,923 (+2 deselected) | **2,856** | **0** | 67 | 22 m 34 s |
| **Kept-data AI/tool sweep** | `WASLA_TEST_KEEP_AI_DATA=1`, the 22 `ai_harness` suites + both invariant files | same | 218 | **218** | **0** | **0** | |
| **Targeted DB remediation suite** (25 files) | migration-built | `wasla_db_remed_lane_a` | 168 | 160 (+8 shell-script tests run separately with `sh`: pass) | **0** | 8 → 0 | 2 m |

**Skips, all environmental and all on CI's allow-list or provisioned there:**

| Reason | Model-built | Migration-built |
|---|---|---|
| no object store (`TEST_S3_ENDPOINT_URL`); CI starts MinIO | 67 | 67 |
| no POSIX shell / `sh`+`curl` for the operational and readiness scripts (Windows host); CI has them, and the backup/restore script tests were run here with Git's `sh`: 52 passed | 48 | 0 (`sh` on PATH) |
| real-provider tests opt-in (`OPENAI_API_KEY`) | 11 | — |
| schema parity only against a migration-built DB (runs in that lane: passed) | 4 | 0 |
| tmpfs cleanup test needs `WASLA_TEST_TMPFS` (CI provisions it) | 1 | — |
| kept-data sweep checks (run in their own session above: passed) | 2 | deselected, as CI does |

No new skip was added or allow-listed by this remediation. Every new
database test runs in both lanes, except `test_schema_parity.py`, which runs in the
migration lane by design. The earlier full run of the migration lane, before the
README range was bumped to 0080, had a single failure,
`test_the_readme_migration_range_ends_at_the_current_head`. That test is the reason
the range is bumped, and it passes at HEAD.

## CI-equivalent gates

All gates below were run at `e15965f` with the commands CI runs.

| Gate | Command | Result |
|---|---|---|
| Ruff | `ruff check .` | **PASS** (all checks passed) |
| Black | `black --check .` | **PASS** (756 files unchanged) |
| MyPy | `mypy app tests` | **PASS** (no issues in 670 source files) |
| Application factory | `create_app()` | **PASS** |
| Single head | `alembic heads` | **PASS** `0080 (head)` |
| Round trip, empty DB | `upgrade head` → `downgrade 0073` → `upgrade head` → `downgrade base` → `upgrade head` | **PASS** (each rc 0) |
| Populated upgrade | audit dataset at `0073` → `upgrade head` (2.6 s) | **PASS**; 84 invariants 0; gate ok |
| Drift | `alembic check` (empty, populated-upgraded and restored DBs) | **PASS** ("No new upgrade operations detected") |
| Constraint gate (DB-027) | `python -m scripts.db_preflight verify` | **PASS** on every migrated, upgraded and restored DB |
| Model/migration parity | `test_schema_parity.py` incl. the catalog diff (columns, defaults, constraints, indexes, triggers, function bodies and settings, enum order, storage) | **PASS** (migration-built lane) |
| Prometheus config and rules | `promtool check config` / `check rules` | **PASS** (60 rules) |
| Alert unit tests | `promtool test rules deploy/monitoring/tests/alerts_test.yml` | **PASS** (incl. 8 new database cases) |
| Intermediate commits | each of `3ec0caa…adc017f` checked out alone: ruff, black, mypy, factory import, heads | **PASS** (all 8) |
| Docker runtime image | `docker build --target runtime`, then `ENVIRONMENT=staging` with a per-run `JWT_SECRET`, `GET /health/live` | **PASS** (`{"status":"alive"}`; the image carries `db_preflight.py`, `schema_catalog.py` and the new `migrate` steps) |
| Docker backup image | `docker build -f Dockerfile.backup` | **PASS** (carries the new `restore_postgres.sh`) |
| Shell-script suites | `test_backup_scripts.py`, `test_backup_upload.py` with a POSIX `sh` on PATH | **PASS** (52 passed, 0 skipped) |

**Connection budget** (`docs/DEPLOYMENT.md`):
- API 2 × 15, worker 2 × 15;
- migrate 1, backup 1, ops ~3;
- DB health gauges 0 extra;
- **~65 of 97 usable** (default `max_connections = 100`).

No exporter was added.

**Secret hygiene.** The 8,704 lines added since `11cf44b` were scanned for Paymob
live/test secret and public keys, JWTs, Meta tokens, AWS keys, Google OAuth secrets,
private keys, and database URLs with inline passwords. The only hits are the synthetic
fixtures `egy_sk_test_boundary`, `egy_pk_test_boundary` and `sk_test_backstop`; the
drill's URL takes a synthetic password from a variable. Logs, dumps and scratch
evidence stayed outside the repository.

**Billing calendar:** 1,160,000 property checks against an independent oracle, 0
violations (unchanged). **Entitlements:** the independent SQL recomputation vs
`EntitlementService` compared 192 workspace/key pairs, covering base, zero,
unlimited, paid top-up, grant, refund_review, expired, future-dated, past_due and
suspended. **0 mismatches.**

## Remaining deployment verification

1. **Production PITR (DB-009).** Configure WAL archiving off-host (or managed PITR),
   keep ≥ 7 days, run `scripts/pitr_drill.sh`'s procedure against the real archive,
   and time a restore at production size against the 2 h RTO.
2. **Paymob Test regression of the TX-split checkout (DB-008).** No Paymob Test keys
   are present in this environment (`E:\wasla\.env` has none), and a hosted payment
   needs a person to enter a Test card. With Test keys (both `_test_`) on an isolated
   DB and Redis lane, and the existing ngrok route to `/api/v1/webhooks/*`:
   - one hosted checkout (e.g. Pro 99 EGP) paid, checking the signed callback,
     `applied`, and the order bound in TX2;
   - one custom-offer accept & pay;
   - replay of that genuine callback, expecting `duplicate` and no change;
   - optionally, stop the API before a payment and let hosted reconciliation recover
     it.

   The deterministic suites cover each of these with the real `PaymobProvider`
   adapter and signed callbacks. **Live: not used.**
3. **Online migration at production size.** 0075/0077/0078/0079 build indexes
   concurrently and validate constraints separately. Their wall-clock time on the
   real tables should be observed at deploy. `db_preflight verify` fails the deploy
   if a build was left INVALID.

## Score / readiness reassessment

Measured final state, on the audit's categories:

| Category | Before | After | Basis |
|---|---|---|---|
| Schema & constraints | 11 / 15 | **14** | settled history, periods, default card, case-insensitive email all DB-enforced; parity catalog-proved; −1: token envelope shape (Y29) and `collection_attempts ≥ 0` (Y30) still unconstrained |
| Migration correctness | 12 / 15 | **14** | single head, fresh/0073-populated/round-trip/base pass, concurrent builds, NOT VALID→VALIDATE, guarded downgrades, validation gate; −1: autocommit enum blocks still need a documented manual `stamp` after a mid-block failure |
| Tenant isolation | 11 / 15 | **13** | every financial binding composite-keyed; −2: non-financial bindings remain app-only (ADR-100), no RLS |
| Financial integrity | 6 / 15 | **14** | concurrent double settlement impossible, backstop through direct SQL, 0 violations everywhere; −1: the real Paymob Test regression of the DB-008 boundary is not yet repeated |
| Concurrency / transactions | 5 / 10 | **9** | 180 racing settlements, 0 deadlocks, conflicts → 409/503; −1: no automatic bounded retry of 40001/40P01 (answered 503 instead) |
| Indexing / query performance | 6 / 10 | **9** | purge index-backed (114× on the hotspot), 22 hot queries healthy, N+1 gone; −1: redundant single-column tenant indexes (INFO) untouched |
| Retention / deletion | 3 / 5 | **4** | raw payloads bounded, purge fast and correct, deletion policy stated; −1: messages/usage/analytics rows themselves have no retention policy |
| Backup / restore readiness | 3 / 5 | **4** | prerequisites handled, restore verified with the gate, PITR contract and local drill; −1: production PITR unverified |
| Operational DB posture | 3 / 5 | **5** | timeouts, no transaction across Paymob, connection budget, preflight in `migrate` |
| Auditability / observability | 2 / 5 | **4** | audit trail append-only, incident evidence frozen, DB metrics and alerts tested; −1: slow queries visible in logs only (no `pg_stat_statements`), bloat as dead tuples only |
| **Total** | **62** | **90 / 100** | |

**Readiness rule:** Critical open 0 · High open 0 · DB-001 race closed · DB-002 purge
plan closed · meaningful mutation survivors 0 · financial invariant violations 0 ·
cross-tenant financial injections refused · full suites green · single Alembic head ·
backup/restore green.

**Verdict: DATABASE FINDINGS CLOSED WITH DEPLOYMENT VERIFICATION DEFERRED**
(production PITR; the real Paymob Test regression of the checkout boundary). This is
not a claim of full production readiness.

## Commit ledger

| Commit | Message | Findings |
|---|---|---|
| `3cd4184` | fix(database): serialize invoice settlement and financial locks | DB-001, 003, 017 |
| `ac19c7c` | perf(database): index workspace purge foreign keys | DB-002 |
| `06d6162` | fix(database): harden runtime audit permissions and timeouts | DB-006, 007 |
| `3ec0caa` | fix(billing): release checkout transaction before Paymob calls | DB-008 |
| `0186ca1` | fix(database): enforce financial tenant and history invariants | DB-004, 005 |
| `a368c0a` | fix(database): enforce period card and email invariants | DB-012, 018, 025 |
| `0205261` | feat(database): bound raw webhook payload retention | DB-011 |
| `9886a3b` | fix(database): close migration and schema parity gaps | DB-015, 019, 020, 024, 027 |
| `c7dcae1` | perf(database): remove member n+1 and order lists deterministically | DB-013, 014, 016, 026 |
| `6ea586e` | ops(database): add restore prerequisites, PITR contract and db alerts | DB-009, 010, 023 |
| `adc017f` | docs(database): record tombstone and RLS decisions | DB-021, 022 |
| `e15965f` | test(database): pin the retention loop and the conversations fillfactor | mutation gaps DBM-017b, DBM-025 |
| *(this commit)* | docs(database): record findings remediation | — |

Each intermediate commit was checked out on its own and passed `ruff`,
`black --check`, `mypy app tests`, the application factory import, and a single
Alembic head.
