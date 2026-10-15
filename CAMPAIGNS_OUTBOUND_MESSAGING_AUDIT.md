# Campaigns & Outbound Messaging Audit

| | |
| --- | --- |
| Date | 2026-10-13 |
| Type | **Audit only** — no production code changed, no remediation |
| Audit number | 10 of 25 (then 26 — Final System-Wide) |
| Base branch / HEAD | `worktree-billing-google-auth` at `989584b` (= `origin/worktree-billing-google-auth`, 0 ahead / 0 behind) |
| Unpushed commits on base | none |
| Audit branch / worktree | `campaigns-outbound-audit-20261013` at `E:\wasla-campaigns-audit` (from `989584b`) |
| Alembic head | `0100` (single head; `alembic check`: no new operations) |
| Merged stages | Omnichannel final remediation (`dd6503e`, ADR-124..130) — **yes**; Entitlements (`7038585`, ADR-131) — **yes**; Platform entitlement ops (`1634160`, ADR-132, 0099–0100) — **yes**; Kept-data fix (`ccfe9e8`, ADR-133) — **yes**; Instagram — **no** (no adapter in `ChannelRegistry`; `default_registry()` builds WhatsApp only) |
| `CONNECTION_SENDS_PER_MINUTE` | present, **unset by default** (`app/core/config.py:579`, `int | None = None`); per-connection override `channel_connections.sends_per_minute` (0090) |
| Finding series | `CMP-01..` findings · `EXT-CMP-01..14` Meta facts · `CMQ-01..` product decisions · `LP-CMP-01..` later product |
| Production / Meta / customers | **Not touched.** No real WhatsApp message, template sync or Meta account action. No real-provider lane was authorised in the session. |

---

## 1. Executive Summary

**Verdict: CAMPAIGNS NOT READY — score 52 / 100.**

The single-process campaign is well built. Within one worker process:

- the audience boundary ("wrote first, on this number, not opted out on this channel") holds across the whole API surface;
- the ADR-093 send protocol abandons rather than resends at every crash boundary (K1–K8, killed with TerminateProcess);
- every uncertain Meta answer is never resent;
- a refused credential stops the campaign once;
- 131050 records the opt-out;
- cancel stops at the next recipient.

Migrations preserve every opt-out up and down.

The guarantees fail at the joins, and four of the failures are release blockers:

1. **CMP-01 (Critical). Two campaign replicas send the same people the same campaign twice.**
   - Reproduced three ways: 3/40, 26/40, and 23/250 people with the **shipped, unscaled constants** (300 s lease, claim limit 5, batch 50, Meta answering in 1.3 s).
   - The recipient row locks end at the first send commit (ADR-093 TX1). After that the batch works from stale in-memory snapshots (`expire_on_commit=False`).
   - Three things let a second replica take the same campaign in that time: the claim ignores the lease for `scheduled` campaigns; the lease is shorter than a worst-case batch; and `claim_by_id` never checks that the lease is still its own.
   - The recipient rows show each person `sent` once. The duplicates are visible only as extra message rows and a doubled `campaign_message` meter (392 meters for 340 sent rows), so the workspace is billed for them.
2. **CMP-02 (High). A platform-suspended or soft-deleted workspace keeps broadcasting.**
   - Nothing on the campaign path asks whether the workspace is served. Suspension and deletion stop only the follow-ups an agent scheduled (TOOL-04).
   - This settles the authorization audit's residual question 54: queued campaigns are not stopped.
3. **CMP-03 (High). Concurrent scheduling overspends `period_campaign_messages`.**
   - `schedule()` evaluates `reserved=await pending_in_other_live_campaigns(...)` as a call argument, before `reserve_period` takes the advisory lock.
   - Result: 40/40 natural trials accepted two 20-recipient campaigns against a 30-message allowance.
   - BILL-08 is reopened by argument evaluation order, and the one concurrent campaign test cannot fail.
4. **CMP-05 (High). A template whose header or buttons need parameters burns every recipient one at a time.**
   - Composition counts only the body's placeholders, and the send carries only body parameters.
   - Meta refuses every recipient (132000, 132012 or 131008). Each refusal spends 3 attempts, and the campaign ends `completed` with everyone `failed` and no campaign-level reason.

Below the blockers there are eight more Medium items:

- a reservation bypass through materialisation (CMP-04);
- 131049 retried within minutes, where Meta says wait at least 24 hours (CMP-06);
- an outage burning the whole audience (CMP-07);
- pause not stopping the batch (CMP-08);
- campaign template categories not enforced (CMP-09);
- provider failure statuses never acted on (CMP-10);
- no awareness of Meta's portfolio messaging limit (CMP-17);
- test suites that cannot see any of this (CMP-21).

Mutation testing (§32): 26 mutants, 16 killed and 10 survived. Survivors include the abandon guard (M04), uncertain-is-terminal (M05) and the whole campaign reservation (M20). Their runtime behaviour is correct, but no test in 1,952 would notice their removal.

Two product decisions the audience boundary rests on are open:

- **CMQ-07.** Meta's WhatsApp opt-in policy asks for explicit opt-in before business-initiated messages. ADR-025's "wrote first" is a weaker signal than that.
- **CMQ-02.** Manual template sends are unpaced, unreserved and ignore opt-out.

---

## 2. Repository Verification and Merged Stages

```text
git branch --show-current     worktree-billing-google-auth            (canonical checkout E:\wasla)
git rev-parse HEAD            989584b4a0fe45b14e81b82ceec65ad61a276084
git status --short            11 untracked *_AUDIT.md / *_REMEDIATION.md / plan files (the user's, left untouched)
git stash list                (empty)
git log -1                    989584b docs: correct the canonical merge verification with the first CI run
git fetch origin --prune      ok
rev-list --left-right         origin/worktree-billing-google-auth...HEAD = 0 0
alembic heads                 0100 (head)        alembic check: No new upgrade operations detected.
Python                        3.12.7
PostgreSQL                    16.15 (pgvector/pgvector:pg16, container wasla-cmp-pg)
Redis                         7.4.11 (redis:7-alpine, wasla-cmp-redis)
Docker                        29.8.0 (Docker Desktop was stopped at session start and was started by the audit)
app.__file__                  E:\wasla-campaigns-audit\app\__init__.py   (audit worktree — correct)
                              E:\wasla-cmp-mut\app\__init__.py           (mutation worktree — correct)
SQLAlchemy / FastAPI / httpx  2.0.52 / 0.141.1 / 0.28.1
```

The editable install did not shadow the worktrees. Every probe and test run printed `app.__file__` from the worktree it was meant to exercise.

`git worktree list` shows 39 older worktrees marked `prunable` (their directories are gone). They belong to earlier stages and were not touched.

---

## 3. Methodology

Evidence was built at four levels. Each finding names its basis.

| Level | What it was |
| --- | --- |
| CODE | Every campaign and outbound file in §4 read; flows reconstructed with `file:line` |
| TEST | The campaign-relevant suites on a **migration-built** schema (`WASLA_TEST_SCHEMA=migrations`): **577 passed, 0 failed** (`-k` campaign / template / opt-out / consent / throughput / outbound / follow / meta_error / stop across 15 integration files and `tests/unit`) |
| RUNTIME | 14 probe scripts in `audit/` against two migration-built databases (`wasla_probe`, `wasla_probe2`), real commits, the real `CampaignService`, `CampaignWorker`, `MessagingService`, `WhatsAppClient` and Meta error classifier |
| PROVIDER DOCS | Meta's official developer documentation, read 2026-10-13 (§36) |

**The fake Meta.** Meta was replaced at the HTTP transport (`httpx.MockTransport` in-process; a real `ThreadingHTTPServer` on 127.0.0.1 for the crash matrix). Everything above the socket is production code: payload building, error-code classification, `Retry-After`, the delivery protocol. **"Sent once" is counted as requests that reached the transport, never as rows.**

**Crashes** are real OS processes killed with `Popen.kill()`, which is TerminateProcess on Windows: no handler, no `finally`, no commit. That is the SIGKILL equivalent.

**Races** use a gate at the decision point (an `asyncio.Barrier` or `Event` placed after the read being raced), plus ungated "natural" repetitions to show the window is reachable without help.

**Mutations** ran in a separate detached worktree (`E:\wasla-cmp-mut`) against a separate database. Each mutant was reverted with `git checkout` before the next.

**Not done, and why:**

- A real-provider lane: not authorised.
- K2, K7, K9 and K10 as separate SIGKILL runs. K2 and K7 fall inside K1 and K8. K9 and K10 are single transactions; this is proven by code reading in §18.
- 200,000-recipient materialisation: the cap is 50,000, and 20,000 was measured.
- A full re-audit of follow-ups: by instruction, only the shared guarantees were checked.

---

## 4. File Inventory

| Area | Relevant files (keyword sweep) | Fully read | Programmatically analysed |
| --- | --- | --- | --- |
| `app/` | 101 (58 name campaigns) | 21 | 80 |
| `alembic/versions/` | 23 | 2 (0097; 0012/0013 campaign tables by `\d`) | 21 |
| `tests/` | 127 | 6 | 121 (577 executed) |
| `docs/` + root docs | 21 | 6 (CAMPAIGNS, ADR-025/026/032/033/123, RUNBOOK §campaigns, API §campaigns, BILLING §suspended) | 15 |
| `scripts/` | 3 | 0 | 3 |

**Fully read:**

- **Services:**
  - `app/services/campaign_service.py`
  - `app/services/opt_out.py`
  - `app/services/template_service.py`
  - `app/services/messaging_service.py` (send path, lines 300–1120)
  - `app/services/entitlement_service.py` (`reserve_period`, `check`)
  - `app/services/workspace_service.py` (suspend, delete, `_stop_automated_messages`)
  - `app/services/conversation_service.py` (`project_message`)
- **Workers:** `app/workers/campaign_worker.py`
- **Repositories:**
  - `app/repositories/campaign_repository.py`
  - `app/repositories/consent_repository.py`
  - `app/repositories/conversation_repository.py` (`apply_status` / `advance_status`, unresolved directory)
- **Models:** `app/db/models/campaign.py`, `app/db/models/whatsapp_template.py`
- **Database session:** `app/db/session.py`
- **Integrations:**
  - `app/integrations/whatsapp/client.py` (header, constants, `_send`, `_retry_after`, decode)
  - `app/integrations/meta/errors.py`
- **API:**
  - `app/api/v1/campaigns.py`
  - `app/api/v1/templates.py`
  - `app/api/v1/conversations.py` (template send)
  - `app/api/v1/__init__.py` (limits)
  - `app/api/dependencies.py` (`get_campaign_service`)
- **Schemas:** `app/schemas/campaign.py`
- **Migrations:** `alembic/versions/20261003_0097_contact_channel_consents.py`
- **Tests:**
  - `tests/integration/test_campaign_worker.py` (header and stubs)
  - `tests/integration/test_campaign_endpoints.py` (header and stubs)
  - `tests/integration/test_campaigns.py` (seeding)
  - `tests/integration/test_topup_concurrency.py::test_a_topup_racing_campaign_scheduling_never_over_promises`
  - `tests/channel_plans.py`
  - `tests/consent.py`

**Not fully read, and therefore unreviewed relevant surface. The target of 0 was not met.**

- `app/services/follow_up_service.py`: only the served gate, opt-out re-read and dispatch were read.
- `app/services/channel_ingestion_service.py`: the button / `user_preferences` opt-out writers were confirmed by tests and grep, not line-read.
- `scripts/omnichannel_invariants.py`: `recover-button-opt-outs`.
- `app/services/analytics_service.py`: the campaign section.
- `app/core/telemetry.py`: campaign counters.
- `app/workers/runner.py`.

Their behaviour enters this report only through tests and runtime probes, and is labelled that way.

---

## 5. Architecture (as built)

```
API (request transaction, CommittingRoute)
  POST /campaigns                      admin   CampaignService.create                       campaign_service.py:229
     account (tenant repo) active? -> template (tenant repo) on that account? -> refusal_reason_for -> approved?
     len(variables) == template.variable_count  (BODY placeholders only)                         :259
  POST /campaigns/audience/preview     admin   AudienceRepository.count_eligible                    campaign_repository.py:487
  POST /campaigns/{id}/audience        admin   draft? (unlocked read) -> list_eligible_members(limit 50,001)
                                               -> one CampaignRecipient per new contact + participant_identity_id   :293
  POST /campaigns/{id}/schedule        admin   status in {draft, paused, failed}? -> pending_count > 0
                                               -> reserved = pending_in_other_live_campaigns()   <- read BEFORE the lock
                                               -> reserve_period(): pg_advisory_xact_lock -> check(additional+reserved)
                                               -> scheduled_at = max(aware(when), now) (naive => UTC) -> audit CAMPAIGN_SCHEDULED  :347
  POST /campaigns/{id}/pause           admin   status = paused  (no audit)                          :414
  POST /campaigns/{id}/cancel          admin   status = cancelled, audit CAMPAIGN_CANCELLED          :430

Worker (WORKER_KINDS=campaign, POLL_SECONDS=15)                                        campaign_worker.py
  TX0  claim_due(now): status=scheduled AND scheduled_at<=now      <- lease NOT consulted
                    OR status=running AND (next_send_at null OR <= now)
       FOR UPDATE SKIP LOCKED, LIMIT 5, next_send_at := now+300 s (lease)  -> COMMIT          campaign_repository.py:421
  for each claimed id, sequentially, one HTTP pool for the sweep:
   TXn claim_by_id: SELECT ... FOR UPDATE SKIP LOCKED (no due / lease check)                  :408
       dispatch_batch(campaign, now=<sweep start>)                                            campaign_service.py:456
         finished/paused/draft? -> return
         _blocking_reason: account active? template exists + approved?   -> FAILED
         scheduled -> running
         claim_pending(min(50, mpm)) FOR UPDATE SKIP LOCKED                                   :492
         in_plan = channel_in_plan(connection.channel)  (once per batch, ENT-16)
         per recipient:
            is_cancelled()  (scalar re-read; PAUSE is not re-read)                            :501
            not in_plan -> SKIPPED channel_not_in_plan
            _deliver:  contact exists? consent on channel? (unlocked read)                    :644
                       message_id already set -> abandon (FAILED, "could not be confirmed")   :655
                       conversation on this account and contact? identity unchanged?          :681
                       MessagingService.send_template(origin=CAMPAIGN, link=...)
                          registry refusal? -> policy.may_send -> connection active -> address identity
                          take_allowance (ADR-123; refuses CAMPAIGN/FOLLOW_UP only)
                          stage message; link(recipient.message_id)
                          REQUESTED; released(): COMMIT   <- TX1+TX2; every row lock of the batch ends here
                          Meta call (no transaction)
                          TX3 (flushed; committed by the next recipient's commit or the session exit)
            Throttled/Paused -> break, wait;  credential/connection refused -> campaign FAILED
         next_send_at = <sweep start> + len(claimed)/mpm minutes (max with throttle window)
         remaining == 0 (read before the last recipient is flushed) -> COMPLETED (next sweep in practice)

Feedback
  status webhook  -> advance_status (delivered/read climb; failed -> failure_reason text, code ignored)
  STOP text / button / user_preferences / 131050-on-send -> record_opt_out(channel)  (consent row, FOR UPDATE)
  template codes 132001/132015/132016 on send -> registry row PAUSED
```

**Where the code and `docs/CAMPAIGNS.md` disagree:**

1. **The diagram omits ENT-16, the connection allowance and the abandon branch.**
2. **"Claimed with FOR UPDATE SKIP LOCKED" suggests the claim protects the batch.** It protects only until the first commit (CMP-01).
3. **"the recipient row is what makes the pair safe from double counting"** is false under CMP-01 (`campaign_service.py:760`).
4. **The model docstring says "`MARKETING` is the only one a campaign may use"** (`whatsapp_template.py:47`). Nothing enforces it (CMP-09).
5. **"The rate is per campaign, not per number"** remains true by default, because `CONNECTION_SENDS_PER_MINUTE` is unset. ADR-123 makes it configurable, and the document does not mention that.

---

## 6. Outbound Producers and Shared Guarantees

Every producer that sends something other than a reply to the message just received:

| Producer | Origin | Opt-out re-read (channel) | Connection allowance (ADR-123) | Uncertain → never resent | Credential refusal stops the sweep once | Workspace served gate | `channel_not_in_plan` | Registry (MSG-10) | Reservation |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Campaign | `CAMPAIGN` | **yes** (`campaign_service.py:647`) | spent; refused → waits, no attempt | yes (abandon) | yes (FAILED once) | **NO (CMP-02)** | yes, per batch | stricter: exists + approved | at schedule (CMP-03, CMP-04) |
| Follow-up | `FOLLOW_UP` | yes (MSG-06/ENT-19, tests green) | spent; refused → waits | yes (ADR-093) | yes (MSG-18, tests) | **yes** (`follow_up_service.py:730`, TOOL-04) | yes (ENT-16, tests) | registry asymmetry | no (not a period allowance) |
| Manual template send `POST /conversations/{id}/messages/template` | `HUMAN` | **no** | counted, **never refused** | idempotency key optional | raised to the caller | API requires active workspace | **not checked** — only automated sends are refused (BILLING.md §553) | asymmetric rule (never-synced allowed) | **none** |
| Agent / human replies | `AGENT`/`HUMAN` | no (by design, CAMPAIGNS.md table) | counted, never refused | yes | n/a | agent path gated (TOOL-04) | agent: yes | n/a | n/a |
| Holding / escalation message | — | does not exist (TASKS.md:240 open) | | | | | | | |
| Platform-initiated send | — | none exists | | | | | | | |

The manual template send is the one non-reply producer that shares almost none of the campaign guarantees:

- any member may call it, not only an admin;
- it skips the opt-out check and the reservation;
- the allowance never refuses it.

The workspace API budget (`RATE_LIMIT_WORKSPACE_PER_MINUTE=300`) is the only pace on it. A script can therefore use it as a 300-a-minute broadcast that ignores opt-out. Because CAMPAIGNS.md's table deliberately exempts "Manual human reply", this is put to the owner as **CMQ-02** and not filed as a defect.

---

## 7. Campaign State Machine

| From → To | Caused by | Columns | Audited |
| --- | --- | --- | --- |
| draft → draft (audience added) | admin `POST /audience` (draft only, unlocked read) | audience, audience_size | no |
| draft/paused/failed → scheduled | admin `schedule` | status, scheduled_at, next_send_at=null, last_error=null | **yes** (`CAMPAIGN_SCHEDULED`, recipients, scheduled_at) |
| scheduled → running | worker, first batch | status, started_at | no (system) |
| scheduled/running → paused | admin `pause` | status | **no** (CMP-14) |
| any but completed/cancelled → cancelled | admin `cancel` (failed included) | status, cancelled_at | yes (`CAMPAIGN_CANCELLED`) |
| scheduled/running → failed | worker: number disabled, template removed or not approved, credential missing or refused, connection refused | status, last_error, next_send_at=null | no (system log `campaign.blocked`) |
| running → completed | worker: no pending left | status, completed_at | no |

**Named transitions only.** There is no PATCH on `status`. The schema has no status field (`CampaignCreateRequest` and `CampaignScheduleRequest`).

**Answers to the state-machine questions:**

- **Completed or cancelled → scheduled:** refused, "A completed campaign cannot be scheduled."
- **Running → scheduled:** refused, since `running` is not in `SCHEDULABLE_FROM`. So there is no double reservation from a double schedule.
- **Pause and cancel racing the worker:** last writer wins on `status`.
  - Cancel committed while the last recipient was in flight ended `cancelled` with every recipient `sent` (P03 `cancel_last`). That is consistent.
  - `_complete` would overwrite a `cancelled` that arrived after `pending_count` was read. That window is narrow, and the result is consistent with the rows.
- **Paused → schedule with a past or different `scheduled_at`:** accepted. A past time means now.
  - Resuming also resets `next_send_at` to null, so pause → resume immediately skips the remaining pacing interval for one batch.
  - It also re-opens the CMP-01 gap: the resumed campaign is `scheduled`, so its lease is ignored by another replica.
- **Audience rebuilt after pause:** refused (draft only).
- **Every recipient skipped:** the campaign completes. `failed` counts recipients, not campaigns.
- **Stuck `running` forever:** possible.
  - An exception escaping `dispatch_batch` (anything outside its `except` lists, such as `NotFoundError` from a vanished template or identity, or an `IntegrityError`) is logged as `campaign.batch_failed`. The session then commits whatever was flushed.
  - The campaign keeps its 300 s lease and is retried every 5 minutes forever, with no attempt count and no `last_error`.
  - Observed once during harness bring-up: a settings error made a campaign sit `scheduled` with a renewed lease every sweep. Classified **DERIVED (CMP-20, Low)**. There is no alert for it (§28).
- **FAILED → schedule:** reserves again for the remaining pending recipients. Recipients that were abandoned stay `failed`. The credential case has its own defect (CMP-11).
- **FAILED → cancel:** final. A later schedule is refused.

Illegal transitions raced concurrently (two `schedule` calls behind a barrier, in P02) are both accepted only when they target *different* campaigns. On one campaign, the second sees `scheduled` and is refused with a stable 422 `ValidationError`.

---

## 8. Recipient State Machine

`pending → sent | failed | skipped`, plus `pending → pending (attempts+1)` for a retryable failure. The ceiling is `MAX_RECIPIENT_ATTEMPTS=3`.

| Transition | Cause | Attempts | `message_id` | Reason text |
| --- | --- | --- | --- | --- |
| → sent | Meta 2xx with id | unchanged | linked | null |
| → skipped | contact gone; opted out on channel; conversation on another number; identity changed; `channel_not_in_plan`; 131050 | unchanged (131050: +1) | linked for 131050 | prose |
| → failed (abandon) | pending + linked found at the next attempt; uncertain answer (2xx without id, 5xx, read timeout, reset) | +1 | kept | "could not be confirmed" / "did not confirm" |
| → pending (retry) | `ExternalServiceError`, `RateLimitedError` (not throttle), `ValidationError` (registry refusal, policy refusal, unaddressable identity) | +1 | unlinked | prose |
| → failed (exhausted) | third retryable failure | 3 | unlinked | prose |
| stays pending | throttled / channel paused | unchanged | unlinked | — |
| stays pending, linked | credential or connection refused (campaign FAILED) | unchanged | **linked to an undelivered message** (CMP-11) | — |

**`sent` without a message row:** 0 in the invariant sweep. The FK is `SET NULL`, so message retention could null it later.

**Linked to a message of another tenant, campaign or connection:** 0, though `message_id` has no tenant-agreed FK.

**Skipped → pending:** never. Once consent resumes, or a connection is re-enabled, those people are lost to that campaign. That is correct by design for opt-out ("never retried"), but it also covers `channel_not_in_plan`. A plan upgrade will not bring those people back, which is part of **CMQ-11**.

**Attempts above the ceiling:** never observed. `is_exhausted` uses `>=`.

**Abandoned recipients** read `failed` with a sentence. The API cannot tell "may have been delivered" apart from "refused", because there is no reason code (CMP-13).

**Skip reasons are not a closed vocabulary.** They are prose strings in `last_error` (CMP-13).

---

## 9. Template Registry

**Re-proved behaviour:**

- Identity is `(account, name, language)` (`template_service.py:206`).
- Sendable means `APPROVED` only.
- A template that is gone from Meta becomes `DISABLED`, never deleted.
- An unrecognised status becomes `UNKNOWN`.
- Sync is admin-only (`TenantAdminDep`), rate-limited by the campaign budget, tenant-scoped through the repository, and account-scoped by `waba_id` from the ownership proof.

**A failure on page 2 changes nothing.** `list_templates` returns every page before any row is written, so a failed sync aborts the request transaction. (CODE: the sync loop runs only after `payloads` is complete.)

**Concurrent syncs** can race to insert the same `(account, name, language)`. One will fail on the unique key and answer 500/409 for that request. Both transactions are atomic, so nothing is lost. This is DERIVED and was not run.

**Write-back on a send** happens only for 132001, 132015 and 132016, and only to a row the registry already holds. The status written is `PAUSED`.

- Re-proved: P06 `400_132001` → template `paused`, campaign `failed` "WhatsApp reports this template as paused.", 1 request.
- **A sync racing a write-back:** last writer wins on `status`. A sync that commits after a write-back restores Meta's real answer, which is correct. A write-back after a sync re-pauses until the next sync.

**Asymmetry.** It lives in one place, `refusal_reason_for`, and `send_template` applies it to every caller (MSG-10, `messaging_service.py:382`). Campaigns add exists + approved at composition and before every batch (`_blocking_reason`).

**Two gaps that are not about staleness:**

1. **Category is stored and never enforced for campaigns (CMP-09).**
2. **The registry does not record which components need send-time parameters (CMP-05).**

**Periodic sync and Meta's status webhooks.** Neither exists:

- no periodic sync;
- no `message_template_status_update` subscription. The field is not parsed anywhere in `app/`, according to grep.

Meta documents `message_template_status_update` for status changes, and documents that held messages of a paced template are dropped with `failed` / `132015` (EXT-CMP-07/08). Wasla learns of a pause only when a send is refused. Because the write-back exists, a withdrawn template costs one refusal per campaign, not one per recipient.

The audit keeps "no periodic sync" as **CORRECT BY DESIGN** for today's volume. Subscribing to the webhook is cheap and matches Meta's own channel, so that part is **CMQ-05**.

---

## 10. Template Parameters and Content

**What composition checks.** Parameters are one list for the whole campaign. Composition checks only `len(variables) == template.variable_count`, and `variable_count = count_placeholders(body_text)` (`template_service.py:313`).

**What the send carries.** `_components` emits a single `body` component (`campaign_service.py:891`).

**Proof (P07).** Composition accepted a template with an `IMAGE` header and a `URL` button carrying `{{1}}`. The payload Meta received was:

```json
{"name": "offer_…", "language": {"code": "ar_EG"},
 "components": [{"type": "body", "parameters": [{"type": "text", "text": "Ahmed"}]}]}
```

There was no header handle and no button suffix.

**What Meta does with it.** It answers 132000, 132012 or 131008 for every recipient. P06 shows each recipient spending 3 attempts, the campaign ending `completed` with every recipient `failed`, `last_error` null, and the template left `approved`. That is the MSG-18 pattern: a campaign-wide fact discovered once per person. → **CMP-05**.

**Named placeholders** (`{{name}}`) are counted, but sent positionally without `parameter_name`. This is DERIVED. Whether Meta accepts it is EXT-CMP.

**Length and Unicode.** `MAX_VARIABLE_LENGTH` bounds each value. Arabic, RTL marks and emoji pass through untouched. A workspace may put URLs and phone numbers in parameters; WhatsApp allows that.

**Language** is one per campaign. The recipient's language is ignored (LATER PRODUCT).

**Header media handles** do not exist on the send side at all (CMP-05).

---

## 11. Who a Campaign Can Reach

The base population is defined once, in `AudienceRepository._eligible`, which is fully read (`campaign_repository.py:349`):

> a contact of this tenant, joined to a conversation of this tenant on `account_id`, with no consent row refusing marketing on `Conversation.channel`.

**Every entry point:**

- `CampaignService` is constructed only in `api/dependencies.py:357` and `workers/campaign_worker.py:215`.
- No AI tool, agent or other route reaches it. A grep of `app/agents` and the tools found nothing.

**No request field can name a person:**

- No schema field accepts a phone number, a body or a list.
- `AudienceRequest` has `last_inbound_within_days` (1..`MAX_RECENCY_DAYS`), `lead_statuses` and `contact_ids` (≤1000 UUIDs).

**Filters only narrow:**

- `contact_ids` is an `IN` inside the tenant- and account-scoped join. A contact of another tenant, or one with no conversation on this number, is silently excluded.
- `lead_statuses` is an `EXISTS` on this tenant's leads joined by `contact_id`.
- No filter widens the population.

**Who opens conversations:**

- Only `ConversationService.project_message` creates one, through `get_or_create` on the inbound path.
- `CampaignService._deliver` also calls `get_or_create`, but only for a recipient who already had a conversation on that number.
- A lead's `phone` is a lead field, never a contact or conversation.

So the "wrote first" boundary holds by code and by the passing endpoint and tenant-isolation tests.

**Boundary cases:**

- **Preview and materialise** use the same `_eligible` query, so their counts match at the same instant. P13: preview 20,000, materialised 20,000.
- **An erased contact** is gone: the cascade removes the recipient as well.
- **A conversation imported by backfill (0082/0083)** counts. It came from an earlier inbound, so this is correct.
- **A conversation whose only inbound is a reaction or an automated message** counts, as long as the projection opened it (ADR-120: only a new customer message has consequences). This is DERIVED.
- **`last_inbound_at` is not required.** A conversation that exists counts as "wrote first". That holds today because only inbound creates one. A future feature that opens conversations outbound-first must add the condition. Recorded as an intersection.
- **Bounds:** 50,000 per campaign (`MAX_AUDIENCE_SIZE`). There is no per-workspace bound on how many campaigns can be composed.

**The legal framing changes the reading (EXT-CMP-09).** Meta's opt-in page says a business may contact people only after receiving opt-in permission that is explicit about the business and the messages. "Wrote first" is a real consent signal, but it is not that opt-in. → **CMQ-07**.

---

## 12. Identity and Connection Binding

Re-proved by code:

- **Materialisation** records `Conversation.participant_identity_id` per contact (`campaign_repository.py:399`).
- **The composite FK** `fk_campaign_recipients_tenant_participant` keys the identity to the recipient's own contact and tenant. The invariant "identity not of own contact" sweeps to 0.
- **At send:**
  - a conversation on another account, or for another contact → skipped (`:681`);
  - a changed identity → skipped (`:690`);
  - a pre-identity row adopts the conversation's identity.
- **`MessagingService._dispatch`** addresses `participant_identity_id` through the conversation's own connection (`messaging_service.py:774-780`). It never reads `contacts.wa_id`.

Mutants M14 and M15 test these guards (§32).

**Connection lifecycle during a campaign:**

- **Disabled:** blocked at the next batch → FAILED.
- **Credential refused:** stops the campaign once, then CMP-11.
- **Released and reclaimed by another workspace:** the old workspace's account becomes inactive, so its campaign FAILs at the next batch. The new workspace's audience is its own conversations only (ADR-101). This is DERIVED.

**Two campaigns on two numbers reaching one person:** two legitimate messages. No cross-campaign guard exists (D6).

**Templates to a BSUID with no phone:** Meta's Marketing Messages page says "Sending by BSUID disables Marketing Messages Lite API delivery optimization for that send". That implies marketing sends by BSUID are possible. Recorded as EXT-CMP-11, partially confirmed. Authentication templates to a BSUID are refused locally (OMNI-054).

---

## 13. Materialisation Atomicity and Concurrency

| Probe | Result |
| --- | --- |
| Unique key in the **migration-built** schema | `uq_campaign_recipients_campaign_id_contact_id` present, plus `ix_campaign_recipients_pending (campaign_id,id) WHERE status='pending'` and the purge indexes (DB-002) |
| 4 concurrent full materialisations of one draft (P11a) | 200 rows, 0 duplicates, no 500 |
| A wider materialisation racing `schedule` (P11b, gated after the eligible read) | **both succeed**. The campaign is `scheduled` with **200 pending** against a reservation of **10** and an allowance of **50** → **CMP-04** |
| 20,000 recipients (P13) | preview 0.1 s; materialise **9.2 s**, peak Python memory **76 MiB**; schedule 0.03 s |

**Scale.** One request transaction inserts the whole audience. Scaled to the 50,000 cap that is about 23 s and about 190 MiB, inside nginx's 75 s `proxy_read_timeout` and the 30 s per-statement timeout. It holds no locks the inbound path needs (inserts plus reads), so this is **Informational**. It becomes **SCALE-TRIGGERED** only if the cap is raised past about 120,000, or if the proxy timeout is reduced.

**Materialisation racing an inbound opt-out:** the consent writer locks the consent row, and the audience reads it unlocked. A STOP that commits after the read is caught by the send-time re-check, which is mutant M10.

**Materialisation racing a workspace suspension:** nothing stops it. See CMP-02.

---

## 14. Scheduling and Time

**Accepted forms of `scheduled_at`.** `CampaignScheduleRequest.scheduled_at: datetime | None` accepts a **naive** datetime, and `_aware()` silently treats it as UTC (`campaign_service.py:924`).

- A workspace in Cairo (EEST, UTC+3 since Egypt reinstated DST in 2023) sending `2026-10-14T10:00` gets 13:00 local.
- No workspace time zone exists.
- → **CMP-15** (Low). Quiet hours are **CMQ-04**.

**Bounds on the time:**

- **Past:** `scheduled_at = max(when, now)`, so the campaign sends now, by design.
- **Far future:** refused beyond 90 days (`MAX_SCHEDULE_AHEAD`).
- **DST transitions** do not apply to UTC instants. The risk exists only through naive input, as above.

**Which clock decides.** The worker compares against **Python's `datetime.now(UTC)` from each worker process**, never the database clock. Clock skew between replicas therefore shifts when a campaign is due, and when a lease expires, by the skew. Combined with CMP-01, skew larger than a lease margin allows another replica to claim early. This is DERIVED.

**Rescheduling.** Paused and failed campaigns are rescheduled through the same path. The reservation is re-taken for `pending` only.

**Across the billing period boundary.** The reservation is checked once, in period N. Sends are metered in whatever period they happen to land. A campaign scheduled in N that runs into N+1 consumes N+1's allowance without a check there. That is by design (ADR-030: never refuse halfway). See §22.

---

## 15. The Sweep

| Question | Answer | Evidence |
| --- | --- | --- |
| 2 workers, 2 due campaigns (one leased but not started) | **3/40 people received two copies**; 43 requests | RUNTIME P01 `scheduled_gap` |
| Lease shorter than one batch (scaled: lease 3 s, batch 40 × 0.25 s) | **26/40 people received two copies**; 66 requests | RUNTIME P01 `lease` |
| Shipped constants, unscaled: 5 running campaigns × 50, rate 60, Meta 1.3 s per send; second replica at 305 s | **23/250 people received two copies**; 273 requests | RUNTIME P01b |
| Lease vs worst batch | lease 300 s, measured from the **sweep start**; a sweep works up to 5 campaigns **sequentially**; each batch is up to 50 sends × up to 10 s read timeout (500 s), or 50 × up to about 70 s for throttled sends with `Retry-After` honoured up to 30 s × 2 | CODE `campaign_worker.py:58`, `client.py:141-161` |
| 4 and 8 workers | not run separately. The mechanism is per pair of overlapping workers and grows with the overlap. | DERIVED |
| One raising campaign | contained: `campaign.batch_failed`, other campaigns continue (`_send_one` try/except); covered by an existing test | TEST |
| A large campaign starving small ones | `ORDER BY scheduled_at NULLS LAST, id LIMIT 5`: the five oldest due campaigns are taken each sweep, so a sixth is not served until one of the five completes or is paced out. With 5 long-running campaigns, a newer workspace's campaign waits. SCALE-TRIGGERED once more than 5 campaigns are due per replica. | CODE |
| Throttled connection (ADR-123) | claimed, released at the first refused send, `next_send_at` = the window; attempts untouched | TEST (`test_connection_throughput`), RUNTIME P06 429-class |
| `campaign.sweep_failed` / `campaign.worker_stopped` | emitted by `run_forever` | CODE |

**Root cause of CMP-01.** It is the composition of four facts. Any one of the first three, fixed alone, narrows the window but does not close it.

1. **The recipient lock ends at the first send commit.**
   - `claim_pending` locks the batch FOR UPDATE SKIP LOCKED.
   - `MessagingService._dispatch` commits through `released()` before the first Meta call. Every recipient lock in the batch ends there.
   - With `expire_on_commit=False` (`session.py:70`), the remaining recipients are stale in-memory snapshots. `_deliver` reads `recipient.message_id` and `status` from the snapshot, not the row.
2. **`claim_due` ignores the lease for `scheduled` campaigns.**
   - `status='scheduled' AND scheduled_at<=now` matches whatever `next_send_at` says (`campaign_repository.py:445`).
   - A campaign another replica has claimed but not yet started stays `scheduled` until its first commit, so it is claimable again.
3. **The lease is shorter than a worst-case sweep.**
   - The lease is 300 s from the sweep start.
   - Five campaigns are worked in turn, at up to 50 × 10 s each.
4. **`claim_by_id` does not check that the lease is still the caller's.**
   - It re-locks by id alone. A worker whose lease expired, and was re-leased to another replica, still proceeds.

**Consequence.** Two workers each process the same recipients. Each writes `sent` and its own `message_id`. The second write overwrites the first link, so **the recipient rows show one `sent`**. Message rows and meters show two. The invariant sweep on `wasla_probe` found 52 conversations with more than one campaign message for one campaign, and `usage_events(campaign_message)` = 392 against 340 recipients `sent`.

---

## 16. Pacing and Rate Limits (L1–L4)

| Limit | Mechanism | Proven | Gaps |
| --- | --- | --- | --- |
| **L1** API budget | `campaign_rate_limit`: 30/min per workspace on the campaign and template routers, on top of the workspace's 300/min; fails open on a Redis outage (ADR-032) | CODE + existing tests | fails open by design: **CORRECT BY DESIGN**. The manual template route sits only under the 300/min workspace budget (CMQ-02) |
| **L2** campaign pace | `messages_per_minute` 1..600 (schema `ge`/`le` and service check); `next_send_at` | RUNTIME P05 (`next_send_at = t0+0.5 s` for 5 at 600/min) | no DB CHECK (CMP-22); `next_send_at` is computed from the **sweep start**, not from when the batch ended, so a late campaign in a long sweep can send its next batch sooner than 1/rate after the last one (transient overshoot, DERIVED, CMP-20). Effective ceiling ≈ 50 per (15 s poll + batch) ≈ **200/min per replica**, so 600/min is not reachable with one replica (Informational) |
| **L3** connection allowance | `take_send_allowance`, one conditional UPDATE, refuses only `THROTTLED_ORIGINS={CAMPAIGN, FOLLOW_UP}`; replies counted, never refused | TEST (`test_connection_throughput`, green) + mutant M19 | **unset by default**, so two campaigns on one number run at twice the rate (ADR-026's recorded limit) |
| **L4** Meta | 429 / 4 / 80007 / 130429 / 131056 / 131057 → client retries 3× with `Retry-After` (≤30 s) and jitter, then `RateLimitedError` → `ProviderThrottledError` → the campaign waits `PROVIDER_THROTTLE_BACKOFF`, attempts untouched | RUNTIME P06: 429, 130429 and 131056 each 15 requests over 5 sweeps, recipients `pending/attempts=0`, campaign `running` | **131056 is per pair** ("Messages to other numbers aren't affected", Meta). Wasla stops the whole campaign and retries the **same** first pending recipient every window (head-of-line block, CMP-18). **131049 is retried** (CMP-06). **The portfolio messaging limit is unknown to Wasla** (CMP-17) |

**Is a reply ever refused?** No. `may_refuse=origin in THROTTLED_ORIGINS`. Mutant M19 makes replies refusable.

---

## 17. Send Protocol and Duplicate Prevention (D1–D8)

| Layer | Holds? | Evidence |
| --- | --- | --- |
| D1 per recipient, `UNIQUE(campaign_id, contact_id)` | yes; present in the migration-built schema | DATABASE, M01 killed |
| D2 per send, link in TX1 + abandon | **within one process, yes** (K1–K8). **Across two processes, no** (CMP-01) | RUNTIME P09, P01 |
| D3 per outcome | uncertain → abandon (2xx without messages, malformed 2xx, 500, 503, read timeout, reset: 1 request per person, `failed/attempts=1`, message `pending/requested`); not attempted → retry | RUNTIME P06 |
| D4 per credential | 401 / 190 / 368 → campaign `failed`, 1 request total | RUNTIME P06 |
| D5 per cancel | stops at the next recipient (5/30) | RUNTIME P03 |
| D6 across campaigns | no guard. The same person in two campaigns the same day receives both. Not promised anywhere → **CMQ-03** | CODE |
| D7 across producers | a follow-up and a campaign in the same minute both go. With L3 set they share the allowance; otherwise nothing relates them | CODE |
| D8 across identities | one person as two contacts (duplicate contacts) is two recipients. Identity merging is provider-asserted only (ADR-118) | CODE |

**Every Meta answer class (P06, 3 recipients, 5 sweeps):**

| Answer | Requests | Recipients | Campaign | Messages | Notes |
| --- | --- | --- | --- | --- | --- |
| 200 with id | 3 | sent ×3 | completed | sent | ✓ |
| 200 `{"messages":[]}` | 3 | failed/1 ×3 | completed | pending/requested | uncertain, never resent ✓ |
| 200 malformed JSON | 3 | failed/1 ×3 | completed | pending/requested | ✓ |
| 400 132001 | 1 | pending/1 ×3 | **failed** "template … paused" | failed/undelivered | write-back ✓. The two un-asked recipients each spent an attempt on the local refusal (CMP-19) |
| 400 132000 / 132012 / 131008 | **9** | **failed/3 ×3** | **completed**, no reason | failed ×9 | **CMP-05** |
| 400 131049 | **9** | failed/3 ×3 | completed | failed ×9 | Meta: wait ≥24 h. **CMP-06** |
| 400 131050 | 3 | skipped ×3 | completed | failed | consent recorded ×3 ✓ |
| 400 131026 | 9 | failed/3 ×3 | completed | failed ×9 | retried pointlessly (part of CMP-06) |
| 401 (no code) | 1 | pending ×3 | **failed** "credentials" | failed ×1 | ✓ then CMP-11 |
| 403 (no code) | 9 | failed/3 ×3 | completed | failed ×9 | classifier: per-message; docs say it stops (CMP-16 drift) |
| 400 190 | 1 | pending ×3 | failed | failed ×1 | ✓ |
| 400 368 | 1 | pending ×3 | failed "refused to send" | failed ×1 | ✓ |
| 429 + Retry-After / 130429 / 131056 | 15 | pending/0 ×3 | running | failed ×5 | waits ✓ (131056: CMP-18) |
| 500 / 503 / read timeout / reset | 3 | failed/1 ×3 | completed | pending/requested | ✓ |
| connect error | **27** | failed/3 ×3 | completed | failed ×9 | **CMP-07** |

**Reservation release.** No row holds a reservation. The "reserved" figure is recomputed from `pending` recipients of `scheduled`/`running` campaigns. Cancelling, pausing or failing a campaign therefore releases its unsent recipients at once, and skipped, failed and abandoned recipients stop counting. Nothing leaks.

**Secrets.** Meta error bodies are logged as code only (`_log_failure`). `failure_reason` and `last_error` hold fixed sentences ("WhatsApp refused this number's credentials."), never Meta's text or the token. The probes' `last_error` values confirm it.

---

## 18. Crash Matrix (K1–K10)

The worker ran as an OS process, killed with TerminateProcess. Recovery used a fresh process with the clock moved past the lease. All counts are requests at the HTTP server.

| Kill point | At kill | After recovery | Received twice | Never asked | Unresolved row visible |
| --- | --- | --- | --- | --- | --- |
| **K1** after campaign claim commit | 0 req, 10 pending | 10 req, sent ×10, completed | 0 | 0 | — |
| **K3/K4** after TX1+REQUESTED commit, before socket (recipient 3) | 2 req, sent ×2 | 9 req; sent ×9, **failed ×1** "could not be confirmed" | 0 | **1** (abandoned, never asked) | yes (`requested`/`pending`) |
| **K5** during the Meta call (recipient 3) | 3 req | 10 req; sent ×9, failed ×1 | 0 | 0 | yes |
| **K6** Meta answered 200, before TX3 | 3 req | 10 req; sent ×9, failed ×1 (the person **has** the message) | 0 | 0 | yes |
| **K8** mid-batch, recipient 25 of 40 | 25 req, sent ×24 | 40 req; sent ×39, failed ×1 | 0 | 0 | yes |
| K2 | inside K1 (no recipient rows written before TX1) | | | | |
| K7 | inside K8 (`next_send_at` reverts to the lease, harmless) | | | | |
| K9 materialisation | one request transaction: all or nothing (CODE) | | | | |
| K10 schedule | reservation check and status change in one transaction (CODE) | | | | |

**The documented window holds within one process.** Exactly one recipient per crash, resolved by abandoning, never by resending.

**Costs:**

- K3 loses a send nobody made, and reports it as "could not be confirmed".
- K6 reports a delivered message as failed. Statistics count that person as `failed`.

Both are the ADR-093 trade, made on purpose.

**Visibility.** The unresolved rows are visible through `UnresolvedOutboundDirectory` (MSG-11). The invariant "requested + pending" counted 22 on `wasla_probe2`, exactly the uncertain and crash probes.

**The duplicate window across two processes is not one recipient.** It is the overlap of two batches (CMP-01).

---

## 19. Pause, Cancel, Fail, Resume

| Probe | Result |
| --- | --- |
| Cancel committed after send 5 of a 30-person batch | **5** requests, 25 pending, `cancelled` ✓ (MSG-22) |
| **Pause** committed after send 5 | **30** requests: the whole batch went, `paused` → **CMP-08** |
| Cancel racing the last recipient | 30 sent, status `cancelled` (consistent) |
| Failed (template withdrawn) → fix → reschedule | remaining recipients only; nobody twice (CODE + P06 132001 path; resumes from pending) |
| Failed (credential refused) → reconnect → reschedule (P10) | **4 of 5** received it. The recipient in flight at the refusal was **abandoned** "could not be confirmed", although its message is `failed/undelivered` → **CMP-11** |
| Failed → cancel → schedule | refused ✓ |
| Pause by a member | 403 (`TenantAdminDep`; mutant M23 for schedule) |
| Paused for 40 days, then resumed | consent re-read per recipient ✓; template re-checked per batch ✓; reservation re-taken in the **current** period ✓; `participant_identity` re-checked ✓; a linked message purged by retention would `SET NULL` `message_id` on a **pending** recipient and re-arm a send (DERIVED, only after a crash) |

---

## 20. Opt-Outs and Consent (W1–W7, R1–R5)

All writers go through `record_opt_out` / `record_resume` (`opt_out.py`). These take `ContactConsentRepository.lock`, which inserts the row if absent and selects it FOR UPDATE.

| Writer | Channel recorded | Evidence |
| --- | --- | --- |
| W1 text stop phrase (inbound transaction) | the connection's | TEST (`test_opt_out_ingestion`, green), M12 |
| W2 "Stop promotions" button (OMNI-030) | the connection's | TEST (`test_channel_consent`, `test_opt_out_ingestion`) |
| W3 `recover-button-opt-outs` (OMNI-030) | WhatsApp, `via=replay` | TEST (`test_opt_out_recovery`) |
| W4 `user_preferences` stop/resume (OMNI-046) | WhatsApp | TEST (`test_provider_consent_signals`) |
| W5 131050 on a send | the connection's | RUNTIME P06 (3/3 recorded) |
| W5' 131050 as an **asynchronous failed status** | **not recorded** | CODE `advance_status` ignores the error code → **CMP-10** |
| W6 member `POST /contacts/{id}/opt-out` (channel required) | as named | CODE + TEST |
| W7 admin `DELETE … ?channel=` / provider resume (`lifts` only its own evidence) | as named | CODE |

| Reader | Channel read | Evidence |
| --- | --- | --- |
| R1 `AudienceRepository` base population | `Conversation.channel` | CODE, mutant M09 |
| R2 per-recipient check at send | `_channel_of(campaign)` (the connection's) | CODE, mutants M10/M11 |
| R3 follow-up dispatch | the conversation's | TEST (`test_follow_up_revalidation`) |
| R4 opt-out read API | per channel, with identities | CODE |
| R5 manual template send | **not checked** | CODE → CMQ-02 |

**The legacy column is gone.** `contacts.marketing_opt_out_at` was dropped by 0097. No reader of it remains (grep), and there is one opt-out system.

**Windows:**

- **Same transaction.** A STOP is stored in the same transaction as the inbound message (CAMPAIGNS.md; W1 tests).
- **STOP while a recipient's send is in flight.** The send-time read happens before TX1 commits, unlocked. A STOP committed after that read is not seen for that one recipient, and the next recipient re-reads. The window is **one recipient's read → socket interval**, bounded by one send. This is the documented and accepted window (DERIVED, CODE).
- **STOP between materialisation and send** → skipped (R2; M10).

**Resume and replay:**

- **Newer resume wins.** `record_resume` keeps the newest `resumed_at`. The replay tool checks resume evidence (OMNI-030 tests).
- **A late-delivered old STOP** whose timestamp predates a resume **is** recorded, because `record_opt_out` does not compare `at` with `resumed_at`. This is DERIVED. The only realistic source is Meta redelivering an old inbound message for the first time, and idempotency makes a true replay a no-op.

**The agent does not keep selling after an opt-out tap.** No agent turn runs for an opt-out tap (OMNI-030 test).

**Evidence for a dispute:** `marketing_opt_out_at`, `opt_out_source`, `opt_out_via` and the channel are all on the row. "Which message" is not on the row; it is recoverable only from the conversation transcript (LP).

**Scope across numbers.** A STOP on WhatsApp number A stops WhatsApp marketing from number B of the same workspace. That is ENT-19, documented. Whether customers expect it is **CMQ-01**.

**The stop vocabulary (P08):**

- **Matched:** STOP, `Stop.`, `stop!!`, `Stop 🙏`, full-width ＳＴＯＰ, RTL-marked stop, إلغاء, الغاء الاشتراك, إلغاء الإشتراك, توقف, ايقاف, لا رسائل, opt out, Stop promotions.
- **Correctly not matched:** "stop by the shop tomorrow".
- **Missed although unambiguous:**
  - `opt-out` (`opt out` and `optout` are listed);
  - `STOP STOP`;
  - `اوقف` and `أوقف الرسائل` (the imperative of the listed ايقاف);
  - `ستوب` (Egyptian Arabizi);
  - `unsub`.
  → **CMP-12** (Low).
- **Missed and arguably ambiguous:** `stop please`, `please stop`, `لا ترسل`, `بطل رسايل`, `وقف`, `remove me`, `Unsubscribe me`, `كفاية`. → **CMQ-12**. The narrow matcher is CORRECT BY DESIGN; extending it is a product choice.

**Opt-out is all-or-nothing across categories.** LATER PRODUCT (LP-CMP-07).

---

## 21. Follow-Ups and Other Automated Outbound

Only the shared guarantees were checked. All hold for follow-ups:

- the opt-out re-read on the conversation's channel;
- connection allowance, with a wait that costs no attempt;
- uncertain sends abandoned;
- a refused credential stopping the sweep once;
- the workspace served gate (`follow_up_service.py:730`, TOOL-04);
- `channel_not_in_plan`;
- registry asymmetry inside `send_template`.

Evidence is TEST: `test_follow_up_revalidation`, `test_follow_up_channels`, `test_crm_follow_up_claims`, `test_outbound_delivery_protocol`, `test_channel_not_in_plan`. All are green in the baseline.

**The asymmetry is the finding.** Follow-ups have the workspace lifecycle gate; campaigns do not (CMP-02).

Manual template sends share none of the automated-send guarantees (§6, CMQ-02).

---

## 22. Entitlements, Quota and Billing Intersection

| Claim | Result |
| --- | --- |
| Reservation for the whole audience under the lock, counting other live campaigns | **the count is read before the lock** (`campaign_service.py:376-380`). Gated race: both 20-recipient campaigns accepted against 30. **Natural: 40/40 trials both accepted.** Sequential control: second → 402 ✓ → **CMP-03** |
| 402 before a message goes out | ✓ sequentially; nothing half-sent |
| Released on cancel, skip, failure, abandon | ✓ by construction: the reservation is recomputed from `pending` of `scheduled`/`running` campaigns, so nothing leaks |
| Metered once in `period_campaign_messages` and once in the neutral sent meter (ENT-22), in the same transaction as SENT | ✓ for one process. Under CMP-01 **both meters double** (392 vs 340) |
| A campaign crossing the period boundary | checked once in N; sends meter in N+1 unchecked. ADR-030 trade; recorded as an intersection |
| Top-up expiring mid-campaign | same: reservation-time only (DERIVED) |
| Downgrade or upgrade mid-campaign | sends continue at the reserved size; only `channel_not_in_plan` is re-checked per batch |
| Capacity reduction disables the connection (ENT-14/15) | `account.is_active` false → campaign FAILED at the next batch (CODE `_blocking_reason`) |
| Plan drops the channel type | recipients SKIPPED `channel_not_in_plan`, never charged ✓ (mutant M22). Not checked at **schedule**, so a campaign that can only skip is still accepted and reserves (CMP-24) |
| Unlimited (absent key, ADR-029) | `check` allows; reservation is a no-op beyond the lock |
| **Subscription `suspended` (dunning) or `past_due` after scheduling** (P12) | **10/10 sent.** The default plan (`starter`) allows `period_campaign_messages: 0`, yet a scheduled campaign keeps sending. BILLING.md §553 documents only `channel_not_in_plan` for automated sends → **CMQ-11** |

**Materialisation as a second route past the reservation (P11b):**

- A wider `set_audience` can commit after another admin's `schedule`: the status check is an unlocked read, and nothing re-checks after the lock.
- The campaign then holds 200 pending recipients against a reservation of 10.
- The next schedule would refuse, but this campaign is already `scheduled`. → **CMP-04**.

**False green (CMP-21).** The only concurrent campaign-scheduling test, `test_topup_concurrency.py:533`, asserts `live * 4 <= 13` with 3 campaigns of 4. It is satisfied by every outcome in which the top-up was granted, so it cannot detect CMP-03.

---

## 23. Workspace and Connection Lifecycle

| Event (P03 / P12 / code) | Effect on a scheduled or running campaign |
| --- | --- |
| Workspace **suspended by the platform** (`tenants.status=suspended`) | **sends everything** (10/10) → **CMP-02**. Question 54 of the authorization audit is settled: no |
| Workspace **soft-deleted**, inside retention | **sends everything** (10/10) → CMP-02 |
| Workspace purged | rows erased by cascade (campaigns, recipients by `tenant_id` FK); a worker holding an id gets `claim_by_id → None` (DERIVED) |
| Subscription suspended or past due (dunning) | sends everything (CMQ-11) |
| Connection disabled by an admin, or by a capacity reduction | FAILED at the next batch ✓ |
| Credential refused by Meta | FAILED once ✓; resume loses one person (CMP-11) |
| Connection released or reclaimed | FAILED at the next batch (inactive) — DERIVED |
| Creating admin removed or demoted mid-campaign | campaign continues. `created_by_id` is only `sent_by_id` for the messages, `SET NULL` on user deletion → **CMQ-09** |
| Contact erased mid-campaign | recipient cascades away; `_deliver` would skip "no longer exists" (CODE) |

---

## 24. Delivery Status and Statistics

**Where the counts come from.** `pending`, `sent`, `failed` and `skipped` come from recipient rows. `delivered` = delivered + read messages, and `read` = read messages, joined through `recipient.message_id`. Delivered includes read ✓.

**Status webhooks:**

- They only climb (`advance_status`).
- `failed` ranks above `sent` and below `delivered` (MSG-14).
- Out-of-order statuses keep the furthest state ✓ (TEST).

**A message `sent` and later reported `failed` by webhook.** The recipient stays `sent` and `failed` does not count it. No statistic shows "accepted by Meta, then failed". The error code in the webhook (131049, 131050, 132015 from template pacing) is dropped → **CMP-10**.

**Duplicates under CMP-01** are invisible to statistics, because only the last link is joined.

**The recipients listing:**

- at most 100 rows;
- **no cursor**;
- filter by status only, no filter by reason;
- `RecipientRead` has no `participant_identity_id`.

So a 2,000-person campaign's recipients cannot be listed past the first 100 → **CMP-13**. The listing is tenant-scoped ✓ (mutant M24).

**Reconciliation.** On a single-process probe campaign, the statistics totals equal the rows. On `wasla_probe`, which holds the race probes, `campaign_message` meters (392) ≠ recipients sent (340), and the difference equals the duplicates.

**Retention.** Message retention purging a linked message sets `message_id` to null, and the person then drops out of `delivered`/`read`. Statistics of old campaigns therefore decay (DERIVED).

---

## 25. Authorization and Tenancy

| Route | Owner | Admin | Member | Other tenant | Anonymous | Platform staff |
| --- | --- | --- | --- | --- | --- | --- |
| list, get, statistics, recipients | ✓ | ✓ | ✓ | 404 (tenant repo) | 401 | via platform routes only |
| create, preview, audience, schedule, pause, cancel | ✓ | ✓ | 403 | 404 | 401 | — |
| template list/get | ✓ | ✓ | ✓ | 404 | 401 | — |
| template sync, opt-out payloads | ✓ | ✓ | 403 | 404 | 401 | — |
| contact opt-out POST / DELETE | ✓ / ✓ | ✓ / ✓ | ✓ / 403 | 404 | 401 | — |

Evidence: CODE (`TenantAdminDep`, `ActiveWorkspaceDep`), TEST (`test_campaign_endpoints`, `test_tenant_isolation`), mutant M23 killed; M24 survives behind the independent tenant-scoped campaign lookup (§32).

**Cross-tenant references:**

- A template of tenant A in a campaign of tenant B → `require_by_id` 404.
- A connection of A → 404.
- `contact_ids` of B → excluded by the tenant-scoped join.

**The worker** builds both services from `campaign.tenant_id` (`campaign_worker.py:204`) ✓.

**Database-level tenant agreement.** `campaigns.account_id` and `template_id` reference their parents by id only, not as ADR-100 tenant-agreed composite keys. That is defence in depth only, since every writer is tenant-scoped (CMP-22).

**Audit trail:** `CAMPAIGN_SCHEDULED` (actor, recipients, time) and `CAMPAIGN_CANCELLED`. **Pause, composition and audience are not audited** (CMP-14).

---

## 26. Security and Abuse

**What bounds a new or compromised workspace on day one:**

- the "wrote first" audience;
- the plan's `period_campaign_messages` (starter: **0**, pro: 5,000);
- the campaign API budget (30/min);
- the 600/min rate (effectively ≈200/min per replica);
- the 50,000 audience cap;
- the connection allowance, if configured.

**No platform-level daily cap** exists independent of the plan.

**The two weaknesses are CMP-03/04 (allowance bypass) and CMQ-02.** The manual template route lets any member send templates, including marketing ones, to every conversation at 300/min. It has no reservation and no opt-out check.

**Row growth.** There is no bound on draft campaigns per workspace (CODE). Rows are small, and the campaign API budget of 30/min bounds the creation rate. Low.

**Input bounds.** Names and descriptions are bounded and stored as `StorableText`.

**Parameters stay out of logs.** Template parameters do not reach logs or audit metadata. Audit records the recipient count and time only.

**Secrets.** Meta error bodies are reduced to their code. The token is never in `last_error`, logs or responses, as the probes' `last_error` values confirm.

**Cross-tenant impact.** One workspace's campaign can delay others: it takes one of the 5 claim slots per sweep (§15, SCALE-TRIGGERED). It cannot lock another tenant's rows.

---

## 27. Privacy and Retention (campaign scope)

- **Nothing sweeps completed campaigns.** Recipient rows live as long as the workspace. This is still true (CAMPAIGNS.md "Known limits") and still a product decision → **CMQ-08**.
- **Contact erasure** cascades the recipient rows away, so "who was written to" is lost for that person. The consent row also cascades with the contact (FK), so **opt-out evidence is erased with the contact**. CMQ-08: legal evidence versus erasure.
- **Workspace purge** erases campaigns and recipients (DB-002 indexes present).
- **Workspace export of campaigns:** not found. Owned by audit 18.

---

## 28. Metrics and Operations

| Signal | Exists | Emitted (probe) | Scraped | Alert rule | Receiver verified |
| --- | --- | --- | --- | --- | --- |
| `campaign.sweep_failed`, `campaign.worker_stopped` (logs) | ✓ | by code path | logs only | no | no |
| `campaign.batch_failed`, `campaign.blocked` (logs) | ✓ | ✓ (P06 log stream: `campaign.blocked`) | logs only | no | no |
| `channel.connection_throttled` (log) | ✓ | ✓ (tests) | logs only | no | no |
| Provider error class counter (`record_provider_error`) | ✓ | ✓ (P06 classes) | Prometheus exposition | not campaign-specific | not verified |
| Opt-outs by source (`record_opt_outs`) | ✓ | ✓ for 131050 | ✓ | no | no |
| Stuck campaign (running, no progress for N minutes) | **no** | | | | |
| Campaign failed (template, credential) | log only | ✓ | | **no** | |
| Unresolved requested sends | `UnresolvedOutboundDirectory` (MSG-11) | | | | |
| 131049 / 130429 rates per connection | provider error class only (bounded labels, no connection) | | | | |
| Campaign worker heartbeat in readiness | worker heartbeat exists (`workers/heartbeat.py`); campaign kind covered by the runner — DERIVED | | | | |

**RUNBOOK "Campaigns are not sending" (§638), as written:**

- The query works.
- It does not cover:
  - a **suspended workspace** whose campaign is (wrongly) sending (CMP-02);
  - a campaign whose `next_send_at` keeps moving 300 s forward because every batch raises (`campaign.batch_failed`);
  - a throttled or paused connection;
  - a template paused by Meta;
  - the credential-refused reason.
- It says "`failed` with `last_error` — the template was withdrawn or the number was disabled". Credential and connection refusals also land there.

This is documentation drift (CMP-23). Observability is owned by audit 19.

---

## 29. Migrations

| Check | Result |
| --- | --- |
| `alembic upgrade head` on empty (`wasla_probe`, `wasla_probe2`, `wasla_mig`) | ✓ 0100 |
| `alembic check` | No new upgrade operations ✓ |
| Populated 0096 → 0097: 300 contacts, 100 legacy person-level opt-outs | **100/100** moved to WhatsApp consent rows |
| Downgrade 0097 → 0096 | **100/100** restored to `contacts`; the downgrade **refuses** when a non-WhatsApp consent or resume provenance exists (no silent loss) |
| Upgrade to head again | 100/100 ✓ |
| Indexes the sweep, audience and purge rely on | `ix_campaigns_due`, `ix_campaign_recipients_pending`, `…_campaign_id_status`, `…_tenant_id`, `…_message_id`, `…_conversation_id`, `…_participant_identity_id`; unique key ✓ |
| Earlier populated chain | the canonical merge verification ran 0081 → 0100, 373 rows, 0 lost (not re-run) |

---

## 30. Database Invariants

Swept after every probe. `wasla_probe` holds the CMP-01 races; `wasla_probe2` holds every other probe.

| Invariant | `wasla_probe` | `wasla_probe2` | Explanation |
| --- | --- | --- | --- |
| duplicate (campaign, contact) | 0 | 0 | |
| recipient `sent` without message | 0 | 0 | |
| recipient message of another tenant / connection | 0 / 0 | 0 / 0 | |
| pending + linked | 0 | **3** | the 401, 190 and 368 probes: the recipient keeps its link to an undelivered message (CMP-11) |
| identity not of the recipient's own contact | 0 | 0 | |
| `sent` while opted out on that channel before `sent_at` | 0 | 0 | |
| running with no pending | 0 | 0 | |
| campaign template on another connection | 0 | 0 | |
| **conversations with >1 campaign message for one campaign** | **52** | 0 | the CMP-01 duplicates (3 + 26 + 23) |
| `campaign_message` meters vs recipients `sent` | **392 vs 340** | 185 vs 185 | the duplicates were metered |
| messages `requested` + `pending` (unresolved) | 0 | 22 | the 18 uncertain-class probes plus K3, K5, K6 and K8 |
| consent rows without channel | 0 | 0 | |

---

## 31. Test Quality

Baseline: **577 passed, 0 failed** on a migration-built schema. Mutation control: **323 passed**, model schema.

**False greens and structural blind spots:**

1. **No test runs two real campaign workers.**
   - `test_campaign_worker.py` hands the worker a `StubMessaging` and **one shared test session** (`SessionHandle`). Concurrency, lease expiry and commit-boundary lock loss are unobservable there.
   - "Sent once" is asserted on the stub's counter or on rows, never on provider calls through the real `MessagingService`.
   - That is why CMP-01 and the survival of M02 and M04 went unseen.
2. **`test_topup_concurrency.py::test_a_topup_racing_campaign_scheduling_never_over_promises` is vacuous** for the reservation race (§22).
3. **`test_campaign_endpoints.py` stubs the service.** Its role matrix is real; its business assertions are not.
4. **No test sends a campaign through a template with a header or button**, so CMP-05 is invisible.
5. **No test drives a running campaign with the workspace suspended**, so CMP-02 is invisible. The follow-up equivalent exists.
6. **No test asserts that pause stops a batch.** MSG-22's test covers cancel only.
7. **No test exercises 131049**, and none exercises connect-error exhaustion at campaign level.
8. The seeding helpers build the audience through the real `AudienceRepository` ✓ (ENT-19 requirement met).

---

## 32. Mutation / Non-Vacuity

**How the mutants ran.** There were 26 mutants, each one exact textual replacement (`audit/run_mutations.py`), in a detached worktree (`E:\wasla-cmp-mut`) on its own database.

- **Narrow lane:** 23 campaign, consent, outbound, entitlement, template and client suites (control 323 passed), run with `-x`.
- **Wide lane:** every survivor re-ran against all **124** test files touching campaigns, consent, opt-out, follow-ups, templates, outbound or provider errors (control-equivalent **1,952 passed, 38 skipped**), so no survivor is an artefact of suite selection.

**Result: 16 KILLED, 10 SURVIVED.**

| # | Mutant | Expected detector | Narrow | Wide | Verdict on the survivor |
| --- | --- | --- | --- | --- | --- |
| M01 | `UNIQUE(campaign_id, contact_id)` removed | model test | KILLED (`test_a_person_appears_in_a_campaign_once`) | | |
| M02 | recipient claim loses FOR UPDATE SKIP LOCKED | concurrent claim test | SURVIVED | **SURVIVED** | test gap. The lock is moot after the first commit anyway (CMP-01) |
| M03 | `link()` never called | delivery protocol | KILLED (`test_meta_timeout_is_not_blindly_retried`) | | |
| M04 | abandon guard removed (pending + linked resent) | crash/resume test | SURVIVED | **SURVIVED** | **test gap; guard real.** Runtime under the mutant: K5 crash → **1 person received two copies** (P09 in `E:\wasla-cmp-surv`) |
| M05 | uncertain delivery treated as retryable | campaign × uncertain answer | SURVIVED | **SURVIVED** | **test gap; guard real.** Runtime under the mutant: 500 and read timeout → **9 requests for 3 people** (P06) |
| M06 | 2xx without id → definite failure (MSG-07) | provider outcomes | KILLED | | |
| M07 | refused credential per recipient (MSG-18) | provider error sweeps | SURVIVED | KILLED (`test_provider_error_sweeps::test_a_connection_level_code_stops_the_campaign_after_one_recipient`) | |
| M08 | cancel re-read removed (MSG-22) | cancel mid-batch | SURVIVED | **SURVIVED** | **test gap; guard real** (P03 cancel: 5/30 with the shipped code). MSG-22 has no regression test |
| M09 | opt-out removed from the audience | audience test | KILLED | | |
| M10 | opt-out re-check at send removed | mid-campaign opt-out | KILLED | | |
| M11 | send-time opt-out read on the wrong channel | same | KILLED | | |
| M12 | opt-out writer ignores the channel | channel consent | KILLED | | |
| M13 | newer-resume-wins reversed | resume ordering | SURVIVED | **SURVIVED** | test gap (OMNI-030's ordering is untested at the writer) |
| M14 | participant identity ignored | omnichannel operations | SURVIVED | KILLED | |
| M15 | sent through another connection's conversation | omnichannel operations | SURVIVED | KILLED | |
| M16 | template approval not re-checked per batch | campaigns | KILLED | | |
| M17 | `next_send_at` pacing not applied | campaigns | KILLED | | |
| M18 | `messages_per_minute` ceiling removed (service) | service validation test | SURVIVED | **SURVIVED** | **equivalent at the API**: `CampaignCreateRequest` keeps `le=600`. A test gap for the service rule, no DB CHECK (CMP-22) |
| M19 | allowance refuses replies too | throughput | KILLED | | |
| M20 | **reservation not taken at scheduling** | entitlement via `schedule` | SURVIVED | **SURVIVED** | **test gap on a billing guarantee.** No test schedules a campaign past `period_campaign_messages` through `CampaignService.schedule` and expects 402 |
| M21 | **reservation ignores other live campaigns** | concurrent scheduling | SURVIVED | **SURVIVED** | **test gap.** The one concurrent test is vacuous (§22); a sequential two-campaign test would kill it |
| M22 | `channel_not_in_plan` removed | ENT-16 | KILLED | | |
| M23 | schedule route open to members | endpoint roles | KILLED | | |
| M24 | tenant filter removed from the recipients listing | isolation | SURVIVED | **SURVIVED** | **independent guard**: the campaign is resolved through the tenant-scoped `require_by_id` first, so another tenant's campaign id 404s before the listing runs. Not exploitable; defence in depth untested |
| M25 | registry write-back on an ambiguous code | client unit | SURVIVED | **SURVIVED** | test gap (132000 would pause a working template) |
| M26 | campaign claim ignores the lease for RUNNING | worker | KILLED | | |

**Exploitable survivors: none in the shipped code.** Every survivor was either reproduced as correct runtime behaviour with the original code (M04, M05, M08), held by an independent guard (M18, M24), or is a pure test gap (M02, M13, M20, M21, M25).

The three most dangerous gaps are **M04, M05 and M20**. Each removes a guarantee whose loss would put a second message on a phone or bypass billing, and each passes the whole relevant suite.

All mutants were reverted. `git status` in `E:\wasla-cmp-mut` and `E:\wasla-cmp-surv` shows no change under `app/`.

---

## 33. Documentation Drift

| Document claim | Reality | Verdict |
| --- | --- | --- |
| CAMPAIGNS.md "Opt-out is part of the base population" | true, per channel (ENT-19) | ✓ |
| CAMPAIGNS.md Known limits "The rate is per campaign, not per number" | true **by default**; ADR-123 / `CONNECTION_SENDS_PER_MINUTE` and the 0090 per-connection override exist and are not mentioned | drift (CMP-23) |
| CAMPAIGNS.md "Marketing opt-out — Recorded per contact and channel" | ✓ (updated by ENT-19) | ✓ |
| CAMPAIGNS.md / `campaign_service.py:760` "The recipient row is what makes the pair safe from double counting" | false under two replicas (CMP-01) | defect, already counted in CMP-01 |
| CAMPAIGNS.md "Periodic background sync is not implemented" | still true | ✓ |
| CAMPAIGNS.md "A 401, 403 or Meta's own code 190 … the campaign stops once" | a **bare 403** is per-message (`meta/errors.py`, deliberately) | drift (CMP-16) |
| CAMPAIGNS.md "Somebody watching a broadcast … needs an action that is not destructive, which is what pause is for" | pause lets the whole claimed batch go (CMP-08) | defect |
| CAMPAIGNS.md "Two campaigns running at once on one number can exceed either one's rate" | true when L3 is unset | ✓ |
| `whatsapp_template.py:47` "`MARKETING` is the only one a campaign may use" | not enforced | CMP-09 |
| CAMPAIGNS.md API "`/recipients` — Who it reached, and who it did not" | first 100 only | CMP-13 |
| API.md campaign operations | 10 listed = 10 mounted ✓ | ✓ |
| TASKS.md Phase 11 | no open campaign item; the holding message (line 240) is still open and correctly filed under escalation | ✓ |
| RUNBOOK "Campaigns are not sending" | incomplete (§28) | drift (CMP-23) |
| BILLING.md §553 suspended → only `channel_not_in_plan` for automated sends | accurate, and the source of CMQ-11 | ✓ |
| DECISIONS ADR-026 "a campaign sends at or slightly under its configured rate, never above it" | transient overshoot possible when a sweep is long (CMP-20) | drift (Low) |

---

## 34. Missing Features and Later Product

| Item | Classification |
| --- | --- |
| Per-recipient personalisation | LATER PRODUCT (Known limits) — LP-CMP-01 |
| Import with consent evidence | PRODUCT DECISION (ADR-025) — CMQ-07 |
| Saved segments | LATER PRODUCT — LP-CMP-02 |
| A/B or variant templates | LATER PRODUCT — LP-CMP-03 |
| Quiet hours or sending windows in the workspace's time zone | PRODUCT DECISION — CMQ-04 |
| Frequency cap across campaigns per person | PRODUCT DECISION — CMQ-03 |
| Send-time re-validation of category or pricing | part of CMP-09 (category) / LP-CMP-04 (pricing) |
| Estimated cost before scheduling | LATER PRODUCT (EXT-CMP-10) — LP-CMP-04 |
| Test send to the workspace's own number | LATER PRODUCT — LP-CMP-05 |
| Campaign analytics events | LATER PRODUCT (Known limits) — LP-CMP-06 |
| Recipient retention policy | PRODUCT DECISION — CMQ-08 |
| Per-category consent | LATER PRODUCT — LP-CMP-07 |
| Periodic template sync / status webhook | PRODUCT DECISION — CMQ-05 |
| Per-number Meta tier and quality awareness | PRODUCT DECISION CMQ-06 + CMP-17 |
| Campaigns on other channels | LATER PRODUCT — LP-CMP-08 |
| Campaign duplication | LATER PRODUCT — LP-CMP-09 |
| Second-admin approval | PRODUCT DECISION — CMQ-10 |
| Marketing Messages API routing | LATER PRODUCT (EXT-CMP-12) — LP-CMP-10 |

---

## 35. Omnichannel Fit

| Part | Today |
| --- | --- |
| Audience (conversation on the connection, consent on `Conversation.channel`) | **neutral** |
| Template registry | **Meta-only** (`whatsapp_templates`, `waba_id`) |
| Send path | **neutral**: `MessagingService.send_template` → `ChannelRegistry.adapter_for(conversation.channel)`. But `SendKind.TEMPLATE` means "WhatsApp template", and the policy refuses it on a channel without templates |
| Outcomes taxonomy | **neutral** (`app.channels.outcomes`: `SendNotAttemptedError`, `UncertainDeliveryError`, `ProviderConnectionRefusedError`, `RecipientOptedOutError`); template withdrawal is WhatsApp's `TemplateWithdrawnError` |
| Statistics | neutral (message status protocol) |
| Meters | neutral sent meter with a channel dimension (ENT-22) plus `campaign_message` |
| Campaign row | `account_id` FKs to `whatsapp_accounts` (WhatsApp-bound) |

Instagram broadcast and Email campaigns are LATER PRODUCT (LP-CMP-08). Email campaigns belong to the Email audit's §39. They are not defects.

---

## 36. Official Documentation Review (Meta)

All read 2026-10-13. EXT verdicts: CONFIRMED BY PROVIDER DOCS / CONFLICTS WITH WASLA / EXTERNAL PROVIDER VERIFICATION.

| ID | Fact | Source | Wasla consequence | Verdict |
| --- | --- | --- | --- | --- |
| EXT-CMP-01 | The messaging limit counts **unique users messaged outside a customer service window in a moving 24 h**, **per business portfolio** ("shared by all business phone numbers within a portfolio"). Tiers are 250 (new), 2,000, 10,000, 100,000 and unlimited. Scaling path to 2,000; automatic scaling up one level within 6 h. Webhooks `business_capability_update` and `account_alerts`. **The error on exceeding it is not stated on the page.** | developers.facebook.com/docs/whatsapp/messaging-limits | Wasla allows 50,000 per campaign with no tier awareness and parses neither webhook. What happens past the limit is unknown (CMP-17, CMQ-06) | CONFLICTS (no awareness) + EXTERNAL (error code) |
| EXT-CMP-02 | Throughput is 80 mps by default per number, 1,000 by automatic upgrade, 20 fixed for Coexistence. Exceeding it returns **130429** "until you are within your allowed level again" | developers.facebook.com/docs/whatsapp/throughput | Wasla pacing (≤600/min) is far below; 130429 → wait | CONFIRMED BY PROVIDER DOCS |
| EXT-CMP-03 | **131049**: "wait at least 24 hours before resending the template message … Doing so will only result in another error response" | developers.facebook.com/docs/whatsapp/cloud-api/support/error-codes | Wasla retries twice more within minutes (CMP-06) | **CONFLICTS WITH WASLA** |
| EXT-CMP-04 | **131050**: "Do not retry sending messages to this user as they will not be received." Users can stop marketing through a template preference (opt-in page) | error-codes; getting-opt-in | Synchronous 131050 → skipped + consent ✓; asynchronous (status) 131050 not recorded (CMP-10) | CONFIRMED (sync) / EXTERNAL (whether Meta reports it asynchronously) |
| EXT-CMP-05 | **131056**: "Wait and retry the operation, if you intend to send messages to the same phone number"; other numbers are not affected | error-codes | Wasla stops the whole campaign (CMP-18) | CONFLICTS (scope) |
| EXT-CMP-06 | Every template is categorised authentication, marketing or utility; category affects pricing; the categorisation guide covers miscategorisation | business-management-api/message-templates; pricing | Campaigns accept any category (CMP-09) | CONFIRMED (fact); recategorisation mechanics EXTERNAL |
| EXT-CMP-07 | Templates must be `APPROVED` to send. Statuses include In review, Rejected, Active (quality pending / high / medium / low), Paused, Disabled, Appeal requested. **`message_template_status_update` webhook** reports changes | message-templates | Wasla maps PAUSED, LIMIT_EXCEEDED → paused, DISABLED, PENDING_DELETION, DELETED → disabled, IN_APPEAL → pending; does not subscribe to the webhook (CMQ-05) | CONFIRMED (statuses); webhook not used |
| EXT-CMP-08 | **Template pacing**: new, unpaused, or not-GREEN marketing and utility templates may be paced. The API answer carries `message_status: held_for_quality_assessment`. On negative feedback the template is PAUSED and **each held message is dropped with a `failed` status webhook, code 132015** | developers.facebook.com/docs/whatsapp/business-management-api/message-templates/template-pacing | Wasla counts a held message as `sent`; the later 132015 failure is stored as text, with no write-back and no recipient change (CMP-10) | CONFLICTS (unhandled) |
| EXT-CMP-09 | Opt-in: a business may contact people only after receiving opt-in permission that clearly states the person is opting in and names the business. It should offer opt-out by category and honour it | developers.facebook.com/docs/whatsapp/overview/getting-opt-in | ADR-025's "wrote first" is not an explicit opt-in (CMQ-07; legal review) | CONFLICTS (policy gap → owner and legal) |
| EXT-CMP-10 | Per-message pricing since 2025-07-01: marketing charged per delivered template by country code; utility free inside an open service window; Egypt has its own rate (rate card, not on the page); Egypt marketing rates lowered 2026-01-01 | developers.facebook.com/docs/whatsapp/pricing | No cost estimate (LP-CMP-04); duplicates under CMP-01 are paid to Meta | CONFIRMED; the Egypt figure is EXTERNAL (rate card) |
| EXT-CMP-11 | The Marketing Messages page notes "Sending by BSUID disables Marketing Messages Lite API delivery optimization", which implies BSUID marketing sends exist | developers.facebook.com/documentation/business-messaging/whatsapp/marketing-messages/send-marketing-messages/ | Campaigns to BSUID identities are plausible; authentication to BSUID refused locally (OMNI-054) | EXTERNAL (direct statement for Cloud API `/messages`) |
| EXT-CMP-12 | The Marketing Messages API is **optional**: "You may still use the `/messages` endpoint to send marketing messages through the Cloud API". TTL and fallback policy are MM-API-only; new or dormant marketing templates need 10 min sync before marketing traffic | same | Wasla uses `/messages` (valid). A campaign started within 10 min of creating a template may be affected (EXTERNAL) | CONFIRMED |
| EXT-CMP-13 | Quality and number status webhooks: messaging-limit increases via `business_capability_update`, denials via `account_alerts`; the page names no quality-rating webhook | messaging-limits | Wasla parses none of them (CMQ-06) | EXTERNAL for the quality webhook |
| EXT-CMP-14 | No explicit backoff numbers for 130429, 4, 80007 ("retry later or reduce frequency"); webhooks are retried for up to 7 days with exponential backoff | throughput; error-codes | Wasla honours `Retry-After` (≤30 s) with jitter, then waits `PROVIDER_THROTTLE_BACKOFF` | CONFIRMED (no conflict) |

**Error-code table (error-codes page).** 132000, 132012 and 131008 mean "fix the request" (parameters), so retrying is pointless (CMP-05). 132001, 132015 and 132016 are template-level ✓. 368 is a policy restriction ✓. 190 means get a new token ✓. 4 and 80007 are rate limits ✓. The page gives **no HTTP status** for any of these codes, which supports Wasla's code-first classification (OMNI-035).

Page access: every URL above was read successfully. No quote exceeds a short phrase.

---

## 37. Real Provider Verification

**Not performed.** The user did not authorise a real-provider lane in this session. **Campaigns have not been verified against real Meta in this audit.**

A real lane must observe all of the following:

- **Template sync.** One read-only sync, with the registry rows compared to WhatsApp Manager (status, category, components).
- **A real campaign.** One to three recipients the user names as their own, with wamid, statuses (`sent`, `delivered`, `read`) and timestamps recorded, and the recipient and statistics rows reconciled.
- **STOP.** A STOP from the user's phone produces a WhatsApp consent row with `via=message`, and a second campaign skips that number.
- **"Stop promotions".** The button tap, if the template carries one, is recorded with `via=reply_action`.
- **Held messages.** Whether a new marketing template's first send returns `message_status: held_for_quality_assessment` (EXT-CMP-08).
- **Past the messaging limit.** The error code returned beyond the limit (EXT-CMP-01), if a test portfolio is at 250.

---

## 38. Walkthroughs

| # | Scenario | What happens (evidence) |
| --- | --- | --- |
| C1 | Compose, materialise 2,000, preview, schedule tomorrow 10:00 Cairo | Composition ✓. Materialise ≈1 s (P13 scale). Preview == materialised. **A naive `10:00` is taken as UTC (13:00 Cairo)**; an aware `+03:00` value is correct (CMP-15). Reservation 2,000 against the plan (pro: 5,000) ✓. **On a new portfolio (250 tier), about 1,750 recipients will exceed Meta's messaging limit, behaviour unknown (CMP-17, EXT-CMP-01)** |
| C2 | Runs at 60/min, two workers, one killed mid-batch | Kill: abandon one, never resend (P09 K8) ✓. **Two live workers: duplicates whenever their sweeps overlap a scheduled campaign or a sweep outlives 300 s (P01, P01b) — CMP-01** |
| C3 | 40 Arabic STOPs mid-run, 3 "Stop promotions" taps, one "stop by tomorrow" | Arabic STOP forms in the list are honoured in the inbound transaction and skipped at send ✓. `اوقف` / `ستوب` are **not** recognised (CMP-12). Taps are recorded (OMNI-030 tests) ✓. "stop by tomorrow" is correctly not a stop ✓. A recipient whose send was already in flight receives it (one-recipient window) |
| C4 | Meta pauses the template half-way; fix; reschedule | The next send's 132015 writes the registry back → campaign FAILED at the next batch ✓. The rest of the in-flight batch each spends an attempt locally (CMP-19). **If Meta paused it through pacing, held messages fail later by webhook and nothing reacts (CMP-10).** Sync, then reschedule → remaining pending only ✓ |
| C5 | Credential revoked mid-campaign; reconnect; resume | FAILED once with reason ✓. Resume: the person in flight is **abandoned and never receives it** (P10, CMP-11) |
| C6 | Admin cancels mid-batch; another admin tries to schedule it | Stops at the next recipient (P03) ✓. Reschedule refused ✓ |
| C7 | Two admins schedule two campaigns at the period limit at the same moment | **Both accepted (P02, 40/40), allowance exceeded — CMP-03** |
| C8 | Crosses the billing period boundary; a top-up expires during it | Reserved once in N; sends meter in N+1 unchecked; top-up expiry does not stop it (§22, ADR-030 trade) |
| C9 | Workspace suspended for non-payment while scheduled; then pays | Subscription `suspended`: **sends everything** (P12, CMQ-11). Platform suspension of the workspace: **sends everything** (P03, CMP-02). Payment restores the plan, with nothing to resume |
| C10 | A capacity reduction disables the connection mid-run | Next batch → FAILED "number … disabled" ✓ |
| C11 | One person, two contacts in the workspace, in the audience twice | Two recipients, two messages (D8). Identity merge is provider-asserted only (ADR-118) |
| C12 | Follow-up and campaign to the same person in the same minute | Both sent (D7); share the allowance only if L3 is set |
| C13 | 131049 for a third of the audience; a 130429 burst; a 2xx without id for one | 131049: each retried 3× within minutes, then failed (P06, CMP-06). 130429: campaign waits, no attempts spent ✓. 2xx without id: abandoned, never resent ✓ |
| C14 | A member composes, schedules, cancels, clears an opt-out | 403 for all four ✓ (records an opt-out: allowed ✓). **The same member can send marketing templates one by one through the manual route (CMQ-02)** |
| C15 | Opted out on number A, writes on number B, asks for offers | Still opted out on WhatsApp for every number (ENT-19); the agent answers (opt-out does not silence the agent) ✓; campaigns skip them until an admin clears it or Meta's resume (CMQ-01) |

---

## 39. Findings Ledger (CMP-*)

| ID | Title | Classification | Severity | Stage | Intersection | Blocker? | Evidence |
| --- | --- | --- | --- | --- | --- | --- | --- |
| CMP-01 | Two campaign replicas send the same people the same campaign | CONFIRMED DEFECT | **Critical** | sweep / send protocol | Workers (12), Billing (meters), Deployment (replicas) | **yes** | RUNTIME P01, P01b; DATABASE §30 |
| CMP-02 | Suspended or soft-deleted workspace keeps broadcasting | CONFIRMED DEFECT | **High** | lifecycle | Authorization Q54, Tools TOOL-04 | **yes** | RUNTIME P03; CODE |
| CMP-03 | Concurrent scheduling exceeds `period_campaign_messages` (count read before the lock) | CONFIRMED DEFECT | **High** | entitlements | Billing BILL-08, Entitlements | **yes** | RUNTIME P02 (gated + 40/40 natural); CODE `campaign_service.py:376` |
| CMP-04 | Materialisation racing scheduling adds an unreserved audience to a scheduled campaign | CONFIRMED DEFECT | Medium | materialisation | Billing | no (fix with CMP-03) | RUNTIME P11b |
| CMP-05 | Header and button parameters never validated or sent; every recipient burns 3 attempts | CONFIRMED DEFECT | **High** | template parameters | — | **yes** | RUNTIME P06, P07; CODE |
| CMP-06 | 131049 (and 131026) retried within minutes; Meta says wait ≥24 h | CONFIRMED DEFECT | Medium | send protocol | — | no | RUNTIME P06; PROVIDER DOCS |
| CMP-07 | Meta unreachable (connect errors) burns every recipient's attempts | CONFIRMED DEFECT | Medium | send protocol | Workers | no | RUNTIME P06 (27 requests) |
| CMP-08 | Pause does not stop the claimed batch (up to 50 more sends) | CONFIRMED DEFECT | Medium | stop semantics | — | no | RUNTIME P03 (30/30) |
| CMP-09 | Campaign template category not enforced (utility, authentication, unknown accepted) | CONFIRMED MISSING | Medium | composition | Billing (pricing) | no | RUNTIME P07; CODE |
| CMP-10 | Provider `failed` statuses' codes ignored (async 131050 not recorded; pacing 132015 not written back; not reflected in statistics) | CONFIRMED MISSING | Medium | feedback / opt-out / statistics | Messaging Core | no | CODE; PROVIDER DOCS |
| CMP-11 | After a credential or connection refusal, the person in flight is abandoned on resume | CONFIRMED DEFECT | Low | resume | — | no | RUNTIME P10; DATABASE |
| CMP-12 | Unambiguous stop variants missed (`opt-out`, `STOP STOP`, `اوقف`, `أوقف الرسائل`, `ستوب`, `unsub`) | CONFIRMED DEFECT | Low | opt-out | — | no | RUNTIME P08 |
| CMP-13 | Recipients listing capped at 100, no cursor, no identity, prose reasons | CONFIRMED MISSING | Low | statistics / API | API audit (23) | no | CODE |
| CMP-14 | Pause, composition and audience not audited | CONFIRMED MISSING | Low | authorization / audit | — | no | CODE |
| CMP-15 | Naive `scheduled_at` silently taken as UTC; no workspace time zone | CONFIRMED DEFECT | Low | scheduling | — | no | CODE |
| CMP-16 | A bare 403 is per-message; docs say it stops the campaign | DOCUMENTATION DRIFT (code right) | Low | docs | — | no | RUNTIME P06 |
| CMP-17 | No awareness of Meta's portfolio messaging limit (250 for new portfolios) vs a 50,000 audience | DERIVED / NEEDS VERIFICATION | Medium | pacing | Entitlements | no | PROVIDER DOCS; CODE |
| CMP-18 | 131056 (pair limit) stalls the whole campaign on one recipient | CONFIRMED DEFECT | Low | pacing | — | no | RUNTIME P06; PROVIDER DOCS |
| CMP-19 | A template withdrawal mid-batch spends an attempt on each remaining recipient without sending | CONFIRMED DEFECT | Low | template | — | no | RUNTIME P06 |
| CMP-20 | Pacing and sweep: `next_send_at` from sweep start (transient overshoot); a raising batch retried forever at 300 s with no `last_error` | DERIVED / NEEDS VERIFICATION | Low | pacing / sweep | Observability | no | CODE |
| CMP-21 | No test runs two real workers; vacuous reservation race test; stubbed worker and endpoint suites | CONFIRMED (test gap) | Medium | tests | Testing (24) | no | CODE; mutation §32 |
| CMP-22 | No DB CHECK on `messages_per_minute`; campaign → account/template FKs not tenant-agreed | CONFIRMED MISSING | Informational | database | Database (16) | no | DATABASE |
| CMP-23 | CAMPAIGNS.md / RUNBOOK drift (rate per number, recipients, runbook states, double-count claim) | DOCUMENTATION DRIFT | Low | docs | Docs (25) | no | CODE vs docs |
| CMP-24 | `channel_not_in_plan` not checked at scheduling (accepted, then all skipped) | CONFIRMED DEFECT | Low | entitlements | Entitlements | no | CODE |

---

## 40. Release Blockers

1. **CMP-01.** Make a batch's ownership survive its own commits. Options:
   - the claim re-checks lease ownership: `claim_by_id` requires `next_send_at` equal to its own lease value, or a lease token column;
   - the `scheduled` branch honours the lease;
   - the lease is extended per recipient, or per-recipient ownership is taken by a conditional `UPDATE … WHERE status='pending' AND message_id IS NULL RETURNING`;
   - `_deliver` re-reads the recipient row (`populate_existing`) before sending.

   The recipient-level conditional claim is the decisive one: it makes D2 hold across processes, whatever the lease does.
2. **CMP-02.** A served-workspace gate in `dispatch_batch` (re-read per batch, and per recipient like cancel). `_stop_automated_messages` pauses or fails `scheduled`/`running` campaigns with a reason. The same gate goes on the manual template send for consistency.
3. **CMP-03.** Read `pending_in_other_live_campaigns` after `pg_advisory_xact_lock`: move the read into `reserve_period` through a callable. CMP-04 is fixed alongside by re-checking `status = draft` under a row lock in `set_audience` and taking the same advisory lock there.
4. **CMP-05.** Record every component's parameter needs at sync (header format and variables, button URL suffixes). Refuse composition when the campaign cannot supply them (or extend the variables model). Treat 132000, 132012 and 131008 as campaign-level facts: fail the campaign once with the code.

---

## 41. Deferred

| Item | Why it can wait | Trigger to bring it back |
| --- | --- | --- |
| CMP-06 (131049 retry) | Retries cost Meta refusals, not customer messages | before the first marketing broadcast at scale |
| CMP-07 (outage burn) | Needs a Graph outage during a campaign | first incident, or before multi-thousand campaigns |
| CMP-08 (pause) | Bounded at 50 extra sends | an admin-facing UI with a pause button |
| CMP-09 (category) | Operator education possible | Meta recategorisation or pricing disputes |
| CMP-10 (async statuses) | Rare until template pacing or async opt-out appears in real traffic | the real lane observing `held_for_quality_assessment` |
| CMP-11..16, CMP-18..20, CMP-22..24 | Low | the remediation sweep |
| CMP-17 (messaging limit) | needs EXT-CMP-01's error code | the first campaign beyond the portfolio's tier |

---

## 42. Correct by Design

| Item | ADR / doc | Why the audit agrees |
| --- | --- | --- |
| No import, no phone field, no body | ADR-025 | The boundary held across every route (§11); this audit adds the Meta opt-in question as CMQ-07, not as a defect |
| No sleep; pacing on the row | ADR-026 | Survives restarts (K1–K8 never burst after recovery) |
| Uncertain delivery never resent | ADR-093, MSG-07 | Proven for every uncertain class (§17) |
| Rate limits fail open, never on the webhook | ADR-032 | A Redis outage must not stop composing; the webhook is unlimited |
| Registry staleness; no periodic sync | CAMPAIGNS.md | The write-back bounds the cost to one refusal per template (P06 132001) |
| Narrow stop matcher | CAMPAIGNS.md | Precision over recall; additions are CMQ-12 except the unambiguous ones (CMP-12) |
| Opt-out does not silence the agent | CAMPAIGNS.md | Refusing marketing ≠ refusing answers |
| Opt-out per channel, covering all numbers of that channel | ENT-19 | Documented, migration-preserved (§29); expectation is CMQ-01 |
| Reservation checked once, at scheduling | ADR-030 | Refusing halfway is worse; the period crossing is an accepted trade |
| One-recipient crash window, resolved by abandoning | ADR-093 | Proven in-process (§18) |
| Replies never refused by the connection allowance | ADR-123 | Proven (M19 detector) |

---

## 43. Product Decisions (CMQ-*)

| ID | Question | Options | Recommended default | Today | What changes |
| --- | --- | --- | --- | --- | --- |
| CMQ-01 | Does a STOP on one WhatsApp number cover the workspace's other WhatsApp numbers? | (a) yes, per channel (ENT-19); (b) per connection | (a): a customer says stop to the business, not to a number | (a) | (b) needs a connection key on consent rows |
| CMQ-02 | Should manual template sends be opt-out-checked, admin-only, paced or reserved when they are marketing? | (a) leave as human replies; (b) refuse MARKETING templates to opted-out contacts; (c) also count against `period_campaign_messages` | (b), plus making MARKETING manual sends admin-only | (a): any member, no checks | (b) adds a consent read in `send_template` for `origin=HUMAN` + marketing category |
| CMQ-03 | Frequency cap per person across campaigns? | none / N per day / N per week | 1 marketing campaign per person per 24 h per workspace | none | a recipient check at send against recent campaign messages |
| CMQ-04 | Quiet hours in the workspace's time zone? | none / workspace window | add a workspace time zone first (fixes CMP-15), then optional windows | none | scheduling and sweep respect a window |
| CMQ-05 | Subscribe to `message_template_status_update` and/or periodic sync? | none / webhook / sweep | webhook (Meta's channel, no call budget) | none | registry updated without a send |
| CMQ-06 | Refuse or split a campaign larger than the portfolio's messaging limit? | ignore / warn / refuse / pace per 24 h | read the limit (`whatsapp_business_manager_messaging_limit`) and warn at scheduling; pace across days later | ignore | needs EXT-CMP-01 |
| CMQ-07 | Is "wrote first" consent enough for marketing, given Meta's explicit opt-in policy? Import with consent evidence? | (a) keep ADR-025; (b) require recorded opt-in evidence per contact for MARKETING; (c) allow import with evidence | (b), after legal review (Egypt PDPL 151/2020 and destination countries) | (a) | consent gains an opt-in event; the audience requires it for marketing |
| CMQ-08 | Retention of recipients and opt-out evidence after contact erasure | keep / anonymise / erase | keep opt-out evidence keyed by a hash of the address; anonymise recipient rows after N months | erase both by cascade | a tombstone table |
| CMQ-09 | Does a demoted or removed admin's running campaign continue? | continue / pause | continue (the act was authorised when made; audit names them) | continue | — |
| CMQ-10 | Second-admin approval above an audience size? | none / above N | none until a frontend exists | none | a `pending_approval` state |
| CMQ-11 | Does a scheduled campaign keep sending when the paid plan stops resolving (dunning `suspended`)? | (a) continue (reservation honoured); (b) pause at the next batch | (b): service is withheld pending payment (BILLING.md) | (a) | a plan check per batch, like `channel_not_in_plan` |
| CMQ-12 | Extend the stop vocabulary with polite and colloquial forms? | keep / extend | extend with `stop please`, `please stop`, `لا ترسل`, `remove me`; keep `كفاية`/`وقف` out | narrow | `STOP_WORDS` additions |

---

## 44. Later Product (LP-CMP-*)

| ID | What | Why later | Trigger |
| --- | --- | --- | --- |
| LP-CMP-01 | Per-recipient personalisation | needs a per-recipient fact source | first customer template with `{{name}}` |
| LP-CMP-02 | Saved segments | filters are cheap to re-enter | a frontend |
| LP-CMP-03 | A/B templates | needs analytics events first | marketing teams asking |
| LP-CMP-04 | Cost estimate before scheduling | needs the rate card (EXT-CMP-10) | billing pass-through of Meta costs |
| LP-CMP-05 | Test send to the workspace's own number | convenience | a frontend |
| LP-CMP-06 | Campaign analytics events | counts already derivable | dashboards |
| LP-CMP-07 | Per-category consent | needs a customer-explainable vocabulary | utility campaigns |
| LP-CMP-08 | Instagram and Email campaigns | channels not implemented | IG-01..52 / Email §39 |
| LP-CMP-09 | Campaign duplication | convenience | a frontend |
| LP-CMP-10 | Marketing Messages API routing | optional (EXT-CMP-12) | Meta pricing or optimisation advantage |

---

## 45. External Verification (EXT-CMP-*)

Facts are listed in §36. Evidence still needed:

| Item | Evidence required |
| --- | --- |
| Real template sync | registry rows equal WhatsApp Manager for one account |
| Real campaign + statuses | wamids, `sent`/`delivered`/`read` webhooks, rows reconciled |
| Real STOP, button, `user_preferences` | consent rows with `via` and the channel; next campaign skips |
| EXT-CMP-01 | the error (code, sync or async) when a send exceeds the portfolio limit |
| EXT-CMP-04 | whether 131050 arrives as an asynchronous `failed` status |
| EXT-CMP-08 | `held_for_quality_assessment` on a new marketing template; the later 132015 status |
| EXT-CMP-11 | an explicit statement or test that Cloud API `/messages` delivers marketing templates to a BSUID |
| Egypt pricing | Meta's current rate card for +20 marketing and utility |
| Legal | marketing consent under Egypt PDPL (Law 151 of 2020) and destination-country law — EXTERNAL / LEGAL |

---

## 46. Intersections for the Final Audit

1. **CMP-01 × Workers / Deployment.**
   - DEPLOYMENT.md encourages `WORKER_KINDS=campaign` replicas.
   - Any deployment with two campaign workers duplicates sends.
   - Re-check the follow-up worker for the same lock-at-first-commit pattern. Its claim is per follow-up, so the window is smaller, but `DueFollowUpClaim` deserves the same two-process probe.
2. **CMP-01 × Billing.** Duplicates are metered and paid to Meta.
3. **CMP-02 × Authorization (Q54) × Tools (TOOL-04).** The lifecycle gate exists for follow-ups and agent turns, not campaigns.
4. **CMP-03/04 × Billing (BILL-08) × Entitlements.** The reservation's lock is correct; its input is read outside it.
5. **CMP-10 × Messaging Core (MSG-14).** The status projection keeps the text and drops the code that carries consent and template facts.
6. **CMQ-11 × Billing dunning.** A withheld service still broadcasts.
7. **CMQ-02 × Security.** The manual template route as a broadcast surface.
8. **§11 × any future outbound-first feature.** The audience assumes a conversation implies an inbound.

---

## 47. Infrastructure Created by This Audit

| Kind | Name | Notes |
| --- | --- | --- |
| Worktree + branch | `E:\wasla-campaigns-audit`, branch `campaigns-outbound-audit-20261013` | holds the report and `audit/` probes; the report commit lives here |
| Worktree (detached) | `E:\wasla-cmp-mut` at `989584b` | mutation runs; every mutant reverted with `git checkout`; tree clean |
| Worktree (detached) | `E:\wasla-cmp-surv` at `989584b` | runtime survivor checks (M04, M05); reverted; holds an untracked copy of `audit/` |
| Container | `wasla-cmp-pg` (pgvector/pgvector:pg16, 127.0.0.1:55911) | databases `wasla_cmp` (test lane, dropped by the suite), `wasla_probe`, `wasla_probe2`, `wasla_mut`, `wasla_mig` |
| Container | `wasla-cmp-redis` (redis:7-alpine, 127.0.0.1:56911) | |
| Container | `wasla-cmp-minio` (ghcr.io/mohamedshhahat1/minio:RELEASE.2025-04-22T22-12-26Z, 127.0.0.1:59911) | started, unused |
| Volumes | the anonymous volumes of the three containers above | no named volumes; `wasla_postgres-data` untouched |
| Images | none built (all three pulled earlier) | |
| Docker Desktop | started by the audit (was stopped) | |
| Scratch | session scratchpad `…/scratchpad/cmp/` (baseline, probe and mutation logs) | outside the repository |

No container of the developer stack (`wasla-api-1`, `wasla-postgres-1`, `wasla-redis-1`) was touched.

---

## 48. Score

| Area | Weight | Score | Deductions |
| --- | --- | --- | --- |
| Audience boundary (wrote first, consent, tenancy) | 8 | 7 | −1 async 131050 not recorded (CMP-10) |
| Identity and connection binding | 5 | 5 | |
| Template registry and parameters | 7 | 3 | −3 CMP-05, −1 CMP-09 |
| Materialisation and scheduling | 5 | 3 | −1 CMP-04, −1 CMP-15 |
| Sweep, lease and concurrency | 8 | 1 | −7 CMP-01 |
| Pacing and rate limits | 5 | 3 | −1 CMP-06, −1 CMP-17/18 |
| Duplicate prevention and crash safety | 10 | 5 | −5 CMP-01 (in-process crash safety fully proven) |
| Stop / cancel / resume semantics | 5 | 3 | −1 CMP-08, −1 CMP-11 |
| Opt-out completeness | 7 | 6 | −1 CMP-12 |
| Entitlements and billing intersection | 6 | 2 | −3 CMP-03, −1 CMP-04 |
| Lifecycle intersection | 5 | 1 | −4 CMP-02 |
| Statistics truth | 4 | 2 | −1 CMP-10, −1 CMP-13 |
| Authorization and audit | 4 | 3 | −1 CMP-14 |
| Security and abuse resistance | 4 | 3 | −1 allowance bypass (CMP-03/04 seen as abuse) |
| Operations and observability | 3 | 1 | −1 no stuck/failed alert, −1 runbook drift |
| Migrations and invariants | 4 | 4 | |
| Testing | 5 | 1 | −4 CMP-21 + survivors (§32) |
| Documentation truth | 3 | 2 | −1 CMP-16/23 |
| Real-provider evidence | 2 | 0 | not authorised |
| **Total** | **100** | **52** | |

No deduction was taken for CMQ, LP or CORRECT BY DESIGN items.

---

## 49. Final Verdict

**CAMPAIGNS NOT READY.**

**What already holds:**

- The single-process engine is careful and largely right.
- The compliance boundary is structural.
- Opt-out is honoured per channel everywhere it is promised.
- Crash safety within one process is proven at every boundary.

**Why not ready:**

- Wasla cannot yet be trusted to write to thousands of customers **once each**. Two campaign workers send duplicates, invisibly, with the shipped constants (CMP-01).
- It cannot be trusted to stop when the platform withholds service (CMP-02).
- It cannot be trusted to stay inside what the workspace paid for under concurrency (CMP-03).
- A template with a header or button turns every broadcast into a run of refusals that harms the number's standing (CMP-05).

One CONFIRMED Critical and three CONFIRMED High defects are open. **CMQ-07**, the product decision the audience boundary rests on against Meta's opt-in policy, is also open.

---

## 50. Required Final Questions

1. **Can a campaign reach anyone who never wrote on that connection?** No. Conversations are opened only by inbound projection, and the audience requires a conversation on the connection (§11). Meta's explicit opt-in standard is a separate question (CMQ-07).
2. **Can any request, tool or field add a recipient, phone number or body?** No (§11). No AI tool reaches campaigns.
3. **Can one tenant's campaign touch another's connection, template, contacts or statistics?** No: tenant-scoped repositories everywhere, the worker scoped by `campaign.tenant_id`, mutant M24 (§25).
4. **Is the recipient always addressed through its recorded identity and the campaign's own connection?** Yes (§12). A changed identity or another connection is skipped.
5. **Duplicates within one materialisation, or two audiences from concurrent ones?** No. The unique key holds; 4 concurrent materialisations give 200 rows, 0 duplicates.
6. **Can a campaign be scheduled with a partial audience?** Not partial. But a **wider** audience can land after scheduling, unreserved (CMP-04).
7. **Time zones, the past, the far future and DST?** UTC instants. A naive value is taken as UTC (CMP-15). Past means now. More than 90 days is refused. No workspace time zone, so DST matters only through naive input.
8. **Which clock decides?** Each worker's Python UTC clock, not the database's (§14).
9. **Can two workers send to one recipient?** **Yes (CMP-01)**: 3/40, 26/40, 23/250.
10. **Is the lease longer than the worst batch?** **No.** 300 s against up to 5 sequential batches of 50 × 10 s, and the `scheduled` branch ignores it altogether.
11. **What pace does a campaign really achieve; can it exceed its rate?** About ≤200/min per replica at the 600 setting (poll 15 s × batch 50). Transient overshoot is possible after a long sweep (CMP-20). It never bursts after a restart.
12. **Can two campaigns on one connection exceed the allowance?** With L3 unset (the default), yes: twice the rate, by design. With L3 set, no (tests, M19).
13. **Is a reply ever refused by the connection allowance?** No.
14. **Are 429-class answers retried with `Retry-After` honoured, without spending attempts?** Yes. In-client up to 3 tries with `Retry-After` ≤30 s, then the campaign waits; attempts stay at 0 (P06).
15. **131049, 131050, 130429, 131056 — handled as Meta says?** 131050 ✓. 130429 ✓. **131049 ✗** (retried within minutes, CMP-06). **131056 ✗** in scope (whole campaign stalls, CMP-18).
16. **Beyond the number's messaging limit?** Unknown error; no awareness or refusal (CMP-17, EXT-CMP-01).
17. **Real duplicate window after a crash?** One recipient, abandoned, never resent (§18). Across two live workers, the overlap of their batches (CMP-01).
18. **Can any Meta answer class cause a second copy to one person?** No, within one process (§17).
19. **Can a campaign-wide fact burn every recipient one by one?** Yes: missing header or button parameters (CMP-05) and Meta unreachable (CMP-07). The credential is stopped once ✓.
20. **Does cancel stop at the next recipient? Pause? Does resume send nobody twice?** Cancel yes. **Pause no** (CMP-08). Resume sends nobody twice in one process, but loses the credential-case person (CMP-11).
21. **Can a FAILED campaign be resumed safely, and closed for good?** Resumed: yes, with CMP-11. Closed for good: yes, by cancel.
22. **Which writers record opt-out, each on the right channel?** W1–W7 all through `record_opt_out` with the channel (§20). An asynchronous 131050 status is not recorded (CMP-10).
23. **Which readers check opt-out on the recipient identity's channel?** R1–R4 yes. R5 (manual template) no (CMQ-02).
24. **Real window between an inbound STOP and a send that ignores it?** One recipient's read-to-socket interval (§20).
25. **Does newer resume evidence always win over an older or replayed STOP?** Over a replay, yes (idempotency, OMNI-030). A never-before-delivered STOP older than a resume would be recorded (DERIVED).
26. **Does any reader still use the legacy person-level column?** No. The column was dropped in 0097.
27. **Do follow-ups and manual template sends share the campaign's outbound guarantees?** Follow-ups: yes, and they also have the lifecycle gate campaigns lack. Manual template: almost none (§6).
28. **Is the reservation taken for the whole audience under the lock; can concurrent scheduling exceed it?** The whole audience, yes, but the "reserved by others" input is read **before** the lock. 40/40 exceeded (CMP-03).
29. **Released on cancel, skip, failure, abandon?** Yes. It is recomputed from pending recipients, so nothing leaks.
30. **Which period pays for a campaign crossing the boundary?** The period each message is sent in; checked only in the scheduling period.
31. **Suspension, soft deletion, purge?** Suspension and soft deletion: **keeps sending** (CMP-02). Purge: rows erased by cascade.
32. **Disabling, releasing or reclaiming the connection?** FAILED at the next batch.
33. **Do statistics equal the rows; do late webhooks keep them right?** Single-process: yes; webhooks climb correctly. Duplicates are invisible. Provider `failed` after acceptance is not counted (CMP-10).
34. **Is every lifecycle change audited with its actor?** Schedule and cancel only. Not pause, compose or audience (CMP-14).
35. **Is the registry correct under concurrent sync and write-back?** Atomic per sync. Last writer wins between sync and write-back; a later sync heals it (§9).
36. **Are template parameters validated before the first send?** Body count only (CMP-05).
37. **Which campaign metrics and alerts exist, are emitted, scraped, reach a receiver?** Logs and provider-error and opt-out counters only. No campaign alert. No receiver verified (§28).
38. **Does every RUNBOOK campaign step work as written?** The query works. The decision list is incomplete (CMP-23).
39. **Do migrations preserve every recipient and opt-out, up and down?** Yes: 100/100 opt-outs up, down and up. The downgrade refuses lossy cases (§29).
40. **Which DB invariants are non-zero, and why?** 52 duplicate campaign messages and 392 vs 340 meters (CMP-01). 3 pending + linked (CMP-11). 22 unresolved requested (the uncertain and crash probes) (§30).
41. **Which campaign guarantees have real concurrency and crash tests?** Crash: the delivery protocol tests (single send). Concurrency: none with two real workers. The reservation race test is vacuous (CMP-21).
42. **Which tests are false green?** `test_topup_concurrency.py:533` for the campaign race. The worker suite's stub and shared session. The endpoint suite's stub service (§31).
43. **Which mutations survive, and are survivors exploitable?** 10 of 26 (M02, M04, M05, M08, M13, M18, M20, M21, M24, M25). None is exploitable in the shipped code: M04, M05 and M08 were re-proved correct at runtime, and M18 and M24 have independent guards. All are test gaps (§32).
44. **Which documentation claims are false?** §33: the double-count claim, the bare-403 stop, the MARKETING-only docstring, the recipients listing, the runbook states, and "never above it".
45. **Which items are intersections the Final audit must recheck?** §46.
46. **Would you let a real workspace broadcast to its customers through Wasla today?** **No.** Not with more than one campaign worker, not without the lifecycle gate, and not with any template carrying header or button parameters. A single-replica deployment with body-only marketing templates is safe from duplicates and crash loss, but still exceeds paid allowances under concurrent scheduling.
