# Entitlements And Channel Capacity — Implementation Report

ADR-131 · decisions ENT-01..ENT-24 · branch `entitlements-channel-capacity-20261002` ·
worktree `E:\wasla-entitlements` · local only: nothing pushed, merged or deployed.

## 1. Executive Summary

Wasla now has one entitlements model for every channel:

- **One AI allowance per workspace** (ENT-01). Every channel draws from
  `period_ai_turns`; an `ai_turn` charge carries `channel`, `connection_id` and
  `agent_turn_id` as reporting dimensions only.
- **Held at engagement, charged on a usable outcome** (ENT-02, ENT-03). A turn
  takes a hold under the workspace's advisory lock - counting charges *and* open
  holds - and is charged only for reply text or an executed handoff. A provider
  error, a timeout or an empty answer gives the hold back. Ten turns racing an
  allowance of three charge three and hand seven to a person. Ten that all fail
  charge nothing.
- **Channel capacity replaces the WhatsApp number limit** (ENT-05..08).
  `channel_connections` = the version's slots + paid channel top-ups + platform
  grants, general or typed. Every active connection weighs one. One guard sits on
  every activation path: a pre-check before any provider call, and an
  authoritative check under the workspace's lock. Refusals are
  `409 channel_capacity_exceeded` and `409 channel_type_not_allowed`. The
  disable/enable bypass the baseline measured (2 active on a 1-slot plan) is
  closed.
- **Channel types belong to the plan** (ENT-09, ENT-12). A top-up never opens a
  type. A version that never stated its types reads as WhatsApp alone.
- **Channel top-ups are WhatsApp-number top-ups with a type** (ENT-10, 11, 13).
  They can be typed or general, and each product names the plans it is offered to.
  The six seeded products are inactive and unpriced until staff price them.
- **A capacity reduction never deletes anything** (ENT-14, ENT-15). The owner
  chooses which connections to keep, before the boundary or during a 7-day grace.
  Otherwise the real billing worker disables disallowed types first, then the
  newest. Suspension, cancellation and expiry disable nothing (ENT-16).
- **Marketing consent is per channel** (ENT-19). Meters are channel-neutral, and
  the registry rule is decided (ENT-22).

Proven by the model-built and migration-built suites (§28, §29), the concurrency
suites run six times in a row (§21), the invariant ledger E01–E15 and a
three-way SQL oracle (§22), a 46-mutant matrix (§26), APP-E2E R-1..R-14 against
the real API and workers on a migration-built database (§32), and three real
Paymob **Test** payments (P-1..P-3, 320.00 EGP Test).

Final verdict: **ENTITLEMENTS IMPLEMENTED WITH EXTERNAL VERIFICATION REMAINING** (§37).

## 2. Starting Repository State

| Item | Value |
| --- | --- |
| Base (`ENTITLEMENTS_BASE_HEAD`) | `2c58c35` - head of `omnichannel-final-remediation-20261002`, which carries the billing line (0071–0081) and the omnichannel remediation (0085–0091); chosen because it is the newest line holding both |
| Branch / worktree | `entitlements-channel-capacity-20261002` / `E:\wasla-entitlements` (baseline evidence from a detached `E:\wasla-ent-baseline` at `2c58c35`) |
| Alembic head at base | `0091` (single head) |
| Origin | `origin/worktree-billing-google-auth` is `354db53`; the base is 41 commits ahead of it, this branch 72 (31 in this stage); origin still lacks `aa11098` |
| Code head reported here | `b85b717`: the application as at `da0d785`, plus two test fixes (the report commit sits on top) |
| Next free ADR | ADR-131 (verified in `DECISIONS.md` at the base) |
| PostgreSQL / Redis / Docker / Python | 16.15 (`pgvector/pgvector:pg16`) / 7.4.11 (`redis:7-alpine`) / 29.8.0, Compose v5.5.1 / 3.12.7 |
| `app.__file__` | `E:\wasla-entitlements\app\__init__.py`; every lane ran from its own worktree and asserted its own path (`E:\wasla-ent-lane`, `E:\wasla-ent-mut`) |
| `E:\wasla` | untouched: the user's nine untracked reports are where they were |

Disposable infrastructure only: `wasla-ent-pg` (56051), `wasla-ent-redis` /
`-redis2` / `-redis3` / `-redis4` / `-redis5` (56151–56155), `wasla-ent-minio`
(59051). One database per purpose (`wasla_ent_models`, `_migrations`, `_mut`,
`_work`, `_e2e`, `_alembic`, `_paymob`, …). No dev database, dev Redis or other
stage's container was used.

## 3. Existing Billing And Channel Architecture Used

Built on, not beside:

- `EntitlementService.check` is still the only place a limit is read.
  `channel_capacity`, `hold_ai_turn` and `scheduled_channel_capacity` live in it.
- The per-workspace advisory lock (`_lock_id(tenant, key)`, BILL-08) serves both
  the AI hold (`period_ai_turns`) and the capacity guard (`channel_connections`).
- `TopupPurchaseRepository.active_totals`, `topup_ledger.validity_window`
  (capacity key → billing term) and `CheckoutService.open_page` are unchanged.
  `InvoiceSettlement` and `TopupLedger` (still granting once) each gained one
  hook: adopting other terms is a capacity boundary (ENT-15), and channel slots
  granted during a grace close the reduction. A channel top-up flows through
  exactly the number top-up's path.
- `PlanVersion` stays immutable (the trigger is never disabled), and
  `allowed_channel_types` lives on it. The placeholder catalogue is new versions.
- The `AgentTurn` claim / engage / outcome lifecycle carries the hold
  (`agent_turns.charge_state`). No second turn table.
- `ChannelRegistry`, `ChannelState` and the outbound choke point are reused. The
  neutral `ChannelConnectionService` and `stop_automation` are new, built on
  them; a manual disable now calls `stop_automation` too.
- `scheduled_change` on subscriptions carries a downgrade. Its boundary is where
  the reduction opens.

## 4. Product Decisions Applied (ENT-01..24)

| ID | Decision | Where |
| --- | --- | --- |
| ENT-01 | One `period_ai_turns` meter per workspace; `used_by_channel` for display only | `EntitlementService._used_and_held`, `ai_turns_by_channel`; `EntitlementRead.used_by_channel` |
| ENT-02 | Charged for reply text (sent, refused by the channel, or withheld by a re-read) or an executed handoff; not for provider error/timeout, empty answer, escalation before composition, quota block, suppression, a lost claim | `AgentOrchestrator` outcome → `AITurnCharge.settle` |
| ENT-03 | Hold → charge / release under the `(workspace, period_ai_turns)` lock, never held across inference | `EntitlementService.hold_ai_turn`, `AgentWorker._hold_and_engage` |
| ENT-04 | No hold → no provider call → `AI_QUOTA_EXHAUSTED` handoff (unchanged) | `AgentWorker._quota_blocked` |
| ENT-05 | `channel_connections` replaces `whatsapp_numbers` (choice A read-time alias + choice B new versions) | `entitlement_terms`, migrations 0093, 0098 |
| ENT-06 | Every active connection weighs 1 | `channel_fit` |
| ENT-07 | Only active, unreleased connections count; disable/release always allowed; enable/reconnect need a slot; re-authorising takes none | `EntitlementService.active_connections`, `ChannelConnectionService`, `WhatsAppAccountService.set_status` |
| ENT-08 | One guard, two checks, 409 codes | `ChannelCapacityGuard` (`precheck`, `reserve_or_refuse`) |
| ENT-09 | `allowed_channel_types` on the version; unstated = `[whatsapp]` | `plan_versions.allowed_channel_types`, trigger `plan_versions_entitlement_terms` |
| ENT-10 | Channel top-ups = number top-ups (one-time, frozen, term validity, refunds, grants) | `TopupService`, `TopupLedger`, re-pointed top-up suites |
| ENT-11 | `topup_products.channel_type` (null = general); prices staff-set, audited | `TopupAdmin`, migration 0093 |
| ENT-12 | A typed top-up for a type the plan lacks: not listed, checkout 422, grant 422 | `TopupNotAvailableError` |
| ENT-13 | `topup_product_plans`: none = every plan; otherwise invisible / 404 | `TopupRepository._offered_to` |
| ENT-14 | Downgrade: new connections must fit the scheduled plan; pre-selection; 7-day grace; owner selection; automatic fallback (disallowed types, then newest) | `ChannelCapacityReductions`, billing worker `_resolve_reductions` |
| ENT-15 | Top-up expiry, refund withdrawal, grant expiry, migration → the same flow | `ChannelCapacityReductions.boundary(cause=…)` |
| ENT-16 | Not served → default plan, `over_limit`, nothing disabled; automation on an excluded channel refused `channel_not_in_plan`, never charged | `lifecycle.refusal_now`, campaign and follow-up guards |
| ENT-17 | Existing subscribers keep their version | unchanged; 0098 re-verified |
| ENT-18 | Only AI turns, channel capacity and channel types newly enforced | `period_messages` `enforced: false` |
| ENT-19 | Consent per `(tenant, contact, channel)` | `contact_channel_consents`, migration 0097 |
| ENT-20 | Placeholder catalogue; top-ups inactive and unpriced | migration 0098 |
| ENT-21 | `telegram`, `tiktok` labels | migration 0092 |
| ENT-22 | Neutral meters with a channel dimension; WhatsApp mapped 1:1; registry decided | `metering.message_meters`, migration 0096 |
| ENT-23 | ADR-122 decisions 4 and 5 unchanged | — |
| ENT-24 | No code reads a plan name | figures are data; every rule reads version terms |

## 5. Semantics Changed From Today

"Before" is measured on the baseline (`2c58c35`) by the Phase 0 probes. "After"
is the same scenario on this branch.

| Today (documented) | After | Decision | Evidence before → after |
| --- | --- | --- | --- |
| AI turn charged at **engagement** (ADR-104) | held at engagement, **charged on a usable outcome**, released otherwise | ENT-02 | B1: provider error → 1 `ai_turn` event, turn left `engaged` → `test_a_provider_failure_is_not_charged_and_gives_its_hold_back`: 0 events, `released / generation_failed`; B1b empty answer → 1 event → 0 (`test_an_empty_answer_is_not_charged`) |
| `whatsapp_numbers` limits numbers only (ADR-122.2) | retired; `channel_connections` counts every channel | ENT-05 | B5: number top-up +2 → effective 3 (numbers only) → R-6: general channel top-up +2 → 3 across channels |
| Refused creation answers **402** | capacity **409 `channel_capacity_exceeded`**, type **409 `channel_type_not_allowed`**; every other key keeps 402 | ENT-08, 09 | B4a: 402 `plan_limit_exceeded` → R-4 / `test_a_second_connection_on_a_one_connection_plan_is_409_and_meta_is_not_asked`: 409, Meta fake 0 calls |
| Downgrade or capacity expiry disables nothing; `over_limit` for ever | channels: owner selection or 7-day grace, the rest **disabled** (never released or deleted) | ENT-14, 15 | B6: Business 10 → Pro 3 with 7 numbers → `over_limit` for ever, 0 disabled → R-8: oldest 3 kept, 4 disabled after the grace |
| Opt-out person-level (ADR-122.3) | per channel | ENT-19 | B7: STOP on WhatsApp → WhatsApp audience 0 and Instagram audience 0 → R-12: WhatsApp skips, the synthetic channel reaches |
| Top-up products: scope only | optional `channel_type`, optional eligible plans | ENT-11, 13 | `test_a_typed_instagram_slot_never_seats_a_whatsapp_number`, `test_a_product_offered_to_other_plans_is_invisible` |
| Only WhatsApp meters decided; registry refuses others (ADR-122.1) | neutral meters with a channel dimension; registry decided by the meter mapping | ENT-22 | B8: registering a second channel refused for an undecided meter → `test_a_second_channel_is_metered_under_the_neutral_meters_never_as_whatsapp`; an adapter with no mapping is still refused |

Also measured at the baseline and closed here:

- **B4c:** enabling a disabled number while another held the only slot left
  **2 active on capacity 1** (a bypass). It is now refused with 409
  (`test_disabling_frees_the_slot_and_enabling_needs_it_back`).
- **B9:** a manual disable left the number's pending follow-up `pending`. It went
  on being dispatched and refused. Every disable path, manual included, now
  cancels it (`test_a_person_disabling_a_number_cancels_its_pending_follow_ups`).

## 6. Entitlement Vocabulary

```
Workspace
 ├── Subscription → Plan → PlanVersion (limits + allowed_channel_types, immutable) → PlanPrice
 ├── Usage, per usage cycle
 │    ├── period_ai_turns           enforced, workspace-wide, hold → charge
 │    ├── period_messages           metered (enforced: false), every channel
 │    └── period_campaign_messages  unchanged
 ├── Capacity, continuous
 │    ├── channel_connections       enforced: included + channel top-ups (general | typed) + grants
 │    └── storage_bytes, team_members, knowledge_documents   unchanged
 └── Channel policy
      └── allowed_channel_types     enforced; a set, never a top-up target
```

The seven top-up / custom-plan keys are `period_messages`, `period_ai_turns`,
`period_campaign_messages`, `storage_bytes`, `channel_connections`,
`team_members`, `knowledge_documents` (ADR-113 amended).

## 7. Retiring whatsapp_numbers

**Choice A + B**, as expected:

- **A, read-time alias.** `entitlement_terms` is the only place that knows the
  old name. A version that states no channel types (published before 0093) and
  carries no `channel_connections` is read with its `whatsapp_numbers` value as
  its channel capacity, and with `[whatsapp]` as its types. No stored version or
  purchase is rewritten, and the immutability trigger is never disabled. A legacy
  `whatsapp_numbers` purchase counts as a typed WhatsApp slot - exactly what was
  paid for.
- **Refused on everything new**: the platform schemas (422 naming
  `channel_connections`, `test_the_retired_number_key_is_refused_by_name_on_a_plan_and_a_version`),
  the `plan_versions_entitlement_terms` trigger (BEFORE INSERT), and
  `topup_refuse_retired_key` on products and purchases. The enum label stays,
  because PostgreSQL cannot drop one.
- **B, new versions.** Migration 0098 publishes new versions of `starter`,
  `pro`, `business` and `enterprise` with `channel_connections` (1 / 3 / 10 /
  unlimited) and their channel types. Subscribers stay on what they bought
  (ENT-17).
- `LimitKey.WHATSAPP_NUMBERS` is gone from every enforcement path. The old
  number slot is the channel capacity guard's `ChannelSlot`.

## 8. AI Turn Hold / Charge / Release

`agent_turns` gains `charge_state` (`held` | `charged` | `released`, null on
history), `held_at`, `charged_at`, `released_at` and `charge_release_reason`
(`not_chargeable`, `generation_failed`, `hold_expired`). A CHECK keeps them
consistent.

1. **Engage**, in one transaction: `hold_ai_turn` waits up to 20 s
   (`AI_TURN_HOLD_LOCK_WAIT`) for `pg_advisory_xact_lock(workspace,
   period_ai_turns)`. It then counts, in one statement, the cycle's `ai_turn`
   charges plus holds taken in this cycle and younger than
   `AI_TURN_HOLD_TTL_SECONDS` (900). `used + held + 1 <= limit` writes the hold
   with the engagement and commits, which releases the lock. Otherwise the
   conversation is handed to a person (`AI_QUOTA_EXHAUSTED`) with no provider call.
2. **Generate**, with no lock and no transaction (ADR-080).
3. **Settle**, in one short transaction on the turn's row lock, idempotent:
   - A usable outcome records one `ai_turn` event, stamped with the hold's moment
     and the turn's channel, connection and id.
   - Anything else releases the hold.
   - `uq_usage_events_tenant_id_agent_turn_id` makes a second charge impossible.
   - The reply is sent only after the settle commits, so a delivery failure is
     still charged.

Contention past the 20 s wait (lock timeout, deadlock, serialization) gives the
claim back and retries the job before engagement (`agent.turn_hold_contended`,
`cbfc273`). APP-E2E R-3 found this: before the fix, six of ten customers were
dead-lettered under load. The billing sweep releases holds past their TTL
(`hold_expired`). A settle after that still charges (`late_charge`) and is
counted: usage that happened is never refused afterwards. The provider-request
meter (`ai_request`) is unchanged.

Required figures, each asserted exactly in `tests/integration/test_ai_turn_charging.py`
(real `AgentWorker` and orchestrator, providers faked at the HTTP transport):

| Scenario | Started | Held | Charged | Released | `ai_turn` events | Replies | Quota handoffs |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Allowance 3, 10 overlapping, provider answers | 10 | 3 | 3 | 0 | 3 | 3 | 7 |
| Allowance 3, 10 overlapping, provider fails | 10 | 3 | 0 | 3 (`generation_failed`, 9 provider attempts) | 0 | 0 | 7 |
| Allowance 3, 10 failing in waves of 3 | 10 | 10 | 0 | 10 | 0 | 0 | 0 |
| Allowance 3: 3 fail, then 3 answer | 6 | 6 | 3 | 3 | 3 | 3 | 0 |
| Allowance 1, WhatsApp + Messenger at once | 2 | 1 | 1 | 0 | 1 | 1 | 1 |
| Expired hold + late settle (allowance 1) | 2 | 2 | 2 (1 `late_charge`) | 1 `hold_expired`, then charged | 2 | 1 | 0 |
| Settle twice | 1 | 1 | 1 | 0 | 1 (`UNCHANGED` on repeat) | — | — |

`hold_expired` / `late_charge` / `held` / `charged` / `released` are also
`wasla_ai_turn_charge_total{outcome}` (§23).

## 9. Workspace-Wide AI Meter

`used` is every `ai_turn` event of the workspace in the usage cycle, whatever
the channel. `used_by_channel` is a `GROUP BY` that no code compares with a
limit. `GET /billing/entitlements` → `period_ai_turns` carries `base_limit`,
`topup_limit`, `platform_grant_limit`, `effective_limit`, `used`, `held`,
`remaining` (holds excluded), `over_limit`, `enforced: true`, the period, and
`used_by_channel`
(`test_one_ai_allowance_reads_used_held_remaining_and_its_channels` reproduces
*Used 742 / Limit 1000 / Remaining 258*). With an allowance of 2, one WhatsApp
and one Messenger turn leave the third customer handed off
(`test_each_channel_draws_from_the_workspace_total`).

## 10. Effective Channel Capacity And Typed Slots

Inside `EntitlementService.channel_capacity`:

```
general  = version.channel_connections + live general top-ups + live general grants
typed[T] = live top-ups and grants typed T
fits     ⇔ Σ_T max(0, active[T] − typed[T]) ≤ general          (channel_fit)
```

An unlimited base is unlimited, and a top-up cannot be bought for it (unchanged).
`GET /billing/entitlements` → `channel_connections.channel_capacity` gives
`general_limit`, `general_topup_limit`, `general_platform_grant_limit`,
`typed_slots [{channel, capacity, used}]`, `active_by_channel` and
`allowed_channel_types`. `test_channel_capacity_reads_included_purchased_and_active_by_channel`
reproduces *Starter: included 1 + purchased 2 = 3; WhatsApp, Instagram,
Messenger active → 3 / 3* (Starter's types widened in the fixture).

## 11. Channel Capacity Guard And 409 Codes

`ChannelCapacityGuard` (`app/services/channel_capacity.py`) is the one guard:

- **`precheck(channel)`** is lock-free and runs before any provider call.
  `WhatsAppAccountService.connect` calls it before Meta's ownership read.
  `ChannelConnectionService.precheck` is the same first answer for an adapter's
  flow, which must ask before its own provider proof (added here, `9e82798`).
- **`reserve_or_refuse(channel)`** takes `pg_advisory_xact_lock(workspace,
  channel_connections)` in the activating transaction, then judges. It returns a
  `ChannelSlot` that every writer of an active connection must take. Writers:
  WhatsApp connect, WhatsApp enable, a neutral connect, a neutral enable.
  `tests/unit/test_channel_activation_writers.py` scans the code and refuses a
  new writer without one.
- Order: capacity, then the scheduled plan's capacity (ENT-14), then type, then
  the scheduled plan's types. When both capacity and type fail, the capacity
  error is returned (`test_capacity_is_judged_before_type`).
- `409 channel_capacity_exceeded` details: `effective_limit`, `active`,
  `channel`, `typed_capacity`, `over_limit` (+ `scheduled_change`).
  `409 channel_type_not_allowed` details: `channel`, `allowed_channel_types`.
- Never refused: disable, release, list, read, inbound, re-authorising an
  active number (`test_disabling_and_releasing_are_never_refused_even_over_capacity`,
  `test_reauthorising_an_active_number_takes_no_new_slot`).

## 12. Allowed Channel Types

`plan_versions.allowed_channel_types` (with a `plans` mirror) is required on
every new version and custom plan (schema + BEFORE INSERT trigger). It holds
vocabulary labels only, each named once, with no wildcard; adding a channel is a
new version. A missing value reads as **`[whatsapp]`**, never "all"
(`test_a_version_published_before_adr_131_is_held_to_its_number_limit`, M-E14).
It is enforced at connect / enable / reconnect (409), on typed top-ups (not
listed, checkout 422, grant 422), and on automation (`channel_not_in_plan`). It
is not enforced on inbound, on a person's reply, or on reads.

## 13. Channel Top-Ups

- `topup_products.channel_type` and `entitlement_key = channel_connections`.
- `topup_product_plans` holds the eligible plans.
- `topup_purchases.channel_type` is frozen by the snapshot trigger.
- `price` became nullable, with `ck_topup_products_priced_when_active`: a product
  nobody priced is never active, and activation of one is a 409.

Everything else is the number top-up's path. The same suites prove it, re-pointed
(`test_topups.py`, `test_topup_concurrency.py`, `test_topup_invariants.py`), plus
`test_channel_topups.py`:

- general +2 on Starter seats three channels and refuses a fourth
- a typed Instagram slot never seats a WhatsApp number
- an unused typed slot takes its channel when the general slots are full
- a callback replayed → one grant
- the same idempotency key ×4 at once → one page
- a declined payment grants nothing
- money after the term → held, plus `topup_paid_but_not_granted`
- refund before grant → cancelled; after the grant → `refund_review`, still counting
- a yearly term's slot lasts the year, not the month (M-E18's killer)
- a typed platform grant is never a sale
- the snapshot survives product edits
- an ineligible plan → invisible and 404
- a typed product for a type the plan lacks → not listed, checkout 422, grant 422

## 14. Capacity-Reduction Lifecycle

`channel_capacity_reductions` stores the cause, the target (general, typed and
allowed types), `effective_at`, `grace_ends_at`, notified / warned, status
(`pending_selection` | `resolved_by_owner` | `resolved_automatically` |
`no_longer_needed`), what was kept and what was disabled, and a revision. A
partial unique index allows one open reduction per workspace; a newer cause
adjusts the open one. `channel_capacity_preselections` holds an owner's choice
ahead of a boundary.

- **Scheduled downgrade**: a new connection must fit the scheduled plan too.
  Owners may pre-select. Nothing is disabled.
- **Boundary**, reached by the real billing worker (`run_once`). If it still
  fits, nothing happens. A valid pre-selection is applied at once. Otherwise a
  reduction opens, with a grace of `CHANNEL_CAPACITY_GRACE_DAYS` (7).
- **Grace**: everything keeps working. New connections and re-enables are
  refused. `POST /billing/channel-capacity/selection` (owners, `expected_revision`)
  keeps what fits, checked under the lock, and disables the rest in one
  transaction (`capacity_reduction`).
- **Grace end**: the billing worker claims due reductions
  (`FOR UPDATE SKIP LOCKED`). It disables types the target drops first, then the
  newest by `ownership_started_at`, keeping the oldest
  (`capacity_reduction_automatic`, actor system).
- **Capacity returning** (a top-up bought, a grant, an upgrade) → `no_longer_needed`.
- Every disable is audited with its reduction. It cancels pending follow-ups,
  and a queued AI turn on the connection is refused. The connection's claim,
  credentials and history are kept. Re-enabling goes through the guard.
- Owners are emailed through the outbox when a downgrade that will not hold is
  scheduled, at the boundary, and 48 h before the automatic fallback.

Proven by `test_capacity_reductions.py` (17 tests):

- Business 7 → Pro 3: an 8th connection is refused after scheduling; the owner
  keeps 3 → 4 disabled, 0 released, 0 deleted; a selection of 4 → 422; a
  disallowed type → 422; a pre-selection is applied at the boundary.
- Grace +1 s → automatic (disallowed types first, oldest 3 kept); grace −1 s →
  nothing.
- **All four ENT-15 causes open the same flow:** top-up expiry (also for slots
  still in refund review at term end), refund-review withdrawal, a **platform
  grant expiring** and a **staff migration to a smaller version** adopted at
  renewal. A top-up bought in the grace closes it.
- Suspended → nothing; another workspace's id → 404.
- §21 adds two billing workers → one set of disables, and an owner racing the
  fallback → resolved once.

The migration test found a defect, fixed in `da0d785`.
`InvoiceSettlement.adopt_renewal_version`, the path a cohort migration and a
free renewal take onto new terms, judged the boundary by the wall clock instead
of the settlement's moment. A sweep run for a given moment dated the grace by a
time it never saw: in the test, about 29 days before the renewal, so the same
pass disabled four connections with no grace at all. The moment is now passed
through, and the grace is exactly the settlement's moment + 7 days. In a
deployment the two clocks agree to the millisecond, but a worker catching up or
a backfill would not have.

## 15. Suspension, Cancellation And Expiry

`SERVING_STATUSES` decide as before (ADR-061). A non-served workspace reads
`DEFAULT_PLAN_CODE`'s terms: `over_limit: true`, `remaining: 0`, connects and
enables refused, nothing disabled and no reduction opened (M-E26). An AI turn,
campaign copy or follow-up on a channel those terms exclude ends
`channel_not_in_plan` and is never charged. Inbound is stored, and a person's
reply is sent. Paying the overdue invoice restores the paid terms with nothing to
re-enable (`test_channel_not_in_plan.py`, R-11).

## 16. Per-Channel Opt-Out

`contact_channel_consents (tenant_id, contact_id, channel)` holds the opt-out,
its source and via, and the resume with its provenance, keyed to `contacts`
through the workspace-agreed key. Migration 0097 moved every person-level
opt-out and resume to the contact's WhatsApp row and **dropped** the four
`contacts` columns, so there is one system and no stale column to read by
mistake (M-E28). The migration refuses an opt-out with no source rather than
inventing one.

Every writer records the channel it came from: stop words, button taps, the
OMNI-030 `recover-button-opt-outs` replay (WhatsApp), `user_preferences`, 131050
and a colleague. Every reader asks about the recipient's channel: the audience
builder, the campaign send guard, the follow-up guard and the read API. Newer
resume evidence still wins.

## 17. Neutral Meters And The Registry Rule

`message_received` / `message_sent` carry the channel; a CHECK enforces it (0096).
WhatsApp keeps `whatsapp_message_received` / `_sent` as their WhatsApp instance,
mapped 1:1 (`metering.message_meters`), so no history is rewritten and no cycle
is split. `period_messages` and `/usage`'s `messages_received` / `messages_sent`
add all four, still `enforced: false`. `ChannelRegistry` refuses a channel with
no meter mapping (M-E31). A synthetic adapter registers in a test registry.
Production still operates WhatsApp only, and Telegram and TikTok are labels.

## 18. Platform Control Plane

Same roles, `reason`, `expected_revision` and audit:

- `/features` lists `channel_connections` (enforced, concurrency-safe),
  `allowed_channel_types` (`channel_policy`) and `whatsapp_numbers` (`retired`,
  `replaced_by`).
- Plans, versions and custom plans take `channel_connections` and require
  `allowed_channel_types`. The version preview reports `workspaces_above_new_limit`
  per key and `workspaces_holding_a_removed_type`.
- Top-up products take `channel_type` and `eligible_plan_codes`. A label outside
  the vocabulary is 422. Activation needs a price.
- Grants take an optional `channel_type`.
- The tenant summary shows AI used / held / by channel, slots by source, active
  connections by channel, and the latest reduction with its grace end.

Nothing here charges a card, disables a connection or bypasses the guard
(`test_staff_read_the_same_figures_and_the_latest_reduction`,
`test_a_preview_counts_who_a_smaller_capacity_or_fewer_types_would_leave_over`,
`test_staff_state_a_products_channel_and_plans_and_every_change_is_audited`).

## 19. Tenant API

All additive (operations 199 → **202**):

- `GET /billing/channel-capacity` (member): slots, `scheduled`, `reduction`,
  `automatic_fallback` preview, pre-selection and `selection_revision`.
- `POST /billing/channel-capacity/selection` (owner).
- `GET /channel-connections` (member): `counts_toward_capacity`,
  `disabled_reason`, `ownership_started_at`.
- `GET /billing/entitlements` and `/billing/topups` gain the fields above.

There is no generic create. Connections are made by a channel's own flow (an
unregistered channel → `ChannelUnavailableError`). `docs/API.md` documents all
202 operations, and the documentation-truth test counts them.

## 20. Tenant Isolation

| Claim | Test |
| --- | --- |
| Another workspace's connections never count | `test_another_workspaces_connections_never_count` |
| Another workspace's connection in a selection → 404 | `test_another_workspaces_connection_in_a_selection_is_not_found` (M-E32) |
| Another workspace's typed product → invisible, 404 | `test_another_workspaces_typed_product_is_invisible_and_404` |
| Another workspace's consent is never read | `test_another_workspaces_consent_is_never_read` |
| Another workspace's usage/holds are not charged here | `test_another_workspaces_usage_is_not_charged_to_this_one` (holds are counted per `tenant_id` in the same statement) |
| A custom plan's terms (types included) unusable by another workspace | `test_another_workspaces_custom_price_is_refused_everywhere`, platform `custom_plan_not_available_for_workspace` |

## 21. Concurrency Results

Every race uses separately committed connections started at an
`asyncio.Barrier`, with no sleep ordering. A barrier only lines up starts, so the
races that count slots or allowance also go through a gate at the decision point:

- **`DecisionGate` (AI)**: every turn arrives before any takes the lock. The
  holder then waits, holding the lock, until every other turn is blocked on it.
- **`GuardGate` (capacity)**: the same at `reserve_or_refuse`.

Each race asserts that every racer reached the decision. The gates took three
fixes:

- `d43c2ea`: an arrival barrier, because the AI race failed on a cold database.
- `045b640`: `GuardGate`, because M-E11 survived 2 runs in 3.
- `9b7c681`: no re-poll once the racers are seen blocked, plus a 100 s lock wait
  for the AI race workers. A trace under four suites at once showed one count
  query taking 16.6 s, so ten serialized decisions could outlast the production
  20 s lock wait.

Four copies of both suites at once, on one PostgreSQL: **27 / 27 passed in every
copy** (before the last fix, 1–2 failures in each).

| Race | Asserted exactly, every run |
| --- | --- |
| Capacity 3, 2 active, 10 WhatsApp connects | 10 attempted, **1** created, **9** × 409, 3 active, every commit ≤ 3 |
| Same, 10 connects on Instagram + Messenger | 1 created, 3 active, every commit ≤ 3 |
| Capacity 3, 3 active, 1 disable + 5 connects | ≤ 1 created, ≤ 3 active at every commit |
| Capacity 1, 2 enables of disabled connections | 1 enabled, 1 refused |
| 1 general + 1 Instagram slot, 5 Instagram + 5 WhatsApp | exactly 2 created, ≤ 1 WhatsApp, ≤ 2 Instagram |
| Base 1 + grant of 2 expiring at T, connects either side | ≤ 3 active, ≤ 1 created after T, every commit ≤ the capacity in force |
| Two billing workers at a grace end | each connection disabled once, one resolution, the oldest kept |
| Owner choosing while the fallback runs | resolved once, 2 disabled, none twice |
| AI: the races in §8 | as tabled there |

Six consecutive runs of `test_channel_capacity_concurrency.py` +
`test_ai_turn_charging.py` (27 tests per run) on the final code:

At `da0d785` (both suites unchanged at `b85b717`), 2026-10-08 14:34:44–14:42:04 UTC, beside the model-built lane:

| Run | `test_channel_capacity_concurrency.py` + `test_ai_turn_charging.py` | Time |
| --- | --- | --- |
| 1 | 27 passed, 0 failed | 57.7 s |
| 2 | 27 passed, 0 failed | 57.1 s |
| 3 | 27 passed, 0 failed | 57.8 s |
| 4 | 27 passed, 0 failed | 69.0 s |
| 5 | 27 passed, 0 failed | 64.0 s |
| 6 | 27 passed, 0 failed | 61.7 s |

Every passing run asserts its figures exactly. In each run, the ten-claim race
attempted 10 connects: **1 succeeded and 9 were refused with 409**, with active
connections ≤ 3 at every commit. The AI races charged 3 and handed 7 to a
person (answers), or charged 0 and released 3 (failures).

History of this table:

- At `25583ce` (the `045b640` gates), the same suites failed 2 of 27 once in a
  six-run loop beside the full lane, the matrix and APP-E2E. That led to the
  `9b7c681` fix, after which four simultaneous copies passed.
- At `9b7c681`, six consecutive runs beside the model-built lane passed 27 / 27
  each (10:49–10:57 UTC).

Meta fake calls for a connect refused at capacity: **0** (asserted in
`test_a_second_connection_on_a_one_connection_plan_is_409_and_meta_is_not_asked`
and in R-4).

## 22. Invariant Ledger And SQL Oracle

E01–E15 run in `scripts/omnichannel_invariants.py verify` beside the
omnichannel invariants (read-only, read-only replica compatible):

| ID | Invariant |
| --- | --- |
| E01 | active connections fit the capacity in force, unless a pending reduction, a non-serving subscription or a top-up expired within one sweep interval explains it |
| E02 | no active connection of a type outside the terms in force, same explanations |
| E03 | no typed channel slot sold or granted for a type its plan did not allow |
| E04 | no channel top-up sold to a plan it was not offered to |
| E05 | at most one `ai_turn` charge per turn |
| E06 | no charge for an outcome ENT-02 does not charge |
| E07 | no hold past TTL + the sweep interval |
| E08 | charged turns = their charges |
| E09 | every reduction disable has its audit entry |
| E10 | nothing a reduction lists was released or deleted |
| E11 | one open reduction per workspace |
| E12 | no `whatsapp_numbers` on terms that state channel types (new rows) |
| E13 | every opt-out names its channel and who decided |
| E14 | no campaign copy sent after an opt-out on its channel |
| E15 | every pinned version's channel types are readable vocabulary |

`tests/integration/test_entitlement_invariants.py` injects one violation per check
and proves exactly that check counts it (19 tests). The previous session also
proved the ledger non-vacuous against four mutants (`a3216d7`).

**Oracle.** `tests/integration/entitlements_oracle.py` is per-workspace SQL
written from the decisions. It must agree with `EntitlementService` and with the
set-wise `ChannelCapacityCensus` for every workspace and moment, across:

- monthly and yearly terms
- active, trialing, past_due, suspended, cancelled, expired and no subscription
- bases of 0, N and unlimited, plus a legacy version and an explicit "no channel"
- general and typed slots, live, expired, future, refund_review and cancelled
- grants, live and expired
- connections active, disabled and released on every channel
- AI charges on several channels, holds fresh and stale, releases
- reductions pending and resolved

Final code: **168 comparisons (42 workspaces × 4 moments), 0 mismatches**
(118 over limit, 50 within). At 0 on the migration-built E2E database: R-13 (§32).

## 23. Observability And Alerts

Closed labels only, with no workspace, connection, contact, product, invoice or amount:

| Metric | Labels |
| --- | --- |
| `wasla_entitlement_refusals_total` | `key` ∈ {channel_connections, allowed_channel_types, period_ai_turns}, `reason` ∈ {capacity_exceeded, type_not_allowed, quota_exhausted} |
| `wasla_ai_turn_charge_total` | `outcome` ∈ {held, charged, released, hold_expired, late_charge} |
| `wasla_channel_capacity_reductions_total` | `cause`, `resolution` |
| `wasla_channel_capacity_reduction_disables_total` | `actor` ∈ {owner, system} |
| `wasla_channel_capacity_over_limit_workspaces` | gauge, set-wise SQL at scrape (`ChannelCapacityCensus`) |
| `wasla_ai_turn_holds_open`, `wasla_ai_turn_holds_past_ttl` | gauges |
| `wasla_channel_capacity_reductions_open` | gauge |

Alerts:

- `AITurnHoldsStuck`: `max(wasla_ai_turn_holds_past_ttl) > 0` for 15 m.
- `ChannelCapacityAutoDisableSpike`: more than 20 system disables in 1 h.
- `AITurnLateChargeSpike`: more than 5 late charges in 1 h.

Each has a promtool unit test that fires, clears, and stays silent on ordinary
traffic (owner disables, ordinary charges and releases). Runbook procedures for
each are in `docs/RUNBOOK.md`.

## 24. Migrations And Alembic Gates

| Migration | Change | Online |
| --- | --- | --- |
| 0092 | `channel_kind` + `telegram`, `tiktok` | autocommit `ADD VALUE` |
| 0093 | `topup_entitlement` + `channel_connections`; `allowed_channel_types` (versions, plans); typed top-ups; `topup_product_plans`; connection disable provenance; retired-key and terms triggers; number products → typed WhatsApp slots | nullable, NOT VALID → VALIDATE, 15 s lock_timeout |
| 0094 | `channel_not_in_plan`; `agent_turns` charge state; `usage_events` channel / connection / turn; two partial indexes `CONCURRENTLY` | metadata-only; final autocommit block |
| 0095 | three audit labels; reductions; pre-selections | new tables |
| 0096 | neutral meter labels + channel CHECK | autocommit VALIDATE |
| 0097 | `contact_channel_consents`; opt-outs moved; four `contacts` columns dropped | one INSERT…SELECT |
| 0098 | nullable top-up price + active-needs-price CHECK; placeholder versions; six unpriced products | catalogue only |

Gates on a fresh database at `25583ce` (`alembic/` and `app/db/` are unchanged since):

- `alembic heads` → `0098` (single)
- empty → head: 98 steps in 12 s
- **0 NOT VALID constraints, 0 invalid indexes, 0 disabled triggers**
- `alembic check` → no operations
- downgrade 0098 → 0091: 7 steps, 0 / 0 / 0
- re-upgrade: 7 steps, clean, `alembic check` clean
- `db_preflight verify` ok
- `omnichannel_invariants verify` ok

Model / migration schema parity runs in the migration-built lane.

Downgrades refuse rather than lose data (table in `docs/RUNBOOK.md`). Found and
fixed here (`9695ec0`): 0094's downgrade dropped its indexes `CONCURRENTLY` in an autocommit
block, which committed every step above it. A refusal below 0094 then left the
stamp at 0094 without its indexes, and `upgrade head` never rebuilt
`uq_usage_events_tenant_id_agent_turn_id`. They are now dropped in the transaction,
the whole 0092–0098 downgrade is one transaction, and
`test_a_downgrade_refused_below_0094_rolls_back_the_whole_run` (failed before
the fix) proves it.

0091 (before this stage) has the same pattern. A refused downgrade that crosses
it stops stamped 0091 without its index, and a plain `upgrade head` never
rebuilds it. This was measured and documented with its recovery
(`alembic stamp 0090` + `upgrade head`), and a separate fix task was suggested.
Enum labels cannot be dropped and stay.

## 25. API Compatibility

There is no frontend and no production client. Removals and changes, stated:

- **Channel connect and enable: 402 → 409** `channel_capacity_exceeded` /
  `channel_type_not_allowed`. Every other key keeps 402.
- **`whatsapp_numbers` is removed** from the entitlements and features responses
  as a limit. `/features` lists it as `retired` with `replaced_by:
  channel_connections`. The platform overview's `whatsapp_numbers` counts keep
  their meaning.
- **The contact opt-out response** loses its person-level fields for
  `channels[]`. `POST /contacts/{id}/opt-out` requires `channel`, and
  `DELETE` requires `?channel=`.
- **Plans, versions and custom plans** require `allowed_channel_types`, and
  `whatsapp_numbers` is 422.

Everything else is additive. OpenAPI, `docs/API.md` and
`docs/AUTHORIZATION.md` are updated, and the documentation-truth tests pass
(202 operations, 17 unauthenticated).

## 26. Mutation Matrix

The runner (`mutate2.py`) works as follows:

- It keeps each file's bytes in memory, compares sha256 after restore, deletes
  the module's `.pyc`, and sets `PYTHONDONTWRITEBYTECODE=1`.
- **KILLED** requires exit 1 *and* a FAILED line naming one of that mutant's own
  killer tests. Exit 2–5 is INVALID, a hang is TIMEOUT, and failures elsewhere
  are KILLED_ELSEWHERE, so none can pass as a kill.
- A control run of every killer on unmutated code must pass first.

Final run at `da0d785` (the application and every killer test are unchanged at `b85b717`; 2026-10-08 14:18–14:34 UTC, worktree `E:\wasla-ent-mut`, model-built `wasla_ent_mut`): control **PASS** (42 killer tests: 43 passed in 112.32s (0:01:52)); **46 / 46 KILLED, 0 survived**, every file restored byte-for-byte.

| ID | Mutation | Killed by | First assertion |
| --- | --- | --- | --- |
| M-E01 | Charge at engagement again (no hold) | `test_a_provider_failure_is_not_charged_and_gives_its_hold_back` | AssertionError: assert None is <AITurnChargeState.RELEASED: 'released'> |
| M-E02 | Settle charges an empty provider response | `test_an_empty_answer_is_not_charged` | AssertionError: assert <AITurnChargeState.CHARGED: 'charged'> is <AITurnChargeState.REL... |
| M-E03 | A reply whose delivery failed is not charged | `test_a_reply_whose_delivery_fails_is_still_charged` | AssertionError: assert <AITurnChargeState.HELD: 'held'> is <AITurnChargeState.CHARGED: ... |
| M-E04 | Open holds ignored when counting | `test_ten_overlapping_turns_against_three_charge_three_and_hand_off_seven` | AssertionError: assert {'charged': 10} == {'charged': 3, 'none': 7} |
| M-E05 | Hold taken without the advisory lock | `test_ten_overlapping_turns_against_three_charge_three_and_hand_off_seven` | AssertionError: assert {'charged': 10} == {'charged': 3, 'none': 7} |
| M-E06 | AI turns counted per channel | `test_each_channel_draws_from_the_workspace_total` | AssertionError: assert [(<Channel.ME...6bae21a316'))] == [(<Channel.ME...6bae21a316'))] |
| M-E07 | A second settle records a second charge | `test_settling_twice_charges_once` | AssertionError: assert (<SettleResul... 'unchanged'>) == (<SettleResul... 'unchanged'>) |
| M-E08 | Expired holds keep counting | `test_an_expired_hold_stops_counting_and_is_released_by_the_sweep` | assert (1, False) == (0, True) |
| M-E09 | The guard counts disabled connections | `test_disabling_frees_the_slot_and_enabling_needs_it_back` | AssertionError: a disabled number frees its slot |
| M-E10 | WhatsApp enable bypasses the guard | `test_disabling_frees_the_slot_and_enabling_needs_it_back` | AssertionError: {"id":"58d5eae0-1898-44e1-9c5f-3ae8d2854a07","phone_number_id":"1769795... |
| M-E10b | Neutral enable forges a slot (bypass) | `test_an_enabled_connection_of_another_channel_needs_its_slot_back` | Failed: DID NOT RAISE ChannelCapacityExceededError |
| M-E11 | The guard without the advisory lock | `test_ten_whatsapp_connects_against_one_free_slot_leave_exactly_one` | AssertionError: ['whatsapp:created', 'whatsapp:created', 'whatsapp:created', 'whatsapp:... |
| M-E12 | WhatsApp pre-check removed (Meta called at capacity) | `test_a_second_connection_on_a_one_connection_plan_is_409_and_meta_is_not_asked` | AssertionError: a workspace at capacity never calls Meta |
| M-E12b | The adapter pre-check refuses nothing | `test_an_adapter_is_refused_before_it_asks_its_provider` | Failed: DID NOT RAISE ChannelTypeNotAllowedError |
| M-E13 | Allowed channel types not checked | `test_a_type_the_plan_does_not_include_is_refused_with_a_slot_free` | Failed: DID NOT RAISE ChannelTypeNotAllowedError |
| M-E14 | Missing `allowed_channel_types` read as every type | `test_a_version_published_before_adr_131_is_held_to_its_number_limit` | AssertionError: assert (2, frozenset...'whatsapp'>})) == (2, frozenset...'whatsapp'>})) |
| M-E15 | A typed slot usable by any type | `test_a_typed_instagram_slot_never_seats_a_whatsapp_number` | Failed: DID NOT RAISE ChannelCapacityExceededError |
| M-E16 | A typed top-up sold for a type the plan lacks | `test_a_slot_for_a_channel_the_plan_does_not_include_is_never_sold_or_granted` | Failed: DID NOT RAISE TopupNotAvailableError |
| M-E17 | Eligible plans ignored | `test_a_product_offered_to_other_plans_is_invisible` | AssertionError: assert UUID('03b03daf-0ad3-4527-ae04-075db80f94be') not in {UUID('03b03... |
| M-E18 | Channel top-up expires with the usage cycle | `test_a_yearly_terms_slot_lasts_the_year_not_the_month` | AssertionError: the paid year |
| M-E19 | Effective capacity omits paid channel top-ups | `test_a_general_topup_on_starter_seats_three_channels_and_refuses_a_fourth` | app.services.channel_capacity.ChannelCapacityExceededError: This workspace has no free ... |
| M-E20 | A scheduled downgrade does not restrict new connections | `test_a_scheduled_downgrade_refuses_an_eighth_and_the_boundary_opens_a_grace` | Failed: DID NOT RAISE ChannelCapacityExceededError |
| M-E21 | Automatic fallback 2 s before the grace ends | `test_with_no_choice_the_fallback_disables_disallowed_types_then_the_newest` | AssertionError: a second early, nothing is disabled |
| M-E22 | Fallback keeps the newest | `test_with_no_choice_the_fallback_disables_disallowed_types_then_the_newest` | AssertionError: assert {UUID('48a522...097a6aef456')} == {UUID('03ed2c...e20fe1f956a')} |
| M-E23 | Fallback ignores the allowed types | `test_with_no_choice_the_fallback_disables_disallowed_types_then_the_newest` | AssertionError: assert {UUID('183f57...d9cd25aba21')} == {UUID('183f57...ad9badfea80')} |
| M-E24 | A reduction releases instead of disabling | `test_the_owner_keeps_three_and_the_other_four_are_disabled_never_released` | assert False |
| M-E25 | A selection that does not fit is accepted | `test_the_owner_keeps_three_and_the_other_four_are_disabled_never_released` | Failed: DID NOT RAISE CapacitySelectionError |
| M-E26 | Suspension opens a reduction | `test_a_suspended_workspace_over_capacity_opens_nothing_and_disables_nothing` | assert <app.db.models.channel_capacity.ChannelCapacityReduction object at 0x000001F8164... |
| M-E26b | A migration adopted at settlement dates its grace by the wall clock | `test_a_migration_to_a_smaller_version_opens_the_same_flow` | AssertionError: assert (<CapacityRed...tomatically'>) == (<CapacityRed...g_selection'>) |
| M-E26c | A platform grant expiring is not a capacity boundary | `test_an_expiring_platform_grant_opens_the_same_flow` | assert None is not None |
| M-E27 | AI allowed on a channel the plan in force excludes | `test_suspended_the_instagram_ai_is_refused_and_whatsapp_still_answers` | AssertionError: assert (<TurnOutcome.REPLIED: 'replied'> is not None and 'replied' == '... |
| M-E27b | Campaign copy sent on an excluded channel | `test_a_campaign_on_a_channel_the_plan_in_force_excludes_is_skipped[suspended]` | assert 2 == 0 |
| M-E27c | Follow-up sent on an excluded channel | `test_suspended_a_follow_up_on_instagram_is_skipped` | AssertionError: assert <FollowUpStatus.SENT: 'sent'> is <FollowUpStatus.SKIPPED: 'skipp... |
| M-E28 | Audience opt-out applied person-wide | `test_a_stop_on_whatsapp_covers_every_number_and_not_instagram` | AssertionError: assert set() == {UUID('145a8a...46a58425c21')} |
| M-E28b | Follow-up guard reads WhatsApp's consent for every channel | `test_a_stop_on_whatsapp_covers_every_number_and_not_instagram` | AssertionError: assert <FollowUpStatus.SKIPPED: 'skipped'> is <FollowUpStatus.SENT: 'se... |
| M-E28c | Send-time guard reads the wrong channel | `test_a_campaign_copy_already_queued_is_skipped_after_a_stop_on_whatsapp` | assert (1, 0) == (0, 1) |
| M-E29 | The opt-out writer ignores the channel (always WhatsApp) | `test_a_stop_on_instagram_opts_out_instagram_alone` | assert (None is not None) |
| M-E29b | Ingestion records a stop on WhatsApp whatever the channel | `test_a_stop_on_instagram_opts_out_instagram_alone` | assert (None is not None) |
| M-E29c | A resume lifts every channel | `test_a_resume_on_whatsapp_resumes_whatsapp_alone` | assert None is not None |
| M-E30 | Schema lets `whatsapp_numbers` through on a new version | `test_the_retired_number_key_is_refused_by_name_on_a_plan_and_a_version` | AssertionError: {"error":{"code":"conflict","message":"Another version was published at... |
| M-E30b | Database trigger lets `whatsapp_numbers` through | `test_a_new_version_naming_the_retired_key_is_refused_by_the_database` | Failed: DID NOT RAISE Exception |
| M-E31 | Registry accepts a channel with no meter mapping | `test_a_channel_without_a_decided_meter_cannot_be_registered` | Failed: DID NOT RAISE ValueError |
| M-E31b | `period_messages` omits the neutral meters | `test_a_second_channel_is_metered_under_the_neutral_meters_never_as_whatsapp` | assert (0, 1, False) == (2, 1, True) |
| M-E31c | WhatsApp counted under the neutral meters too | `test_a_whatsapp_message_is_counted_once_under_its_own_meter` | AssertionError: assert [(<UsageEvent...dc7a0c8a28'))] == [(<UsageEvent...dc7a0c8a28'))] |
| M-E31d | A neutral message row loses its connection dimension | `test_a_second_channel_is_metered_under_the_neutral_meters_never_as_whatsapp` | AssertionError: assert [('message_re...0ede3d830e'))] == [('message_re...0ede3d830e'))] |
| M-E32 | Another workspace's connection accepted in a selection | `test_another_workspaces_connection_in_a_selection_is_not_found` | Failed: DID NOT RAISE NotFoundError |

The first full run (`9e82798`) found one survivor: **M-E11** (the guard without
the advisory lock). Applied by hand it oversold (7 of 10 created, active 9 against
3), so the guard was sound and the test's overlap was not. Through the runner it
was killed 1 run in 3. The `GuardGate` (`045b640`) made the overlap deterministic,
and M-E11 was then killed 5 of 5 before the final run. Earlier survivors, now
killed: M-E18 (by `test_a_yearly_terms_slot_lasts_the_year_not_the_month`) and
M-E21 (a grace −1 s assertion added in phase 7). M-E30 had no killer: no test
asserted the API's refusal of the retired key, so one was written (`b7f752e`).
M-E26b and M-E26c came with `da0d785`. They undo the settlement moment fix,
and they take a platform grant's expiry out of the boundaries, so the two new
ENT-15 tests must kill them. The previous tool counted captured `ERROR` log
lines as failures. The new runner does not.

## 27. Test Non-Vacuity

- **ENT-02:** turns run through the real `AgentWorker`, claim, engagement,
  orchestrator and settle. OpenAI and Meta are faked at the httpx transport, so
  the real clients' requests are what is answered. Outcomes are produced, not
  hand-set.
- **ENT-03:** the gates prove every racer reached the decision
  (`gate.decided == 10`). A slow provider holds the three winners open while the
  seven decide. M-E04 and M-E05 die for the asserted counts.
- **ENT-08:** the connect goes through the real ASGI app and route
  (`POST /whatsapp/accounts`). The Meta fake's call count is unchanged at
  capacity (M-E12 dies on that count, not on the status).
- **ENT-09:** the refusal is `ChannelTypeNotAllowedError` with the guard's
  details, from a registry where TikTok *has* a synthetic adapter. "No adapter"
  is a separate test.
- **ENT-14 / 15:** boundaries are crossed by `BillingWorker.run_once` at a
  moment, and top-up expiry by the clock and the same sweep.
- **ENT-19:** the audience comes from the real builder and the copy from the real
  send-time guard.
- **Invariants:** each E-check is shown counting exactly one injected row.
- **Mutations:** every KILLED verdict names its killer's FAILED line. The first
  assertion line is recorded (e.g. M-E13 "DID NOT RAISE ChannelTypeNotAllowedError").

## 28. Model-Built Test Results

`WASLA_TEST_SCHEMA` unset (schema from the models), database `wasla_ent_models`,
worktree `E:\wasla-ent-lane` at the code head:

Final run at `b85b717`, 2026-10-08 16:43:53–17:20:24 UTC (0:36:15), run alone after the migration-built lane:

**collected 6,749 — passed 6,731, failed 0, skipped 18, xfailed 0, xpassed 0**
(4 warnings: `websockets.legacy` deprecations and two deliberately short test
HMAC keys).

Earlier full model-built runs of this stage: `d43c2ea` 6,726 passed; `9b7c681` 6,729 passed; `da0d785` 6,731 passed (each 0 failed, 18 skipped).

The 18 skips are the same set as on every earlier lane of this stage, and none
is new:

| Skips | Where | Why |
| --- | --- | --- |
| 11 | `tests/real_provider/test_openai_contract.py` | real OpenAI is opt-in (no key), and the brief forbids it |
| 4 | `tests/integration/test_schema_parity.py` | parity is only meaningful against a migration-built database; they run in §29 |
| 1 | `tests/unit/test_local_storage_cleanup.py` | needs a size-limited filesystem (`WASLA_TEST_TMPFS`) |
| 1 | `tests/integration/test_ai_invariants.py` | sweeps only a run that kept the AI suites' data |
| 1 | `tests/integration/test_tool_invariants.py` | sweeps only a run that kept the tool suites' data |

Targeted suites inside it, all green:

- the entitlements suites (`test_ai_turn_charging`, `test_channel_capacity`,
  `_concurrency`, `test_channel_topups`, `test_capacity_reductions`,
  `test_channel_not_in_plan`, `test_channel_consent`, `test_entitlements_api`,
  `test_entitlement_invariants`, `test_entitlements_oracle`,
  `test_entitlement_migrations`, `test_entitlement_observability`)
- the billing family (`test_topups`, `test_topup_concurrency`, `test_annual_billing*`,
  `test_custom_plan*`, `test_billing_*`, `test_settlement_concurrency`)
- the omnichannel / WhatsApp suites

Triage of the earlier run (`9592344`, 7 failed / 6,718 passed):

- The two AI races were the lock contention fixed in `cbfc273`. A cold-database
  straggler artefact in the test gate remained and was fixed in `d43c2ea`,
  reproduced with a 25 s straggler: the old gate failed exactly so, the new one
  passed.
- Two oracle-suite assertions predated the E-ledger (`466521b`).
- The catalogue assertion failed on eight plans leaked by the disclosure page
  (`7188f29`).
- Two structural worker tests read `ai_worker.py` while the contention fix was
  being edited in the same worktree. They pass on the committed code, and every
  lane now runs in its own worktree.

A full rerun at `d43c2ea` gave **6,726 passed, 0 failed, 18 skipped**.

## 29. Migration-Built Test Results

`WASLA_TEST_SCHEMA=migrations` (fresh `alembic upgrade head`), database
`wasla_ent_migrations`, never run concurrently with the model-built lane:

Final run at `b85b717`, 2026-10-08 16:06:19–16:43:29 UTC
(0:36:54), database built by `alembic upgrade head` from empty, run alone before
the model-built lane:

**collected 6,749 — passed 6,735, failed 0, skipped 14, xfailed 0, xpassed 0**

The schema-parity tests run here: the model and migration schemas agree,
constraint by constraint.

The earlier migration-built runs found one class of test defect, in two
places. Fixtures built workspaces with **no subscription**, which on this schema
read the seeded default plan (one connection, WhatsApp only). The ledger then
judged the fixture instead of the code under test:

- at `da0d785`, the E09 and E10 injections (their Instagram and Messenger setup
  already counted under E02), fixed in `fb9b1d0`;
- at `fb9b1d0` (6,734 passed, 1 failed), the omnichannel write-path oracle's
  population (E01 and E02), fixed in `b85b717`.

No product path makes a workspace without a subscription. This run comes after
both fixes. The skips:

| Skips | Where | Why |
| --- | --- | --- |
| 11 | `tests/real_provider/test_openai_contract.py` | no OPENAI_API_KEY in the environment; real-provider tests are opt-in |
| 1 | `tests/integration/test_ai_invariants.py` | only a run that kept the AI suites' data has something to prove was swept |
| 1 | `tests/integration/test_tool_invariants.py` | only a run that kept the tool suites' data has something to prove was swept |
| 1 | `tests/unit/test_local_storage_cleanup.py` | No size-limited filesystem provided in WASLA_TEST_TMPFS. |

## 30. Ruff / Black / MyPy

At the code head `b85b717`: `ruff check .` → **All checks passed** (two S608
findings in 0097 fixed first, `25583ce`); `black --check .` → **885 files
unchanged**; `mypy app tests` → **Success: no issues found in 778 source files**.
No broad `type: ignore` added.

## 31. Prometheus / Alertmanager Gates

`promtool check config` → valid. `promtool check rules alerts.yml` → **SUCCESS:
70 rules**. `promtool test rules tests/alerts_test.yml` → **SUCCESS** (the three
new alerts fire, clear and stay silent on ordinary traffic).
`amtool check-config alertmanager.yml` → **SUCCESS**. Images:
`prom/prometheus:v3.1.0`, `prom/alertmanager:v0.28.0` (the CI pins).

## 32. Runtime Verification

### APP-E2E

These ran against:

- the real API (uvicorn on a socket) and the real `AgentWorker` and `BillingWorker`
- the real Paymob adapter, signing and verifying
- PostgreSQL built by `alembic upgrade head` from empty (`wasla_ent_e2e`, 98
  steps) and its own Redis
- OpenAI, Meta Graph and Paymob faked at the httpx transport, so nothing left the
  machine

Time was moved only in that database, and only for billing boundaries. Code head
`da0d785` (application unchanged at `b85b717`):

Run 2026-10-08 14:21–14:24 UTC, **14 / 14 PASS** (`evidence.jsonl` in the
session scratch folder). Every figure matches the earlier run at `25583ce`
(10:14–10:18), which also passed 14 / 14.

| # | Scenario | Result |
| --- | --- | --- |
| R-1 | Allowance 5; turns on WhatsApp and the synthetic Instagram channel, then a 6th | **PASS** - charges WhatsApp 3 + Instagram 2 = **5 of 5**, `used_by_channel` {whatsapp 3, instagram 2}, `held` 0; the 6th `quota_blocked`, handed off `AI_QUOTA_EXHAUSTED`, no provider call (5 agent calls for 6 turns) |
| R-2 | Provider error, timeout, empty answer, then an answer; a reply whose delivery fails | **PASS** - error / timeout → released `generation_failed` (2), empty → released `not_chargeable` (1), **0 charges**; the answer **1** charge; the failed delivery **charged** |
| R-3 | 10 workers, allowance 3 | **PASS** - 3 inside the provider holding while 7 decided, **3 charged, 3 replies, 7 handed off** |
| R-4 | Starter: a second number | **PASS** - 201 then **409 `channel_capacity_exceeded`** (`effective_limit` 1, `active` 1), **Meta fake 0 calls** for the refused one |
| R-5 | Pro: TikTok with 3 free slots | **PASS** - **409 `channel_type_not_allowed`**, allowed `[whatsapp, instagram, messenger]` |
| R-6 | General channel top-up +2 on Starter (150.00, fake Paymob, real adapter signing) | **PASS** - limit 1 + 2 = **3**; connects 201, 201, then 409; the replayed callback `received`, still **3**, one purchase granted |
| R-7 | Typed Instagram slot on Pro | **PASS** - unpriced activation 409; listed to Pro only `instagram-connection-1`; TikTok slot checkout **422**; a 4th WhatsApp number **409**; limit 4 = 3 + 1 typed, active WhatsApp 3 + Instagram 1 |
| R-8 | Business with 7 (every channel) → Pro 3, no selection, grace passed (real billing worker) | **PASS** - 8th connect 409 after scheduling; reduction `downgrade` / `pending_selection`, grace 7 days; after the grace: Telegram, TikTok (types Pro lacks) and the newest Messenger and WhatsApp **disabled (4)**, the oldest 3 kept, **0 released, 0 deleted**, 4 audited disables, `resolved_automatically` |
| R-9 | Same, owner selects | **PASS** - selection of 4 → 422, a selection keeping TikTok → 422, the chosen 3 → 200; 4 disabled by the owner, 0 released |
| R-10 | Channel top-up expiring at the term end, then slots bought in the grace | **PASS** - reduction `topup_expired`, grace 7 days, limit 1 / used 3 `over_limit`; a top-up bought in the grace → `no_longer_needed`, **0 disabled**, 3 WhatsApp active |
| R-11 | Suspended workspace with WhatsApp + Instagram, then pays | **PASS** - limit 1 / used 2 `over_limit`, a new connection 409, **0 reductions**; the Instagram turn `channel_not_in_plan`, **0 provider calls, not charged**; WhatsApp still answered and charged; paying (201) → `active`, limit 3, `over_limit` false, nothing to re-enable |
| R-12 | STOP on WhatsApp number A; campaigns on A, B and the synthetic Instagram | **PASS** - audiences before 1 / 1 / 1, after **0 / 0 / 1**; consent row `whatsapp`, opted out, `customer` |
| R-13 | `omnichannel_invariants verify` and the SQL oracle on this database | **PASS** - exit 0, **42 checks, all 0** (E01–E15 included); oracle on 10 workspaces, **0 mismatches** |
| R-14 | Every secret of the run in the logs, the evidence and a `pg_dump --data-only` | **PASS** - 9 secrets (JWT, HMAC, Meta app secret, Paymob secret and public, encryption, fingerprint, OpenAI, WhatsApp token) over 147,639 + 6,082 + 217,492 bytes: **0 found** |

ENT-14 / ENT-15 timings, read from this run's database (UTC, 2026-10-08):

| Scenario | Reduction | Grace start → end | Resolved | Kept / disabled / released / deleted | Disabled at |
| --- | --- | --- | --- | --- | --- |
| R-8, no choice | `downgrade` → `resolved_automatically` | opened at the boundary; moved back in this database so it ended at 14:23:18, a minute before the sweep | 14:24:19.67 by the billing worker | 3 / 4 / **0** / **0** | 14:24:19.75–.80 (`capacity_reduction_automatic`) |
| R-9, owner chooses | `downgrade` → `resolved_by_owner` | 14:24:18.99 → 10-15 14:24:18.99 | 14:24:20.24 by the owner | 3 / 4 / **0** / **0** | 14:24:20.27–.30 (`capacity_reduction`) |
| R-10, slots bought in the grace | `topup_expired` → `no_longer_needed` | 14:24:18.99 → 10-15 14:24:18.99 | 14:24:20.82 | all 3 stay active / 0 / 0 / 0 | — |

"Deleted" is 0 by construction (nothing deletes a connection), and E10 counts
any listed connection that is not still there and unreleased: 0 (R-13).

### Real Paymob Test

**Authorised in this session.** The user pointed to `E:\secrets.txt` and allowed
ngrok and Chrome. Setup:

- **API.** Run from this worktree on `127.0.0.1:57455`, at `9b7c681`, with
  `BILLING_PROVIDER=paymob`. The later `da0d785` changes only how a renewal
  adopts new plan terms, a path a top-up never takes. The launcher read only the four
  Paymob values from the secrets file and refused unless both keys were Test
  keys (`egy_sk_test…` / `egy_pk_test…`). It printed no value.
- **Data.** Evidence database `wasla_ent_paymob` (migrated from empty, 98
  steps), Redis `56155/7`, integration **5885262**.
- **Prices** are synthetic Test values set on that database only:
  `channel-connection-1` 100.00 and `whatsapp-connection-1` 120.00 EGP. The
  seeded products stay unpriced in the code (ENT-20).
- **Tunnel.** No ngrok agent was running at the start of this session. One was
  started on the static domain the Paymob integrations already name; public
  `/health/ready` gave 200 and an unsigned callback 403.
  The line was in the ISP's quota-redirect state: every plain-HTTP request,
  even to `example.com`, was answered with a redirect to TE Data's
  `VDSL-Redirection_100` page. So the agent could not fetch its CRL
  ("timed out fetching CRL"). Its CRL check was disabled for this run only,
  through an override file passed with `--config`. The user's ngrok
  configuration was not changed.
- **Browser.** Hosted pages were opened in Chrome through a localhost
  redirector, so no client secret reached a command line or a file. **The user
  entered Paymob's published Test card (Mastercard …2346) on each page.** Claude
  typed no card data.
- **Dashboard.** No Paymob dashboard setting was changed.

| # | Scenario | UTC | Wasla invoice · payment | Paymob order · txn | Amount | Callback | Result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| P-1 | General channel slot +1 on Starter | paid 10:49:10 | `01f83ad6` · `c0f901b9` | 627820563 · **550131044** | 100.00 EGP | `transaction.succeeded` → `applied`; the genuine callback replayed through the ngrok agent at 10:50:22 → 200, **`duplicate`**, no new event row | **PASS - REAL PAYMOB TEST.** Invoice `topup` paid 100.00; purchase granted once; capacity 1 → 3 with P-2 (general 1 + 1, typed WhatsApp 1); still 3 after the replay |
| P-2 | Typed WhatsApp slot +1 | paid 10:49:45 | `6f096ca1` · `2f76c757` | 627820603 · **550131393** | 120.00 EGP | `applied` | **PASS - REAL PAYMOB TEST.** Purchase `channel_type = whatsapp` frozen, granted once; `typed_slots` `[{whatsapp, capacity 1, used 0}]` |
| P-3 | Lost callback (API stopped) | paid 10:51:42; recovered 14:03:00 | `8461a62d` · `14ab507a` | 627822910 · **550132485** | 100.00 EGP | Paymob's callback **502** at the tunnel; Wasla still `pending`, invoice open 0.00, no event; API restarted | **PASS - REAL PAYMOB TEST.** The real billing sweep's Transaction Inquiry settled it **once**: invoice paid, purchase granted, `recovered_by_reconciliation` raised and resolved, capacity 3 → **4**; the next sweep handled **0** |

P-3 needed three sweeps:

1. The first (10:57:07, just past the 300 s grace) found the transaction but
   failed in the harness. The settlement path reads the global settings, as
   the six services that take optional settings do, and the sweep process had
   not been given the environment a deployment's worker has. The whole
   transaction rolled back with nothing granted, and only the reconciler's
   lease marker committed.
2. With the environment primed, sweep 3 settled it once the lease had lapsed
   (the session was paused in between, hence 14:03).
3. Sweep 4 changed nothing.

Totals: **3 real Test payments, 320.00 EGP (Test)**, all on integration 5885262
in `test` mode. 0 Live keys, integrations or transactions.

Evidence database afterwards: 3 settled payments, 3 paid top-up invoices, 3
`transaction.succeeded` events (one per payment, the replay added none) and 1
incident (resolved).

Secret scan of the API log, the ngrok log, every evidence file and a data-only
`pg_dump` of the database, for the four Paymob values and the run's JWT,
encryption, fingerprint and password: **0 found, 0 Paymob client-secret
shapes**. The scratch files that held the session secrets and the hosted URLs
were deleted. The API, the redirector and ngrok were stopped, and the browser
tabs closed.

## 33. Documentation Changes

| Document | What changed |
| --- | --- |
| `DECISIONS.md` | ADR-131; ADR-104, ADR-113 and ADR-117 amended; ADR-122 decisions 1–3 superseded by name, 4–5 standing |
| `docs/BILLING.md` | vocabulary, effective capacity, typed slots, the guard and the adapter pre-check, channel top-ups, the reduction lifecycle, the AI hold, the 409 codes |
| `docs/BILLING_OPERATIONS.md` | channel products, eligibility, typed products, preview impact, reading a reduction, what to tell a customer in grace, the new alerts |
| `docs/AI_AGENTS.md` | charge on a usable outcome; the hold; what is and is not charged |
| `docs/SAAS.md` | the plan table with channel connections and types (placeholders) |
| `docs/WHATSAPP.md` | connect and enable through the guard |
| `docs/API.md` | 202 operations; the new routes, the 409 codes, the changed contracts |
| `docs/AUTHORIZATION.md` | the new routes; connect and enable limits |
| `docs/CAMPAIGNS.md` | per-channel opt-out |
| `docs/OBSERVABILITY.md` | the new metrics and alerts |
| `docs/RUNBOOK.md` | *Entitlements and channel capacity (0092-0098)*, plus: stuck holds, late charges, automatic disables, reading capacity, the 0091 downgrade trap, the rolling-deploy constraint, corrected outcome and refusal sections |
| `README.md` | migration range 0001–0098 |

## 34. Decisions Ledger

| Decision | Status |
| --- | --- |
| ENT-01 | IMPLEMENTED |
| ENT-02 | IMPLEMENTED |
| ENT-03 | IMPLEMENTED |
| ENT-04 | IMPLEMENTED (behaviour unchanged, re-verified: R-1, R-3, `test_ten_overlapping_turns_against_three_charge_three_and_hand_off_seven`) |
| ENT-05 | IMPLEMENTED |
| ENT-06 | IMPLEMENTED |
| ENT-07 | IMPLEMENTED |
| ENT-08 | IMPLEMENTED |
| ENT-09 | IMPLEMENTED |
| ENT-10 | IMPLEMENTED (real Paymob Test: P-1..P-3; plus APP-E2E R-6, R-7, R-10 and the re-pointed top-up suites) |
| ENT-11 | IMPLEMENTED |
| ENT-12 | IMPLEMENTED |
| ENT-13 | IMPLEMENTED |
| ENT-14 | IMPLEMENTED |
| ENT-15 | IMPLEMENTED |
| ENT-16 | IMPLEMENTED |
| ENT-17 | UNCHANGED BY DESIGN (re-verified: 0098's new versions leave the subscriber on the version it bought) |
| ENT-18 | IMPLEMENTED |
| ENT-19 | IMPLEMENTED |
| ENT-20 | IMPLEMENTED (placeholders; real prices EXTERNAL - an operator sets them) |
| ENT-21 | IMPLEMENTED |
| ENT-22 | IMPLEMENTED |
| ENT-23 | UNCHANGED BY DESIGN |
| ENT-24 | IMPLEMENTED |
| ADR-122.1 | superseded by ENT-22 |
| ADR-122.2 | superseded by ENT-05 |
| ADR-122.3 | superseded by ENT-19 |
| ADR-122.4 | UNCHANGED BY DESIGN |
| ADR-122.5 | UNCHANGED BY DESIGN |

## 35. Remaining External Actions

For the operator; none was performed here:

1. Review, then push and merge in order: billing → omnichannel remediation
   (`2c58c35`) → this branch. Origin still lacks `aa11098`.
2. **Deploy with the old processes stopped before `migrate`.** 0093's enum label
   and 0097's dropped contact columns are not readable by the previous image
   (`docs/RUNBOOK.md`).
3. Set real prices on the six channel top-ups and activate the ones to sell.
4. Confirm the placeholder plan values (AI turns, connections, channel types) or
   publish real versions.
5. Review the owner email wording for scheduled, started and imminent capacity
   reductions.
6. Paymob Live verification (its own audit); real-Meta verification belongs to
   each adapter stage.
7. The suggested follow-up: make 0091's downgrade consistent and let
   `db_preflight` detect a missing index.

## 36. Adapter-Stage Prerequisites

What each channel adapter (Instagram, Messenger, Telegram, TikTok) still needs.
None of it is a commercial decision.

- The adapter itself: route, signature check, parser, connect flow, sender and
  `ChannelPolicy` (disclosure where required), registered in `ChannelRegistry`.
- The connect flow calls `ChannelConnectionService.precheck(channel)` before its
  provider ownership proof, then `ChannelConnectionService.connect` (the
  authoritative guard). Enable, disable and release go through the same service.
- A meter mapping onto the neutral meters (`metering.message_meters`). Without
  one, the registry refuses the adapter.
- Its opt-out and resume signals written through the one writer with the
  adapter's channel. A provider preference/refusal signal mapped like
  `user_preferences` / 131050.
- Recorded, redacted real provider payloads for the contract suites, and a real
  provider verification (as `REAL_META_WHATSAPP_E2E_VERIFICATION` did for
  WhatsApp).
- Remove the channel from no list: the plan types, typed products and capacity
  already cover it. Selling it is publishing a version that names it.

## 37. Final Verdict

**ENTITLEMENTS IMPLEMENTED WITH EXTERNAL VERIFICATION REMAINING**

Every condition the brief sets for this verdict holds, at the code head `b85b717`:

- **One workspace AI meter across channels**, proven with two channels: R-1
  (WhatsApp 3 + Instagram 2 = 5 of 5, the 6th handed off),
  `test_one_allowance_is_shared_by_every_channel`,
  `test_each_channel_draws_from_the_workspace_total`.
- **Charged only on successful generation, with hold / charge / release proven
  under concurrency**: the §8 table, R-2, R-3, the gated races six times in a row
  (§21), and M-E01..M-E08 killed.
- **The AI quota cannot be oversold**: 10 against 3 charges exactly 3 in every
  run, and M-E04 / M-E05 die on `{'charged': 10}`.
- **Effective capacity = included + top-ups + grants, typed slots honoured**:
  §10, R-6, R-7, M-E15 / M-E19.
- **Every activation path goes through the guard, with 409s and no provider call
  at capacity**: the activation-writers scan, R-4 (Meta fake 0 calls), the
  neutral `precheck` for adapters, M-E09..M-E12b.
- **Allowed channel types on connect, top-ups and automation**: R-5, R-7, R-11,
  M-E13 / M-E14 / M-E16 / M-E27.
- **Channel top-ups pass every test the number top-up passes** (the re-pointed
  suites), plus **three real Paymob Test payments**: granted once, the replay a
  `duplicate`, the lost callback recovered once by Transaction Inquiry.
- **The capacity-reduction lifecycle through the real billing worker**, all
  four ENT-15 causes: R-8..R-10 and `test_capacity_reductions.py`. The moment
  defect it exposed was fixed in `da0d785`, with M-E26b killed.
- **Suspension and expiry behave as WhatsApp does today**: R-11,
  `test_channel_not_in_plan.py`.
- **Per-channel opt-out through the real campaign audience**: R-12 and
  `test_channel_consent.py`.
- **All meaningful mutations killed**: 46 / 46, control passing, every file
  restored.
- **Invariants and the SQL oracle at 0**: 168 / 0 on the generated population,
  and 42 checks at 0 plus the oracle on the migration-built E2E database.
- **Model-built and migration-built suites green, reported separately**: §28,
  §29.
- **No real provider contacted except the authorised Paymob Test**, and nothing
  pushed, merged or deployed.

What stays open is external to this stage: the operator actions of §35. Those
are pushing and merging in order, real prices for the six channel top-ups, the
placeholder plan values, the reduction email wording, Paymob Live verification,
and real-provider verification per adapter. The suggested 0091 follow-up sits
outside this stage.

> If Wasla registered Instagram, Messenger, Telegram and TikTok tomorrow, could it
> prove that every AI reply on every channel draws from one allowance that
> concurrent traffic cannot oversell and that failed generations never consume,
> that no workspace can hold one connection more than its plan and top-ups allow
> or connect a channel its plan does not include, and that a downgrade or an
> expiring top-up is resolved by the customer's own choice - or, after a week, by
> keeping their oldest connections - without ever deleting anything?

Yes, for everything the backend owns. Each clause is a gated race, an
invariant, a real-database oracle or a mutant-killed test above, on both schema
builds. What each new channel adds is its adapter (§36), not a commercial
decision.

## Required Final Runtime Matrix

| Scenario | Before | After | Verdict |
| --- | --- | --- | --- |
| Provider fails on every round | AI turn charged at engagement (B1: 1 event, turn left engaged) | not charged, hold released (`generation_failed`); R-2 | PASS |
| Reply generated, delivery fails | charged (B1c) | charged (`test_a_reply_whose_delivery_fails_is_still_charged`, M-E03) | PASS |
| 10 concurrent turns, allowance 3 | 3 charged, 3 replies, 7 handed off (B2) | 3 charged, 3 replies, 7 handed off; failing: 0 charged, 3 released; R-3 | PASS |
| Turns on WhatsApp and a second channel | second channel uncounted by channel, meter undecided (B3, B8) | one workspace total, `used_by_channel` shares; R-1 | PASS |
| Second connection on a 1-connection plan | 402 `plan_limit_exceeded`, numbers only (B4a) | 409 `channel_capacity_exceeded`, Meta fake 0 calls; R-4 | PASS |
| Disable #1, enable #1 while #2 active | 200: 2 active on capacity 1 (B4c bypass) | 409 | PASS |
| TikTok on Pro with a free slot | not expressible | 409 `channel_type_not_allowed`; R-5 | PASS |
| Channel top-up +2 on a 1-connection plan | numbers only (B5) | 3 / 3 across channels, replay still 3; R-6 | PASS |
| Typed Instagram slot used by WhatsApp | not expressible | refused; R-7 | PASS |
| Downgrade 10 → 3 with 7 active | `over_limit` for ever, 0 disabled (B6) | owner selection (R-9) or oldest 3 kept after 7 days (R-8); 0 released, 0 deleted | PASS |
| Channel top-up expires | `over_limit` for ever (B5) | reduction, grace, selection; bought in grace → nothing disabled; R-10 | PASS |
| Subscription suspended with 5 connections | `over_limit`, nothing disabled | same; disallowed types not automated, not charged; R-11 | PASS |
| STOP on WhatsApp | person-level (B7: both audiences 0) | WhatsApp only; R-12 | PASS |
| Register a second-channel adapter | refused: undecided meter (B8) | meter decided; refused only without a mapping; needs only an adapter | PASS |
