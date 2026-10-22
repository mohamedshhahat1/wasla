# CAMPAIGNS PAGE FEATURES — MERGE VERIFICATION (RE-RUN)

Stage: campaigns page features and Meta sending limits (`campaigns-page-features-20261016`, ADR-145..150),
merged into the canonical branch `worktree-billing-google-auth` after making every calendar-bound test
independent of the date. Verification of 2026-10-22 (two sessions: 08:09–09:20 UTC and from 11:53 UTC).

Every lane is reported with its schema path and its clock beside it. "Real clock" means the machine's clock,
not shifted; "shifted" means the Python process clock moved by `time-machine` while PostgreSQL, Redis and MinIO
keep the real one.

## 1. Executive Summary

| Question | Answer |
| --- | --- |
| Part A verdict | **DATE-BOUND TESTS FIXED ON CANONICAL, PUSHED, CI GREEN** |
| Final verdict | see section 27 (the push of this report and its GitHub Actions run are recorded in the addendum, as `752a9ba` did for the campaigns merge) |
| Calendar bombs found | **5**: CPM-01 (stage, detonated 2026-10-20/21), CPM-02 (canonical, detonated 2026-10-20), CPM-03 (canonical, would detonate 2027-08-02), CPM-04 (stage, would detonate **2026-10-22 20:00 UTC**, the evening of this verification) — each fixed test-only, each still killing the mutants of the behaviour it guards |
| Other findings | **CPM-05** — GitHub-only dependency drift (CI resolves pypdf 6.21.2; the lock pins 6.19.0) that turned run 39785483142 red in two jobs; fixed test-only under section 30, run 39801370475 green. **CPM-06** — a quiet-hours test with a 1.0 s real-time margin that failed once under extra load I added myself; passed in every correct run since (section 24) |
| Application code, migrations, scripts, docs changed by this stage | **none** (`git diff --name-only` of every branch: `tests/` only) |
| Pushes | 2 before the merge (`9d51152`, `46cf10f`), the third with this report (section 22) |
| GitHub Actions | run 39785483142 (`9d51152`) red (CPM-05); run 39801370475 (`46cf10f`) **green in every job**; the run of this report's push: section 23 / addendum |

The answer to the final question is in section 27.

## 2. Source Branches and Exact Heads

This report **supersedes** the first merge verification of the stage, `CAMPAIGNS_PAGE_FEATURES_MERGE_VERIFICATION.md`
of 2026-10-22 on the local branch `campaigns-page-merge-verification-20261022` (`5d35dc4`, from `752a9ba`, never
pushed, left where it is). That attempt ended **NOT MERGED** on three date-bound tests — CPM-01 ×2 (the stage's
quiet-hours change tests) and CPM-02 (a campaigns-remediation test already on origin, which made GitHub CI red
for any run of the canonical branch) — and did not reach the kept-data sweep, the races, the mutation spot-check, a
clock run inside quiet hours, the merge, the push or a GitHub Actions run.

Recorded 2026-10-22 08:09 UTC (11:09 Cairo), machine uptime 1.58 h, after `git fetch origin --prune`:

| Name | Ref | Head |
| --- | --- | --- |
| ORIGIN_CANONICAL_START_HEAD | `origin/worktree-billing-google-auth` | `752a9ba56d5bb07e36c0a051a830d6c579b2db5d` (= local, 0 ahead / 0 behind) |
| CPF_HEAD | `campaigns-page-features-20261016` | `3e1769f645d6eccb91129d1f03e6318ec963253c` (not moved) |
| CPF_CODE_HEAD | — | `b385527` |
| ORIGIN_MIGRATION_HEAD | — | `0107` |
| MAIN_HEAD | `main` | `8e3a1ba5a63651b7b7da3bdf14845f40a9d93667` (untouched) |
| first attempt's report | `campaigns-page-merge-verification-20261022` | `5d35dc4` (not moved, not merged, not pushed) |

`git branch -a --contains b385527`: only `campaigns-page-features-20261016`. No branch of this stage existed
(no earlier session to resume). `E:\wasla`: `worktree-billing-google-auth` at `752a9ba`, 14 untracked paths only,
no stash.

| Tool | Version |
| --- | --- |
| Python | 3.12.7 |
| Docker / Compose | 29.8.0 / v5.5.1 |
| PostgreSQL | 16.15 (`pgvector/pgvector:pg16`) |
| Redis | 7.4.11 (`redis:7-alpine`) |
| `app.__file__` in `E:\wasla` | `E:\wasla\app\__init__.py` (editable install); every run of this stage asserted its own worktree's |
| venv | `E:\wasla-cpr-notes\venv`, a copy of the first attempt's lock-pinned venv: `pip freeze` = `requirements.lock` at `3e1769f` (54 pins) **except `uvloop`** (Linux-only), plus the `dev` extra's exact pins; **this stage added `time-machine==3.5.1`** (section 12's shifted lanes), never to `pyproject.toml` or the lock. At `752a9ba` the lock has 53 pins (no `tzdata`): the venv has `tzdata 2026.5` beyond it. |

Heads made by this stage:

| Name | Ref | Head |
| --- | --- | --- |
| CANON_FIX_HEAD | `canonical-date-bound-tests-20261022` | `46cf10fc5221c1d3c00bef28d179c963c0fb76cd` (`744f77f` CPM-02, `9d51152` CPM-03, `46cf10f` CPM-05) — pushed as the canonical branch |
| STAGE_FIX_HEAD | `campaigns-page-features-dates-20261022` | `debde5f3ecc98c9562db3d7222521f31ace7ded3` (`f52dd56` CPM-01, `34510a0` CPM-04, merges `b236fed` and `debde5f`) |
| CANONICAL_MERGED_HEAD | `worktree-billing-google-auth` | `0f767ec8076a4c36d508f598e60640bb6cc742ae` (merge of `46cf10f` and `debde5f`) + the report commit |

The session stopped once (usage limit, 09:20 UTC) and resumed at 11:53 UTC from the draft's phase table; before
every fast-forward, merge and push `git log` and `origin` were checked, and no step was made twice.

## 3. Stage Summary

`CAMPAIGNS_PAGE_FEATURES_IMPLEMENTATION.md` (at `3e1769f`): **CAMPAIGNS PAGE FEATURES IMPLEMENTED WITH EXTERNAL
VERIFICATION REMAINING**, self-score 97/100. Quiet hours (22:00–09:00 in the workspace's IANA zone, a minute's
send lead), Meta's portfolio messaging limit read on v24.0 and enforced at 80 % through a ledger under the
portfolio's advisory lock, one marketing message per customer per 24 h, frozen per-recipient personalisation, the
21 page elements and `docs/CAMPAIGNS_PAGE_CONTRACT.md`; CPF-01..CPF-08 fixed in the branch. Its numbers were
re-measured below, not copied. The first merge verification (`5d35dc4`) measured the same and stopped on CPM-01 and
CPM-02.

## 4. Part A — Calendar Sweep of `tests/` at the Canonical Head

Static sweep of every literal calendar moment (`datetime(20…`, `date(20…`, ISO strings, `fromisoformat`,
`strptime`, epoch literals > 1.7e9, frozen or monkeypatched clocks) under `tests/` at `752a9ba`, each traced to the
clock that decides on it. **252 hits in 112 files.**

| Class | Hits |
| --- | --- |
| SAFE-PINNED | 228 |
| SAFE-PAST | 19 |
| BOMB-DETONATED | **1** — CPM-02 (`test_campaign_api_surface.py:163`, detonated 2026-10-20 07:00 UTC) |
| BOMB-FUTURE | **1** — CPM-03 (`test_platform_access_audit.py`, `since=2026-08-01` with `until` = now: a window over 366 days is refused from **2027-08-02**) |
| DST-FIXTURE | 0 |
| N/A (regex false positive: a value parsed at run time) | 3 |

The dynamic proof (DT-06): the full model-built lane at `752a9ba` with the Python clock shifted **+100 days** and
to **2029-06-15** (discovery runs) — every failure classified individually (section 5's shift table): CPM-02, CPM-03
(2029 only), and artefacts of the shift itself. No other bomb.

<details><summary>Every hit (252), by file</summary>

| File | Lines | Class | Reason |
| --- | --- | --- | --- |
| `channel_fakes.py` | 18 | SAFE-PAST | docstring of the synthetic payload shape; Meta/synthetic webhook timestamp in the past; stored as the event's occurred/sent time; the only real-clock use (the 24 h service window) can only move further into the past, so the outcome cannot flip |
| `e2e/test_billing_lifecycle.py` | 275 | SAFE-PINNED | Paymob callback field carried as signed data (HMAC input / stored), compared only with other literals; no code compares it with any clock |
| `integration/test_annual_billing.py` | 117, 903, 907, 908, 1199, 1203 | SAFE-PINNED | T0 / april / february passed as now= to BillingWorker.run_once and the services; asserted periods are calendar arithmetic of T0 (billing_calendar) |
| `integration/test_annual_billing_concurrency.py` | 59, 115, 202, 248, 254, 255, 277, 278, 306 | SAFE-PINNED | START 2025-03-31 and moments passed as now= to run_once ('in the past so every clock in the run is after it'); asserted periods are arithmetic of START |
| `integration/test_annual_billing_invariants.py` | 96, 109 | SAFE-PAST | created_at 2020 plan-version seed (older than now) / random 2026 moments given to the oracle as now= |
| `integration/test_audit_trail.py` | 39 | SAFE-PINNED | fixed moment passed explicitly (now=/at=/clock=/today=) to the code under test, which takes the moment from its argument; no real-clock read decides the asserted value |
| `integration/test_backup_upload.py` | 389, 390, 411, 459, 460, 489 | SAFE-PINNED | backup status file content written and read back unchanged (a failed run must not advance it); no clock |
| `integration/test_billing_concurrency.py` | 152, 199 | SAFE-PINNED | invoice period moment / Paymob callback field carried as signed data (HMAC input / stored), compared only with other literals; no code compares it with any clock |
| `integration/test_billing_constraints.py` | 28 | SAFE-PINNED | returned by a stubbed service or written into a row whose value is only read back and compared with other literals; no clock decides on it |
| `integration/test_billing_crash_recovery.py` | 107, 108 | SAFE-PINNED | fixed moment passed explicitly (now=/at=/clock=/today=) to the code under test, which takes the moment from its argument; no real-clock read decides the asserted value |
| `integration/test_billing_endpoints.py` | 49, 110, 217 | SAFE-PINNED | fixed moment passed explicitly (now=/at=/clock=/today=) to the code under test, which takes the moment from its argument; no real-clock read decides the asserted value; asserted ends are add_interval(NOW) |
| `integration/test_billing_migrations.py` | 32, 33, 34, 35, 36, 37 | SAFE-PINNED | rows seeded at 0069; 0071's anchors computed from the stored periods (no now()); the one now() of 0070 revives the expired free trial and is asserted only '> FEB_28' (a direction that never flips) |
| `integration/test_billing_remediation_journeys.py` | 77, 226 | SAFE-PINNED | fixed moment passed explicitly (now=/at=/clock=/today=) to the code under test, which takes the moment from its argument; no real-clock read decides the asserted value; created_at: Paymob callback field carried as signed data (HMAC input / stored), compared only with other literals; no code compares it with any clock |
| `integration/test_billing_sweep_concurrency.py` | 67, 68, 69 | SAFE-PINNED | fixed moment passed explicitly (now=/at=/clock=/today=) to the code under test, which takes the moment from its argument; no real-clock read decides the asserted value |
| `integration/test_billing_worker.py` | 54 | SAFE-PINNED | fixed moment passed explicitly (now=/at=/clock=/today=) to the code under test, which takes the moment from its argument; no real-clock read decides the asserted value |
| `integration/test_campaign_api_surface.py` | 159 | SAFE-PINNED | naive datetime: refused for its missing offset before CampaignService.schedule reads the clock (campaign_service.py:620 precedes :627) |
| `integration/test_campaign_api_surface.py` | 163 | **BOMB-DETONATED** | CPM-02: aware 2026-10-20 10:00 +03:00 scheduled and asserted stored unchanged; schedule() stores max(when, real now); detonated 2026-10-20 07:00 UTC |
| `integration/test_campaign_api_surface.py` | 245 | SAFE-PINNED | two resume moments ordered among themselves at the writer (newer wins); no clock |
| `integration/test_campaign_endpoints.py` | 54 | SAFE-PINNED | returned by a stubbed service or written into a row whose value is only read back and compared with other literals; no clock decides on it (stubbed service; cursor encoding) |
| `integration/test_collection_abandonment.py` | 64 | SAFE-PINNED | fixed moment passed explicitly (now=/at=/clock=/today=) to the code under test, which takes the moment from its argument; no real-clock read decides the asserted value |
| `integration/test_commercial_api.py` | 379 | N/A | regex false positive: not a calendar moment (parses a value returned at run time) |
| `integration/test_commercial_api.py` | 1215 | SAFE-PAST | expires_at 2020: an offer that already expired is refused 422; it can only stay in the past |
| `integration/test_connection_throughput.py` | 46 | SAFE-PINNED | fixed moment passed explicitly (now=/at=/clock=/today=) to the code under test, which takes the moment from its argument; no real-clock read decides the asserted value (throughput window from now=) |
| `integration/test_contact_endpoints.py` | 51 | SAFE-PINNED | returned by a stubbed service or written into a row whose value is only read back and compared with other literals; no clock decides on it |
| `integration/test_conversation_endpoints.py` | 67, 421 | SAFE-PINNED | returned by a stubbed service or written into a row whose value is only read back and compared with other literals; no clock decides on it; reply_policy comes from the stub (free_text_allowed True with a window_expires_at already past proves no real clock decides it) |
| `integration/test_conversation_projection.py` | 41, 42 | SAFE-PAST | Meta/synthetic webhook timestamp in the past; stored as the event's occurred/sent time; the only real-clock use (the 24 h service window) can only move further into the past, so the outcome cannot flip |
| `integration/test_crm_integrity.py` | 429 | N/A | regex false positive: not a calendar moment (parses a value returned at run time) |
| `integration/test_dead_letter_atomicity.py` | 57 | SAFE-PINNED | fixed moment passed explicitly (now=/at=/clock=/today=) to the code under test, which takes the moment from its argument; no real-clock read decides the asserted value |
| `integration/test_declined_and_saved_cards.py` | 55, 86, 431 | SAFE-PINNED | fixed moment passed explicitly (now=/at=/clock=/today=) to the code under test, which takes the moment from its argument; no real-clock read decides the asserted value; created_at: Paymob callback field carried as signed data (HMAC input / stored), compared only with other literals; no code compares it with any clock |
| `integration/test_dunning_lifecycle.py` | 90, 376 | SAFE-PINNED | 'Every moment in this file is derived from this one. Nothing calls now()'; fixed moment passed explicitly (now=/at=/clock=/today=) to the code under test, which takes the moment from its argument; no real-clock read decides the asserted value |
| `integration/test_email_outbox.py` | 38 | SAFE-PINNED | fixed moment passed explicitly (now=/at=/clock=/today=) to the code under test, which takes the moment from its argument; no real-clock read decides the asserted value |
| `integration/test_entitlements.py` | 423, 458 | SAFE-PINNED | released_at / revoked_at stored as evidence of a status; the application reads only the status |
| `integration/test_entitlements_oracle.py` | 140 | SAFE-PAST | plan-version created_at 2020: seed older than now |
| `integration/test_entitlement_invariants.py` | 152 | SAFE-PAST | Meta/synthetic webhook timestamp in the past; stored as the event's occurred/sent time; the only real-clock use (the 24 h service window) can only move further into the past, so the outcome cannot flip |
| `integration/test_financial_integrity.py` | 56 | SAFE-PINNED | fixed moment passed explicitly (now=/at=/clock=/today=) to the code under test, which takes the moment from its argument; no real-clock read decides the asserted value |
| `integration/test_follow_up_endpoints.py` | 41 | SAFE-PINNED | returned by a stubbed service or written into a row whose value is only read back and compared with other literals; no clock decides on it |
| `integration/test_invoice_endpoints.py` | 42, 43 | SAFE-PINNED | returned by a stubbed service or written into a row whose value is only read back and compared with other literals; no clock decides on it |
| `integration/test_invoicing.py` | 36, 37, 38 | SAFE-PINNED | fixed moment passed explicitly (now=/at=/clock=/today=) to the code under test, which takes the moment from its argument; no real-clock read decides the asserted value |
| `integration/test_knowledge_endpoints.py` | 39 | SAFE-PINNED | returned by a stubbed service or written into a row whose value is only read back and compared with other literals; no clock decides on it |
| `integration/test_lead_endpoints.py` | 48 | SAFE-PINNED | returned by a stubbed service or written into a row whose value is only read back and compared with other literals; no clock decides on it |
| `integration/test_media_crash_recovery.py` | 59 | SAFE-PINNED | fixed moment passed explicitly (now=/at=/clock=/today=) to the code under test, which takes the moment from its argument; no real-clock read decides the asserted value (grace compared with now=) |
| `integration/test_media_endpoints.py` | 53 | SAFE-PINNED | returned by a stubbed service or written into a row whose value is only read back and compared with other literals; no clock decides on it |
| `integration/test_media_lifecycle.py` | 212 | SAFE-PAST | a message sent 2026-07-01 to a former workspace: older than now, recorded terminal |
| `integration/test_media_projection.py` | 30 | SAFE-PAST | Meta/synthetic webhook timestamp in the past; stored as the event's occurred/sent time; the only real-clock use (the 24 h service window) can only move further into the past, so the outcome cannot flip |
| `integration/test_media_retention.py` | 49 | SAFE-PINNED | 'A fixed instant, and every age below is expressed relative to it'; fixed moment passed explicitly (now=/at=/clock=/today=) to the code under test, which takes the moment from its argument; no real-clock read decides the asserted value |
| `integration/test_media_url_and_limits.py` | 353 | SAFE-PAST | Meta/synthetic webhook timestamp in the past; stored as the event's occurred/sent time; the only real-clock use (the 24 h service window) can only move further into the past, so the outcome cannot flip |
| `integration/test_media_write_atomicity.py` | 77 | SAFE-PINNED | 'a test that read the real clock would be asserting a boundary that moves'; fixed moment passed explicitly (now=/at=/clock=/today=) to the code under test, which takes the moment from its argument; no real-clock read decides the asserted value |
| `integration/test_omnichannel_final_invariants.py` | 239 | SAFE-PAST | Meta/synthetic webhook timestamp in the past; stored as the event's occurred/sent time; the only real-clock use (the 24 h service window) can only move further into the past, so the outcome cannot flip |
| `integration/test_omnichannel_operations.py` | 515 | SAFE-PAST | long_ago 2019 channel event: older than any retention boundary, only ever 'older than now' |
| `integration/test_opt_out_ingestion.py` | 70 | SAFE-PAST | Meta/synthetic webhook timestamp in the past; stored as the event's occurred/sent time; the only real-clock use (the 24 h service window) can only move further into the past, so the outcome cannot flip |
| `integration/test_pagination.py` | 35 | SAFE-PINNED | rows at START + i ordered among themselves; no clock |
| `integration/test_paid_plan_settlement.py` | 68, 260 | SAFE-PINNED | fixed moment passed explicitly (now=/at=/clock=/today=) to the code under test, which takes the moment from its argument; no real-clock read decides the asserted value; created_at: Paymob callback field carried as signed data (HMAC input / stored), compared only with other literals; no code compares it with any clock |
| `integration/test_payment_reconciliation.py` | 65 | SAFE-PINNED | fixed moment passed explicitly (now=/at=/clock=/today=) to the code under test, which takes the moment from its argument; no real-clock read decides the asserted value |
| `integration/test_paymob_checkout.py` | 167, 878, 879, 904, 905, 926, 927, 930, 947, 948, 971, 972 | SAFE-PINNED | invoice periods / paid_at stored and read back; Paymob callback field carried as signed data (HMAC input / stored), compared only with other literals; no code compares it with any clock |
| `integration/test_paymob_refunds.py` | 127, 200 | SAFE-PINNED | invoice period stored and read back; Paymob callback field carried as signed data (HMAC input / stored), compared only with other literals; no code compares it with any clock |
| `integration/test_paymob_webhook_endpoint.py` | 197, 221 | SAFE-PINNED | Paymob callback field carried as signed data (HMAC input / stored), compared only with other literals; no code compares it with any clock |
| `integration/test_period_card_email_invariants.py` | 38 | SAFE-PINNED | returned by a stubbed service or written into a row whose value is only read back and compared with other literals; no clock decides on it (rows written with NOW, refused or accepted by CHECK/UNIQUE) |
| `integration/test_plan_price_migration.py` | 37 | SAFE-PINNED | returned by a stubbed service or written into a row whose value is only read back and compared with other literals; no clock decides on it |
| `integration/test_platform_access_audit.py` | 203 | **BOMB-FUTURE** | CPM-03: GET /platform/overview?since=2026-08-01 without until -> until = real now (usage_service.resolve_window); a window over 366 days is refused 422 != 200; detonates 2027-08-02 00:00 UTC |
| `integration/test_platform_analytics.py` | 46, 47, 48 | SAFE-PINNED | explicit since AND until (both literals): the window never depends on the real clock |
| `integration/test_platform_billing_api.py` | 55 | SAFE-PINNED | fixed moment passed explicitly (now=/at=/clock=/today=) to the code under test, which takes the moment from its argument; no real-clock read decides the asserted value |
| `integration/test_recurring_billing.py` | 66 | SAFE-PINNED | fixed moment passed explicitly (now=/at=/clock=/today=) to the code under test, which takes the moment from its argument; no real-clock read decides the asserted value |
| `integration/test_refund_entitlements.py` | 67, 218 | SAFE-PINNED | fixed moment passed explicitly (now=/at=/clock=/today=) to the code under test, which takes the moment from its argument; no real-clock read decides the asserted value; created_at: Paymob callback field carried as signed data (HMAC input / stored), compared only with other literals; no code compares it with any clock |
| `integration/test_sentiment_escalation.py` | 46, 47 | SAFE-PINNED | two message moments ordered among themselves |
| `integration/test_settlement_backstop.py` | 43, 305 | SAFE-PINNED | fixed moment passed explicitly (now=/at=/clock=/today=) to the code under test, which takes the moment from its argument; no real-clock read decides the asserted value; created_at: Paymob callback field carried as signed data (HMAC input / stored), compared only with other literals; no code compares it with any clock |
| `integration/test_settlement_concurrency.py` | 158 | SAFE-PINNED | Paymob callback field carried as signed data (HMAC input / stored), compared only with other literals; no code compares it with any clock |
| `integration/test_template_endpoints.py` | 45 | SAFE-PINNED | returned by a stubbed service or written into a row whose value is only read back and compared with other literals; no clock decides on it |
| `integration/test_tenant_isolation.py` | 93 | SAFE-PINNED | returned by a stubbed service or written into a row whose value is only read back and compared with other literals; no clock decides on it |
| `integration/test_tenant_metrics.py` | 49, 50, 51 | SAFE-PINNED | explicit since AND until (both literals): the window never depends on the real clock; seeded rows state their own moment (NOON) |
| `integration/test_usage_endpoints.py` | 56, 101, 237, 238, 260 | SAFE-PINNED | StubUsage returns WINDOW (UNTIL - 30 d = 2026-08-02); explicit since/until params; no clock |
| `integration/test_usage_index_only.py` | 34, 35 | SAFE-PINNED | explicit since AND until (both literals): the window never depends on the real clock |
| `integration/test_usage_metering.py` | 25, 26, 27, 161, 162 | SAFE-PINNED | explicit since AND until (both literals): the window never depends on the real clock |
| `integration/test_usage_recording.py` | 234 | SAFE-PAST | Meta/synthetic webhook timestamp in the past; stored as the event's occurred/sent time; the only real-clock use (the 24 h service window) can only move further into the past, so the outcome cannot flip |
| `integration/test_webhook_body_cap.py` | 82 | SAFE-PAST | Meta/synthetic webhook timestamp in the past; stored as the event's occurred/sent time; the only real-clock use (the 24 h service window) can only move further into the past, so the outcome cannot flip |
| `integration/test_webhook_media_metadata.py` | 101, 111 | SAFE-PAST | Meta/synthetic webhook timestamp in the past; stored as the event's occurred/sent time; the only real-clock use (the 24 h service window) can only move further into the past, so the outcome cannot flip |
| `integration/test_webhook_payload_retention.py` | 45 | SAFE-PINNED | EPOCH 2019 and NOW = EPOCH + 90 d passed as now= to the sweep ('Long before anything else the suite writes') |
| `integration/test_whatsapp_accounts.py` | 69, 70, 71, 147 | SAFE-PINNED | returned by a stubbed service or written into a row whose value is only read back and compared with other literals; no clock decides on it (account attributes for a stubbed service) |
| `integration/test_whatsapp_number_handover.py` | 60 | N/A | regex false positive: not a calendar moment (a phone number) |
| `integration/test_whatsapp_persistence.py` | 240 | SAFE-PINNED | three event moments ordered among themselves |
| `integration/test_workspace_billing_lifecycle.py` | 70 | SAFE-PINNED | fixed moment passed explicitly (now=/at=/clock=/today=) to the code under test, which takes the moment from its argument; no real-clock read decides the asserted value |
| `integration/test_workspace_entitlements.py` | 60 | SAFE-PINNED | fixed moment passed explicitly (now=/at=/clock=/today=) to the code under test, which takes the moment from its argument; no real-clock read decides the asserted value |
| `integration/test_workspace_purge.py` | 52 | SAFE-PINNED | fixed moment passed explicitly (now=/at=/clock=/today=) to the code under test, which takes the moment from its argument; no real-clock read decides the asserted value |
| `integration/test_workspace_switching.py` | 156 | SAFE-PAST | membership created_at 2026-01-01 + n s orders memberships among themselves; older than now |
| `integration/topup_harness.py` | 111 | SAFE-PINNED | Paymob callback field carried as signed data (HMAC input / stored), compared only with other literals; no code compares it with any clock |
| `unit/test_agent_memory.py` | 25 | SAFE-PINNED | message moments ordered among themselves (unit) |
| `unit/test_agent_orchestrator.py` | 53 | SAFE-PINNED | message moments ordered among themselves (unit) |
| `unit/test_agent_queue.py` | 39 | SAFE-PINNED | fixed moment passed explicitly (now=/at=/clock=/today=) to the code under test, which takes the moment from its argument; no real-clock read decides the asserted value |
| `unit/test_agent_worker_retry.py` | 32 | SAFE-PINNED | fixed moment passed explicitly (now=/at=/clock=/today=) to the code under test, which takes the moment from its argument; no real-clock read decides the asserted value |
| `unit/test_backup_status.py` | 26, 32, 33, 55, 136, 182 | SAFE-PINNED | 'No sleeping: the timestamps are fixed and the age is arithmetic' (age against now=) |
| `unit/test_billing_calendar.py` | 34, 38, 40, 42, 59, 68, 83, 91, 92, 93 | SAFE-PINNED | pure function of its argument (parse / format / calendar arithmetic); no clock on the path |
| `unit/test_billing_models.py` | 30 | SAFE-PINNED | returned by a stubbed service or written into a row whose value is only read back and compared with other literals; no clock decides on it |
| `unit/test_channel_adapter_contract.py` | 63, 73, 87, 88, 225, 258, 260, 281, 411 | SAFE-PINNED | payload timestamps parsed by the adapter and compared with the same literal; pure function of its argument (parse / format / calendar arithmetic); no clock on the path |
| `unit/test_channel_policy_contract.py` | 62, 469 | SAFE-PINNED | fixed moment passed explicitly (now=/at=/clock=/today=) to the code under test, which takes the moment from its argument; no real-clock read decides the asserted value |
| `unit/test_connection_throughput.py` | 29 | SAFE-PINNED | fixed moment passed explicitly (now=/at=/clock=/today=) to the code under test, which takes the moment from its argument; no real-clock read decides the asserted value |
| `unit/test_crash_recovery.py` | 55 | SAFE-PINNED | fixed moment passed explicitly (now=/at=/clock=/today=) to the code under test, which takes the moment from its argument; no real-clock read decides the asserted value |
| `unit/test_disclosure_rules.py` | 23 | SAFE-PINNED | fixed moment passed explicitly (now=/at=/clock=/today=) to the code under test, which takes the moment from its argument; no real-clock read decides the asserted value |
| `unit/test_email_signature.py` | 18, 21 | SAFE-PINNED | svix timestamp 1700000000 verified against now=1700000000.0 passed explicitly |
| `unit/test_email_templates.py` | 47, 48, 62, 69, 75 | SAFE-PINNED | template variables rendered as text; pure function of its argument (parse / format / calendar arithmetic); no clock on the path |
| `unit/test_job_dispatch.py` | 33 | SAFE-PINNED | fixed moment passed explicitly (now=/at=/clock=/today=) to the code under test, which takes the moment from its argument; no real-clock read decides the asserted value |
| `unit/test_metrics_collection.py` | 33 | SAFE-PINNED | fixed moment passed explicitly (now=/at=/clock=/today=) to the code under test, which takes the moment from its argument; no real-clock read decides the asserted value |
| `unit/test_models.py` | 98, 139 | SAFE-PINNED | model properties: deleted_at makes is_active false whatever its value; invitation openness against an explicit now |
| `unit/test_pagination.py` | 19, 87, 91 | SAFE-PINNED | pure function of its argument (parse / format / calendar arithmetic); no clock on the path (cursor encode/decode) |
| `unit/test_paymob_card_tokens.py` | 43, 108 | SAFE-PINNED | Paymob callback field carried as signed data (HMAC input / stored), compared only with other literals; no code compares it with any clock (tampering list) |
| `unit/test_paymob_hmac.py` | 68, 210 | SAFE-PINNED | Paymob callback field carried as signed data (HMAC input / stored), compared only with other literals; no code compares it with any clock (tampering list) |
| `unit/test_paymob_refund_semantics.py` | 63, 95 | SAFE-PINNED | Paymob callback field carried as signed data (HMAC input / stored), compared only with other literals; no code compares it with any clock |
| `unit/test_queue_operator_command.py` | 21 | SAFE-PINNED | fixed moment passed explicitly (now=/at=/clock=/today=) to the code under test, which takes the moment from its argument; no real-clock read decides the asserted value |
| `unit/test_retention_worker.py` | 26 | SAFE-PINNED | fixed moment passed explicitly (now=/at=/clock=/today=) to the code under test, which takes the moment from its argument; no real-clock read decides the asserted value |
| `unit/test_subscription_lifecycle.py` | 24, 25, 43, 44, 62, 73, 74, 78, 82, 83, 120, 126, 133, 134, 142, 143, 154, 155, 157, 159, 160, 162, 163 | SAFE-PINNED | pure function of its argument (parse / format / calendar arithmetic); no clock on the path (add_interval / roll_over with now=) |
| `unit/test_trace_propagation.py` | 299 | SAFE-PINNED | legacy queue message field parsed; pure function of its argument (parse / format / calendar arithmetic); no clock on the path |
| `unit/test_upload_recovery_worker.py` | 40 | SAFE-PINNED | fixed moment passed explicitly (now=/at=/clock=/today=) to the code under test, which takes the moment from its argument; no real-clock read decides the asserted value |
| `unit/test_usage_models.py` | 31, 74, 75 | SAFE-PINNED | resolve_window(since=..., now=NOW): now passed explicitly |
| `unit/test_whatsapp_identity_parsing.py` | 56, 94, 309 | SAFE-PINNED | payload timestamps parsed; pure function of its argument (parse / format / calendar arithmetic); no clock on the path |
| `unit/test_whatsapp_lifecycle_and_backoff.py` | 36, 84 | SAFE-PINNED | api_version_warning(..., today=date(2026, 9, 11)): today passed explicitly |
| `unit/test_whatsapp_models.py` | 165 | SAFE-PINNED | released_at set; is_active reads the status only |
| `unit/test_whatsapp_payload.py` | 41, 57, 198 | SAFE-PINNED | pure function of its argument (parse / format / calendar arithmetic); no clock on the path (1767225600 parsed to 2026-01-01) |

</details>

## 5. Part A — Fixes, Mutants, Lanes, Shifted Lane

| ID | Commit | Test | Before | After (intent kept) |
| --- | --- | --- | --- | --- |
| CPM-02 | `744f77f` (+9 / −2) | `test_campaign_api_surface.py::test_a_scheduled_time_without_an_offset_is_refused` | schedules `2026-10-20T10:00:00+03:00` and asserts it is stored unchanged; `schedule()` moves a past time to now | the aware time is **two days from the real now, 10:00 at +03:00**, built when the test runs; precondition asserted ("a future time: a past one moves to now"); the naive time is still refused with the same status and code; the aware time still asserted stored unchanged in UTC |
| CPM-03 | `9d51152` (+5 / −2) | `test_platform_access_audit.py::test_platform_aggregate_route_uses_expected_audit_policy` | `since=2026-08-01T00:00:00Z`, no `until` | `since` = **30 days before the real now**; the same route, the same audit policy assertions |
| CPM-05 | `46cf10f` (+106 / −1) | `test_media_parser_containment.py` (+ `tests/pdf_fixtures.py`) | section 6 | section 6 |

**Mutants** (strict protocol: every anchor found once, the named killer with `-x`, files restored by SHA-256,
KILLED only when the first failure is the named killer), in `E:\wasla-cpr-mut`:

| Mutant | Behaviour broken | Fixed test (`9d51152`) | Original test (`752a9ba`) |
| --- | --- | --- | --- |
| M-D01 | `schedule()` stores a time without an offset as if it were UTC instead of refusing it | KILLED | KILLED (with `campaign_service`'s clock pinned to 2026-10-15) |
| M-D02 | `schedule()` moves an aware future time to now | KILLED | KILLED (pinned 2026-10-15) |
| M-D08 | `schedule()` shifts an aware future time by one hour | KILLED | KILLED (pinned 2026-10-15) |
| M-C35 | naive `scheduled_at` accepted again (the campaigns remediation's mutant; its named killer is the CPM-02 test) | KILLED | KILLED (pinned 2026-10-15) |
| M-D06 | a window given by `since` alone is not recorded as windowed (CPM-03) | KILLED | KILLED (unpinned: not yet detonated) |

Controls green before each set. Re-run at STAGE_FIX_HEAD `debde5f` (section 17): every M-D mutant and M-C35 KILLED again by its fixed test.

**Named dates** — the fixed files (17 tests) at the real time, with `TZ=UTC0`, and with the process clock moved to
2026-10-29 21:30, 2027-04-29 21:30, 2027-01-01 00:30 and 2028-02-29 10:00 Cairo: **0 failed in every run**. CPM-03's
original fails at 2027-08-03 (422) and the fixed test passes there.

**Lanes at `9d51152`**, run together on separate servers:

| Lane | Clock | Collected | Passed | Failed | Skipped | Start → end (UTC / Cairo) |
| --- | --- | --- | --- | --- | --- | --- |
| MODEL-BUILT | real | 7,053 | 7,035 | **0** | 18 | 11:53:45 → 12:37:53 / 14:53 → 15:37 |
| MIGRATION-BUILT (`WASLA_TEST_SCHEMA=migrations`, fresh `alembic upgrade head`) | real | 7,053 | 7,039 | **0** | 14 | 11:53:45 → 12:37:54 / 14:53 → 15:37 |
| KEPT-DATA SWEEP (ci.yml selection, 28 files + the two invariant suites) | real | 301 | 301 | **0** | 0 | 12:38 → 12:45 |
| MODEL-BUILT | Python **+100 days** | 7,053 | 6,946 | 89 (all classified) | 18 | 12:38 → 13:16 |

Collected 7,053 = `752a9ba`'s 7,053 (no test added by CPM-02/03). Skips = the known set (model: OpenAI ×11, schema
parity ×4, tmpfs ×1, kept-data-only ×2; migration: the same without parity). `ruff`, `black --check`, `mypy app
tests`, `mypy scripts`: clean. `git diff --exit-code 752a9ba 9d51152 -- app alembic scripts deploy .github
pyproject.toml requirements.lock docs`: exit 0, so the alembic chain, the populated upgrade and the docker /
monitoring jobs were not re-run for Part A ("unchanged, proven by diff"); GitHub ran them.

**The +100-day lane at `9d51152`** failed exactly the discovery run's set less CPM-02 (the split of the two runs'
failures differs by that one test and nothing else): 73 tests whose MinIO calls are refused `RequestTimeTooSkewed`
(the object store's clock is not shifted; proven by a direct probe, `proof/probe_s3_skew.py`), 10 whose Python-shifted
value is compared with a value PostgreSQL wrote with `now()`, and 6 consequential failures (each passes alone at the
same shifted clock on a fresh database and fails only after the object-store suites left rows behind; the same set
passes at the real clock). **0 unclassified.**

<details><summary>The 16 non-object-store failures of the shifted lanes, each with its cause</summary>

| Test | Class | Cause (the two values and the comparison) |
| --- | --- | --- |
| `test_ai_invariants.py::test_a_hold_nothing_settled_released_or_expired_is_counted` | SHIFT-ARTEFACT (DB now()) | held_at = Python now - 1561 s = 2027-01-30 08:xx vs SQL `t.held_at < now() - make_interval(secs => :hold_cutoff_seconds)` with now() = 2026-10-22 (tests/integration/test_ai_invariants.py:101): the hold is in the database's future, not counted |
| `test_campaign_api_surface.py::test_a_scheduled_time_without_an_offset_is_refused` | BOMB-DETONATED | CPM-02 (fixed in 744f77f): scheduled_at stored = shifted now 2027-01-30 != the fixed 2026-10-20 10:00 +03:00 |
| `test_campaign_header_media_lifecycle.py::test_a_file_no_live_campaign_names_follows_ordinary_retention[completed]` | SHIFT-ARTEFACT (DB now()) | CampaignHeaderMedia.created_at = server_default now() = 2026-10-22 vs MediaRetentionService.sweep cutoff = datetime.now(UTC) (2027-01-30) - 30 d (app/services/media_retention_service.py:210): a file a second old reads as 100 days old and is purged |
| `test_campaign_header_media_lifecycle.py::test_a_file_no_live_campaign_names_follows_ordinary_retention[cancelled]` | SHIFT-ARTEFACT (DB now()) | CampaignHeaderMedia.created_at = server_default now() = 2026-10-22 vs MediaRetentionService.sweep cutoff = datetime.now(UTC) (2027-01-30) - 30 d (app/services/media_retention_service.py:210): a file a second old reads as 100 days old and is purged |
| `test_campaign_header_media_lifecycle.py::test_a_file_no_live_campaign_names_follows_ordinary_retention[failed]` | SHIFT-ARTEFACT (DB now()) | CampaignHeaderMedia.created_at = server_default now() = 2026-10-22 vs MediaRetentionService.sweep cutoff = datetime.now(UTC) (2027-01-30) - 30 d (app/services/media_retention_service.py:210): a file a second old reads as 100 days old and is purged |
| `test_campaign_header_media_lifecycle.py::test_a_file_no_live_campaign_names_follows_ordinary_retention[None]` | SHIFT-ARTEFACT (DB now()) | CampaignHeaderMedia.created_at = server_default now() = 2026-10-22 vs MediaRetentionService.sweep cutoff = datetime.now(UTC) (2027-01-30) - 30 d (app/services/media_retention_service.py:210): a file a second old reads as 100 days old and is purged |
| `test_campaign_invariants.py::test_cr04_recipient_added_after_scheduling` | SHIFT-ARTEFACT (DB now()) | audit occurred_at = Python now - 1 d = 2027-01-29 vs recipient created_at = now() 2026-10-22; CR-04 `a.occurred_at < r.created_at` (scripts/omnichannel_invariants.py:534) is false |
| `test_entitlement_invariants.py::test_e14_a_campaign_copy_sent_after_a_stop_on_its_channel` | SHIFT-ARTEFACT (DB now()) | opt-out at = Python now - 1 h = 2027-01-30 07:xx vs message created_at = now() 2026-10-22; E14 `m.created_at > ct.marketing_opt_out_at` (scripts/omnichannel_invariants.py:731) is false |
| `test_ingestion_recovery.py::test_a_document_still_being_published_is_not_raced` | SHIFT-ARTEFACT (DB now()) | document written with now() 2026-10-22 vs IngestionRecoveryWorker moment = datetime.now(UTC) 2027-01-30 - 300 s grace (app/workers/ingestion_recovery.py:138,144): a document a second old reads as 100 days old and is claimed |
| `test_media_attempt_recovery.py::test_the_sweep_finishes_only_what_nothing_else_will` | SHIFT-ARTEFACT (DB now()) | the `fresh` attachment's created_at = now() 2026-10-22 vs run_once(now=Python now 2027-01-30): it reads as lost and is requeued (tests/integration/test_media_attempt_recovery.py:596) |
| `test_media_observability.py::test_stranded_media_and_owed_purge_deletes_are_gauged` | SHIFT-ARTEFACT (consequential) | passes alone at the same shifted clock on a fresh database (disc_A_other_isolated_shift100_r2) and fails again when run after the object-store files at the shifted clock (disc_A_consequential_shift100); the same set passes at the real clock (205 passed): it counts rows left by tests the object store refused before their cleanup |
| `test_omnichannel_low_findings.py::test_analytics_split_traffic_by_channel` | SHIFT-ARTEFACT (DB now()) | report window = Python now 2027-01-30 +/- 1 h vs the conversation and message rows created with now() 2026-10-22: outside the window, [] (tests/integration/test_omnichannel_low_findings.py:333-342) |
| `test_platform_access_audit.py::test_platform_aggregate_route_uses_expected_audit_policy` | BOMB-FUTURE | CPM-03 (fixed in 9d51152): since=2026-08-01 with until = now 2029-06-15 -> window > 366 days -> 422 |
| `test_platform_analytics.py::test_the_overview_counts_every_workspace_and_its_numbers` | SHIFT-ARTEFACT (consequential) | passes alone at the same shifted clock on a fresh database (disc_A_other_isolated_shift100_r2) and fails again when run after the object-store files at the shifted clock (disc_A_consequential_shift100); the same set passes at the real clock (205 passed): it counts rows left by tests the object store refused before their cleanup |
| `test_platform_analytics.py::test_a_deleted_workspace_is_not_counted` | SHIFT-ARTEFACT (consequential) | passes alone at the same shifted clock on a fresh database (disc_A_other_isolated_shift100_r2) and fails again when run after the object-store files at the shifted clock (disc_A_consequential_shift100); the same set passes at the real clock (205 passed): it counts rows left by tests the object store refused before their cleanup |
| `test_platform_analytics.py::test_each_workspace_carries_its_own_consumption` | SHIFT-ARTEFACT (consequential) | passes alone at the same shifted clock on a fresh database (disc_A_other_isolated_shift100_r2) and fails again when run after the object-store files at the shifted clock (disc_A_consequential_shift100); the same set passes at the real clock (205 passed): it counts rows left by tests the object store refused before their cleanup |
| `test_platform_analytics.py::test_the_page_total_counts_matches_not_the_page` | SHIFT-ARTEFACT (consequential) | passes alone at the same shifted clock on a fresh database (disc_A_other_isolated_shift100_r2) and fails again when run after the object-store files at the shifted clock (disc_A_consequential_shift100); the same set passes at the real clock (205 passed): it counts rows left by tests the object store refused before their cleanup |
| `test_whatsapp_inbound_recovery.py::test_a_status_event_owes_nothing_and_is_finished_immediately` | SHIFT-ARTEFACT (consequential) | passes alone at the same shifted clock on a fresh database (disc_A_other_isolated_shift100_r2) and fails again when run after the object-store files at the shifted clock (disc_A_consequential_shift100); the same set passes at the real clock (205 passed): it counts rows left by tests the object store refused before their cleanup |

SHIFT-ARTEFACT (object store clock), 73 tests, each individually: its failure section carries `media.object_store_refused`, `StorageError: The file store is unavailable` or `dependency_unavailable` - MinIO refusing a SigV4 request dated by the shifted clock (`RequestTimeTooSkewed`):

- `test_campaign_header_media_api.py` (15): `test_an_upload_is_stored_with_its_kind_size_and_hash[s3-\x89PNG\r\n\x1a\na spring sale photographa spring sale`, `test_an_upload_is_stored_with_its_kind_size_and_hash[s3-\xff\xd8\xff\xe0a spring sale photographa spring sale `, `test_an_upload_is_stored_with_its_kind_size_and_hash[s3-\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom\x00\x`, `test_an_upload_is_stored_with_its_kind_size_and_hash[s3-%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrail`, `test_the_kind_is_the_bytes_never_the_name_or_the_claim[s3]`, `test_a_member_cannot_upload_but_can_read[s3]`, `test_another_workspaces_file_is_not_found_anywhere[s3]`, `test_a_media_header_template_composes_with_a_file_of_its_kind[s3]`, `test_a_wrong_missing_or_extra_file_is_refused_at_composition[s3-IMAGE-pdf-template_parameters]`, `test_a_wrong_missing_or_extra_file_is_refused_at_composition[s3-VIDEO-png-template_parameters]`, `test_a_wrong_missing_or_extra_file_is_refused_at_composition[s3-DOCUMENT-mp4-template_parameters]`, `test_a_wrong_missing_or_extra_file_is_refused_at_composition[s3-TEXT-png-template_parameters]`, `test_a_wrong_missing_or_extra_file_is_refused_at_composition[s3-GIF-png-template_unsupported]`, `test_no_campaign_response_carries_metas_id_or_a_key[s3]`, `test_the_header_file_cannot_be_changed_after_composition[s3]`
- `test_media_crash_recovery.py` (1): `test_a_killed_worker_leaves_an_object_reconciliation_can_finish`
- `test_media_isolation.py` (8): `test_the_owner_can_read_their_own_attachment[s3]`, `test_another_workspace_cannot_read_the_attachment[s3]`, `test_the_bytes_survive_the_refusal[s3]`, `test_no_route_lets_another_workspace_delete_the_attachment[s3]`, `test_knowing_the_object_key_is_worth_nothing[s3]`, `test_a_conversation_from_another_workspace_is_not_a_way_in[s3]`, `test_an_unauthenticated_caller_gets_nothing[s3]`, `test_a_member_of_the_workspace_may_read_it[s3]`
- `test_media_retention.py` (18): `test_a_file_past_its_retention_is_removed[s3]`, `test_a_recent_file_is_untouched[s3]`, `test_old_and_recent_files_in_one_pass[s3]`, `test_a_file_exactly_at_the_boundary_is_kept[s3]`, `test_retention_of_zero_deletes_nothing[s3]`, `test_the_batch_bounds_one_pass[s3]`, `test_the_transcript_survives_the_file[s3]`, `test_a_purged_row_can_explain_itself[s3]`, `test_a_store_that_refuses_leaves_the_row_recoverable[s3]`, `test_the_next_pass_finishes_what_a_refusal_left[s3]`, `test_a_claimed_row_is_finished_even_when_retention_is_raised[s3]`, `test_purging_twice_is_not_an_error[s3]`, `test_a_partly_failed_pass_still_removes_what_it_can[s3]`, `test_pending_counts_only_unfinished_claims[s3]`, `test_a_replayed_media_job_does_not_undo_a_purge[s3]`, `test_reading_a_purged_file_is_not_attempted[s3]`, `test_a_sweep_with_no_clock_of_its_own_counts_honestly[s3]`, `test_a_sweep_never_touches_a_file_it_was_not_due_to[s3]`
- `test_media_url_and_limits.py` (2): `test_a_grant_issues_a_working_url_for_its_own_object_only`, `test_a_url_stops_working_when_it_expires`
- `test_media_write_atomicity.py` (12): `test_a_failed_finalisation_leaves_an_object_reconciliation_adopts`, `test_a_pending_intent_is_not_readable`, `test_an_intent_whose_object_never_arrived_is_not_called_stored`, `test_the_same_job_run_twice_writes_one_object`, `test_a_retry_after_a_crashed_write_reuses_the_committed_key`, `test_an_object_that_is_not_what_the_row_describes_is_never_adopted`, `test_a_store_that_cannot_be_reached_settles_nothing`, `test_retention_will_not_claim_an_upload_that_never_finished`, `test_recovered_storage_is_metered_once_and_only_once`, `test_two_reconcilers_settle_one_intent_once`, `test_reconciliation_settles_each_row_under_its_own_workspace`, `test_an_outbound_attachment_is_owned_before_it_is_written`
- `test_object_store.py` (13): `test_what_is_put_comes_back_byte_for_byte[s3]`, `test_a_key_is_tenant_prefixed_and_matches_the_shared_pattern[s3]`, `test_a_key_is_never_built_from_the_content_or_a_filename[s3]`, `test_two_workspaces_never_share_a_prefix[s3]`, `test_one_workspaces_key_never_returns_anothers_bytes[s3]`, `test_a_key_is_allocated_before_anything_is_written[s3]`, `test_deleting_removes_the_object[s3]`, `test_deleting_is_idempotent[s3]`, `test_an_empty_file_round_trips[s3]`, `test_a_large_file_round_trips[s3]`, `test_a_fresh_client_reads_what_another_wrote`, `test_exists_distinguishes_absent_from_unreachable`, `test_a_rejected_request_says_nothing_about_the_deployment`
- `test_workspace_purge_objects.py` (4): `test_a_purge_whose_deletes_are_refused_keeps_owing_them_until_they_land`, `test_a_process_that_dies_after_the_purge_commit_leaves_the_deletes_owed`, `test_a_write_landing_after_the_purge_leaves_no_object_behind`, `test_a_writer_that_dies_after_its_late_write_is_cleaned_up_behind`

</details>

## 6. Part A — Fast-Forward, Push and GitHub Actions Runs

| Step | Result |
| --- | --- |
| before | `origin` = local = `752a9ba`; 14 untracked paths, hashes equal; no incoming path collides or lies under `docs/ui/` |
| fast-forward + push #1, 13:17 UTC | `git merge --ff-only canonical-date-bound-tests-20261022` → `9d51152`; `git push origin worktree-billing-google-auth` (no force); `origin` = `9d51152` |
| run [39785483142](https://github.com/mohamedshhahat1/wasla/actions/runs/39785483142) on `9d51152` | **red**: lint, docker, alert rules green; **tests** (13:17–13:53 UTC) and **migration-built** (13:17–13:45 UTC) **1 failed each**, `test_media_parser_containment.py::test_an_amplifying_pdf_is_killed_on_time_while_the_loop_keeps_running` (`'This document could not be read.' != 'This document is too large or too complex to read automatically.'`) — 7,035 passed, the CPM-02/03 fixes green on GitHub |

**CPM-05 — dependency drift, GitHub only.** `ci.yml` installs with `pip install -e ".[dev]"`, so `pypdf>=5.1,<7`
resolves to **6.21.2** on GitHub while `requirements.lock` pins **6.19.0** (47 packages differ in all,
`evidence/gh_A_vs_lock.txt`). pypdf 6.21 added a per-content-stream parsing budget and refuses the audit's 25 KB
amplifying PDF after **5.4 s** (`LimitReachedError: Reached maximum number of tokens per stream`); the child answered
"unreadable" before the 20 s deadline. The application behaved correctly (the row SKIPPED and terminal); the test's
point — the child is killed at the deadline while the loop keeps running — was no longer exercised. Reproduced
locally with `venv_ci` (CI's exact 73 installed versions, less uvloop, which does not build on Windows): the unchanged
test fails with the same assertion; on 6.19.0 the same file parses past 20 s.

Fix `46cf10f` (tests only, the same pattern as `4b0d1d1`'s fix of the earlier pypdf drift):
`stalling_parser_child(directory)` runs the **real** `pdf_extract_child` with pypdf's page extraction replaced by a
loop that never returns, so only the parent's clock can end it on any pypdf release; the deadline test uses it with
**every assertion unchanged** (SKIPPED, `DocumentBeyondLimitsError.message`, deadline ≤ elapsed < deadline + 10, > 100
ticks, longest gap < 1 s, parent RSS growth < 150 MB). The real parser against the same file keeps a new,
version-tolerant test (refused or killed; terminal; released once; never past the deadline; the loop never stalls;
the parent does not grow). The child's CPU cap is the timeout + 1 s, so the parent's 20 s wall-clock kill comes first.

| Check of `46cf10f` | Result |
| --- | --- |
| the two media files on pypdf 6.19.0 (lock) / 6.21.2 (CI) | 40 / 40 passed each |
| the unchanged test on 6.21.2 | fails as on GitHub (`logs/A_cifix_repro_orig_ci.log`) |
| mutant: the stand-in parser returns instead of stalling | KILLED by the deadline test ("No text could be read…"); file restored, SHA-256 `957ed4…` |
| MODEL-BUILT, real clock, **CI's versions** (`venv_ci`) | 7,054 collected, **7,036 passed, 0 failed**, 18 skipped (14:16–14:56 UTC) |
| MIGRATION-BUILT, real clock, CI's versions | 7,054, **7,040 passed, 0 failed**, 14 skipped (14:16–14:56 UTC) |
| KEPT-DATA SWEEP, CI's versions | 301 passed, 0 skipped |
| quality with CI's tools (ruff 0.12.12, black 26.5.1, mypy 1.20.2) | clean |
| fast-forward + push #2, 15:04 UTC | `9d51152` → `46cf10f`, no force; `origin` = `46cf10f` = **CANON_FIX_HEAD**; untracked hashes equal |
| run [39801370475](https://github.com/mohamedshhahat1/wasla/actions/runs/39801370475) on `46cf10f` | **success, every job**: Lint, format, types 15:04–15:05; Alert rules and receivers 15:04–15:06; Docker build 15:04–15:05; Migration-built schema 15:04–15:38; Tests and migrations 15:04–15:48 UTC (18:04–18:48 Cairo) |

Collected 7,054 = 7,053 + the new real-parser test.

## 7. Part B — Calendar Sweep of the Stage's Tests

The same sweep over every file under `tests/` the stage added or changed (`git diff --name-only 752a9ba 3e1769f --
tests/`: 221 files, 85 with hits). **209 hits**: 70 on lines the stage added, 139 on pre-existing lines of files it
changed only to pass a time zone (each carrying Part A's class for the same line).

| Class | Stage-added lines | Pre-existing lines | Total |
| --- | --- | --- | --- |
| SAFE-PINNED | 47 | 121 | 168 |
| SAFE-PAST | 0 | 15 | 15 |
| BOMB-DETONATED | **5** (CPM-01) | 0 | 5 |
| BOMB-FUTURE | **5** (CPM-04, **detonates 2026-10-22 20:00 UTC**) | 1 (CPM-03, the same line as Part A's) | 6 |
| DST-FIXTURE | 8 (2026-04-23/24 and 2026-10-29/30, every clock on the path pinned: SimClock + `make_due`, or a pure function of its argument) | 0 | 8 |
| N/A | 5 | 2 | 7 |

The four uses the first attempt named: `release_quiet_waits` (`campaign_settings_service.py:55`, real `now`) — the
CPM-01 path; `messaging_limit_service.py:196` (`next_send_at = func.now()` on a limit read) — reached only by tests
whose SimClock starts at or after the real now (the `next_local` moments) or that assert no send time; the October
DST dates — DST-FIXTURE, safe; every test of `test_campaign_quiet_hours.py` classified (lines 123–362 SAFE-PINNED,
218–244 CPM-04, 262–329 DST-FIXTURE, 390–443 CPM-01).

**Dynamic proof.** The 22 stage-added test files at `34510a0` with the clock shifted +100 days and to 2029-06-15:
the same 6 failures each, every one a SHIFT-ARTEFACT (a Python-shifted window compared with a row PostgreSQL stamped
with `now()`); confirmed in the full shifted lane at STAGE_FIX_HEAD (section 13).

<details><summary>Every stage-added hit (70), by file</summary>

| File | Lines (added by the stage) | Class | Reason |
| --- | --- | --- | --- |
| `integration/test_campaign_contact_cap.py` | 289 | N/A | regex false positive: parses a value returned at run time |
| `integration/test_campaign_cost.py` | 99, 122 | SAFE-PINNED | rate card effective_from stored and listed; rate_card_service never compares it with a clock (activation is explicit) |
| `integration/test_campaign_delivery_plan.py` | 73, 129, 199 | N/A | regex false positive: parses a value returned at run time |
| `integration/test_campaign_delivery_plan.py` | 108 | SAFE-PINNED | naive send_until refused 422 for its missing offset before schedule() compares it with the clock (campaign_service.py:1660 precedes :1665) |
| `integration/test_campaign_quiet_hours.py` | 123, 137, 141, 146, 148, 152, 188, 207, 343, 362 | SAFE-PINNED | SimClock moment; the campaign is made due at it with make_due (no schedule() call), the worker is driven by the SimClock and the fake Meta stamps requests with it; no real-clock decision on the path |
| `integration/test_campaign_quiet_hours.py` | 218, 229, 239, 241, 244 | **BOMB-FUTURE** | CPM-04: CampaignService.schedule(scheduled_at=2026-10-22 23:00 Cairo) stores max(when, real now); from 2026-10-22 20:00 UTC the stored time is the real now, after the SimClock's 23:00, so the first sweep finds nothing due and waiting_reason stays None; detonates 2026-10-22 20:00 UTC |
| `integration/test_campaign_quiet_hours.py` | 262, 263, 280, 282 | DST-FIXTURE | deliberate DST transition date in Cairo; every clock on its path pinned (SimClock + make_due / a pure function of its argument) (April: 2026-04-23/24) |
| `integration/test_campaign_quiet_hours.py` | 300, 301, 320, 329 | DST-FIXTURE | deliberate DST transition date in Cairo; every clock on its path pinned (SimClock + make_due / a pure function of its argument) (October: 2026-10-29/30) |
| `integration/test_campaign_quiet_hours.py` | 390, 401, 418, 431 | **BOMB-DETONATED** | CPM-01: SimClock pinned to a fixed date while CampaignSettingsService.update / WorkspaceService.update call release_quiet_waits, which makes the waiting campaign due at the REAL now; once the real date passed the simulated one, the released campaign is due in the simulated future (edited window; detonated 2026-10-21 03:30 UTC) |
| `integration/test_campaign_quiet_hours.py` | 443 | **BOMB-DETONATED** | CPM-01: SimClock pinned to a fixed date while CampaignSettingsService.update / WorkspaceService.update call release_quiet_waits, which makes the waiting campaign due at the REAL now; once the real date passed the simulated one, the released campaign is due in the simulated future (zone change; detonated 2026-10-20 05:30 UTC) |
| `integration/test_campaign_results.py` | 237 | SAFE-PINNED | naive `from` refused 422 before the route reads the clock (campaigns.py summary: the offset check precedes now) |
| `integration/test_campaign_test_send.py` | 251 | N/A | regex false positive: parses a value returned at run time |
| `unit/test_business_initiated_ledger.py` | 29 | SAFE-PINNED | window_open(..., at=now) with an explicit now: pure function of its argument (quiet-hours window / delivery plan / zone offset with an explicit at/now) |
| `unit/test_delivery_plan.py` | 36, 47, 57, 60, 64, 71, 78, 85, 95, 114, 121, 137, 147 | SAFE-PINNED | plan(..., now=now): pure function of its argument (quiet-hours window / delivery plan / zone offset with an explicit at/now) |
| `unit/test_messaging_limit_values.py` | 47 | SAFE-PINNED | webhook entry time 1739321024 parsed as data; pure function of its argument (quiet-hours window / delivery plan / zone offset with an explicit at/now) |
| `unit/test_quiet_hours.py` | 38, 39, 40, 44, 45, 46, 51, 52, 56, 57, 61, 62 | SAFE-PINNED | pure function of its argument (quiet-hours window / delivery plan / zone offset with an explicit at/now) |
| `unit/test_timezones.py` | 13, 16, 22, 47, 56, 57 | SAFE-PINNED | describe / listing_etag with an explicit at: pure function of its argument (quiet-hours window / delivery plan / zone offset with an explicit at/now) |

Pre-existing lines in the files the stage changed (only to pass a time zone): 139 hits in 74 files, each carrying Part A's class for the same line text: BOMB-FUTURE 1, N/A 2, SAFE-PAST 15, SAFE-PINNED 121.

</details>

| Test (shifted) | Python side | Database side | Comparing line |
| --- | --- | --- | --- |
| `test_campaign_drafts.py::test_a_retried_create_with_one_key_makes_one_draft` (SHIFT-ARTEFACT) | since = datetime.now(UTC) - CREATE_KEY_LIFETIME (shifted +100 d) | campaign_create_keys.created_at server_default now() (real) | app/repositories/campaign_repository.py:353 CampaignCreateKey.created_at >= since (called from campaign_service.py:1125): the key row is 76 days older than "since", so the retry makes a second draft |
| `test_campaign_drafts.py::test_the_retention_sweep_forgets_create_keys_after_a_day` (SHIFT-ARTEFACT) | stale_before = datetime.now(UTC) - CREATE_KEY_LIFETIME (shifted) | created_at server_default now() (real) | app/services/campaign_service.py:711: the "fresh" key's real created_at is before the shifted stale_before, so the sweep deletes it |
| `test_campaign_page_migrations.py::test_0120_fills_the_cap_from_the_last_day_of_marketing` (SHIFT-ARTEFACT) | the seed writes the "old" message at Python now - 30 h = 2027-01-29 03:15 (shifted) | 0120 SQL m.sent_at > now() - interval '24 hours' (real) | alembic/versions/20261017_0120_backfill_marketing_claims.py:53: a shifted "30 h ago" is in the real future, so 0120 claims it |
| `test_campaign_results.py::test_the_summary_is_the_sum_of_every_campaign_not_a_page` (SHIFT-ARTEFACT) | start = local_midnight(now - SUMMARY_DEFAULT_DAYS) (shifted) | campaigns.created_at server_default func.now() (real) | app/api/v1/campaigns.py:506 then campaign_service.py:463 Campaign.created_at >= since: every campaign is created before the shifted window, so all counts 0 |
| `test_campaign_results.py::test_the_export_is_excel_safe_filtered_bounded_and_audited` (SHIFT-ARTEFACT) | moment = datetime.now(UTC) (shifted) | audit_logs.occurred_at server_default func.now() (real) | app/services/campaign_service.py:538 AuditLog.occurred_at > moment - EXPORT_WINDOW: earlier exports are 100 days "old", so the 1-hour limit never answers 429 |
| `test_campaign_test_send.py::test_a_workspace_sends_ten_tests_a_day` (SHIFT-ARTEFACT) | since = moment - TEST_SEND_WINDOW (campaign_service.py:1436, shifted) | campaign_test_sends.created_at server_default now() (real) | app/services/campaign_service.py:1436-1450: the ten earlier test sends are outside the shifted 24 h, so the eleventh is 201 not 429 |

## 8. Part B — Fixes, Mutants, the Merges of Part A, STAGE_FIX_HEAD

| ID | Commit | Test | Before | After (intent kept) |
| --- | --- | --- | --- | --- |
| CPM-01 | `f52dd56` (+109 / −5, with the helper and its unit test) | `test_campaign_quiet_hours.py::test_an_edited_window_recomputes_the_wait_and_switching_off_releases_it` | SimClock at 2026-10-21 06:30 Cairo; `release_quiet_waits` makes the waiting campaign due at the **real** now | the SimClock starts at **the next 06:30 Cairo at least an hour after the real now** (`next_local`); `resumes_at` still asserted 09:00 then 07:00 local of that morning after the edit, released when switched off, **2 messages**; precondition "released at the real now: it must not be later" asserted at each release |
| CPM-01 | `f52dd56` | `…::test_a_new_zone_moves_the_window_and_never_the_scheduled_time` | SimClock at 2026-10-20 05:30 UTC | **the next 05:30 UTC at least an hour ahead** (07:30 or 08:30 Cairo: quiet either side of DST; 09:30 Dubai: not); window moved, scheduled time never moved, **2 messages**; precondition asserted |
| CPM-04 | `34510a0` (+12 / −5) | `…::test_a_schedule_inside_the_window_is_accepted_and_starts_after_it` | `schedule(scheduled_at=2026-10-22 23:00 Cairo)`; from 20:00 UTC today `schedule()` would store the real now, after the SimClock's 23:00 | **the next 23:00 Cairo at least an hour ahead** (a fold night passed over); `resumes_at` = 09:00 the next morning; first send ≥ that morning; precondition asserted |

`tests/clock_moments.py` (DT-04): `next_local(hour, minute, tz=, after=)` — the first wall time after the real now
that exists exactly once (a DST gap or fold day is passed over); `tests/unit/test_clock_moments.py`, 5 tests,
covers Cairo's October fold and April gap. `campaign_harness.SimClock` unchanged.

| Mutant | Behaviour broken | Fixed test | Original test (`3e1769f`, `campaign_settings_service` / `campaign_service` clock pinned to 2026-10-17) |
| --- | --- | --- | --- |
| M-D03 | `release_quiet_waits` does not recompute `resumes_at` after a window edit | KILLED | KILLED |
| M-D04 | `release_quiet_waits` does not release when quiet hours are switched off | KILLED | KILLED |
| M-D05 | a zone change moves the campaign's scheduled time | KILLED | KILLED |
| M-D07 | `schedule()` inside the window does not wait for its end (CPM-04) | KILLED | KILLED |

Unpinned, the original CPM-01 control fails (the bomb); CPM-04's original passed before 20:00 UTC and fails with the
clock at 23:30 Cairo tonight, where the fixed test passes. Named dates (the quiet-hours file, the helper, the DST unit
files: 46 tests) at `TZ=UTC0` and the four Cairo dates: 0 failed each.

**Merges.**

| Commit | Parents | What |
| --- | --- | --- |
| `b236fed` | `34510a0`, `9d51152` | `merge: canonical date-bound test fixes into campaigns page features` — no conflict |
| `debde5f` = **STAGE_FIX_HEAD** | `b236fed`, `46cf10f` | `merge: canonical PDF deadline test fix into campaigns page features` — no conflict |

**Deviation:** section 20 plans one merge commit. CPM-05 changed CANON_FIX_HEAD after the first merge; without the
second, section 26's merged tree could not equal the verified tree. Both are normal merge commits; nothing was
rebased or rewritten. Every gate of sections 21–25 ran at `debde5f`; the lanes started at `b236fed` were discarded
(section 13).

## 9. Ancestry and Commits Landing

```text
git diff --name-only 3e1769f debde5f        tests/clock_moments.py, tests/unit/test_clock_moments.py,
                                             tests/integration/test_campaign_quiet_hours.py, test_campaign_api_surface.py,
                                             test_platform_access_audit.py, test_media_parser_containment.py,
                                             tests/pdf_fixtures.py   (7 files, +241 / −15)
git diff --exit-code 3e1769f debde5f -- app alembic scripts docs deploy .github pyproject.toml requirements.lock
    README.md DECISIONS.md                   exit 0
git merge-base --is-ancestor 46cf10f debde5f true      git merge-base --is-ancestor 3e1769f debde5f   true
git log --oneline 752a9ba..debde5f           36 = the stage's 29 + Part A's 3 + Part B's 2 + 2 merges
type: ignore at debde5f                      227 = 211 at 752a9ba + the stage's 16 narrow; this stage added 0; 0 broad
```

## 10. Migration Changes

`752a9ba..debde5f -- alembic/versions`: 13 files added (0108–0120), **0 existing migrations modified** (0001–0107
byte-identical); this stage changed nothing under `alembic/`.

| Revision | Adds |
| --- | --- |
| 0108 | `tenants.timezone` (backfill `Africa/Cairo`, validated CHECK) |
| 0109 | quiet-hours columns; `campaigns.waiting_reason`, `resumes_at`; two audit actions |
| 0110 | `whatsapp_business_portfolios`; `whatsapp_accounts.portfolio_id` |
| 0111 | `business_initiated_sends` (the ledger) |
| 0112 | `campaigns.send_until`; reason `send_window_closed` |
| 0113 | `contacts.last_marketing_sent_at`; message cap and held columns; stop and skip reasons |
| 0114 | `campaign_recipients.rendered_parameters` |
| 0115 | `campaigns.version`, `campaign_create_keys`; seven audit actions |
| 0116 | `campaign_segments`, `campaigns.audience_segment_id` |
| 0117 | `campaign_test` origin; `campaign_test_sends` |
| 0118 | `replied_at`, `opted_out_after_at` |
| 0119 | rate cards, rates, markets; two audit actions |
| 0120 | data only (CPF-04 backfill) |

## 11. Independent Static and Alembic Gates

From `E:\wasla-cpr-verify` at `debde5f` (`app.__file__` asserted, tree clean before and after):

| Gate | Result |
| --- | --- |
| `ruff check .` / `black --check .` / `mypy app tests` / `mypy scripts` | clean (1,002 files; 869 + 12 sources) |
| `alembic heads` | `0120`, single head |
| fresh `wasla_cpr_alembic` → head | **120 steps, 9.6 s** |
| `alembic check` / catalogue (NOT VALID, invalid indexes, disabled triggers) | no operations / 0 / 0 / 0 |
| `db_preflight verify` | ok, exit 0 |
| `omnichannel_invariants verify` | **59 checks, every one 0** (26 omnichannel, E01–E18, CR-01–CR-15), exit 0 |
| head → 0107 → head | 13 downgrades, 13 upgrades; check clean; catalogue 0/0/0 |
| base → head → check → preflight (CI's step) | 120 down, 120 up, clean, ok |
| `test_documentation_truth` | green in both lanes (README "0001–0120") |

| Refused downgrade (fresh database at head each) | Result |
| --- | --- |
| a workspace in `Asia/Riyadh` → 0107 | exit 1, "Workspace time zones cannot be downgraded … Nothing has been changed"; stamp `0120`; catalogue 0/0; preflight ok |
| a `business_initiated_sends` row → 0110 | exit 1, "The business-initiated ledger cannot be downgraded …"; stamp `0120`; 0/0; ok |
| a `campaign_segments` row → 0115 | exit 1, "Saved segments cannot be downgraded …"; stamp `0120`; 0/0; ok |
| 0120 → 0119 (data only) | 1 step; md5 of the ledger and of the per-contact claims equal before and after |

## 12. Populated Upgrade from Origin

With this stage's copies of `cpm_populate.py` and `cpm_upgrade_check.py` (run 3's version; copies changed only in
paths and database name, `copies_changes.txt`), on a freshly populated `wasla_cpr_upgrade` (server 2).

**Stage A** in `E:\wasla-cpr-origin` at `9d51152` (day run) — `app/`, `alembic/` and `scripts/` byte-identical to
`752a9ba` and to CANON_FIX_HEAD `46cf10f` (`git diff --exit-code`); the two test files that differ (CPM-05's) are not
imported by the populate script. Origin's own `alembic upgrade head` (0107), populated through origin's code: 3
workspaces with owner/admin/member, numbers A and B on one WABA and C disabled, sealed tokens, inbound history,
MARKETING body-only / IMAGE header / UTILITY templates, leads, an opt-out, campaigns in every status, the old code's
double send, a follow-up and a manual template in the last 24 h. Origin's worker and services: **163 requests to 161
people, 2 uploads**; **57 tables, 1,984 rows**; stage A's processes exited before the upgrade.

| Measure (stage B, `debde5f`) | Day run (16:23–16:25 UTC, 19:23 Cairo — outside quiet hours) | Night run |
| --- | --- | --- |
| `alembic upgrade head` | **one run, 13 steps, 6.1 s**, exit 0; head 0120 | (19:09–19:10 UTC, **22:09 Cairo — inside quiet hours**; stage A at `46cf10f`: 163 requests, **160** people, 3 twice — origin's old code populated differently at night) **one run, 13 steps, 5.3 s**, exit 0 |
| check / catalogue / preflight | clean / 0/0/0 / ok | clean / 0/0/0 / ok |
| invariants after the upgrade | 59 checks, all 0 | 59, all 0 |
| tables / rows / columns compared | **57 / 1,984 / 836; differences: `alembic_version` only** | 57 / 1,984 / 836; `alembic_version` only |
| workspaces | 3, all `Africa/Cairo`, quiet hours on 22:00–09:00 | |
| pre-existing campaigns / recipients | 8 campaigns, every status unchanged, new columns null, `version` 1; 668 recipients (507 pending, 161 sent) | |
| 0120 ledger | **163 rows = the 163 template messages of the last 24 h** (campaign 161, follow_up 1, manual_template 1); 0 orphans either way | 163 = 163 (campaign 161, follow_up 1, manual 1); claims **160 = 160** — exact for its data |
| 0120 claims | **161 contacts = the 161 sent a MARKETING template in the last 24 h** | |
| 0120 portfolios | 2, WABA-keyed, `checked_at` null (unknown); 3 numbers unlinked | |
| sealed tokens | all 3 decrypt; `pg_dump --data-only` (738,210 bytes): **0** of the key, the 3 tokens, the password | all 3 decrypt; dump 715,044 bytes: 0 |
| U-1 before the deploy step | schedule → **422 `messaging_limit_unknown`**; the running header campaign `running` / `limit_unknown`; workspace 3's due campaign not started; **0 requests** at the fake Meta besides the schedule's limit read | 422 `messaging_limit_unknown`; the running campaign waits **`quiet_hours`** until 06:00 UTC (09:00 local); 0 requests |
| U-2 the deploy step | **5 numbers read on v24.0, `messaging_limit=250`, exit 0**, each with its own sealed token (the platform token for the two holding none); again: identical; one refused: **exit 1**, `<id>  FAILED  known (kept)`, `5 numbers, 1 not read` — by internal id only (no token, phone or WABA printed) | identical: 5 read on v24.0, again identical, one refused → exit 1 by id only |
| U-3 after it | **the first sweep sends** (0.0 simulated min): 8 + 6 = **14 sends, 0 to anybody stage A reached, 0 twice**; 1 person held `contact_cap` (claim restored by 0120); workspace 3's campaign `completed` | **0 sends in the first 90 minutes after the deploy step**; both campaigns `quiet_hours`, `resumes_at` 2026-10-23 09:00 local; with the workers' clock moved past it: **first send 09:00:30 local, 0 requests inside 22:00–09:00 local**; then 10 + 6 sent, `completed`, **0 to anybody stage A reached, 0 twice** |
| U-4 shared WABA | new campaign on B (60 people): **41 sent, then `daily_limit`**; after the window the other 19, `completed`; **159 + 41 = 200; at most 200 people in any moving 24 h**, 0 twice | 41 sent, then `daily_limit` until 22:14 Cairo — then **`quiet_hours`** (the other 19 wait for 09:00), not sent at night; max 200 people per moving 24 h; 0 twice |
| end | invariants 59 checks, all 0 | 59, all 0 |

Runs recorded, not hidden: night run 1 (19:05) stopped after U-3 because my script paused a campaign the U-3-night
block had already completed (`logs/upgrade_night_run1_pause_refused.log`; its U-1/U-3 matched run 3); night run 2
(19:07) stopped in stage A — a generated credential key that began with `/` was rewritten by Git Bash's MSYS path
conversion and refused by origin as "not valid base64" (`logs/upgrade_night_run2_msys_key.log`). Both were defects
of my verification scripts, fixed in my copies (`copies_changes.txt`); run 3 above is complete.

## 13. Model-Built Test Results

| Lane | Head | Clock | Collected | Passed | Failed | Skipped | Start → end UTC (Cairo) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| MODEL-BUILT | `debde5f` | real | 7,298 | 7,280 | **0** | 18 | 15:04:45 → 16:00:36 (18:04 → 19:00) |
| MODEL-BUILT | `debde5f` | Python **+100 days** | 7,298 | 7,185 | 95 (**all classified**, 0 unclassified) | 18 | 16:21 → 17:19 (19:21 → 20:19) | |

Collected 7,298 = the first attempt's 7,292 + 5 (`test_clock_moments`) + 1 (CPM-05's real-parser test). Skips:
OpenAI real-provider ×11, schema parity ×4, tmpfs ×1, kept-data-only ×2 (18) — the known set. The log ends with its
count line; exit 0; tree clean after.

The lanes started at `b236fed` (13:18–14:14 UTC) are **discarded, not evidence**: at 14:16 I moved their worktrees to
`debde5f` while they ran (their modules were already imported), and I ran CPM-05's probes on server 1 meanwhile.
Their result (model 1 failed — CPM-06, below; migration 0 failed) is recorded only for CPM-06.

## 14. Migration-Built Test Results

| Lane | Head | Clock | Collected | Passed | Failed | Skipped | Start → end UTC (Cairo) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| MIGRATION-BUILT (`WASLA_TEST_SCHEMA=migrations`, fresh `alembic upgrade head`) | `debde5f` | real | 7,298 | 7,284 | **0** | 14 | 15:04:45 → 16:00:49 (18:04 → 19:00) |

Run at the same time as section 13's model-built lane, on the other server. Skips: OpenAI ×11, tmpfs ×1,
kept-data-only ×2 (14).

## 15. Kept-Data Sweep and CI Job Inventory

| Run | Selection | Collected | Passed | Failed | Skipped |
| --- | --- | --- | --- | --- | --- |
| 1 (16:01–16:08 UTC) | ci.yml's at `debde5f` = `kept_selection.txt` (28 files) + `test_ai_invariants.py` + `test_tool_invariants.py` | 301 | 301 | 0 | 0 |
| 2 (16:08–16:15) | equal | 301 | 301 | 0 | 0 |
| 3 (16:15–16:23) | equal | 301 | 301 | 0 | 0 |

Fresh migration-built database each run, `WASLA_TEST_KEEP_AI_DATA=1`, nothing else on that PostgreSQL. No file of the
selection was changed by Part A or B.

| CI job | Locally at `debde5f` | GitHub |
| --- | --- | --- |
| quality | ruff, black, mypy app tests, mypy scripts: clean | green on `46cf10f` (run 39801370475); the merged tree: section 23 |
| tests | model-built lane 0 failed; skip gate (18, all known); upgrade → base → head; preflight; alembic check | green on `46cf10f` (run 39801370475); the merged tree: section 23 |
| migration-parity | schema parity (in the migration lane, not skipped); migrated suites 0 failed; kept-data sweep 3/3 | green on `46cf10f` (run 39801370475); the merged tree: section 23 |
| monitoring | `promtool check config` (78 rules), `promtool test rules` (PortfolioLimitReached ×4, CampaignMetaQualityReview ×3, MessagingLimitUnknown ×3 cases among them), `amtool check-config`: all SUCCESS | green on `46cf10f` (run 39801370475); the merged tree: section 23 |
| docker | `docker build --target runtime` 180 s (571 MB); `/health/live` → `{"status":"alive"}` at attempt 4, after 9 s; uid **1001** (non-root); host port 18071 (8000 is the dev stack's) | green on `46cf10f` (run 39801370475); the merged tree: section 23 |

Coverage dropped locally (as every earlier stage): a recorded deviation. Dependencies: the verification venv equals
`requirements.lock` except `uvloop` (Linux-only) plus `tzdata` and `time-machine 3.5.1` (this stage's shifted lanes;
never added to `pyproject.toml` or the lock). GitHub resolves newer versions (CPM-05): every local lane at
CANON_FIX_HEAD was repeated with GitHub's exact versions.

## 16. The Clock

The clock-sensitive selection (the ten listed suites + every file a fix of this stage touched:
`test_platform_access_audit.py`, `test_clock_moments.py`, `test_media_parser_containment.py`), each on a fresh
model-built database on server 1, from `E:\wasla-cpr-verify` at `debde5f`. Cairo is UTC+3 until the night of
2026-10-29/30: quiet hours are 19:00–06:00 UTC tonight.

| Run | Process zone | Cairo time | UTC | Quiet hours? | Uptime | Result |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | machine (Africa/Cairo) | 19:01 → 19:10 | 16:01 → 16:10 | **outside** | 9.43 h | **121 passed, 0 failed** |
| 3 | `TZ=UTC0` | 19:10 → 19:19 | 16:10 → 16:19 | outside | 9.6 h | **121 passed, 0 failed** |
| 2 | machine | **23:01 → 23:10** | 20:01 → 20:10 (after CPM-04's old detonation moment, 20:00) | **inside** | 13.43 h | **121 passed, 0 failed** |

**CPM-06** — `test_campaign_quiet_hours.py::test_a_batch_reaching_the_window_stops_at_the_next_person_and_resumes_in_the_morning`
starts its SimClock at 21:58:59 Cairo with Meta answering in 0.3 s; campaigns stop deciding sends at 21:59:00
(`SEND_LEAD`), so the test has **1.0 s of real time** from the sweep's start to its first decision. In the discarded
`b236fed` model lane — while I ran CPM-05's PDF probes (each child burning a core for 20 s) on the same server — it
saw 0 requests (`assert 1 <= 0`). Not calendar-bound (SAFE-PINNED: SimClock + `make_due`). It passed in both correct
lanes at `debde5f`, in every clock run and in the race suites 3/3. Status: **open, recorded; not fixed** — widening the
margin is not a calendar fix and so outside what this stage may change; recommended for a reviewed test change
(section 26).

## 17. Races, Invariants and Mutation Spot-Check

Races at `debde5f`, 3 consecutive runs, a fresh model-built database each (`wasla_cpr_race1..3`, server 1, nothing else
on it), the seven files of section 25 (the two-worker, reservation, top-up, header-media worker, messaging-limit 1/2/4
workers and the 40 races for the last place, contact-cap with CPF-06's open-window race, quiet-hours with CPF-08's
slow send, CPM-06's edge and the fixed CPM-01/04 tests):

| Run | UTC (Cairo) | Collected | Passed | Failed |
| --- | --- | --- | --- | --- |
| 1 | 17:21:57 → 17:31:30 (20:21 → 20:31) | 80 | 80 | 0 |
| 2 | 17:31:32 → 17:41:05 (20:31 → 20:41) | 80 | 80 | 0 |
| 3 | 17:41:07 → 17:50:38 (20:41 → 20:50) | 80 | 80 | 0 |

Each of these tests asserts its own counts at the fake Meta's transport — `received_twice() == 0`, the unique people
in any moving 24 h at or under the budget (200 of 250; 40 of 50), one upload per campaign file, meters equal to the
recipients sent, no send inside 22:00–09:00 local — so 80 passed × 3 means every one of those held in every run.

Invariants — every check 0, E01–E18 and CR-01–CR-15 by name, on: the fresh alembic database (section 11), the
upgraded database after the upgrade and at the end (section 12), the APP-E2E database (V-8). The suites drop their schema at the end of a session, so `omnichannel_invariants verify` cannot run on the migration-built lane's or the kept-data sweep's database afterwards (tried: `relation "whatsapp_accounts" does not exist`, `logs/B_inv_migrations_db.txt`); the invariant suites inside both lanes (`test_campaign_invariants.py`, `test_entitlement_invariants.py`, the omnichannel ones) and the kept-data sweep's `test_ai_invariants.py` / `test_tool_invariants.py` over the kept data (3/3) are the in-run equivalent — a recorded deviation, as in the first attempt.

Mutation spot-check with this stage's copy of `run_cpm_mutations.py` (read in full; its one change from the stage's
runner: CONTROL runs only the named mutants' suites) in `E:\wasla-cpr-mut` detached at `debde5f`, `wasla_cpr_mut`
(server 2), after the full lanes:

| Set | Control | Result |
| --- | --- | --- |
| this stage's: M-F01n, M-F02, M-F03, M-F06, M-F08, M-F10, M-F11, M-F12, M-F13, M-F16, M-F22, M-F26, M-F27, M-F31 | 158 passed | **14 / 14 KILLED**, each by its named test |
| regression: M-C01 + M08, M-C09, M-C21, M-H01, M-H11 | (same control) | **5 / 5 KILLED** |
| this stage's M-D01..M-D08 and M-C35 | 9 controls green | **9 / 9 KILLED** by the fixed tests |

Every file restored and checked by SHA-256; `git status` of the worktree clean after (status [0]). 0 survivors.
M-F13 (quiet hours checked per batch only) and M-F31 (the send lead removed) are killed by CPM-06's test.

An attempt at 16:29 UTC started with `E:\wasla-cpr-mut` still at `b236fed`; it was stopped during its CONTROL (no
mutant applied, tree clean) and restarted at `debde5f` — `logs/B_spot_ABORTED_b236fed.log`.

## 18. Runtime Verification (APP-E2E)

This stage's copy of the e2e harness (run 3's version: preview of one nameless and two differently named customers;
copies changed in paths only), `wasla_cpr_e2e` built by `alembic upgrade head` from empty (120 steps, 11 s) on
server 2, Redis `wasla-cpr-redis4` DB 7 (no lane on it). Real API under uvicorn, real registration, login,
authorization and webhook signatures, two real `CampaignWorker`s; Meta, OpenAI and Paymob faked at the transport.
Run 16:27:14 → 16:27:46 UTC (19:27 Cairo).

| # | Result |
| --- | --- |
| V-1 | PASS — registration without a zone → 422; Cairo workspace; limit read once on v24.0 → `TIER_50`, budget 40 |
| V-2 | PASS — 60 customers, 2 STOPs → 2 opt-outs |
| V-3 | PASS — the retried create (same Idempotency-Key) returned the same draft; audience 58; three previews, three different bodies; estimate; test send 201 |
| V-4 | PASS — scheduled for **21:50 Cairo tonight**: 39 people on 2026-10-22 21:50; 19 people 2026-10-23 19:31–21:55 (the test send's ledger row frees its place 24 h after it); `spreads_over_days` |
| V-5 | PASS — 26 sweeps: **58 sends to 58 people, 0 twice, 0 inside quiet hours, at most 40 in any moving 24 h (40 with the test send), 0 values of another person, 0 to the opted out**; local days 40 / 18; 2 uploads; 58 delivered |
| V-6 | PASS — 20 replies; summary = statistics; ETag 304; CSV with BOM, 58 rows |
| V-7 | PASS — a member: writes 403, reads 200; activity complete |
| V-8 | PASS — invariants 59 checks all 0; **9 run secrets, 30 customer names and Meta's media id searched in the run log (95,496 bytes), the evidence (2,694), `/metrics` (69,099) and the audit metadata (1,260): 0 found**; the terms file deleted afterwards |

Runs recorded: run 1 (16:26, all PASS) is not the evidence — I deleted the terms file before scanning the run log;
kept in `e2e/run1_terms_deleted_early/`.

## 19. Untracked Files

`E:\wasla`: the same **14 untracked paths** (11 documents and `docs/ui/` with three mockups) before and after each
change — the fast-forwards of 13:17 and 15:04 and the merge of 20:11 UTC; SHA-256 of each equal to the first attempt's
(`untracked_before.sha256`, `evidence/untracked_*.sha256`). No incoming path collided with one; none lies under
`docs/ui/`. None was staged or committed.

## 20. Merge and Tree Equality

In `E:\wasla` on `worktree-billing-google-auth`, 20:11 UTC, after `git fetch origin --prune`:

| Check | Result |
| --- | --- |
| before | `origin/worktree-billing-google-auth` = local = `46cf10f` (= CANON_FIX_HEAD, not moved since push #2); `git status`: the 14 untracked paths only, hashes equal; no incoming path collides; none under `docs/ui/` |
| `git merge --no-ff debde5f -m "merge: campaigns page features and Meta sending limits (ADR-145..150)"` | **`0f767ec8076a4c36d508f598e60640bb6cc742ae`** = CANONICAL_MERGED_HEAD; parents `46cf10f`, `debde5f`; no conflict (`46cf10f` is an ancestor of `debde5f`) |
| `git diff --exit-code debde5f HEAD` | **exit 0** — tree `5592f904…` for both: the merged tree is byte-identical to the tree that was verified |
| `alembic heads` | `0120`, single head |
| untracked files after | the same 14, hashes equal |

The development stack was not restarted, migrated or rebuilt.

## 21. Gates on the Final Canonical Tree

From `E:\wasla-cpr-final`, detached at `0f767ec` (`app.__file__` asserted, tree clean before and after):

| Gate | Result | Same as at `debde5f` |
| --- | --- | --- |
| ruff / black / mypy app tests / mypy scripts | clean (1,002 files; 869 + 12 sources) | yes |
| fresh `wasla_cpr_final` → head | 120 steps, 11.9 s; check clean; catalogue 0/0/0; preflight ok; invariants 59 checks all 0; 13 down / 13 up; base → head clean | yes |
| MIGRATION-BUILT lane, real clock, uninterrupted | 7,298 collected, **7,284 passed, 0 failed, 14 skipped** (known set), 20:12:22 → 21:04:12 UTC (23:12 → 00:04 Cairo, inside quiet hours); the log ends with its count line; `test_documentation_truth` green | yes |
| KEPT-DATA SWEEP, ci.yml's selection (equal), fresh database | **301 passed, 0 failed, 0 skipped** | yes |
| race suites once | **80 passed, 0 failed** (20:23 → 20:33 UTC, 23:23 → 23:33 Cairo) | yes |

## 22. Push

Only after every gate above: `git fetch origin --prune`; `origin/worktree-billing-google-auth` still `46cf10f`;
`git merge-base --is-ancestor origin/worktree-billing-google-auth HEAD` → true; `git push origin
worktree-billing-google-auth` (no force), with this report's commit on top of `0f767ec`. Outcome: the addendum.

## 23. First GitHub Actions Run after Part B

Watched after the push; recorded in the addendum below.

## 24. Findings

| ID | Where | What | Status |
| --- | --- | --- | --- |
| CPM-01 | stage | the two quiet-hours change tests pinned a SimClock to dates the real clock passed (`release_quiet_waits` uses the real now) | **fixed** `f52dd56`; M-D03/04/05 killed |
| CPM-02 | canonical (campaigns remediation) | scheduling test with a fixed future date; GitHub red on any run since 2026-10-20 | **fixed** `744f77f`, pushed; M-D01/02/08, M-C35 killed; GitHub green (run 39801370475) |
| CPM-03 | canonical | platform overview audit test, `since=2026-08-01` without `until`: refused 422 from 2027-08-02 | **fixed** `9d51152`, pushed; M-D06 killed |
| CPM-04 | stage | quiet-hours schedule test at a fixed 2026-10-22 23:00 Cairo: would fail from 2026-10-22 20:00 UTC | **fixed** `34510a0`; M-D07 killed; its fixed test passed at 23:01 Cairo tonight, after the moment the original would have started failing |
| CPM-05 | canonical, GitHub only | CI resolves pypdf 6.21.2 (lock 6.19.0); the PDF deadline test no longer reached the deadline | **fixed** `46cf10f` (test only), pushed; GitHub green |
| CPM-06 | stage | quiet-hours edge test with 1.0 s of real-time margin; failed once under load I added | **open** (recorded; not a calendar fix; passed in every correct run) |

No defect in application code or a migration was found.

## 25. Infrastructure Created by This Stage

| Kind | Names |
| --- | --- |
| Containers | `wasla-cpr-pg` (55971), `wasla-cpr-pg2` (55972), `wasla-cpr-redis` (56971), `-redis2` (56972, `TEST_REDIS_URL`), `-redis3` (56973), `-redis4` (56974), `wasla-cpr-minio` (59971, bucket `wasla-media`) |
| Worktrees | `E:\wasla-cpr-canon`, `-dates`, `-a-lane`, `-verify`, `-lane`, `-origin`, `-mut`, `-final` |
| Branches (local) | `canonical-date-bound-tests-20261022` (`46cf10f`), `campaigns-page-features-dates-20261022` (`debde5f`) |
| Notes | `E:\wasla-cpr-notes` (scripts, sweeps, evidence, logs; `venv` lock-pinned + time-machine; `venv_ci` = GitHub's resolution) |

Cleanup (section 37) follows the push and is confirmed in the addendum: the `wasla-cpr-*` containers and volumes
and this stage's worktrees, removed with `git worktree remove`. The populated upgrade's key, tokens and password lived
only in process environments; the one file that held run secrets (APP-E2E's scan terms) was deleted after each scan.
Kept, as section 37 says: `E:\wasla`, `E:\wasla-campaign-features`, `E:\wasla-cpf-base`, `E:\wasla-cpf-lane`,
`E:\wasla-cpf-lane2`, `E:\wasla-cpf-mut`, the `wasla-cpf-*` containers and every branch.

## 26. Remaining External Actions

```text
decisions this stage did not take (section 10), with its evidence:
    pass the caller's clock into release_quiet_waits / messaging_limit_service, so tests and code share one clock
        (CPM-01 and CPM-04 exist because a SimClock met a real-clock decision; the shifted lanes found 16 more
        places where a Python clock is compared with PostgreSQL's now())
    install CI from requirements.lock: GitHub resolved 47 packages differently from the lock on 2026-10-22 and pypdf
        6.21.2 broke a test (CPM-05), the second pypdf drift after 4b0d1d1
    a scheduled CI job (nightly, at a Cairo night hour) and/or a CI job with a shifted clock: every bomb of this
        stage would have been caught on the night it became due; CPM-04 would have failed tonight at 20:00 UTC
CPM-06: give test_a_batch_reaching_the_window_stops_at_the_next_person_and_resumes_in_the_morning more than 1.0 s of
    real-time margin before 21:59 (a reviewed test change)
subscribe business_capability_update, account_alerts and message_template_status_update (CMQ-05) in the Meta App
on the first deploy of 0108–0120: stop the old processes, alembic upgrade head, then
    python -m scripts.read_messaging_limits (with META_ACCESS_TOKEN set for numbers holding no token of their own),
    and only then start the campaign workers (RUNBOOK)
real-Meta lane (only when authorised): the messaging limit on the real number vs WhatsApp Manager; whether any
    field names a WABA's portfolio (LIM-02); Meta past the portfolio limit (EXT-CMP-01); a new template's first
    send held_for_quality_assessment? (EXT-CMP-08); one small personalised image-header campaign to 1–3 numbers
Meta's current rate card (XLSX → CSV UTF-8), upload and activate (COST-01); Egypt (+20) rates (EXT-CMP-10)
open product decisions: LIM-04 (follow-ups and manual templates at 100 %); quiet hours for follow-ups; CMQ-07
    (legal review, Egypt PDPL 151/2020, then contact import); CMQ-08 (recipient-row retention with
    rendered_parameters); CMQ-10; per-recipient header media; push instead of polling
FRONTEND_MASTER_PLAN.md (untracked): the campaigns page against docs/CAMPAIGNS_PAGE_CONTRACT.md, the time zone
    at registration, the three mockup differences the contract records, the §26 admin screens
remove the implementation stage's infrastructure after review (infrastructure cleanup prompt): the wasla-cpf-*
    containers, E:\wasla-campaign-features, E:\wasla-cpf-base, E:\wasla-cpf-lane, E:\wasla-cpf-lane2,
    E:\wasla-cpf-mut — keep a copy of audit\ first
the local branches of this line of work (campaigns-page-merge-verification-20261022, canonical-date-bound-tests-20261022,
    campaigns-page-features-dates-20261022): keep or delete after review — never pushed
the deploy, with the RUNBOOK rule: stop old processes before alembic upgrade
whether and when the canonical branch is merged into main (not part of this stage)
```

## 27. Final Verdict

**Part A: DATE-BOUND TESTS FIXED ON CANONICAL, PUSHED, CI GREEN** (run 39801370475).

At the commit of this report every local gate of this stage is green, the merge is made with a byte-identical
tree, and no stop condition was hit. The final verdict word depends on the GitHub Actions run of this report's push,
so it is written in the addendum, with that run.

| Scenario | First attempt (2026-10-22 morning) | After | Verdict |
| --- | --- | --- | --- |
| CPM-02 on the canonical head | fails every run since 2026-10-20 | passes at any date, hour and zone (named dates, +100 d, 2029); M-D01/02/08, M-C35 killed | PASS |
| GitHub Actions on the canonical branch | red on any run | **green in every job** (39801370475, after CPM-05) | PASS |
| CPM-01 on the stage | fails every run since 2026-10-21 | passes at any date, hour and zone; M-D03/04/05 killed | PASS |
| Every other calendar-bound test | five suites checked | 252 + 209 hits classified; CPM-03 and CPM-04 found and fixed | PASS |
| Full model-built lane, Python clock +100 days | — | 95 failures, **0 unclassified** (73 object store, 16 + 6 database `now()`/consequential) | PASS |
| Clock selection outside quiet hours / TZ=UTC0 | 2 failed each | **121 / 121 passed** | PASS |
| Clock selection INSIDE quiet hours | not run | **121 passed at 23:01 Cairo** | PASS |
| Model-built / migration-built lanes together | 3 failed each | **0 failed each** (7,280 / 18 and 7,284 / 14 of 7,298) | PASS |
| Kept-data sweep | not run | **301 × 3, 0 failed** | PASS |
| Races | not run | **80 × 3** (+ 80 on the final tree), 0 people twice, within budget | PASS |
| Mutation spot-check | not run | **19 + 9 killed**, every file restored | PASS |
| Origin's database with data → 0120, U-1..U-4 | PASS | PASS by day **and inside quiet hours** (nothing sent before 09:00 local) | PASS |
| APP-E2E V-1..V-8 | PASS | PASS | PASS |
| Merged canonical tree vs verified tree | not merged | byte-identical (`5592f904…`) | PASS |
| origin after the final push / GitHub Actions after it | `752a9ba` / — | addendum | addendum |

**The final question.** Deployed tomorrow from what this push puts on GitHub, onto yesterday's database, by someone
following the RUNBOOK: the populated upgrade says that every campaign running before the deploy waits
(`limit_unknown`, 0 sends, schedules refused 422) until `scripts.read_messaging_limits` has read the limit; after it,
nobody stage A reached is messaged again that day (0 of 14 / 15, the 0120 claims hold), Meta's limit is held at 80 %
(200 of 250 in any moving 24 h, old and new sends together), and inside 22:00–09:00 Cairo nothing is sent — the night
run's campaigns waited to 09:00:30 local. Every gate ran by day and by night, in Cairo's zone and on UTC, and the suite
now passes with the clock months and years ahead (apart from artefacts of shifting only Python's clock). GitHub's CI
agreed on `46cf10f` at 15:04 UTC; whether it agrees on the merged tree is the addendum. Two things remain outside a
test's reach: CI resolving newer dependencies than the lock (CPM-05 was the second such break; section 26) and
CPM-06's one-second margin.

## Addendum — Push and GitHub Actions Run after Part B (recorded after the push)

| Step | Result |
| --- | --- |
| push #3, 21:05 UTC | `git fetch origin --prune`: `origin/worktree-billing-google-auth` = `46cf10f` (unchanged since push #2); `git merge-base --is-ancestor origin/worktree-billing-google-auth HEAD` → true; `git push origin worktree-billing-google-auth` (no force) `46cf10f..06592a4`; `origin` = local = **`06592a4`** (this report's commit on top of the merge `0f767ec`) |
| run [39854395131](https://github.com/mohamedshhahat1/wasla/actions/runs/39854395131) on `06592a4` | **success, every job** |

| Job | Conclusion | UTC (Cairo) |
| --- | --- | --- |
| Lint, format, types | success | 21:05:36 → 21:07:17 (00:05 → 00:07) |
| Docker build | success | 21:05:36 → 21:07:12 |
| Alert rules and receivers | success | 21:05:36 → 21:07:13 |
| Migration-built schema (schema parity, migrated suites, kept-data sweep) | success | 21:05:37 → 21:43:38 (00:05 → 00:43) |
| Tests and migrations | success | 21:05:37 → 21:57:59 (00:05 → 00:57) |

GitHub ran the merged tree's test jobs after midnight Cairo, inside quiet hours, a day after the date-bound tests'
original dates: green. The canonical branch's three runs of this stage: 39785483142 (`9d51152`, red, CPM-05),
39801370475 (`46cf10f`, green), 39854395131 (`06592a4`, green).

**Cleanup done** (after the run was green): the seven `wasla-cpr-*` containers with their volumes and the
`wasla-cpr:debde5f` image removed; the eight worktrees removed with `git worktree remove`; the branches kept. The
development stack was never touched. `E:\wasla-cpr-notes` and `E:\wasla-cpm-notes` are removed once this addendum's
own run is green (section 37, verdict CI GREEN); the reusable verification scripts (`cpm_populate.py`,
`cpm_upgrade_check.py`, `upgrade_env.sh`, `run_upgrade.sh`, `e2e/`, `audit/run_cpm_mutations.py`,
`audit/run_cpr_date_mutants.py`, `proof/shift_clock.py`) are first copied to `E:\wasla-verification-scripts`, outside
every repository, for a later system-wide audit.

### Final verdict

```text
Part A   DATE-BOUND TESTS FIXED ON CANONICAL, PUSHED, CI GREEN
Final    CAMPAIGNS PAGE FEATURES MERGED AND PUSHED, CI GREEN
```

Open: CPM-06 (a one-second test margin; section 16) and the three decisions of section 26 (one clock for tests and
code, CI from the lock, a nightly or shifted-clock CI job).
