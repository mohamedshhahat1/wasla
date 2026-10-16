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
| 4 | Ledger and the 80 % budget | committed | b2482c7 | every campaign, WhatsApp, follow-up, template, delivery-protocol, retention and unit suite (4,087 passed, 10 failed in one stub - fixed, 103 re-run passed); limit worker tests (14 passed: 200/200/200 with 1, 2, 4 workers; 40/40 races); migration-built: parity, downgrade atomicity, limit API |
| 5 | Spreading, deadline, delivery plan, ETA | committed | fdddae1 | every campaign suite + limit suites + unit + docs (3,807 passed, 1 failed in the endpoints stub - fixed, re-run 15 passed); plan unit (11) and API/worker (5); migration-built: parity, downgrade atomicity, plan |
| 6 | Per-contact cap, pacing, 131049 | committed | 429f892 | broad send-path regression (4,221 passed, 4 failed: 3 intended test updates below, 1 PDF-deadline timing flake that passed on re-run; 100 re-run passed); contact cap + pacing (8); migration-built: parity, downgrade atomicity, cap |
| 7 | Personalisation | committed | 8b97c3d | send-path regression: unit + every campaign, WhatsApp, template and follow-up suite + documentation truth (4,052 passed, 1 failed: the documented operation count, 217 -> 218, fixed; 1 tmpfs skip); every route-walking suite (231 passed); personalisation (7 integration, 8 unit); migration-built: parity, downgrade atomicity, personalisation (16) |
| 8 | Draft lifecycle, search, idempotent create, activity | committed | a2e9c91 | unit + every campaign, WhatsApp, template, follow-up, audit, retention, metric and route-walking suite (4,313 passed, 1 failed: CPF-01, a pre-existing test-isolation defect, fixed in 3980c37); drafts (13); migration-built: parity, downgrade atomicity, drafts (22) |
| 9 | Segments, contact picker | committed | 0889109 | unit + campaign, WhatsApp, template, follow-up, contact, lead, audit, retention, metric, route-walking suites (4,396 passed, 4 failed: 2 stubs without `segment_id` - intended; 2 = CPF-02, fixed in its own commit); segments + picker (7); migration-built: parity, downgrade atomicity, segments |
| 10 | Test send | committed | (this commit) | unit + campaign, WhatsApp, template, follow-up, contact, lead, conversation, messaging, analytics, audit, retention, metric, route-walking suites (4,461 passed, 1 failed: the tie-breaker rule on the new window query - fixed); test send (5); migration-built: parity, downgrade atomicity, test send (14) |
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

## Phase 5 notes (LIM-05, LIM-06, PG-13)

- Migration **0112**: `campaigns.send_until` (CHECK after the start) and the reason code
  `send_window_closed`.
- `app/services/delivery_plan.py`: one pure function replays the worker's rules (batches of 50 at
  the rate, quiet hours in the zone, the campaign budget with places returned 24 h + 5 min after
  they were taken, per-person deferrals, the deadline) and returns days (local date, first/last
  send, people), completion, notices (`starts_after_quiet_hours`, `spreads_over_days`,
  `limit_shared_with_other_numbers`, `limit_unknown`, `contact_cap_defers`, `plan_exceeds_ttl`,
  `deadline_leaves_recipients`) and the ETA basis (`rate`, `quiet_hours`, `daily_limit`,
  `contact_cap`, `deadline`). `CampaignPlanner` gathers the facts; the schedule response, `GET
  /campaigns/{id}/delivery-plan` (admin) and the ETA all use it.
- The deadline: validated at schedule (`send_until_needs_offset`, `send_until_before_start`); at
  the next batch or recipient after it, every pending person is skipped `send_window_closed` and
  the campaign completes; a wait never sleeps past it.
- `CampaignRead` gains `send_until`(`_local`) and `eta` {completion (UTC, local), basis,
  confidence, reason}; `delivery_plan` on the schedule response. ETA from the observed rate of the
  last ten minutes once running; null with the status as reason otherwise, `limit_unknown` without
  a limit.
- Tests: `tests/integration/campaign_api.py` drives the real API on the real database, the active
  workspace resolved from a membership row it writes.
- Phase 4 defect fixed here: black's reformat of migration 0111 moved its `# noqa: S608` off the
  line it covers (ruff S608/RUF100 on b2482c7); the statement is now split so the comment sits on it.
- Intended test changes: `test_campaign_endpoints.py`'s stub `schedule` takes `send_until`, and a
  stub planner replaces the real one there (its workspace is not in the database).

## Phase 6 notes (FREQ-01, LIM-08)

- Migration **0113**: `contacts.last_marketing_sent_at`; `messages.marketing_cap_claimed_at`,
  `marketing_cap_previous_at`, `provider_held_at`; stop reason `meta_quality_review`; reason code
  `contact_frequency_cap`.
- The claim (`app/services/contact_cap.py`): one conditional `UPDATE contacts ... WHERE
  last_marketing_sent_at IS NULL OR <= at - interval RETURNING (previous)` in the send intent's
  transaction, after the ledger's decision; held to marketing templates from a campaign or a
  person (`CAPPED_ORIGINS`); not a test send, a follow-up, an agent, a utility or authentication
  template. Without an idempotency key (a campaign) it comes before anything is staged; with one
  (a person) after the replay check, so a retried request returns its original message.
  Released when the send is provably undelivered (if nobody claimed since); kept when uncertain.
- Campaign: a capped person is deferred to their moment + 5 minutes, no attempt spent; skipped
  `contact_frequency_cap` if the deadline comes first; when only capped people remain the
  campaign waits `contact_cap`. A person's manual marketing template inside the cap: 409
  `contact_frequency_cap` with `details.available_at`.
- LIM-08: Meta's `message_status: held_for_quality_assessment` in the send answer marks the
  message `provider_held_at` (the recipient is sent; `statistics.held` counts it); a `failed` status
  with 135000 on a campaign message pauses that campaign `meta_quality_review` (audited by the
  system) without failing the others; 131049 (at send or by status) pushes the person's marketing
  claim to now, so nothing of this workspace asks for 24 hours.
- Intended test changes: `test_campaign_endpoints.py` expects `held: 0` in statistics;
  `test_campaign_header_media.py`'s two campaigns sharing a file reach the same five people a day
  apart (FREQ-01); `test_payment_methods_are_configuration.py` allowlists Meta's 135000 and 131049
  in `provider_feedback.py`.

## Phase 7 notes (PER-01 .. PER-05)

- Migration **0114**: `campaign_recipients.rendered_parameters` (JSONB, nullable, catalogue-only).
  Downgrade refuses while any recipient carries values.
- `app/services/personalisation.py`: the closed source vocabulary (`constant`,
  `contact.display_name`, `lead.name`, `lead.interest`, `lead.status` as its label, `lead.budget` as
  amount + currency, `lead.custom_fields` with a key); the phone number is not a source. Normalisation
  (PER-05: newline, CR, tab -> space; runs of spaces -> one; control characters removed; trimmed) -
  Meta documents no rule of its own for parameter values. A value missing, empty after normalisation
  or longer than `CAMPAIGN_PARAMETER_MAX_CHARS` (60) is the fallback; a person whose values carry the
  rendered body past Meta's 1,024 characters gets every fallback and the row records `body_too_long`.
- Composition: `POST /campaigns` takes `body_parameters` / `header_parameters` (one entry per
  placeholder) in place of `variables` / `header_variables` (both -> 422 `parameters_conflict`).
  Every non-constant source needs a fallback (`parameter_fallback_required`, at most 60 characters:
  `parameter_fallback_too_long`); a custom field must exist on a lead of the workspace
  (`parameter_key_unknown`); a button's source other than `constant` -> `parameter_source_not_allowed`.
  One validator (`CampaignService._composition`) - the draft edit of Phase 8 reuses it. The send's
  count check runs on the fallbacks, so a mapping of the wrong length is refused as a constant list is.
- Frozen at materialisation (PER-02): `set_audience` resolves each person's values from the contact
  and the active lead (the `uq_leads_active_contact` predicate: not won, not lost) in one read and
  writes them with the contact id they were resolved for. The send uses exactly those; a row whose
  values name another contact is skipped `identity_changed` rather than sent a stranger's values
  (CR-15's runtime guard; the invariant itself is Phase 13).
- `POST /campaigns/{id}/preview-messages` (admin): up to five contacts; the written values once the
  audience exists, otherwise resolved now for people of the eligible population; anybody else 404.
  Header text, body, buttons (a URL with its constant suffix) and the fallback positions.
- Privacy (CMQ-08, still open): each recipient row now carries a copy of the values sent to that
  person (a name, an interest). Retention of recipient rows covers it.
- Fixed while building: a URL suffix was substituted with `re.sub` and a string replacement (a
  backslash in the value would have been read as a group reference); now a function.
- `docs/API.md` states 218 operations; `docs/AUTHORIZATION.md` lists `preview-messages` (ADMIN).
- Intended test changes: none in Phase 7.
- To do in Phase 13: the migrations' `RUNBOOK` pointer names "Campaign page features (0108-0113)";
  the RUNBOOK section will carry the final range and the pointers are updated with it.

## Phase 8 notes (PG-01 .. PG-05, PG-08)

- Migration **0115**: seven `audit_action` values (`campaign_updated`, `_duplicated`, `_deleted`,
  `_completed`, `_failed`, `_test_sent`, `_recipients_exported` - the last two used by Phases 10 and
  11); `uq_campaigns_tenant_id_id` built `CONCURRENTLY` (the target of ADR-100's tenant-composite
  key); `campaigns.version` (integer, constant default 1, catalogue-only); `campaign_create_keys`
  (tenant, key, the draft, the request's sha256, created_at; `UNIQUE(tenant_id, key)`,
  `UNIQUE(tenant_id, id)`, `(tenant_id, campaign_id)` -> campaigns cascading).
- The audience rules were already stored: `campaigns.audience` has kept the filter a list was written
  from since 0004 (`last_inbound_within_days`, `lead_statuses`, `contact_ids` up to the 1,000 cap).
  No `audience_rules` column was added; duplicate copies `audience`.
- PG-01: `GET /campaigns?q=` (at most 100 characters), case-insensitive "contains" on the name with
  `%`, `_` and `\` escaped (`escape_like`, `app/repositories/base.py`), combined with status, number
  and cursor. Measured at 5,000 campaigns in one workspace (`audit/p22_search_plan.py`): an index
  scan of `ix_campaigns_tenant_id_created_at` with the name as a filter; p95 2.7 ms for a common
  term, 5.6 ms for a term matching nothing (every row filtered). Not SCALE-TRIGGERED; no extension.
- PG-02: `POST /campaigns/{id}/duplicate` (admin, any status): a new draft "<name> (copy)" with the
  description, number, template, values and mappings, header file, rate and audience rules; never
  the written list, schedule, results, stop reasons, Meta's media id, lease or counters. Validated by
  the one validator: a withdrawn template is refused with create's code. Audited
  `campaign_duplicated` (target the copy, `source_id`); counted `wasla_campaign_events_total{event=
  "duplicated"}`.
- PG-03: `PATCH /campaigns/{id}` (admin, drafts only - 409 `campaign_not_editable`): name,
  description, number, template, values (constants or mappings), header file, rate. The version is
  `If-Match` (`3`, `"3"`, `W/"3"`) or `version`; missing -> 422 `campaign_version_required`;
  different -> 409 `campaign_version_conflict` with `details.version`. A change to the number,
  template, values or header file deletes the written list (`audience_cleared: true`), keeps the
  rules. Unknown fields (`status`) -> 422. Audited `campaign_updated` with field names only;
  response `ETag: "<version>"`. Writing the audience also bumps the version.
- PG-04: `DELETE /campaigns/{id}` (admin) -> 204; anything but a draft, or any recipient ever
  attempted, sent or linked to a message -> 409 `campaign_not_deletable`. The header file stays.
  Audited `campaign_deleted` (id, name, how many list rows went).
- PG-05: `GET /campaigns/{id}/activity` (member), keyset-paged on the audit trail by
  `(occurred_at, id)` - everything targeting the campaign plus `campaign_duplicated` entries naming
  it as the source. Closed vocabulary (`app/services/campaign_activity.py`): created, updated,
  duplicated_from / duplicated_to, audience_set, scheduled, resumed (a schedule from paused or
  failed, now recorded as `resumed` in the entry), paused, cancelled, deleted, waiting, completed,
  failed, test_sent, recipients_exported. Each action shows a whitelisted set of metadata (field
  names, counts, reasons, times; audience filters with hand-picked people as a count). The worker now
  audits `campaign_completed` and `campaign_failed` with the system as actor, once per transition.
- PG-08: `POST /campaigns` takes `Idempotency-Key` (1-128 characters): the same key in the workspace
  within 24 hours returns the draft it made; reused for a different request (sha256 of the canonical
  body) -> 409 `idempotency_key_reused`. The draft and its key are written in one savepoint; a
  concurrent retry's insert waits on the first's and then returns its draft (4 concurrent retries ->
  1 draft). A key older than a day is forgotten by the next create that uses it, and deleted by the
  existing retention sweep (`RetentionWorker.prune_campaign_create_keys`). `CampaignRead` gains
  `version` and `audience_ready`; a draft whose audience step failed is listed with
  `audience_ready: false`.
- Intended test changes: `test_campaign_endpoints.py::test_there_is_no_way_to_set_a_status_directly`
  now expects 422 (PATCH edits drafts; `status` is not a field) instead of 405, and its stub
  `list_campaigns` takes `name_contains`; `test_campaign_header_media_api.py::
  test_the_header_file_cannot_be_changed_after_composition` became
  `test_the_header_file_changes_only_through_a_draft_s_versioned_edit` (405 -> 422 without a version,
  200 with one).
- `docs/API.md` states 222 operations; `docs/AUTHORIZATION.md` lists PATCH, DELETE (ADMIN),
  duplicate (ADMIN) and activity (MEMBER).
- How Phase 7 was committed: Phase 8 edits had begun in five files Phase 7 also changed before the
  Phase 7 regression finished. The Phase 7 versions of those files were rebuilt (Phase 8's scripted
  edits reversed, the rest from HEAD plus Phase 7's own hunks), assembled with the Phase-7-only files
  in a scratch worktree, checked there (ruff, black, mypy, 30 personalisation and endpoint tests,
  231 route-walking tests) and committed; the branch was fast-forwarded to it and the feature
  worktree's index refreshed (`git reset`, files untouched). The regression of 4,052 tests ran on the
  Phase 7 code itself (imported before any Phase 8 edit).

## Findings of this stage (CPF)

- **CPF-01** (test isolation, pre-existing): `test_whatsapp_ownership.py::
  test_concurrent_claims_produce_one_winner_and_one_conflict` deleted its two workspaces but not the
  winner's `whatsapp_account_connected` audit entry. `audit_logs.tenant_id` is `SET NULL`, so the
  entry stayed as a platform entry, and `test_audit_trail.py::
  test_the_platform_reader_sees_every_workspace_and_the_platform` counted 4 instead of 3 whenever the
  ownership suite ran first. The full lane runs them alphabetically (audit first) and never met it;
  this stage's targeted regression did. Fixed in 3980c37 (the cleanup deletes the entries first).
- **CPF-02** (test defect of this stage's Phase 5): `test_campaign_delivery_plan.py` marked the whole
  module `quiet_hours`, so its two tests on the real clock (the deadline worker test and the ETA
  test) ran the real window and failed between 22:00 and 09:00 Cairo - found when the Phase 9
  regression ran at 22:25. The marker now sits on the two tests that schedule at chosen Cairo times.

## Phase 9 notes (PG-06, PG-07)

- Migration **0116**: `campaign_segments` (tenant, name - unique per workspace, and the service also
  refuses a name differing only in case - description, `rules` JSONB with
  `ck_campaign_segments_rules_have_no_contacts`, optional number of the same workspace via
  `fk_campaign_segments_tenant_account`, `created_by_id`, `version`, `UNIQUE(tenant_id, id)`);
  `campaigns.audience_segment_id` with `fk_campaigns_tenant_audience_segment` (`NOT VALID` ->
  `VALIDATE`, no action on delete); indexes `CONCURRENTLY`: `ix_campaigns_audience_segment_id`
  (partial), `ix_conversations_tenant_id_account_id_last_inbound_at`,
  `ix_contacts_tenant_id_last_seen_at`. Downgrade refuses while a segment exists or a campaign
  names one.
- Segments: `GET /campaign-segments`, `GET /campaign-segments/{id}` (member); `POST`, `PATCH`
  (If-Match or `version`; 409 `segment_version_conflict`, 422 `segment_version_required`), `DELETE`
  (admin). In the campaign rate bucket with the campaigns page. Rules are `last_inbound_within_days`
  and `lead_statuses`; anything else (`contact_ids`) is 422. At most 100 per workspace, counted and
  inserted under a `CAMPAIGN_SEGMENTS` advisory lock (registered in `app/db/advisory_locks.py`): 3
  concurrent creates for the 100th place -> one 201, two 409 `segment_limit_reached`. A name taken
  -> 409 `segment_name_taken`. Segments are not audited (no campaign is changed by one).
- Using one: `POST /campaigns/audience/preview` and `POST /campaigns/{id}/audience` take
  `segment_id`. The segment's rules are narrowed by the filters sent with it (the shorter recency
  window; lead statuses intersected - an empty intersection reaches nobody, recorded as
  `audience.nobody = true`; hand-picked contacts narrow further) and copied onto the campaign's
  `audience` with `audience_segment_id`. Editing or deleting the segment changes no campaign;
  deleting it clears `audience_segment_id` first and the copied rules stay. A segment meant for
  another number -> 422 `segment_account_mismatch`; another workspace's -> 404. Duplicate copies the
  reference. The audience audit entry and the activity log carry `segment_id`.
- Contact picker: `GET /contacts` (member, workspace rate bucket - the existing contacts router), at
  most 50 per page, keyset `(last_inbound_at DESC NULLS LAST, id DESC)`. With `account_id`: contacts
  with a conversation on that number, `last_inbound_at` of that conversation, `opted_out_at` on its
  channel, `eligible_for_campaign` and `ineligible_reason` (`opted_out`; everybody listed has a
  conversation on the number); `eligible_for_campaign=true` keeps only the base population. Without
  `account_id`: every contact by `last_seen_at`, eligibility null; `eligible_for_campaign=true`
  without a number -> 422 `account_required`. `q`: digits (an optional `+`, spaces ignored) search
  the WhatsApp number, anything else the name or the active lead's name; wildcards literal. Another
  workspace's number -> 404.
- Measured at 100,000 contacts in one workspace (`audit/p23_picker_plan.py`, 10 % opted out, a
  third with a lead), p95: first page on a number 6.3 ms; eligible only 6.7 ms; name search 8.7 ms;
  digits 83 ms (a substring of the number cannot use a b-tree; no extension added); without a
  number 4.2 ms, name search 15.7 ms. Not SCALE-TRIGGERED.
- Intended test changes: `test_campaign_endpoints.py`'s stub `preview_audience` and `set_audience`
  take `segment_id`.
- `docs/API.md` states 228 operations; `docs/AUTHORIZATION.md` lists the five segment routes and
  `GET /contacts`.
- Two defects found by the measurement and fixed before commit: (1) after five executions
  PostgreSQL switched the prepared statement to a generic plan and a name search went from 15 ms to
  5.3 s - the picker now sets `plan_cache_mode = force_custom_plan` for its transaction, the
  knowledge search's documented remedy (docs/RAG.md); (2) "eligible only" filtered the outer-joined
  consent's null, and the planner hashed every contact of the number (153 ms) - it now uses the
  audience's own `refused()` anti-join (1 ms), with the displayed consent aliased so the subquery is
  not correlated away.

## Phase 10 notes (PG-09)

- Migration **0117**: `message_origin` gains `campaign_test`; `campaign_test_sends` (tenant, campaign,
  contact - tenant-composite keys cascading -, message and sender set null, created_at;
  `UNIQUE(tenant_id, id)`). The table is what the daily allowance counts and what links the test
  message to its campaign; messages carry no campaign column. Downgrade refuses while a test or a
  `campaign_test` message exists (an enum value cannot be dropped and stays).
- `POST /campaigns/{id}/test-send` (admin, `{contact_id}`) -> 201 with the message. Any status but
  cancelled (409 `campaign_not_testable`). The contact must be of this workspace (404 otherwise) and
  already have a conversation with the campaign's number (422 `contact_has_no_conversation` -
  ADR-025: the admin writes to the business first), not opted out on its channel (422
  `contact_opted_out`).
- One send path: the composition is validated as for a send, the one component builder
  (`send_components`) gets the campaign's parameters with this contact's own values resolved now
  (`_renderer`, `_facts`), and `MessagingService.send_template` sends it with origin
  `campaign_test` through the same client.
- Header file: the campaign's Meta id is reused only while fresh and uploaded through the
  campaign's number; otherwise the stored file is read and uploaded for the test - before any lock
  (ADR-093) - and the id is never written onto the campaign.
- Not a recipient: no list row, no statistics, not in `CAPPED_ORIGINS` (the contact's
  `last_marketing_sent_at` is untouched), not checked against quiet hours (only the worker asks).
  In Meta's limit: `LEDGER_SOURCES[campaign_test] = campaign_test`, enforcement `whole_limit` -
  refused only when the portfolio has nothing left at all (409 `portfolio_limit_reached` with
  `details.available_at`); a never-read limit -> 422 `messaging_limit_unknown`.
- `CAMPAIGN_TEST_SENDS_PER_DAY` (10) per workspace in a moving 24 hours, counted and recorded under
  a `CAMPAIGN_TEST_SENDS` advisory lock; the 11th -> 429 `test_send_limit` with `details.reset_at`
  (the oldest test of the window + 24 h).
- Audited `campaign_test_sent` (contact id in the entry; the activity log shows no contact);
  `wasla_campaign_events_total{event="test_sent"}`.
- Tests (`test_campaign_test_send.py`, 5): the transport request carries the contact's own name and
  the image header (one upload, the campaign's media id still null); statistics and the list
  unchanged; the cap untouched; the ledger row `campaign_test`; quiet the whole day and the contact
  capped a minute ago -> still sent; 10 then 429; opted out, no conversation, another workspace,
  cancelled, member; 240 of 250 used -> sent, 251 used -> a new person refused 409.
- `tests/integration/campaign_api.py::client_for` takes `overrides` (the messaging service on the
  fake Meta, a test media store).
- `docs/API.md` states 229 operations; `docs/AUTHORIZATION.md` lists `test-send` (ADMIN).
