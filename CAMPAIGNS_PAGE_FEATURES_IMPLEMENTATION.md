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
| 1 | Workspace timezone, timezones API | committed | (this commit) | timezones unit + API, workspace lifecycle, route authorization, documentation truth, auth endpoints (89 passed); migration-built: schema parity, downgrade atomicity, every migration suite |
| 2 | Quiet hours | not started | | |
| 3 | Meta messaging limit: read, store, webhooks | not started | | |
| 4 | Ledger and the 80 % budget | not started | | |
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
