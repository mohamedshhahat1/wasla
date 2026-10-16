# Campaigns page features and Meta sending limits — implementation (DRAFT)

Stage branch `campaigns-page-features-20261016`, worktree `E:\wasla-campaign-features`,
based on `worktree-billing-google-auth` at `752a9ba` (= origin). Nothing in this stage is
pushed, merged or deployed.

This file is a working draft. A resumed session reads the **Phase status** table and
`git log`, then continues from the first phase that is not committed. The final report
replaces this draft with the sections listed in the stage brief (section 32).

## Phase status

| Phase | Subject | State | Commit | Suites run |
|---|---|---|---|---|
| 0 | Baseline, Meta's documentation, before-probes | committed | 1ca3e86 | p20, p21 at BASE_HEAD |
| 1 | Workspace timezone, timezones API | committed | 5450912 | timezones unit + API, workspace lifecycle, route authorization, documentation truth, auth endpoints (89 passed); migration-built: schema parity, downgrade atomicity, every migration suite |
| 2 | Quiet hours | committed | 2015b7d | every campaign suite + unit (308 passed after the fixes below); quiet hours, settings API, timezones API; migration-built: schema parity, downgrade atomicity, quiet hours |
| 3 | Meta messaging limit: read, store, webhooks | committed | e5bbb6e | every campaign, WhatsApp and webhook suite + metric/doc/deployment checks (658 passed after the fixes below); messaging limit reads + values; migration-built: parity, downgrade atomicity, limit reads |
| 4 | Ledger and the 80 % budget | committed | (this commit) | every campaign, WhatsApp, follow-up, template, delivery-protocol, retention and unit suite (4,087 passed, 10 failed in one stub - fixed, 103 re-run passed); limit worker tests (14 passed: 200/200/200 with 1, 2, 4 workers; 40/40 races); migration-built: parity, downgrade atomicity, limit API |
| 5 | Spreading, deadline, delivery plan, ETA | not started | | |
| 6 | Per-contact cap, pacing, 131049 | not started | | |
| 7 | Personalisation | not started | | |
| 8 | Draft lifecycle, search, idempotent create, activity | not started | | |
| 9 | Segments, contact picker | not started | | |
| 10 | Test send | not started | | |
| 11 | Results, summary, progress, CSV export | not started | | |
| 12 | Rate cards, cost estimate | not started | | |
| 13 | Invariants, observability, documentation, contract | not started | | |
| 14 | Mutation matrix | not started | | |
| 15 | Full lanes, CI jobs, after-probes | not started | | |
| 16 | Final report | not started | | |

## Starting repository state (recorded 2026-10-16)

```text
canonical          worktree-billing-google-auth at 752a9ba = origin (0 ahead / 0 behind)
752a9ba vs 450333e CAMPAIGNS_CANONICAL_MERGE_VERIFICATION.md only (documentation)
BASE_HEAD          752a9ba
BASE_MIGRATION     0107 (single head)
last ADR           ADR-144
Graph API version  v21.0 (Settings.meta_api_version)
Python             3.12.7
Docker             29.8.0, Compose v5.5.1
PostgreSQL         16.15 (pgvector/pgvector:pg16)
Redis              7.4.11 (redis:7-alpine)
app.__file__       E:\wasla-campaign-features\app\__init__.py
```

The earlier stages' containers (`wasla-cmh-*`, `wasla-cmv-*`, `wasla-cmp-*`) were no longer
running; only the development stack was. This stage's containers:

```text
wasla-cpf-pg      127.0.0.1:55951   model-built lane, probes, targeted runs
wasla-cpf-pg2     127.0.0.1:55952   migration-built lane
wasla-cpf-redis   127.0.0.1:56951
wasla-cpf-redis2  127.0.0.1:56952   TEST_REDIS_URL (Google OAuth races)
wasla-cpf-redis3  127.0.0.1:56953   second lane
wasla-cpf-minio   127.0.0.1:59951   bucket wasla-media
```

## Meta's documentation, read 2026-10-16 (working notes)

| Fact | Value used | Source |
|---|---|---|
| Limit definition | unique WhatsApp user numbers delivered to outside a customer service window, moving 24 h, per business portfolio | messaging-limits |
| Levels | 250, 2,000, 10,000, 100,000, unlimited | messaging-limits |
| Read | `whatsapp_business_manager_messaging_limit` on the phone number, the WABA and the portfolio; v24.0 and newer | changelog 2025-10-08 |
| Values | `TIER_50`, `TIER_250`, `TIER_2K`, `TIER_10K`, `TIER_100K`, `TIER_UNLIMITED`, `UNTIERED` (WABA enum) | WhatsApp Business Account reference |
| Change webhook | `business_capability_update`, `entry[].id` = WABA id; `max_daily_conversations_per_business` (`TIER_*` strings; the example carries an integer) / deprecated `max_daily_conversation_per_phone` (integers, -1 = unlimited) | business_capability_update reference |
| Denied increase | `account_alerts`, `alert_type` `INCREASED_CAPABILITIES_ELIGIBILITY_DEFERRED` / `_FAILED` / `_NEED_MORE_INFO` | messaging-limits |
| Held message | `message_status: held_for_quality_assessment` in the **send response** | portfolio-pacing |
| Dropped held | status webhook `failed`, code `135000`; no sends or template creation during review | portfolio-pacing |
| Per-user marketing | adaptive, no number; 131049; wait at least 24 h; across businesses | per-user-limits |
| Throughput | 80 mps default, 1,000 upgraded; 130429 | throughput |
| Components | body 1024; text header 60 with 1 parameter; footer 60; 10 buttons; 2 URL; 1 phone; 1 copy code (20); label 25 | components |
| TTL | marketing 12 h .. 30 days, default 30 days, `message_send_ttl_seconds` | time-to-live |
| Pricing | per delivered template; recipient calling code; rate cards in 16 currencies | pricing |

Not documented (handled conservatively): what Meta does past the portfolio limit (no code;
131048 "restrictions on how many messages can be sent from this phone number" is not
documented as the portfolio limit); the per-user number; the pair-rate number; a price API;
**which business portfolio owns a WABA** (the WABA node lists no such field, so the ledger is
keyed on the WABA - LIM-02's fallback); whitespace rules for parameter values (none on the
components or error-code pages; PER-05 normalises anyway); the split of calling code +7
between Russia and Kazakhstan.

Differences from the brief's section 2.1: the WABA enum also documents `TIER_50` and `UNTIERED`;
`business_capability_update`'s per-business field is labelled Integer with `TIER_*` values and
an integer example; the rate card (effective 2026-10-01) prices Bangladesh, Iraq, Kazakhstan,
Kuwait, Morocco, Nepal, Oman, Sri Lanka and Ukraine as standalone markets; Meta's "Rates (CSV)"
file is an XLSX workbook (`usd_list_rates`, sha256
`3e996b56a86d9e7f70a9cfded896c8f39a19cb0e88683ed06b28f191856c9260`, 20,545 bytes).

## Before (BASE_HEAD 752a9ba)

`audit/p20_limit_probe.py` (untracked): 600 people, portfolio limit 250, every window closed,
one worker, migration-built database at 0107:

```text
transport sends 600, unique people 600, received twice 0
max unique people in any moving 24 h: 600
```

`audit/p21_page_before.py` (untracked), the application's own OpenAPI document: every item of
section 31 is MISSING - search, duplicate, PATCH (405), DELETE, test send, preview messages,
segments, campaign settings, cost estimate, CSV export, activity, progress, replied, reasons,
ETA, messaging limit, delivery plan, send_until, contact picker, Idempotency-Key, summary,
timezones, workspace and registration timezone, rate cards, waiting_reason.

The campaigns page design was found at `E:\wasla\docs\ui\campaigns-page-mockup.html` (the
user's untracked file; read, not modified). Its elements marked "new": send test to myself,
ETA, edit draft, delete, duplicate, replied, updates live, why recipients were not reached,
export CSV, activity, Meta daily messaging limit, personalisation buttons (`{contact.first_name}`,
`{lead.interest}`), saved segments, respect quiet hours (a per-campaign checkbox in the design;
TZ-03 makes quiet hours a workspace setting), estimated cost.

## Phase 1 notes (TZ-01, TZ-02)

- `tzdata>=2026.2,<2028` added; the lock pins `tzdata==2026.5` (IANA 2026e). Zones are read
  from the package's files (`ZoneInfo.from_file`), never through the system path, so Windows
  and the Linux CI compute with the same data.
- Accepted names: the union of tzdata's `zone.tab` and `zone1970.tab` (418 zones). `zone.tab`
  keeps a country's own name where the database linked it to a neighbour (`Asia/Kuwait`,
  `Asia/Bahrain`, `Asia/Muscat`, all in the suggested list). `US/Eastern`, `Egypt`, `Etc/*`,
  `UTC` and `Asia/Calcutta` are refused (`timezone_unknown`).
- Migration **0108**: `tenants.timezone` added nullable, backfilled `Africa/Cairo` by keyset
  batches, NOT NULL through a validated CHECK; no server default. Downgrade refuses while any
  workspace has another zone.
- Every route that creates a workspace requires it: `POST /workspaces` and
  `POST /auth/register`. `PATCH /workspace` changes it (owner or admin), audited as
  `workspace_updated` with `previous_timezone` / `timezone` only. `GET /workspace` returns
  `timezone` and `utc_offset`.
- `GET /api/v1/timezones`: public, `PublicRateLimit` (client address, workspace budget's
  number), `Cache-Control: public, max-age=3600`, ETag over the tzdata release, every zone's
  current offset and the filter; 304 on a match.
- Test fixtures: one helper, `tests/workspace_timezone.py` (`TEST_TIMEZONE = "Africa/Cairo"`).
  166 test files gained it on their `Tenant(...)` constructions, 16 on registration payloads
  and calls, 5 on `POST /workspaces` payloads, 9 on raw `INSERT INTO tenants` against the head
  schema; 189 test files changed in all, three of them new. `scripts/pitr_drill.sh` inserts a
  zone too.

## Phase 2 notes (TZ-03, TZ-04)

- Migration **0109**: `tenants.campaign_quiet_hours_enabled` (true), `_start` (22:00), `_end`
  (09:00) with constant defaults (catalogue-only), CHECKs for a non-empty window and whole
  minutes; `campaigns.waiting_reason` (CHECK: quiet_hours, daily_limit, contact_cap,
  limit_unknown) and `campaigns.resumes_at`; audit labels `campaign_settings_updated`,
  `campaign_waiting`. Migration numbering: one revision per phase that needs one, so the
  brief's four-revision sketch (0108-0111) became 0108 onwards, one per phase.
- `app/services/quiet_hours.py`: the window as pure arithmetic in the workspace's zone; the end
  is built as a local date-time and converted with that date's rules (PEP 495 `fold=0`).
- The worker asks before claiming a batch and again before every recipient; inside the window
  the campaign **waits** (`CampaignService._wait`): status unchanged (a scheduled campaign is
  not started), no attempt spent, lease given up, `next_send_at = resumes_at = window end`,
  one `campaign_waiting` audit entry and one `wasla_campaign_waits_total{reason}` per
  transition. The next batch that sends clears the wait; every transition out of the live
  statuses clears it.
- `GET/PUT /api/v1/workspace/campaign-settings` (member / admin), audited old and new;
  `quiet_hours_empty_window` for start = end; HH:MM only. A change, and a change of zone,
  makes every campaign waiting for quiet hours due now, so its next batch asks the new window.
- `CampaignRead` gains `waiting_reason`, `resumes_at` and `resumes_at_local` (the workspace's
  zone, with its offset).
- Tests: `tests/conftest.py` makes the window never open outside tests marked `quiet_hours`
  (registered marker): the suites run at any hour, and a suite run at 23:00 Cairo would
  otherwise find every campaign waiting. Every quiet-hours test carries the mark and runs the
  real check at moments it chooses (`SimClock` in the harness; the fake Meta stamps each
  request with the worker's clock).

Phase 1 defects found while building Phase 2 and fixed in this commit: the new
`test_timezones_api.py` imported the lifecycle fixtures in a way ruff flags (F811; now the
repository's file-level `# ruff: noqa: F811` convention); three `TenantRepository.create`
calls in `tests/unit/test_repositories.py` and `tests/integration/test_authorization.py`
lacked the zone (mypy); the three new `timezone` request fields were plain `str` instead of
`StorableText` (the NUL guard) - `tests/unit/test_request_text_storability.py` found it.

## Phase 1 full model-built lane (on 5450912)

`12 failed, 7047 passed, 18 skipped` in 40 min (concurrent targeted runs on the same server).
Every one of the 12 is a defect of the Phase 1 commit already fixed in 2015b7d: seven in
`test_authorization.py` and two in `test_repositories.py` (`TenantRepository.create` without
the zone), three in `test_request_text_storability.py` (the zone fields did not refuse NUL).

## Phase 3 notes (LIM-01, LIM-02)

- Meta's value -> a number, one table (`messaging_limit_service.interpret`): `TIER_50`, `TIER_250`,
  `TIER_2K`, `TIER_10K`, `TIER_100K`, `TIER_UNLIMITED`; the deprecated per-phone integers and -1.
  Anything else (`UNTIERED`, `TIER_NOT_SET`, ...) is 250, `recognised = false`, counted
  (`wasla_campaign_messaging_limit_unknown_total`).
- Migration **0110**: `whatsapp_business_portfolios` (platform-level; keyed `(key_kind, meta_key)`
  = the WABA, because Meta names no portfolio field on the WABA) and
  `whatsapp_accounts.portfolio_id` (FK NOT VALID -> VALIDATE, partial index CONCURRENTLY).
- The read: `MetaMessagingLimitReader` on **v24.0 for this one call** (`META_MESSAGING_LIMIT_API_VERSION`);
  the global `META_API_VERSION` stays v21.0. With the number's own credential (ADR-034).
- Read on connect and credential rotation (with the credential just proven) and on enable (stored
  credential) - each **before** the capacity lock; on schedule when older than
  `CAMPAIGN_MESSAGING_LIMIT_REFRESH_SECONDS` (route, before the schedule's row lock); and from the
  `business_capability_update` webhook (entry id = WABA; ordered by time - an older delivery never
  overwrites). `account_alerts` is recorded (log, `wasla_messaging_limit_alerts_total{alert_type}`)
  and changes nothing.
- Fail closed: a never-read limit refuses the schedule (422 `messaging_limit_unknown`) and makes a
  running campaign wait (`waiting_reason = limit_unknown`, re-checked every 5 minutes).
- Deploy step: `python -m scripts.read_messaging_limits` (idempotent; lists every number it could
  not read; exit 1 if any).
- Tests: `tests/conftest.py` gives unmarked tests a known, unlimited limit and an unreadable reader;
  tests marked `messaging_limit` run the real reads, rows and gates. The fake Meta answers
  `GET /{phone-number-id}?fields=whatsapp_business_manager_messaging_limit` in Meta's documented
  shape and records each read (version, field, credential).
- Intended test changes: `test_campaign_endpoints.py`'s service stub gained
  `refresh_messaging_limit` (the schedule route now calls it); `test_whatsapp_models.py` expects
  `ix_whatsapp_accounts_portfolio_id`.

## Phase 4 notes (LIM-03, LIM-04, LIM-07)

- Migration **0111**: `business_initiated_sends` (platform-level; `tenant_id` cascades; campaign and
  message set null), five indexes `CONCURRENTLY`. Its portfolio key is named explicitly
  (`fk_business_initiated_sends_portfolio_id`): the convention's name was 66 characters.
- `BusinessInitiatedLedger.reserve` in `MessagingService._dispatch`, for every WhatsApp template:
  the service window (23 h with an hour's margin) exempts; otherwise `pg_advisory_xact_lock`
  (namespace `BUSINESS_PORTFOLIO`, registered in `app/db/advisory_locks.py`), count distinct people
  of the last 24 h (no upper bound, so a racing sender's row a moment later still counts), decide,
  and write the row with the staged message in TX1; `_undelivered` deletes it (provably not
  delivered); an uncertain answer keeps it.
- Campaigns are held to `floor(CAMPAIGN_PORTFOLIO_LIMIT_SHARE x limit)` (0.80); refused, the
  campaign waits `daily_limit` until the first counted person leaves the window + 5 minutes, no
  attempt spent, and carries on by itself. Follow-ups and manual templates are recorded, never
  refused. A new limit (read or webhook) releases every `daily_limit` / `limit_unknown` wait on
  the portfolio at once.
- `GET /api/v1/whatsapp/accounts/{id}/messaging-limit` (member): tier, limit, share, budget, used in
  24 h (all sources, whole portfolio), available now, next capacity (UTC and local), shared with
  other numbers / workspaces - aggregates only.
- Retention: the existing retention sweep deletes ledger rows older than 48 hours.
- Transport-counted results (`test_campaign_messaging_limit.py`): 600 people on TIER_250 ->
  `[200, 200, 200]` per 24 h window with 1, 2 and 4 workers, max 200 in any moving window, 0 twice,
  every recipient `sent/0`; two campaigns on one number, two numbers of one portfolio, two
  workspaces on one portfolio -> `[200, 200, 200]`; 30 by follow-up and manual -> campaigns 170;
  100 open-window people sent beside 200 counted; unlimited -> 2,000 in one window; webhook
  250 -> 2,000 mid-campaign -> the rest at once; a lowered read -> 200; UNTIERED -> 200; 5 refused
  sends released, an uncertain one kept; the last place: exactly 1 winner in 40 of 40 races.
- Intended test change: `test_campaigns.py`'s messaging stub gained `at` and `campaign_id`.
