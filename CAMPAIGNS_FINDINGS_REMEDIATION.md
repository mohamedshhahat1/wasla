# Campaigns & Outbound Messaging — Findings Remediation

| | |
| --- | --- |
| Date | 2026-10-15 |
| Type | **Remediation** of `CAMPAIGNS_OUTBOUND_MESSAGING_AUDIT.md` (audit 10 of 25, 2026-10-13, verdict CAMPAIGNS NOT READY, 52/100) |
| Base | `6ecbdd6` = canonical `989584b` + the audit report only (`git diff --stat 989584b 6ecbdd6` → the report file alone) |
| Branch / worktree | `campaigns-findings-remediation-20261015` at `E:\wasla-campaigns-remediation` |
| Head | see section 2 ("Final head") |
| Alembic | `0100` → **`0106`** (single head) |
| Pushed / merged / deployed | **No.** Nothing pushed, merged or deployed |
| Meta / production | **Not touched.** No real message, template sync or Meta App change; no real provider contacted |

---

## 1. Executive Summary

**Verdict: CAMPAIGNS FINDINGS CLOSED WITH REQUIRED PRODUCT DECISIONS AND EXTERNAL VERIFICATION.**

Every P0 and P1 finding is closed in code, with tests that run real
`CampaignWorker`s, real commits and a fake Meta at the HTTP transport. "Sent
once" is counted as requests that reached the transport, never as rows:

| Release blocker | Before (audit, re-measured here) | After |
| --- | --- | --- |
| **CMP-01** two campaign workers | 3/40, 26/40, and **24/250** people (shipped constants, unscaled) received the campaign twice | **0** people twice in every probe and in 2/4/8-worker tests; meters = recipients sent |
| **CMP-02 / CMQ-11** unserved workspace | suspended / deleted / dunning-suspended: 10/10 sent | 0 sent; campaign `paused` with `status_reason`; `past_due` (the grace) still 10/10 |
| **CMP-03 / CMP-04** allowance under concurrency | 40/40 races both accepted; a wider audience landed on a scheduled campaign (200 vs 10 reserved) | exactly one accepted in 40/40 races; the widening is refused or the schedule is refused |
| **CMP-05** header / button templates | body-only payload; 9 requests for 3 people, `completed`, no reason | header, body and buttons sent; missing values or a media header refused at composition; a parameter refusal costs **1** request and fails the campaign with the code |

Below the blockers, CMP-06..CMP-24 are closed except CMP-17 (Meta's portfolio
messaging limit), which is documented and left as EXTERNAL VERIFICATION
(EXT-CMP-01) with enforcement as the product decision CMQ-06, and CMP-22's
template composite key, an ACCEPTED RESIDUAL because `whatsapp_templates`
carries no `(tenant_id, id)` key.

The guarantees the audit proved are unchanged: the crash matrix K1–K8 gives
exactly one abandoned recipient per crash and nobody twice; every uncertain
answer is abandoned, never resent; the audience boundary, identity pinning,
opt-out writers and readers and tenant scoping are untouched.

Mutation: the audit's 26 mutants and 38 new ones — **all killed or classified**
(no meaningful survivor; four redundant guards, each proven by a combined mutant,
section 23). Model-built and migration-built suites are reported separately in
sections 25–26.

**The answer to the final question** is in section 34.

---

## 2. Starting Repository State

```text
canonical branch       worktree-billing-google-auth
HEAD / origin          989584b4a0fe45b14e81b82ceec65ad61a276084 (0 ahead / 0 behind after git fetch --prune)
git status (E:\wasla)  11 untracked *_AUDIT.md / *_REMEDIATION.md / plan files - the user's, untouched
git stash list         empty
audit branch           campaigns-outbound-audit-20261013 at 6ecbdd6 (= 989584b + the report only)
alembic heads          0100 (single)
Python                 3.12.7
PostgreSQL             16.15 (pgvector/pgvector:pg16)
Redis                  7.4.11 (redis:7-alpine)
Docker / Compose       29.8.0 / v5.5.1
app.__file__           E:\wasla-campaigns-remediation\app\__init__.py (every lane asserted its own)
CMP_REMEDIATION_BASE_HEAD  6ecbdd6
Final head             4b9f1cd (+ this report's commit)
```

No intervening commits on `origin/worktree-billing-google-auth`; nothing to revalidate against.

---

## 3. Findings Revalidation

Every finding was reproduced at `6ecbdd6` before any change, with the audit's own
probes (copied into the untracked `audit/` of the remediation worktree) on this
remediation's own migration-built PostgreSQL:

| Probe | Audit | Reproduced at 6ecbdd6 |
| --- | --- | --- |
| P01 scheduled gap | 3/40 twice, 43 requests | **3/40, 43** |
| P01 lease (scaled) | 26/40, 66 | **26/40, 66** |
| P01b unscaled (5 × 50, rate 60, Meta 1.3 s, replica at 305 s) | 23/250, 273 | **24/250, 274** |
| P02 gated / natural | both accepted / 40/40 | **both / 40/40** |
| P02b sequential | second 402 | **402** |
| P03 suspend / delete / pause / cancel | 10/10, 10/10, 30/30, 5/30 | **10/10, 10/10, 30/30, 5/30** |
| P06 answer matrix | section 17 | **identical row by row** |
| P07 IMAGE header + URL button | body-only; utility/auth/unknown accepted | **same** |
| P08 stop phrases | opt-out, STOP STOP, اوقف, أوقف الرسائل, ستوب, unsub missed | **same** |
| P10 credential refused → resume | 4/5 | **4/5** |
| P11b widen vs schedule | 200 pending vs 10 reserved | **200 vs 10, allowance 50** |
| P12 suspended / past_due | 10/10, 10/10 | **10/10, 10/10** |
| P09 K1/K3/K5/K6/K8 | one abandoned per crash, 0 twice | **same** |
| P13 materialise 20,000 | 9.2 s, 76 MiB | **9.5 s, 76 MiB** |
| CMP-10 / CMP-24 / CMQ-02 | code | confirmed by reading (`advance_status`, `schedule`, manual route) |

**One procedural note.** The first baseline batch ran in the remediation worktree
while the CMP-01 edits were being written, so six probes (P07, P09, P10, P11,
P12, P13) imported half-edited code and failed. They were re-run from a pristine
detached worktree at `6ecbdd6` (`E:\wasla-cmp-rem-base`); the numbers above are
those clean runs.

---

## 4. Locked Remediation Decisions

Applied as the brief locked them (section 8): per-recipient claim + lease token
for CMP-01; served gate per batch and per recipient, lifecycle pauses with
`status_reason` and no automatic resume (CMP-02, CMQ-11 (b)); manual MARKETING
templates admin-only and opt-out-checked, not counted against the allowance
(CMQ-02 (b)); MARKETING-only campaigns (CMP-09); naive `scheduled_at` refused
(CMP-15); the stop vocabulary of CMP-12 and CMQ-12 with the "keep out" list; the
template status webhook handled in code, the Meta subscription left external
(CMQ-05). Not taken, as locked: CMQ-07, CMQ-01, CMQ-03, CMQ-04, CMQ-06, CMQ-08,
CMQ-09, CMQ-10, LP-CMP-*.

**Stop conditions checked, none met.** The follow-up "served" predicate was
extracted without changing what follow-ups do; BILLING.md and the code agree that
`past_due` serves and `suspended` does not (`SERVING_STATUSES`); ADR-093's state
machine is unchanged (guards were added in front of it); ADR-025's audience is
unchanged; every migration is additive and online; the full lanes were not red at
the base for an unrelated reason.

---

## 5. Product Decisions Applied

| ID | Decision | Behaviour change | ADR |
| --- | --- | --- | --- |
| CMQ-11 | (b) dunning `suspended` pauses at the next batch, `subscription_suspended`; `past_due` continues; resume = admin reschedule | yes | ADR-135 |
| CMQ-02 | (b) MARKETING manual template: member 403 `marketing_template_admin_only`; opted-out contact 422 `recipient_opted_out`, nothing staged; not metered against campaigns | yes | ADR-141 |
| CMQ-12 | add stop please, please stop, لا ترسل, remove me; keep كفاية, وقف, بطل رسايل, "Unsubscribe me" out | yes | — (CAMPAIGNS.md) |
| CMQ-05 | `message_template_status_update` parsed and applied; Meta subscription EXTERNAL; no periodic sync | yes | ADR-142 |
| CMP-09 | MARKETING only | yes | ADR-138 |
| CMP-15 | naive `scheduled_at` → 422 `scheduled_at_needs_offset` | yes | ADR-143 |
| CMQ-01, CMQ-09 | unchanged (per channel; demoted admin's campaign continues) | no | — |

---

## 6. CMP-01 — Per-Recipient Ownership, Lease and Two-Worker Proof

**Model** (ADR-134). Before every recipient, two single statements:

1. `CampaignRepository.hold` — `UPDATE campaigns SET next_send_at = :lease WHERE id AND tenant AND status IN ('scheduled','running') AND lease_token = :mine RETURNING id`. Zero rows → the batch stops (pause, cancel, or a lease taken over) without writing the campaign.
2. `CampaignRecipientRepository.take` — `SELECT ... WHERE id AND campaign AND tenant AND status = 'pending' FOR UPDATE SKIP LOCKED` with `populate_existing`. None → skipped, no send, no attempt; otherwise the row is re-read, so the abandon rule and every later decision use the row as it is, never the stale snapshot.

The owned marker is the **existing `message_id` link**, written in TX1 while the
row lock is held (no new recipient column). `campaigns.lease_token` (0101) is
written per claimed row by `claim_due`; `claim_by_id` requires it; the batch
clears it at its end. Both claim branches honour `next_send_at`.

**Lease arithmetic.** The lease is renewed per recipient, so it must outlast one
send: 3 attempts × 10 s read timeout + 2 waits × (30 s `Retry-After` + 50 %
jitter) = **120 s**; `CLAIM_LEASE_SECONDS` stays **300**. A sweep's later
campaigns may lose their claim-time lease while earlier ones send; another
replica then re-takes them with a new token and `claim_by_id` refuses the old
one.

**Clocks.** Due-ness and lease expiry still compare each worker's Python clock.
Skew changes only when a lease is re-taken; it cannot produce a second copy,
because a person's ownership is decided by the row lock and the link.

**Tests** (`tests/integration/test_campaign_two_workers.py`, `campaign_harness.py`): real workers, each with its own `Database`, the real `MessagingService` / `WhatsAppClient` / classifier, `httpx.MockTransport` counting requests per recipient and per worker.

| Test | At 6ecbdd6 | After |
| --- | --- | --- |
| scheduled gap (2 workers, 2 campaigns) | 3 twice | 0 twice, 40 requests, meters = sent = 40 |
| batch longer than its lease | 26-ish twice | 0 twice, 40 requests |
| lease taken over (A stalled 2 s, lease 0.5 s) | **17 twice** | 0 twice; A sent exactly its 3 (nothing after takeover) |
| shipped shape scaled (5 × 50, claim limit 5) | twice | 0 twice, 250 requests, meters = 250 |
| 2 / 4 / 8 workers × 8 campaigns × 30 | twice | 0 twice, 240 requests, meters = 240 |
| worker held between its check and its send (stale-snapshot window) | **8 twice** | 0 twice, 10 requests |

All seven original tests fail at `6ecbdd6`; the eighth was added for mutation
coverage and also fails there.

**Runtime proofs** (after the fix): P01 gap 0/40 (40 requests); P01 lease 0/40 (40);
**P01b unscaled 0/250 (250 requests, 339 s)**; P14 (A killed mid-send on
recipient 5 of 30 while B, clock past A's lease, sweeps) 30 requests, **0 twice,
1 abandoned**. Crash matrix K1, K3, K5, K6, K8 unchanged (section 17).
Invariants CR-01 and CR-02 are 0 over every after-fix row (section 18).

**Counters.** `wasla_campaign_events_total{event}` counts `lease_taken_over`,
`lease_lost` and `recipient_owned_elsewhere`; an alert fires on any
`recipient_owned_elsewhere` (two workers overlapped).

---

## 7. CMP-02 / CMQ-11 — Served Gate

`app/services/workspace_serving.py` is the one predicate (`workspace_serving`,
`workspace_is_served`); `FollowUpService._workspace_is_served` and
`CampaignService._unserved_reason` both call it through the module, which a test
proves by replacing it once and observing both gates refuse.

* Worker: before the batch and before every recipient; unserved → `PAUSED`,
  `status_reason` `workspace_suspended` / `workspace_deleted` /
  `subscription_suspended`, `next_send_at` and `lease_token` cleared, a system
  audit entry, no send, no attempt.
* Lifecycle: `WorkspaceService.suspend` / `delete` and the dunning transition to
  `suspended` (`BillingWorker._dun`) pause live campaigns in the same transaction
  with one `CAMPAIGN_PAUSED` audit entry each naming the actor (platform staff,
  owner, system).
* `campaigns.status_reason` (0102, text + CHECK, closed vocabulary) on every pause
  and failure, on `CampaignRead`, cleared on schedule.

| Test (`test_campaign_served_gate.py`, `test_dunning_lifecycle.py`) | Result |
| --- | --- |
| suspended via `WorkspaceService.suspend` while scheduled | 0 requests; paused `workspace_suspended`; audit by the staff member |
| suspended after Meta has seen 3 of 30 | ≤ 4 requests; paused; meters = sent |
| deleted via `WorkspaceService.delete` | 0 requests; `workspace_deleted`; audit by the owner |
| suspended by a path that paused nothing | the worker gate pauses it (system audit) |
| restored | still paused; admin reschedule → remaining people only, 20 requests for 20, nobody twice |
| dunning `suspended` / `past_due` | 0 / 10 sent |
| the billing worker's transition | paused `subscription_suspended`, system audit; `past_due` transition leaves it scheduled |
| same predicate | both gates call it |

After probes: P03 suspend 0/10, delete 0/10; P12 suspended 0/10 paused, past_due 10/10.

---

## 8. CMP-03 / CMP-04 / CMP-24 — Reservation Under the Lock

`reserve_period` takes each count as a coroutine function and awaits it **after**
`pg_advisory_xact_lock`; `schedule()` passes its own pending count and the other
live campaigns' that way. `schedule()` and `set_audience()` lock the campaign row
(`lock_by_id`, `populate_existing`) and then take the allowance's advisory lock,
in that order. `schedule()` refuses **402 `channel_not_in_plan`** before
reserving (CMP-24; 402 because the answer is an upgrade, like every plan
refusal).

| Test (`test_campaign_reservation.py`) | At 6ecbdd6 | After |
| --- | --- | --- |
| sequential 20 + 20 vs 30 | 402 | 402 (kills M21) |
| 40 vs 30 | 402 | 402 (kills M20) |
| gated at the old decision point | **both accepted** | one scheduled, one 402 |
| natural, 40 trials | **40/40 both** | **1 accepted in each of 40/40** |
| widen racing schedule | **200 pending, scheduled** | widened, schedule 402 |
| set_audience after scheduling | 422 | 422 |
| channel outside the plan | **scheduled, reserved** | 402 `channel_not_in_plan`, nothing reserved, still draft |

The vacuous `test_topup_concurrency.py::test_a_topup_racing_campaign_scheduling_never_over_promises`
now races **four** campaigns of four against a ceiling of 13: a reservation
ignoring other live campaigns schedules all four (16) and fails it; it was killed
by M21 in the mutation run.

After probes: P02 gated `scheduled, 402`; natural **0/40** both accepted; P02b 402;
P11b `widened, 402`.

---

## 9. CMP-05 / CMP-09 / CMP-19 — Template Parameters and Category

**Spec derived, not stored** (ADR-137): `app/services/template_parameters.py`
reads what a template needs from `whatsapp_templates.components` (written
unedited by every sync) — body (positional, or named → `parameter_name`), text
header, dynamic URL suffix, COPY_CODE. No `parameter_spec` column: one copy,
nothing to drift. A row with no components → `422 template_not_synced` ("sync
first"). A media header → `422 template_unsupported` (media-header sending
DEFERRED). Missing or extra values → `422 template_parameters`, naming the
component. New optional request fields `header_variables`, `button_parameters`;
stored in `campaigns.template_parameters` (0103).

**Codes carried.** Per-message refusals carry `provider_code`; `messages.failure_code`
(0103) stores it. 132000/132012/131008 → campaign `FAILED
template_parameters_refused` with the code, after one request, template **not**
paused. 132001/132015/132016, or the registry refusing the template mid-batch
(`TemplateRefusedError`) → `FAILED template_withdrawn`; recipients not yet asked
spend nothing. MARKETING only at create and per batch (CMP-09).

| Test (`test_campaign_templates.py`) | Result |
| --- | --- |
| TEXT header + URL + COPY_CODE with values | payload at the transport: header, body (2), url button index 0, copy_code index 2 |
| named placeholders | `parameter_name` on each |
| header / button values missing | 422 naming header / button |
| media header | 422 `template_unsupported` |
| no stored components | 422 `template_not_synced` |
| 132000 / 132012 / 131008 for 3 people | **1 request**, FAILED, `{pending/1: 1, pending/0: 2}`, code on the message, template approved |
| 132001 on the 3rd of 6 | 3 requests, `{sent: 2, pending/1: 1, pending/0: 3}`, template paused |
| template paused by another sender mid-batch | 2 requests, `{sent: 2, pending/0: 4}`, `template_withdrawn` |
| utility / authentication / unknown | 422 `template_category` |
| recategorised after scheduling | 0 requests, FAILED `template_category` |

Registry by spec shape in the probes: body-only (all P01–P13 workspaces), TEXT
header + URL button (P07), IMAGE header + URL button (P07, refused), named body
(tests). After P07: IMAGE refused `template_unsupported`; the TEXT-header payload
Meta received was `header [Ahmed], body [Ahmed], button url index 0 [spring-20]`.

---

## 10. CMP-06 / CMP-07 / CMP-18 — Answer Classes

| Answer (3 people) | Target | P06 after | Recipients | Campaign / reason |
| --- | --- | --- | --- | --- |
| 200 with id | 3 | **3** | sent ×3 | completed |
| 200 no id / malformed / 500 / 503 / read timeout / reset | 3 | **3** each | failed/1 ×3 (`delivery_unconfirmed`) | completed |
| 132001 | 1 | **1** | pending/1, pending/0 ×2 | FAILED `template_withdrawn`, template paused |
| 132000 / 132012 / 131008 | 1 | **1** each | pending/1, pending/0 ×2 | FAILED `template_parameters_refused` |
| 131049 | 3 | **3** | failed/1 ×3 `marketing_limit_per_user` | completed |
| 131026 | 3 | **3** | failed/1 ×3 `undeliverable` | completed |
| 131050 | 3 | **3** | skipped/1 ×3, consent ×3 | completed |
| 401 / 190 / 368 | 1 | **1** each | pending/0 ×3 | FAILED `credential_refused` / `connection_refused` |
| 403 (no code) | per-message (classifier) | 9 | failed/3 ×3 `attempts_exhausted` | completed — documented (CMP-16) |
| 429 / 130429 | ≤ client retries, wait | 15 over 5 sweeps (3 per sweep) | pending/0 ×3 | running |
| 131056 (every send) | 1 per person per window | 15 over 5 windows = 1 per person per window | pending/0, `not_before` | running |
| 131056 (first person once) | that person deferred, others sent | 3 then 4 (test) | others sent at once; the one sent after its deferral | completed |
| connect error | 0 reached Meta | 15 connection attempts, **0 reached** | pending/0 ×3 | running; **PAUSED `provider_unreachable` after 6 h** (test, connect error and connect timeout) |

**Which httpx errors cannot have delivered.** `ConnectError`, `ConnectTimeout`
and `PoolTimeout` all end before the request is written (no connection, the
TCP/TLS handshake did not finish, no pooled connection free). `ReadTimeout`,
`WriteTimeout`, `RemoteProtocolError` and other transport errors stay
`UncertainDeliveryError` — M-C21 (read timeout re-classified) is killed.

131049 is not re-queued after 24 hours (LATER PRODUCT). 131056 is no longer
retried inside the client for any sender; non-campaign senders still treat it as
a throttle.

---

## 11. CMP-08 / CMP-11 / CMP-20 — Stop, Resume, Sweep Health

* **CMP-08**: pause and cancel are seen at the next recipient through `hold`.
  After P03: pause **5/30** (was 30/30), cancel 5/30. Tests: pause and cancel
  after Meta has seen 5 of 30 → ≤ 6 requests; MSG-22 now has its regression test.
* **CMP-11** (ADR-140): a pending recipient linked to a message whose
  `delivery_state` is `UNDELIVERED` is unlinked and sent, attempts unchanged;
  `REQUESTED` (and anything else) is abandoned. The credential path now clears
  the link at once. After P10: **5/5 received, 0 twice** (was 4/5). Tests: P10
  ported; REQUESTED never re-armed (kills M-C25); a legacy UNDELIVERED link is
  re-armed (kills M-C26).
* **CMP-20**: `next_send_at = batch end + batch/rate`; a raising batch is counted
  (`campaigns.consecutive_batch_failures`, 0104) with a fixed `last_error`, in its
  own transaction and only under the caller's token, and paused
  `repeated_batch_failure` at `CAMPAIGN_BATCH_FAILURE_PAUSE_AFTER` (5); a batch
  that runs resets it. The batch also flushes before counting what remains, so a
  finished batch completes in the same sweep (after P05: `completed` at sweep 0).

---

## 12. CMP-10 / CMQ-05 — Provider Feedback

* `StatusUpdate.error_code` from `errors[0].code`; `advance_status` keeps it on
  `messages.failure_code` whichever way the status ranking goes (MSG-14 unchanged).
* `app/services/provider_feedback.py`: 131050 on a campaign or marketing-template
  message → `record_opt_out` on the conversation's channel, `via=provider_status`
  (0105) and `wasla_opt_outs_total{via="provider_status"}`; 132001/132015/132016 →
  registry `PAUSED` (one write-back function, shared with the send path).
* `CampaignStatistics.failed_after_send`.
* `message_template_status_update` parsed (keyed by `entry.id`), applied to every
  registry row for (account, name, language) via `META_STATUSES`; older than the
  row's last write → ignored; unmapped word → ignored; unknown template → logged.

Tests post **signed webhooks through the real ASGI route** (`test_campaign_provider_feedback.py`):
131050 → consent `provider_status` on WhatsApp and the next audience preview is 2
of 3; 132015 → template paused and a second scheduled campaign fails
`template_withdrawn` with 0 requests; statistics `sent 3, failed_after_send 1,
delivered 0`; the template webhook pauses the registry without a send; an older
event changes nothing; another account's same-named template is untouched.

EXT-CMP-04 (whether Meta reports 131050 asynchronously) stays EXTERNAL; the code
handles it if it does.

---

## 13. CMQ-02 / CMP-12 / CMQ-12 — Opt-Out Surfaces

* Manual template route: member + MARKETING → **403** `marketing_template_admin_only`, 0 requests; admin + opted-out contact → **422** `recipient_opted_out`, 0 requests, nothing staged; admin + not opted out → 201, 1 request; UTILITY / AUTHENTICATION to an opted-out contact by a member → 201, unchanged. Through the real ASGI app with real access tokens.
* Vocabulary — P08 before → after:

| Phrase | Before | After |
| --- | --- | --- |
| opt-out, STOP STOP, stop stop stop, "Stop, stop!", unsub, اوقف, أوقف, أوقف الرسائل, أُوقِف, اوقـــف, ستوب | not matched | **matched** (each through the real inbound path) |
| stop please, please stop, لا ترسل, remove me | not matched | **matched** |
| كفاية, وقف, بطل رسايل, Unsubscribe me | not matched | not matched |
| stop by the shop tomorrow, stop the order, please stop by | not matched | not matched |
| STOP, stop!!, ＳＴＯＰ, RTL-marked, الغاء, ايقاف, توقف, opt out, Stop promotions | matched | matched |

Repetition is compared word for word on the whole message (`_repeats_a_stop_phrase`);
the matcher is never a substring search (M-C34 killed).

---

## 14. CMP-13 / CMP-14 / CMP-15 / CMP-22 — API, Audit, Time, Database

* **CMP-13**: recipients by keyset cursor (`next_cursor`), `status` and `reason` filters; `RecipientRead.reason_code`, `participant_identity_id`. A 250-person campaign is listed whole, once, in order. Closed reason codes (0104, CHECK): opted_out, contact_gone, other_connection, identity_changed, channel_not_in_plan, recipient_opted_out_at_provider, marketing_limit_per_user, undeliverable, delivery_unconfirmed, attempts_exhausted, template_parameters_refused. Old rows: null.
* **CMP-14**: CAMPAIGN_CREATED (account, template, rate), CAMPAIGN_AUDIENCE_SET (count + filters with the number of hand-picked contacts), CAMPAIGN_PAUSED (admin or system, with `status_reason`). No contact id or parameter in audit metadata (asserted).
* **CMP-15**: naive → 422 `scheduled_at_needs_offset`; `2026-10-20T10:00:00+03:00` stored as given.
* **CMP-22** (0106): `CHECK messages_per_minute BETWEEN 1 AND 600` and `(tenant_id, account_id) → whatsapp_accounts (tenant_id, id)`, both NOT VALID → VALIDATE. Template twin: **ACCEPTED RESIDUAL** (no `(tenant_id, id)` key on `whatsapp_templates`; every writer is tenant-scoped).

---

## 15. Observability and Alerts

Counters `wasla_campaign_sends_total{outcome}`, `wasla_campaign_stops_total{reason}`,
`wasla_campaign_events_total{event}`, `wasla_template_status_updates_total{source}`;
`wasla_opt_outs_total{via}` gains `provider_status`. Scrape-time gauges
`wasla_campaigns_stuck`, `wasla_campaigns_stuck_oldest_age_seconds`. Labels are
closed vocabularies only (ADR-072); a unit test holds the stop-reason domain
equal to `CampaignStopReason`.

Alerts (`wasla-campaigns`): `CampaignsStuck`, `CampaignRecipientOwnedElsewhere`,
`CampaignsPausedProviderUnreachable`, `CampaignsPausedRepeatedBatchFailure`, each
with a promtool unit test (fires, clears, silent on ordinary traffic).

---

## 16. Documentation and RUNBOOK (CMP-16, CMP-17, CMP-23)

ADR-134..143 and amendments to ADR-025/026/093/123 (DECISIONS.md). CAMPAIGNS.md:
the send path as built, the answer-class table, the vocabularies, pause, the
corrected double-count sentence (also in `campaign_service.py`), **a bare 403 is
per-message** (CMP-16), the connection allowance and its 0090 override, and
**Meta's portfolio messaging limit** — 250 new, 2,000, 10,000, 100,000, unlimited
unique users per moving 24 h, shared by the portfolio — which Wasla neither reads
nor enforces (CMP-17). RUNBOOK: "Campaigns are not sending" by `status_reason`,
stuck and overlapping workers, and "Campaigns remediation (0101-0106)". API,
WHATSAPP, BILLING, DEPLOYMENT (campaign replicas safe from ADR-134 on, not
before), OBSERVABILITY, ARCHITECTURE updated. `test_documentation_truth` green.

---

## 17. Re-Verification of What the Audit Proved

| Guarantee | Evidence after |
| --- | --- |
| Audience boundary; preview == materialised | `test_campaigns` audience suite green; P11a 200 rows / 0 duplicates; P13 re-run in section 21 |
| No request field accepts a phone, body or list | schema unchanged in substance (only optional header/button values added) |
| Identity changed / other connection → skipped | M14, M15 killed by `test_omnichannel_operations` |
| Crash matrix K1/K3/K5/K6/K8 | **unchanged**: K1 10/10; K3 9 requests, 1 never asked, failed/1 ×1; K5 10, failed ×1; K6 10, failed ×1; K8 40, failed ×1; **0 twice** everywhere |
| Uncertain → 1 request, never resent | P06 rows above |
| 401/190/368 → 1 request, FAILED | P06 |
| 131050 on a send → skipped + consent | P06 (opted_out=3) |
| Cancel → next recipient | P03 cancel 5/30 |
| Replies never refused by the allowance | M19 killed |
| 0097 round trip | 100/100 (section 19) |

---

## 18. Database Invariant Sweep

`python -m scripts.omnichannel_invariants verify` now includes CR-01..CR-07
(`campaign_violations` alone): each has an injection test proving it counts one
violation (M-C38 makes CR-01 vacuous and is killed).

| Database | CR-01 | CR-02 | CR-03 | CR-04 | CR-05 | CR-06 | CR-07 | verify |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `wasla_cmp_rem_probe2` (after-fix probes only) | 0 | 0 | 0 | 0 | 0 | 0 | 0 | **ok** |
| `wasla_cmp_rem_probe`, rows created **after** the fix | 0 | 0 | 0 | 0 | 0 | 0 | 0 | — |
| `wasla_cmp_rem_probe`, rows from the **baseline** runs of 6ecbdd6 | 29 | 2 | 3 | 0 | 0 | 0 | 0 | — |

The baseline counts are exactly the audit's defects re-created at 6ecbdd6: CR-01
29 = 3 (P01 gap) + 26 (P01 lease); CR-02 the two campaigns those probes doubled;
CR-03 the 401/190/368 P06 rows (CMP-11).

Unresolved `requested` + `pending` messages after the fix: **23** = 18 uncertain
P06 rows + 4 crash kills (K3, K5, K6, K8) + 1 P14 kill — exactly the probes that
create them (baseline: 22, as the audit).

---

## 19. Migrations and Alembic Gates

| Revision | Adds | Online | Downgrade |
| --- | --- | --- | --- |
| 0101 | `campaigns.lease_token` | nullable, no default | drops (a lease is not data) |
| 0102 | audit labels; `campaigns.status_reason` + CHECK | autocommit labels; NOT VALID → VALIDATE | refuses while reasons or new audit actions exist |
| 0103 | `campaigns.template_parameters`, `messages.failure_code` | nullable, 15 s lock_timeout on `messages` | refuses while either holds data |
| 0104 | recipient `reason_code` (CHECK), `not_before`; campaign `provider_unreachable_since`, `consecutive_batch_failures` (CHECK, DEFAULT 0) | constant default is catalogue-only (PG ≥ 11) | refuses while any is set |
| 0105 | `opt_out_via` + `provider_status` | autocommit | refuses while used; label stays |
| 0106 | `ck_campaigns_messages_per_minute_bounded`, `fk_campaigns_tenant_account` | NOT VALID → VALIDATE | drops both |

No index was added: the per-recipient claim is a primary-key lookup, and the
`not_before` filter rides the existing partial `ix_campaign_recipients_pending`
(deferred rows are few). No table rewrite; no long lock on `messages` or
`campaign_recipients`.

```text
alembic heads                                   0106 (head) - single
alembic check                                   No new upgrade operations detected
fresh 0001 -> head                              ok (both lanes' fixtures and wasla_cmp_rem_mig)
upgrade head / downgrade base / upgrade head    ok (the CI step)
populated 0100 -> head -> 0100 -> head          tenants 1, campaigns 5 (draft..completed), recipients 100,
                                                consents 100, opt-outs 100, pending 50 - equal at all four points
catalog at head                                 0 NOT VALID constraints, 0 invalid indexes
refusing downgrade (a status_reason set)        RuntimeError "... Nothing has been changed", still at 0106
db_preflight verify                             ok
0097 round trip                                 100 opt-outs -> 100 on contacts at 0096 -> 100 consents at head
test_migration_downgrade_atomicity, test_schema_parity   green
```

---

## 20. API Compatibility

Additive: `CampaignRead.status_reason`, `template_parameters`;
`RecipientRead.reason_code`, `participant_identity_id`;
`RecipientListResponse.next_cursor`; recipients `cursor` and `reason` query
parameters; `CampaignStatisticsRead.failed_after_send`;
`CampaignCreateRequest.header_variables`, `button_parameters`. Every existing
field and route unchanged.

Intentional refusals: naive `scheduled_at` 422 `scheduled_at_needs_offset`;
non-MARKETING 422 `template_category`; missing/extra parameters 422
`template_parameters`; no components 422 `template_not_synced`; media header 422
`template_unsupported`; audience after scheduling 422; channel outside the plan
402 `channel_not_in_plan`; manual MARKETING by a member 403
`marketing_template_admin_only`; to an opted-out contact 422
`recipient_opted_out`. Documented in docs/API.md and docs/CAMPAIGNS.md.

---

## 21. Ported Probe Tests

| Probe | Test file | Notes |
| --- | --- | --- |
| P01 / P01b | `test_campaign_two_workers.py` | scaled in time; P01b also run unscaled (section 6) |
| P02 / P02b, P11b | `test_campaign_reservation.py` | gated and 40 natural trials |
| P03 | `test_campaign_served_gate.py`, `test_campaign_stop_resume.py` | suspension through `WorkspaceService` |
| P06 | `test_campaign_answer_classes.py` | one parametrised matrix, 21 cases |
| P07 | `test_campaign_templates.py` | payload asserted at the transport |
| P08 | `test_opt_out_ingestion.py` | every phrase through the inbound path |
| P09 | the existing delivery-protocol tests cover single sends; the campaign crash matrix stays a runtime probe (real OS processes) and P14 extends it to a live second worker |
| P10 | `test_campaign_stop_resume.py` | |
| P12 | `test_campaign_served_gate.py`, `test_dunning_lifecycle.py` | |

There is no slow/race marker in this project (`pyproject.toml` markers:
integration, e2e, real_provider); every test above is `integration` and runs in
CI's `tests` job (model-built) and the `migration-parity` job (migration-built).
The slowest (two-worker shipped shape, 8 workers) take ~20 s each.

**P13 scale** (20,000 people, after the fix, on a fresh migration-built database
`wasla_cmp_rem_p13`): preview 0.1 s; materialise **8.8 s, 80 MiB** peak Python
memory (baseline 9.5 s, 76 MiB); schedule 0.02 s. The first after-run, on the
long-lived probe database while both full lanes were running, hit the 30 s
statement timeout in the audience preview. The base code (6ecbdd6) hit the same
timeout on that database, so the timeout comes from that database (bulk-seeded
rows without fresh planner statistics), not from this change. The audience query
is unchanged by this remediation.

---

## 22. Audit Mutants Re-Run (M01..M26)

Strict runner (`audit/run_remediation_mutations.py`, untracked): exact anchor
found once, the named killers' suites with `-x`, every file restored byte for byte
(SHA-256 checked) in a detached worktree (`E:\wasla-cmp-rem-mut`) on its own
database and Redis. Control: **276 passed**.

| # | Verdict | Killer |
| --- | --- | --- |
| M01 | KILLED | `test_a_person_appears_in_a_campaign_once` |
| M02 | **REDUNDANT GUARD** (proven) | survives behind the per-recipient claim and hold; `M02+M-C01+M08` killed |
| M03 | KILLED | `test_meta_timeout_is_not_blindly_retried` |
| M04 | KILLED | `test_a_send_that_may_have_left_is_never_re_armed` (was a survivor) |
| M05 | KILLED | answer matrix `200_no_messages` (was a survivor) |
| M06 | KILLED | answer matrix `200_no_messages` |
| M07 | KILLED | answer matrix `401_no_code` |
| M08 (hold removed; was the cancel re-read) | KILLED | pause/cancel mid-batch (was a survivor) |
| M09–M12 | KILLED | audience / mid-campaign opt-out / channel consent |
| M13 | KILLED | `test_a_newer_resume_is_never_overwritten_by_an_older_one` (was a survivor) |
| M14, M15 | KILLED | omnichannel operations |
| M16, M17 | KILLED | template paused / rate written down |
| M18 | KILLED | `test_the_service_refuses_a_rate_above_the_ceiling` (+ the DB CHECK) (was a survivor) |
| M19 | KILLED | reply never held back |
| M20 | KILLED | reservation tests (first failure: the sequential test) (was a survivor) |
| M21 | KILLED | sequential control (was a survivor) |
| M22, M23 | KILLED | channel_not_in_plan / member cannot schedule |
| M24 | KILLED | `test_the_recipient_listing_is_tenant_scoped_on_its_own` (was a survivor) |
| M25 | KILLED | answer matrix `400_132000` (template stays approved) (was a survivor) |
| M26 | KILLED | scheduled gap |

---

## 23. New Mutation Matrix (M-C01..M-C38)

| # | Mutant | Verdict | Killer / proof |
| --- | --- | --- | --- |
| M-C01 | per-recipient claim removed | **REDUNDANT GUARD** while `hold` holds the campaign row from check to send intent | M08 alone passes `a_batch_longer_than_its_lease_is_not_shared`; **M-C01+M08 fails it** - the claim is an independent guard against duplicates |
| M-C02 | scheduled branch ignores the lease | KILLED | scheduled gap |
| M-C03 | `claim_by_id` ignores the token | **REDUNDANT GUARD** behind `hold`'s token check | M-C03+M08 killed (takeover test) |
| M-C04 | per-batch served gate removed | **REDUNDANT** with M-C05 | M-C04+05 killed |
| M-C05 | per-recipient served gate removed | **REDUNDANT** with M-C04 for these tests | M-C04+05 killed |
| M-C06 | lifecycle no longer pauses | KILLED | suspended-workspace audit actor |
| M-C07 / M-C08 | dunning suspended served / past_due unserved | KILLED | dunning matrix |
| M-C09 | other reservations read before the lock | KILLED | gated race |
| M-C10 | audience draft check unlocked | KILLED | widen vs schedule |
| M-C11 | channel_in_plan not at schedule | KILLED | channel outside the plan |
| M-C12 / M-C13 | header / button not emitted | KILLED | payload test |
| M-C14 | spec ignored at composition | KILLED | missing values refused |
| M-C15 | 132000 per recipient | KILLED | one-request parameter refusal |
| M-C16 / M-C17 | category at create / per batch | KILLED | |
| M-C18 | withdrawal continues the batch | KILLED (after a test was added) | template paused elsewhere mid-batch |
| M-C19 | 131049 retried | KILLED | matrix 131049 |
| M-C20 | connect error spends an attempt | KILLED | unreachable test |
| M-C21 | read timeout as not attempted | KILLED | matrix read_timeout |
| M-C22 / M-C23 | 131056 stops campaign / `not_before` ignored | KILLED | pair-limit test |
| M-C24 | hold ignores status | KILLED | pause mid-batch |
| M-C25 / M-C26 | re-arm REQUESTED / abandon UNDELIVERED | KILLED | never re-armed / re-armed |
| M-C27 | batch failures never pause | KILLED | keeps raising |
| M-C28 / M-C29 / M-C30 | async 131050 / 132015 / older template event | KILLED | provider feedback |
| M-C31 / M-C32 | manual MARKETING opt-out / members | KILLED | ASGI tests |
| M-C33 | ستوب removed | KILLED | inbound vocabulary |
| M-C34 | substring matcher | KILLED | "an ordinary message changes nothing" |
| M-C35 | naive time accepted | KILLED | offset test |
| M-C36 | cursor ignored | KILLED | 250-person listing |
| M-C37 | pause not audited | KILLED | admin pause audited |
| M-C38 | CR-01 vacuous | KILLED | CR-01 injection |

**Exit criterion met**: meaningful survivors 0. Every survivor is a redundant
guard with a combined mutant that kills it; none of M04, M05 or any
second-message guard survives. `git status` under `app/` in the mutation worktree
is clean after every run.

---

## 24. Test Non-Vacuity

| Claim | How it was shown |
| --- | --- |
| CMP-01 tests use separate sessions and count requests | each worker has its own `Database` and `httpx` client; 7/7 + the stall test **fail at 6ecbdd6** (17 and 8 people twice) |
| CMP-02 suspension is real | `WorkspaceService.suspend` / `delete` with a staff user / the owner; audit actor asserted |
| CMP-03 gate sits after the old read | the gated race, natural race and P11b fail at 6ecbdd6 |
| CMP-05 payload is the transport's bytes | asserted on `meta.requests[i]["payload"]` |
| CMP-07 raises before the request is written | `MockTransport` raises `ConnectError` / `ConnectTimeout`; read timeout is a separate row |
| CMP-10 status reaches `advance_status` via the route | signed POST through `create_app`'s ASGI app |
| CMQ-02 through the app with a member's token | `create_access_token`, real membership, email verified |
| reservation sequential / over-allowance | killers of M21 / M20 (they pass at 6ecbdd6 by design) |

---

## 25. Model-Built Test Results

`TEST_DATABASE_URL=…/wasla_cmp_rem_models` (pg 55921), full `tests/`, at `a02a423`:

```text
2 failed, 6853 passed, 93 skipped in 2463.75s (0:41:03)
FAILED tests/integration/test_omnichannel_oracles.py::test_no_write_path_breaks_an_invariant
FAILED tests/integration/test_omnichannel_oracles.py::test_the_operator_checks_run_on_a_read_only_replica
```

**Both failures were caused by this remediation.** `violations()` now also returns
CR-01..CR-07 (bea5e7b), while the two oracle tests still compared its names with
the omnichannel and entitlement sets only (extra items `cr01_…` .. `cr07_…`). The
fix is the expected set (`4b9f1cd`, test-only). After it, `test_omnichannel_oracles.py` and
`test_campaign_invariants.py` pass in both lanes (model-built: 4 passed; migration-built: 11 passed).
No other test failed. The skips are the standing ones: no object store, no
OpenAI key, no tmpfs, kept-data-only tests, and schema parity (migration lane only).

## 26. Migration-Built Test Results

`WASLA_TEST_SCHEMA=migrations`, fresh `alembic upgrade head`, its own PostgreSQL
(`wasla-cmp-rem-pg2`, 55922) so the two lanes never shared a server:

```text
2 failed, 6857 passed, 89 skipped in 2463.01s (0:41:03)
FAILED - the same two oracle tests, the same cause; fixed in 4b9f1cd and re-run green
```

Schema parity (the four tests skipped model-built) passed here.

**Intermediate commits not all green.** From `3e0b3bb` to `66e45f2`, the README
migration-range documentation test failed, because the README still said
`0001–0100` after 0101..0103 were added. `0a5c369` corrected the range, and every
commit from there on passes it. The oracle census test (above) failed from
`bea5e7b` until `4b9f1cd`. Each phase's targeted selection was green when it was
committed. These two only showed in the documentation suite and the full run.

**Race suites, three consecutive runs** (`test_campaign_two_workers.py`,
`test_campaign_reservation.py`, `test_topup_concurrency.py`, model-built):
24 passed / 24 passed / 24 passed (80 s, 80 s, 83 s).

Campaign-relevant selections, migration-built, during the work (each phase):
2,080 → 2,089 → 1,415 (phase 7 selection) → 1,034 (phase 8 selection) passed;
the audit's 577-test selection is contained in them.

---

## 27. CI Jobs Run Locally

| Job | Steps | Result |
| --- | --- | --- |
| quality | ruff, black, mypy `app tests` | ruff clean, black unchanged (900 files), mypy **0 errors / 805 files** (tracked tree; the untracked `audit/` probe copies are not in CI) |
| tests | full suite model-built; skip gate; app factory; upgrade → base → head; db_preflight; alembic check | sections 25, 19; application factory ok |
| migration-parity | schema parity; migrated integration/e2e; kept-data sweep | section 26; kept-data sweep (`WASLA_TEST_KEEP_AI_DATA=1`, every `ai_harness` suite + AI and tool invariants, migration-built): **301 passed, 0 skipped** |
| monitoring | promtool check/test rules, amtool | 74 rules; all unit tests SUCCESS; config ok |
| docker | build `runtime`, liveness | `wasla:cmp-rem` built (exit 0); container answered `{"status":"alive"}` on `/health/live` (two empty replies during start-up first); container removed |

---

## 28. Ruff / Black / MyPy

```text
ruff check .        All checks passed!   (tracked tree)
black --check .     900 files would be left unchanged
mypy app tests      Success: no issues found in 805 source files
```

No `# type: ignore` was added in application code.

---

## 29. Prometheus / Alertmanager Gates

```text
promtool check rules /m/alerts.yml          SUCCESS: 74 rules found
promtool test rules /m/tests/alerts_test.yml SUCCESS
amtool check-config /m/alertmanager.yml     1 inhibit rule, 2 receivers - valid
```

(`prom/prometheus:v3.1.0`, `prom/alertmanager:v0.28.0`, disposable containers.)

---

## 30. Findings Ledger

| ID | Status | Where |
| --- | --- | --- |
| CMP-01 | **CLOSED** | 3e0b3bb; ADR-134 |
| CMP-02 | **CLOSED** | db102af; ADR-135 |
| CMP-03 | **CLOSED** | 66e45f2; ADR-136 |
| CMP-04 | **CLOSED** | 66e45f2 |
| CMP-05 | **CLOSED** (media-header sending DEFERRED; composition refuses it) | 0a5c369; ADR-137 |
| CMP-06 | **CLOSED** (24 h re-queue: LATER PRODUCT) | e856770; ADR-139 |
| CMP-07 | **CLOSED** | e856770 |
| CMP-08 | **CLOSED** | 3e0b3bb/ee0e1db |
| CMP-09 | **CLOSED** (decision: MARKETING only) | 0a5c369; ADR-138 |
| CMP-10 | **CLOSED** in code; whether Meta sends 131050 asynchronously is EXTERNAL VERIFICATION (EXT-CMP-04) | d66be87; ADR-142 |
| CMP-11 | **CLOSED** | ee0e1db, bea5e7b; ADR-140 |
| CMP-12 | **CLOSED** | 01e92c3 |
| CMP-13 | **CLOSED** | 302e420; ADR-143 |
| CMP-14 | **CLOSED** | db102af, 302e420 |
| CMP-15 | **CLOSED** (refusal; workspace time zone = CMQ-04) | 302e420 |
| CMP-16 | **CLOSED** (documentation) | 83a1f24 |
| CMP-17 | **EXTERNAL VERIFICATION** (EXT-CMP-01) + documented; enforcement = CMQ-06 | 83a1f24 |
| CMP-18 | **CLOSED** | e856770 |
| CMP-19 | **CLOSED** | 0a5c369, c0ae5dc |
| CMP-20 | **CLOSED** | ee0e1db; ADR-026 amended |
| CMP-21 | **CLOSED** | 3e0b3bb, 66e45f2 and every phase's suites |
| CMP-22 | CHECK **CLOSED**; account composite key **CLOSED**; template composite key **ACCEPTED RESIDUAL** (no `(tenant_id, id)` key on `whatsapp_templates`) | 302e420 |
| CMP-23 | **CLOSED** | c071b18, 83a1f24 |
| CMP-24 | **CLOSED** | 66e45f2 |
| CMQ-01 | DECISION APPLIED (unchanged: per channel) | |
| CMQ-02 | DECISION APPLIED (b) | 01e92c3; ADR-141 |
| CMQ-03 | DEFERRED PRODUCT DECISION | |
| CMQ-04 | DEFERRED PRODUCT DECISION | |
| CMQ-05 | DECISION APPLIED (code); Meta field subscription EXTERNAL | d66be87 |
| CMQ-06 | DEFERRED PRODUCT DECISION (needs EXT-CMP-01) | |
| CMQ-07 | DEFERRED PRODUCT DECISION + LEGAL (Egypt PDPL 151/2020) | ADR-025 amended (unchanged in substance) |
| CMQ-08 | DEFERRED PRODUCT DECISION (Privacy audit 18) | |
| CMQ-09 | DECISION APPLIED (unchanged: continues) | |
| CMQ-10 | DEFERRED PRODUCT DECISION | |
| CMQ-11 | DECISION APPLIED (b) | 2655184 |
| CMQ-12 | DECISION APPLIED | 01e92c3 |
| LP-CMP-01..LP-CMP-10 | DEFERRED — LATER PRODUCT (untouched; LP-CMP-06's campaign counters exist now as metrics, not analytics events) | |
| EXT-CMP-01 | EXTERNAL VERIFICATION — documented in CAMPAIGNS.md / RUNBOOK; not read or enforced | |
| EXT-CMP-02 | CONFIRMED; unchanged (130429 → wait) | |
| EXT-CMP-03 | CONFLICT **resolved**: 131049 no longer retried | |
| EXT-CMP-04 | sync CONFIRMED; async handled in code, EXTERNAL | |
| EXT-CMP-05 | CONFLICT **resolved**: 131056 per pair | |
| EXT-CMP-06 | CONFIRMED; MARKETING-only enforced; recategorisation mechanics EXTERNAL | |
| EXT-CMP-07 | CONFIRMED; webhook now handled (subscription EXTERNAL) | |
| EXT-CMP-08 | CONFLICT **resolved** in code (132015 status → write-back); `held_for_quality_assessment` EXTERNAL | |
| EXT-CMP-09 | CONFLICT stays → CMQ-07 (owner + legal) | |
| EXT-CMP-10 | Egypt rate EXTERNAL; duplicates no longer paid twice | |
| EXT-CMP-11 | EXTERNAL | |
| EXT-CMP-12 | CONFIRMED (`/messages` valid) | |
| EXT-CMP-13 | EXTERNAL (no quality webhook parsed; CMQ-06) | |
| EXT-CMP-14 | CONFIRMED, unchanged | |
| CR-01..CR-07 | **CLOSED** (invariants in the tool, injection-tested, 0 after the probes) | bea5e7b |

---

## 31. Remaining External Actions

```text
review -> merge campaigns-findings-remediation-20261015 into worktree-billing-google-auth -> CI green -> push
subscribe message_template_status_update in the Meta App (CMQ-05)
real-Meta lane (only when authorised), per the audit's section 37:
    one read-only template sync compared with WhatsApp Manager (status, category, components)
    one campaign to 1-3 owner numbers: wamids, sent/delivered/read, rows reconciled
    a STOP from the owner's phone -> consent via message; the next campaign skips it
    "Stop promotions" tap -> consent via reply_action
    a header-image template (composition refuses it today; media headers are deferred)
    a new marketing template's first send: held_for_quality_assessment? (EXT-CMP-08)
    past the portfolio limit on a 250-tier test portfolio: the error returned (EXT-CMP-01)
    whether 131050 arrives as an asynchronous failed status (EXT-CMP-04)
Egypt (+20) marketing and utility rates (EXT-CMP-10)
CMQ-07 legal review: "wrote first" vs explicit opt-in (Egypt PDPL 151/2020 and destination countries)
product decisions CMQ-03, CMQ-04, CMQ-06, CMQ-08, CMQ-10
remove the audit's and this remediation's disposable infrastructure (section 32) after merge
```

---

## 32. Infrastructure Created by This Remediation

| Kind | Name | State |
| --- | --- | --- |
| Worktree + branch | `E:\wasla-campaigns-remediation`, `campaigns-findings-remediation-20261015` | holds the commits and this report; untracked `audit/` (copied probes, runners) |
| Worktree (detached) | `E:\wasla-cmp-rem-base` at `6ecbdd6` | pristine baseline probes; untracked `audit/`; tracked tree clean |
| Worktree (detached) | `E:\wasla-cmp-rem-mut` at `c0ae5dc` | mutation runs; tracked tree clean (no mutant left applied) |
| Container | `wasla-cmp-rem-pg` (pgvector/pgvector:pg16, 127.0.0.1:55921) | databases `wasla_cmp_rem_models`, `_migrations`, `_probe`, `_probe2`, `_mut`, `_mig`, `_p13` |
| Container | `wasla-cmp-rem-pg2` (pgvector/pgvector:pg16, 127.0.0.1:55922) | `wasla_cmp_rem_migrations` (migration-built lane) |
| Containers | `wasla-cmp-rem-redis` 56921, `-redis2` 56922, `-redis3` 56923 (redis:7-alpine) | left running |
| Image | `wasla:cmp-rem` (runtime target) | built for the docker job; the liveness container removed |
| Volumes | anonymous volumes of the containers above | no named volume; `wasla_postgres-data` untouched |
| Scratch | session scratchpad `…/scratchpad/cmp-rem/` | baselines, lanes, probes, mutations |

Untouched: the user's untracked files in `E:\wasla`; the audit's worktrees
(`E:\wasla-campaigns-audit`, `E:\wasla-cmp-mut`, `E:\wasla-cmp-surv`) and
containers (`wasla-cmp-pg`, `-redis`, `-minio`); the developer stack
(`wasla-api-1`, `wasla-postgres-1`, `wasla-redis-1`). Docker Desktop was already
running.

---

## 33. Updated Score

**A self-score is not an independent measurement**; the next verification stage
should re-measure. Same dimensions as the audit's section 48.

| Area | Weight | Audit | Now | Remaining deduction |
| --- | --- | --- | --- | --- |
| Audience boundary | 8 | 7 | 8 | (CMQ-07 is a decision, not deducted) |
| Identity and connection binding | 5 | 5 | 5 | |
| Template registry and parameters | 7 | 3 | 6 | −1 media headers cannot be sent (refused, deferred) |
| Materialisation and scheduling | 5 | 3 | 5 | |
| Sweep, lease and concurrency | 8 | 1 | 7 | −1 replicas compare Python clocks (ADR-134 §5); proven in-process and with OS processes, not on separate hosts |
| Pacing and rate limits | 5 | 3 | 4 | −1 CMP-17: portfolio messaging limit neither read nor enforced |
| Duplicate prevention and crash safety | 10 | 5 | 10 | |
| Stop / cancel / resume | 5 | 3 | 5 | |
| Opt-out completeness | 7 | 6 | 7 | |
| Entitlements and billing intersection | 6 | 2 | 6 | |
| Lifecycle intersection | 5 | 1 | 5 | |
| Statistics truth | 4 | 2 | 4 | |
| Authorization and audit | 4 | 3 | 4 | |
| Security and abuse resistance | 4 | 3 | 4 | |
| Operations and observability | 3 | 1 | 2 | −1 alert delivery to a receiver not verified end to end |
| Migrations and invariants | 4 | 4 | 4 | CMP-22 template key accepted residual (defence in depth only) |
| Testing | 5 | 1 | 5 | |
| Documentation truth | 3 | 2 | 3 | |
| Real-provider evidence | 2 | 0 | 0 | not authorised |
| **Total** | **100** | **52** | **94** | |

---

## 34. Final Verdict

**CAMPAIGNS FINDINGS CLOSED WITH REQUIRED PRODUCT DECISIONS AND EXTERNAL VERIFICATION.**

Every condition of the brief's section 51 holds: 0 people twice in every 2-, 4-
and 8-worker test and in unscaled P01b, meters equal recipients sent; no send
from a suspended, deleted or dunning-suspended workspace, `past_due` continuing;
one schedule accepted in every gated and natural race; no scheduled campaign
gaining unreserved recipients; header and button parameters sent or refused,
parameter refusals stopping after one request; every answer-class row of section
22.4 met; pause at the next recipient; MARKETING only; failed-status codes stored
and acted on; two real workers in the suite and the vacuous test replaced;
manual MARKETING templates admin-only and opt-out-checked; the crash matrix
unchanged; M04, M05, M08, M20, M21 and every meaningful mutant killed; CR-01..CR-07
at 0 after the probes; no real provider contacted; nothing pushed, merged or
deployed. Left open: CMQ-07, the deferred product decisions, CMP-17's external
verification and the external actions of section 31.

**Would Wasla now prove the brief's closing scenario** — a 5,000-person campaign,
three campaign workers, a second admin scheduling another, a platform suspension
half-way, and Meta refusing the template's header?

* **Nobody twice**: each person's send is owned by one statement (`take`) and one
  campaign-level check (`hold`) that also carries the lease token; tests with 2,
  4 and 8 workers, the unscaled shipped shape (0/250) and a killed-plus-live
  worker probe (0/30) counted requests at the transport, and invariants CR-01 and
  CR-02 check any deployment's data.
* **Nothing after the suspension**: the suspension pauses the campaign in its own
  transaction and every worker re-reads the served predicate before every person;
  at most the one send already in flight goes (test: ≤ 4 of 30 after suspending at
  the 3rd).
* **Never charged past the allowance**: both admins' schedules count each other
  under the workspace's lock (0/40 races over-promised); the reservation is the
  pending audience, which cannot grow after scheduling (CR-04).
* **The header refusal costs one request**: a header the campaign cannot fill is
  refused at composition, before anyone is written to; if Meta still refuses the
  parameters (132000/132012/131008), the campaign fails after **one** request with
  the code, and nobody else is asked.
