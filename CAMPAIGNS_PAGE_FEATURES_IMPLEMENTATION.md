# Campaigns page features and Meta sending limits — implementation

Stage branch `campaigns-page-features-20261016`, worktree `E:\wasla-campaign-features`, based on
`worktree-billing-google-auth` at `752a9ba` (= origin). Nothing in this stage was pushed, merged or
deployed; `main` was not touched; no real provider was contacted.

## 1. Executive Summary

The campaigns line ended at `752a9ba` with every send guarantee proven but four things the brief
calls out left open: Meta's business-portfolio messaging limit was neither read nor enforced
(CMP-17 / EXT-CMP-01, CMQ-06), no per-customer frequency cap (CMQ-03), no workspace time zone or
quiet hours (CMQ-04 / CMP-15), no per-recipient personalisation (BE-FE-BLOCK-031), and fifteen
"new" elements of the campaigns page design had no endpoint.

This stage closes all of it, in 13 migrations (0108–0120) and 29 commits on the stage branch:

- **Part 1 — Meta's limits.** Every workspace has an IANA time zone chosen at registration
  (`GET /timezones`, `tzdata` pinned); campaigns keep quiet hours (22:00–09:00 by default) in that
  zone, per batch and per recipient, across DST, stopping a minute before the window. A number learns its portfolio's messaging limit
  from Meta (`whatsapp_business_manager_messaging_limit`, v24.0 for that one call) on connect,
  rotation, enable, schedule and the `business_capability_update` webhook, and fails closed while it
  is unknown. Every business-initiated template is written to one ledger under the portfolio's
  advisory lock before Meta is called; campaigns stop at 80 % of the limit and resume by themselves
  when the oldest counted person leaves the moving 24-hour window. One marketing message per
  customer per 24 hours per workspace is claimed atomically with the send. 135000 pauses a campaign
  `meta_quality_review`; 131049 pushes the person's cap.
- **Part 2 — the page.** Personalisation from the contact and lead (frozen with the audience, with
  fallbacks), draft edit / delete / duplicate with optimistic versions, search, idempotent create,
  activity log, saved segments, the contact picker, test send, replies / opt-outs after / reasons,
  KPI summary, live progress with ETag, ETA, CSV export, delivery plan with an optional deadline,
  the messaging-limit counter, rate cards and a cost estimate.
- **Part 3 — the contract.** `docs/CAMPAIGNS_PAGE_CONTRACT.md` maps all 21 elements to routes,
  bodies, roles, buckets, states, Arabic and English wording, and what the page must never claim.

The decisive numbers, counted at the fake Meta's transport (never from Wasla's own rows): a
600-person campaign on a portfolio of 250 sends **200 / 200 / 200** in three successive 24-hour
windows with 1, 2 and 4 workers, **0 people twice, 0 attempts spent by waits**; two campaigns, two
numbers of one portfolio and two workspaces on one portfolio together stay at 200 per window;
**0 sends inside quiet hours** across Cairo's April and October transitions; **0 people with two
marketing messages in a day**; **0 payloads carrying another person's value**. APP-E2E (the real
API under uvicorn, two real workers) ran the brief's final question end to end: scheduled at 21:30
Cairo, 150 sent before 22:00, nothing until 09:00, at most 200 people in any 24 hours, 290 delivered
over two days, each with their own name, 20 replies attributed, every member-forbidden action 403.

Eight findings (CPF-01..CPF-08) were found while building and verifying, and fixed — the most
important three by this stage's own verification: **CPF-04** (the populated upgrade: the ledger and
the per-customer claim started empty, so the 24 hours after a deploy could not see the sends just
before it — migration 0120 backfills both), **CPF-07** (the full lanes: four new tables unclassified
for the workspace purge) and **CPF-08** (the migration-built lane under load: a send decided just
before 22:00 reached Meta after it — campaigns now stop a minute early).

Verdict (section 28): **CAMPAIGNS PAGE FEATURES IMPLEMENTED WITH EXTERNAL VERIFICATION REMAINING**.

### Findings of this stage (CPF)

| ID | Kind | Found by | Fixed in |
|---|---|---|---|
| CPF-01 | pre-existing test isolation: `test_whatsapp_ownership` left a platform audit entry that `test_audit_trail` counted when run after it | Phase 8 targeted regression | `3980c37` |
| CPF-02 | this stage's test: the delivery-plan module marked `quiet_hours` whole, so two real-clock tests failed at night in Cairo | Phase 9 regression at 22:25 | `fcf6034` |
| CPF-03 | this stage's code: the rate-card parser joined Meta's two-line heading only for `\n`, not `\r\n` (an Excel CSV on Windows) | git's CRLF warning on the fixture | `f0259bf` |
| CPF-04 | this stage's migrations: the ledger and the per-customer claim started empty, so the 24 h after a deploy could not see the sends just before it (CR-11 = 1 after the populated upgrade) | populated upgrade | `4342f7b` (migration 0120) |
| CPF-05 | this stage's harness: `SimClock` dated requests made before its first sweep "now + machine uptime" | a mixed-sources limit test failing once the machine had been up 24 h | `335c748` |
| CPF-06 | test gap: the cap's single-statement claim was never raced without the portfolio lock (M-F10 survived) | mutation matrix | `7f98e9f` |
| CPF-07 | this stage's code: four tenant-scoped tables in neither the purged nor the retained list | full lanes at `7f98e9f` | `e0f71ca` |
| CPF-08 | this stage's code: a campaign send decided just before 22:00 could reach Meta after it under load | migration-built lane at `e0f71ca` (2 requests after 22:00) | `b385527` (`SEND_LEAD`) |

CR-11 was also refined (`2160ee2`, section 13). Every fix has a test that fails without it.

## 2. Starting Repository State

```text
recorded 2026-10-16, before any change
canonical          worktree-billing-google-auth at 752a9ba = origin/worktree-billing-google-auth
                   0 ahead / 0 behind after git fetch --prune
752a9ba vs 450333e git diff --stat: CAMPAIGNS_CANONICAL_MERGE_VERIFICATION.md only (documentation;
                   CI run 38827475623 green on 450333e covers every line of code)
BASE_HEAD          752a9ba
BASE_MIGRATION     0107 (alembic heads: single)
last ADR           ADR-144
Graph API version  v21.0 (Settings.meta_api_version); unchanged by this stage
webhook fields     messages (with statuses), user_preferences, message_template_status_update
                   (app/integrations/whatsapp/payload.py SUPPORTED_FIELDS); this stage adds
                   business_capability_update and account_alerts
Python             3.12.7
Docker             29.8.0, Compose v5.5.1
PostgreSQL         16.15 (pgvector/pgvector:pg16)
Redis              7.4.11 (redis:7-alpine)
app.__file__       E:\wasla-campaign-features\app\__init__.py (every lane asserted its own)
```

Worktrees of this stage: `E:\wasla-campaign-features` (the branch), `E:\wasla-cpf-base` (detached at
`752a9ba`: before-probes, the populated upgrade's stage A), `E:\wasla-cpf-lane` (detached, full
lanes), `E:\wasla-cpf-mut` (detached, mutation). Probes copied from the header media stage into
`audit/` (untracked). Containers, all this stage's own:

```text
wasla-cpf-pg      127.0.0.1:55951   model-built lane, probes, targeted runs, mutation
wasla-cpf-pg2     127.0.0.1:55952   migration-built lane, APP-E2E
wasla-cpf-redis   127.0.0.1:56951
wasla-cpf-redis2  127.0.0.1:56952   TEST_REDIS_URL (Google OAuth races)
wasla-cpf-redis3  127.0.0.1:56953   second lane, APP-E2E (DB 3)
wasla-cpf-minio   127.0.0.1:59951   bucket wasla-media
```

The earlier stages' containers were no longer running; nobody else's container was touched.
The 11 untracked documents in `E:\wasla` and `docs/ui/` were read (the mockup), never modified.

## 3. Meta's Documentation

Read 2026-10-16 from developers.facebook.com/documentation/business-messaging/whatsapp/ (index
`llms.txt`); each value is cited by URL in its fixture, ADR and code comment.

| Fact | Value used | Page |
|---|---|---|
| Limit | unique WhatsApp user numbers delivered to outside a customer service window, in a moving 24 h, per **business portfolio** | `/messaging-limits` |
| Levels | 250, 2,000, 10,000, 100,000, unlimited | `/messaging-limits` |
| Portfolio scope | changelog 2025-10-08: "Messaging limits are now business portfolio-based" | `/changelog` |
| Read | `whatsapp_business_manager_messaging_limit` on the phone number (also WABA, portfolio), v24.0+; `messaging_limit_tier` deprecated | `/messaging-limits`, WABA and phone number references |
| Values | `TIER_50`, `TIER_250`, `TIER_2K`, `TIER_10K`, `TIER_100K`, `TIER_UNLIMITED`, `UNTIERED` | WABA reference |
| Change webhook | `business_capability_update`: `max_daily_conversations_per_business` (v24.0+), `max_daily_conversation_per_phone` (≤ v23.0, integers, -1 unlimited) | webhook field reference |
| Denied increase | `account_alerts`, `alert_type` `INCREASED_CAPABILITIES_ELIGIBILITY_DEFERRED` / `_FAILED` / `_NEED_MORE_INFO` | `/messaging-limits` |
| Scaling | automatic when quality is high and ≥ half the limit used in 7 days; next level within 6 h | `/messaging-limits` |
| Throughput | 80 mps default, 1,000 upgraded; 130429 | `/throughput` |
| Per-user marketing | adaptive, no number; 131049; "wait at least 24 hours"; not in the EEA, UK, Japan, South Korea | `/templates/marketing-templates/per-user-limits` |
| Portfolio pacing | send answer `message_status: held_for_quality_assessment`; dropped → status `failed` 135000; no sends or template creation during review | `/templates/portfolio-pacing`, `/templates/template-pacing` |
| Components | body ≤ 1,024; text header ≤ 60 with 1 parameter; footer ≤ 60; ≤ 10 buttons; ≤ 2 URL; 1 phone; 1 copy code (≤ 20); label ≤ 25 | `/templates/components` |
| Time-to-live | marketing 43,200 .. 2,592,000 s, default 30 days, `message_send_ttl_seconds` | `/templates/time-to-live` |
| Pricing | per delivered template, by category and the recipient's calling-code market; rate cards in 16 currencies; marketing has no volume tiers | `/pricing`, business.whatsapp.com/products/platform-pricing#rates |
| Opt-in | collect opt-in; ADR-025's "wrote first" unchanged (CMQ-07 open) | `/getting-opt-in` |

**Differences from the brief's section 2.1.** The WABA enum also documents `TIER_50` and
`UNTIERED` (`TIER_50` maps to 50; `UNTIERED` is unrecognised → 250, counted and alerted).
`business_capability_update`'s per-business field is typed Integer with `TIER_*` values and an
integer example (both are read). The rate card effective 2026-10-01 prices Bangladesh, Iraq,
Kazakhstan, Kuwait, Morocco, Nepal, Oman, Sri Lanka and Ukraine as standalone markets. Meta's
"Rates (CSV)" link serves an **XLSX** workbook (`usd_list_rates`, 20,545 bytes, sha256
`3e996b56a86d9e7f70a9cfded896c8f39a19cb0e88683ed06b28f191856c9260`); the parser takes it saved as
CSV UTF-8, and the fixture is that file. None of these contradicts a locked decision.

**Not documented, handled conservatively:**

| Gap | Handling |
|---|---|
| What Meta does past the portfolio limit (no code; 131048 is documented as a phone-number restriction) | Wasla never relies on a refusal: it stops itself at 80 % (LIM-04). EXTERNAL VERIFICATION. |
| The per-user marketing number | FREQ-01 (1 per 24 h per workspace) is stricter than any plausible value; 131049 stays terminal per person and pushes the cap. |
| The pair-rate number | 131056 keeps the remediation's per-person deferral. |
| A price API | rate cards uploaded by platform staff (COST-01). |
| Which business portfolio owns a WABA | **no field on the WABA node**: the ledger keys on the WABA (LIM-02's fallback, `key_kind = waba`); two WABAs of one portfolio are counted apart — EXTERNAL VERIFICATION, recorded in docs/WHATSAPP.md and docs/CAMPAIGNS.md "Known limits". |
| Whitespace rules for parameter values | none on the components or error-code pages; PER-05's normalisation applies. |
| Calling code +7 (Russia and Kazakhstan) | priced as Russia, the higher marketing rate (0.0802 vs 0.0604): the estimate stays an upper bound. |

## 4. Decisions Applied

Every locked decision of the brief's section 11 is applied as written, with these recorded
specifics:

| ID | Applied as |
|---|---|
| TZ-01 | `tenants.timezone` NOT NULL (0108, backfilled `Africa/Cairo` in keyset batches, validated CHECK, no table rewrite). Required on `POST /workspaces` and `POST /auth/register` (422 `timezone_unknown` / field required); `PATCH /workspace` owner or admin, audited `workspace_updated` with the old and new zone only; `GET /workspace` returns it and its current offset. Validation from the `tzdata` package's own files (`tzdata==2026.5`, IANA 2026e, `ZoneInfo.from_file`), identical on Windows and Linux. 189 test files changed to pass a zone through one helper (`tests/workspace_timezone.py`). Changing the zone never moves `scheduled_at`. |
| TZ-02 | `GET /api/v1/timezones` public, per-IP bucket, `Cache-Control: public, max-age=3600`, ETag over the tzdata release, every offset and the filter; 418 canonical zones (zone.tab ∪ zone1970.tab); the 16 suggested zones of the brief first; aliases (`US/Eastern`, `Egypt`, `Etc/*`, `UTC`, `Asia/Calcutta`) refused. A test proves every listed id is accepted by workspace creation. |
| TZ-03 | Quiet hours on the workspace (0109): on, 22:00–09:00, minute precision, `quiet_hours_empty_window`. Checked per batch and per recipient; a wait keeps the status, spends no attempt, gives up the lease, `resumes_at` = the window's end built as a local time (DST aware). Binds campaign sends only; not test sends, replies or follow-ups. Changing the window or zone releases waiting campaigns at the next batch. |
| TZ-04 | A schedule inside the window is accepted; the plan says `starts_after_quiet_hours`. |
| LIM-01 | Read on connect, rotation, enable, stale schedule (`CAMPAIGN_MESSAGING_LIMIT_REFRESH_SECONDS` 21,600) and the webhook; **v24.0 for this one call** (`META_MESSAGING_LIMIT_API_VERSION`), the global version stays v21.0. One mapping table; unrecognised → 250, counted, alerted. Never read → 422 `messaging_limit_unknown`; a running campaign waits `limit_unknown`. A failed read keeps the last value. |
| LIM-02 | Portfolio keyed on the WABA (no documented portfolio field; see section 3). |
| LIM-03 | `business_initiated_sends` (0111, platform-level): one row per template outside the 23-hour window (one hour of margin), every path (campaign, test, manual, follow-up, other), under `pg_advisory_xact_lock(portfolio)` in the send intent's transaction; released only when provably undelivered; pruned after 48 h by the existing retention sweep. |
| LIM-04 | `CAMPAIGN_PORTFOLIO_LIMIT_SHARE` 0.80; at the budget the campaign waits `daily_limit` until the oldest counted person + 24 h + 5 min. Non-campaign paths are recorded, not refused (open decision, section 27). |
| LIM-05 | One pure function (`app/services/delivery_plan.py`) for the schedule response, `GET /campaigns/{id}/delivery-plan` and the ETA; notices as the brief lists plus `deadline_leaves_recipients`. |
| LIM-06 | `send_until` (0112); pending at the deadline → skipped `send_window_closed`, the campaign completes. |
| LIM-07 | `GET /whatsapp/accounts/{id}/messaging-limit` (member): aggregates only; `shared_with_other_workspaces` is a boolean, nothing else of another workspace. |
| LIM-08 | `held_for_quality_assessment` → recipient sent, `messages.provider_held_at`, counted `held`; 135000 → that campaign paused `meta_quality_review` (audited by the system); 131049 → terminal for the person, the cap pushed to now. |
| FREQ-01 | `contacts.last_marketing_sent_at` (0113), one conditional UPDATE in the send intent's transaction; capped campaign recipients deferred (`not_before`), no attempt; `contact_cap` wait when nobody else can go; manual MARKETING → 409 `contact_frequency_cap` with `available_at`; released when provably undelivered. `CAMPAIGN_CONTACT_MARKETING_INTERVAL_SECONDS` 86,400. |
| PER-01..05 | Closed source vocabulary from the columns that exist (`contact.display_name`, `lead.name`, `lead.interest`, `lead.status`, `lead.budget`, `lead.custom_fields.<key>`, `constant`); wa_id not a source; buttons constant only; fallback required; values frozen on `campaign_recipients.rendered_parameters` (0114) with the contact id they were resolved for; normalisation; ≤ 60 characters; a body over 1,024 → every fallback (`body_too_long`); `preview-messages` for ≤ 5. |
| PG-01..14 | As section 11. The audience rules were already stored on `campaigns.audience` since 0004, so no `audience_rules` column was added. |
| COST-01..02 | As section 12. The calling-code map is a code constant copied onto each card at upload (correcting it never reprices an uploaded card), not seed data in a migration. |

Migration numbering: the brief sketched four revisions; the stage wrote one per phase that needed
one (0108–0119) plus the CPF-04 data fix (0120).

## 5. Migrations 0108..0120

| Revision | Adds | Downgrade |
|---|---|---|
| 0108 | `tenants.timezone` (nullable → batch backfill `Africa/Cairo` → validated CHECK) | refuses while any workspace has another zone |
| 0109 | quiet-hours columns on `tenants` (constant defaults); `campaigns.waiting_reason`, `resumes_at`; audit actions `campaign_settings_updated`, `campaign_waiting` | refuses while any workspace's quiet hours differ from the defaults, any campaign waits or carries `resumes_at`, or an audit entry uses a new action |
| 0110 | `whatsapp_business_portfolios`; `whatsapp_accounts.portfolio_id` (FK NOT VALID → VALIDATE, index CONCURRENTLY) | refuses while any portfolio has been read or any number is linked to one |
| 0111 | `business_initiated_sends`, five indexes CONCURRENTLY | refuses while any row exists |
| 0112 | `campaigns.send_until`; reason `send_window_closed` | refuses while any campaign has a deadline or any recipient was skipped `send_window_closed` |
| 0113 | `contacts.last_marketing_sent_at`; `messages.marketing_cap_claimed_at`, `marketing_cap_previous_at`, `provider_held_at`; stop reason `meta_quality_review`; reason `contact_frequency_cap` | refuses while any claim, held mark, `meta_quality_review` stop or `contact_frequency_cap` skip exists |
| 0114 | `campaign_recipients.rendered_parameters` | refuses while any is set |
| 0115 | `campaigns.version`, `uq_campaigns_tenant_id_id` CONCURRENTLY, `campaign_create_keys`; seven audit actions | refuses while any campaign was edited (`version > 1`), any create key exists, or an audit entry uses a new action |
| 0116 | `campaign_segments`, `campaigns.audience_segment_id`; picker indexes CONCURRENTLY | refuses while a segment exists or a campaign names one |
| 0117 | `message_origin` + `campaign_test`; `campaign_test_sends` | refuses while a test or a `campaign_test` message exists (the enum label stays) |
| 0118 | `campaign_recipients.replied_at`, `opted_out_after_at`, index CONCURRENTLY | refuses while any recipient records a reply or an opt-out after |
| 0119 | `whatsapp_rate_cards`, rates, markets; audit actions `rate_card_uploaded`, `rate_card_activated` | refuses while a card exists or an audit entry uses a new action |
| 0120 | data only (CPF-04): the last 24 hours' marketing claims and ledger rows | changes nothing (0111's and 0113's refuse) |

All additive and online, written like 0101–0107: new constraints on existing tables NOT VALID then
validated under a 15 s `lock_timeout`; enum labels and indexes in autocommit blocks with an INVALID
leftover dropped first; no table rewrite. New tenant tables carry `UNIQUE(tenant_id, id)` and
tenant-composite foreign keys (ADR-100). `whatsapp_business_portfolios`,
`business_initiated_sends` and the rate-card tables are platform-level on purpose: a portfolio and
its count cross workspaces (LIM-02), and a rate card is Meta's price list for everybody. The README
range moved with each revision; `test_documentation_truth` was green at every commit.

## 6. Timezone, the Timezones API and Quiet Hours

- `app/services/timezones.py`: the accepted names are tzdata's `zone.tab` ∪ `zone1970.tab` (418),
  read from the installed `tzdata` package so Windows and the Linux CI compute with the same data;
  `zone.tab` keeps a country's own name where IANA linked it to a neighbour (`Asia/Kuwait`,
  `Asia/Bahrain`, `Asia/Muscat` — all in the suggested list). Suggested, in order: Africa/Cairo,
  Asia/Riyadh, Asia/Dubai, Asia/Kuwait, Asia/Qatar, Asia/Bahrain, Asia/Muscat, Asia/Amman,
  Asia/Baghdad, Africa/Casablanca, Africa/Tunis, Africa/Algiers, Africa/Tripoli, Asia/Beirut,
  Europe/London, Europe/Istanbul — each exists in tzdata 2026e.
- `GET /api/v1/timezones` → `{tzdata_version, suggested, timezones: [{id, label, utc_offset,
  offset_minutes, region}]}`, sorted by offset then name, offsets computed at request time, `?q=` ≤ 64
  on id and label; ETag changes with the release or any offset; 304 on match.
- `app/services/quiet_hours.py`: pure arithmetic in the zone; the window's end is built as a local
  date-time and converted with that date's rules (PEP 495 `fold=0`), never a fixed offset. Tests
  cross Cairo's 2026 transitions (April 24 into summer time, October 29 out of it) — M-F12 (a fixed
  offset) is killed by both.
- The worker asks before claiming a batch and before every recipient; a campaign that reaches
  22:00 mid-batch stops at the next person (M-F13). Each wait is one `campaign_waiting` audit entry
  and one `wasla_campaign_waits_total{reason}` per transition, never per sweep.
- `GET/PUT /workspace/campaign-settings` (member / admin, audited old → new).
- Test isolation: `tests/conftest.py` keeps the window shut outside tests marked `quiet_hours`, so
  the 7,000-test suite runs at any hour; every quiet-hours test runs the real check at moments it
  chooses on the simulated clock.

## 7. The Messaging Limit: Read, Store, Webhooks

- `MetaMessagingLimitReader` reads `GET /v24.0/{phone-number-id}?fields=whatsapp_business_manager_messaging_limit`
  with the number's own credential (ADR-034), through the guarded client; the global
  `META_API_VERSION` stays v21.0 — no wider change was needed, so LIM-01's stop condition did not
  arise.
- When: on connect and credential rotation (the credential just proven), on enable (the stored
  one) — each before the capacity lock (ADR-093); on schedule when older than 6 h, before the
  schedule's row lock; from `business_capability_update` (entry id = WABA, ordered by time — an older
  delivery never overwrites a newer reading); `account_alerts` is recorded (log and
  `wasla_messaging_limit_alerts_total{alert_type}`) and changes nothing. Both fields are signature
  checked and idempotent like every webhook.
- `messaging_limit_service.interpret`: one table; unrecognised (`UNTIERED`, anything new) → 250,
  `recognised = false`, `wasla_campaign_messaging_limit_unknown_total`, alert `MessagingLimitUnknown`.
- A changed limit (either direction) releases every `daily_limit` and `limit_unknown` wait on the
  portfolio: the next batch decides with the new figure (250 → 2,000 mid-campaign: the rest goes at
  once; a lowered read: 200).
- Fail closed: never read → schedule 422 `messaging_limit_unknown`; running → wait `limit_unknown`,
  re-checked every 5 minutes. Deploy step: `python -m scripts.read_messaging_limits` (idempotent,
  exit 1 listing the numbers it could not read).
- Without the webhook subscribed in the Meta App (an external action), the six-hourly read is the
  only source: a change is noticed later, never wrongly.

## 8. The Ledger, the 80 % Budget and Spreading

The reservation (`BusinessInitiatedLedger.reserve`, inside `MessagingService._dispatch` for every
WhatsApp template): service window open (inbound within 23 h on that number) → not counted;
otherwise `pg_advisory_xact_lock(BUSINESS_PORTFOLIO, portfolio)`, count distinct people of the last
24 h with **no upper bound** (a racing sender's row dated a moment later still counts), decide,
stage the row with the message, commit — and only then call Meta (ADR-093). Provably undelivered →
the row deleted in its own transaction; uncertain → kept.

Transport-counted results (`tests/integration/test_campaign_messaging_limit.py`, every window
closed unless stated):

| Case | Unique people per 24-hour window at the transport | Twice | Attempts by waits |
|---|---|---|---|
| 600 people, TIER_250, 1 worker | 200 / 200 / 200; max in any moving 24 h 200 | 0 | 0 |
| the same, 2 workers | 200 / 200 / 200 | 0 | 0 |
| the same, 4 workers | 200 / 200 / 200 | 0 | 0 |
| two campaigns, one number, 300 each | 200 / 200 / 200 together | 0 | 0 |
| two numbers of one portfolio | 200 / 200 / 200 together | 0 | 0 |
| two workspaces on one portfolio | 200 / 200 / 200 together; ledger rows of both tenants | 0 | 0 |
| 30 people by follow-up and manual template first | campaigns 170; window 200 | 0 | 0 |
| 100 open-window people beside 200 counted | 300 sent; 200 counted | 0 | 0 |
| a window open on another number | not exempt; counted | 0 | 0 |
| unlimited | 2,000 in one window | 0 | 0 |
| webhook 250 → 2,000 mid-campaign | the rest at once | 0 | 0 |
| a lowered read | 200 | 0 | 0 |
| `UNTIERED` | 200; counted; alert | 0 | 0 |
| refused sends (provably) | rows released, retried once each | 0 | — |
| the last place, 40 natural races | exactly 1 winner in 40 / 40 | — | — |

Spreading: at the budget the campaign stays RUNNING with `waiting_reason = daily_limit` and
`resumes_at` = the first counted person's last send + 24 h + 5 min (M-F06: "now + 24 h" is killed);
no recipient is failed, skipped or charged (M-F07 killed); the lease is given up while waiting
(ADR-134 amendment). Day 2 and day 3 are the same rule; nobody resumes it.

Ported probe `audit/p20_limit_probe.py` (600 people, TIER_250, one worker): before, at `752a9ba`,
**600 unique people in one window**; after, at `b385527`: 600 sends to 600 people over three UTC
days, **200 / 200 / 200, at most 200 in any moving 24 h, 0 twice, completed by itself** (the probe
now reads the number's limit first, as connecting the number does).

## 9. The Per-Contact Cap and Pacing

- One conditional `UPDATE contacts … WHERE last_marketing_sent_at IS NULL OR <= at - interval
  RETURNING previous` in the send intent's transaction, after the ledger's decision; for MARKETING
  templates from a campaign or a person only (not a test, follow-up, agent, utility or
  authentication). Two campaigns on two workers can never both win (M-F10, check-then-update, killed).
- A capped campaign recipient is deferred (`not_before` = their moment + 5 min), no attempt; past
  `send_until` → skipped `contact_frequency_cap` (M-F11, skip-instead-of-defer, killed); the campaign
  waits `contact_cap` only when nobody else can go.
- `test_campaign_contact_cap.py` (8): one contact in two campaigns a minute apart on two workers →
  1 message in 24 h, the second campaign's 24 h later, 0 attempts; manual MARKETING inside the cap →
  409 with `available_at`; a test send inside the cap → sent; a utility template → not capped; a
  provably refused send releases the claim and the person receives it once on retry; cap + deadline
  → skipped; held_for_quality_assessment → sent and `held`; 135000 → paused
  `meta_quality_review`, resume works; 131049 → terminal for the person, cap pushed.
- CR-11 counts any two marketing templates to one contact within 24 h whose later send was made
  under the cap (section 13).

## 10. Personalisation

Composition takes `body_parameters` / `header_parameters` (one per placeholder; mixing with the
old `variables` → 422 `parameters_conflict`); errors `parameter_fallback_required`,
`parameter_fallback_too_long`, `parameter_key_unknown`, `parameter_source_not_allowed` (a button or
wa_id). One validator serves create, edit and duplicate. `set_audience` resolves every person's
values once, from the contact and their active lead, and stores them with the contact id; the send
uses exactly those (a row naming another contact is skipped `identity_changed` — CR-15's runtime
guard). Tests (`test_campaign_personalisation.py` 7, `tests/unit/test_personalisation.py` 8): three
contacts (a name, no name, a lead interest, a custom field) each receive their own values at the
transport, fallbacks where empty, no payload with another person's value; newline / tab / runs of
spaces normalised; 300 characters → fallback; a body over 1,024 → fallbacks; a renamed contact
after materialisation → the old value sent; preview of 5 → exactly those 5; another workspace's
contact → 404. Privacy: each recipient row now carries a copy of what was sent to that person — added
to CMQ-08 (still open).

## 11. The Campaigns Page Features

| # | Element | Route | Notes |
|---|---|---|---|
| 1 | Search | `GET /campaigns?q=` | ≤ 100, case-insensitive contains, `%` `_` `\` escaped; 5,000 campaigns: index scan of `ix_campaigns_tenant_id_created_at`, p95 2.7 ms (common term), 5.6 ms (no match). Not SCALE-TRIGGERED; no extension. |
| 2 | Duplicate | `POST /campaigns/{id}/duplicate` | admin, any status; a draft "<name> (copy)" with rules, mappings, header file, rate; never the list, schedule, results, media id, lease; validated like create; audited `campaign_duplicated`. |
| 3 | Edit draft | `PATCH /campaigns/{id}` + `If-Match` | drafts only (409 `campaign_not_editable`), version required (422) and checked (409 `campaign_version_conflict`); number / template / values / header clear the list (`audience_cleared`); audited field names only. Replaces the deliberate 405: `test_there_is_no_way_to_set_a_status_directly` now expects 422, `test_the_header_file_changes_only_through_a_draft_s_versioned_edit` replaces the 405 test. |
| 4 | Delete draft | `DELETE /campaigns/{id}` | drafts never attempted only (409 `campaign_not_deletable`); header file kept; audited. |
| 5 | Test send | `POST /campaigns/{id}/test-send` | section PG-09 below. |
| 6 | Per-contact variables | mappings + `preview-messages` | section 10. |
| 7 | Saved segments | `/campaign-segments` | rules only (`contact_ids` → 422); ≤ 100 per workspace under an advisory lock; If-Match versions; copied onto a campaign (editing or deleting the segment changes none); narrowing filters only. |
| 8 | Quiet hours | `/workspace/campaign-settings` | section 6. |
| 9 | Cost estimate | `GET /campaigns/{id}/cost-estimate` | section 12. |
| 10 | CSV export | `GET /campaigns/{id}/recipients/export.csv` | admin; UTF-8 BOM; times in the workspace's zone with offset; `= + - @ \t \r` prefixed with `'`; 5 per hour (429 `export_limit`); audited with counts and filters only; read in pages of 1,000 within the request, sent in chunks. |
| 11 | Activity log | `GET /campaigns/{id}/activity` | member; keyset over the audit trail; closed vocabulary; the worker audits completed / failed / waiting once per transition with a system actor; metadata whitelisted (no contact data). |
| 12 | Live updates | `GET /campaigns/progress?ids=` | ≤ 20 ids; `poll_after_seconds` 10 / 60; ETag, 304. |
| 13 | Replied | `statistics.replied`, summary | the latest accepted copy on that number within 72 h, one bounded UPDATE at ingestion; STOP = replied + opted_out_after. |
| 14 | Reasons | `statistics.reasons` | `{status, reason_code, provider_code, count}` summing to the total; `unrecorded` for none. |
| 15 | ETA | `CampaignRead.eta`, progress | from the delivery plan; null with a reason when not live or limit unknown. |
| 16 | Daily limit counter | `GET /whatsapp/accounts/{id}/messaging-limit` | section 7 / LIM-07. |
| 17 | Spread over days | automatic; `delivery_plan`; `send_until` | section 8. |
| 18 | Contact picker | `GET /contacts` | the existing contacts router extended; ≤ 50 per page, keyset; digits search the number, otherwise names; `account_id`, `eligible_for_campaign`. 100,000 contacts p95: first page 6.3 ms, eligible 6.7 ms, name 8.7 ms, digits 83 ms (substring of a number; no extension), without a number 4.2 / 15.7 ms. Two defects found by the measurement and fixed before commit (generic plan: `plan_cache_mode = force_custom_plan`; eligible-only anti-join). |
| 19 | Four-step composer | create (Idempotency-Key) → audience → plan + cost → schedule | a retried create returns the same draft (4 concurrent retries → 1 draft); `audience_ready: false` lists an unfinished draft. |
| 20 | KPI cards | `GET /campaigns/summary` | aggregate SQL over every campaign created in the window (default the last 30 days from local midnight), never a page of the list. |
| 21 | Timezone at onboarding | `GET /timezones`, registration | section 6. |

**PG-09, test send.** Admin, any status but cancelled, a contact of this workspace with a
conversation on the campaign's number (ADR-025: no new recipient), not opted out. The same component
builder, this contact's own values, and the header file (Meta's id reused only while fresh,
otherwise uploaded for the test; the campaign's id never written by a test). Origin `campaign_test`;
no recipient row, no statistics, no cap, no quiet hours; in the ledger with the **whole** limit as
its budget (409 `portfolio_limit_reached` only when nothing is left). 10 per workspace per 24 h
(429 `test_send_limit`). Audited `campaign_test_sent`.

The mockup (`E:\wasla\docs\ui\campaigns-page-mockup.html`, the owner's untracked file, read only)
assumes three things the API serves differently, all recorded in the contract:
`{contact.first_name}` (contacts have a display name), a per-campaign quiet-hours checkbox (a
workspace setting, TZ-03), and replies / ETA / cost computed in the page (the API returns them).

## 12. Cost Estimate

- 0119: `whatsapp_rate_cards`, `whatsapp_rate_card_rates` (market, closed category, `numeric(12, 6)`),
  `whatsapp_rate_card_markets` (the card's calling-code map). Platform routes (platform staff only,
  every workspace role 403): upload (multipart file + optional `market_map`; currency and
  effective date as bounded query parameters) → an inactive card with every rate and
  `changes_from_active`; activate; list; one. Writes and reads audited as platform actions.
- Strict parser: preamble until the `Market` header; Meta's two-line
  "Authentication-International" heading (CPF-03: whatever the line ending); `n/a` = no price; a
  non-decimal, negative or NaN rate, wrong currency, duplicate market or missing header → 422
  `rate_card_invalid` with `details.line`; the card must price "Other"; ≤ 1 MB.
- `GET /campaigns/{id}/cost-estimate` (admin): `label: "estimate"`, per market (longest calling-code
  prefix; unlisted → Other), exclusions (opted out, skipped, failed, no number, unpriced), the card,
  and the assumptions (charged only when delivered; Meta bills the business directly; Wasla does not
  charge for messages; free entry point windows not modelled). Decimal throughout, strings on the
  wire. No active card → `total: null`, `reason: no_rate_card`.
- APP-E2E V-3: 232 Egypt + 58 Saudi Arabia recipients → USD 18.2816 on the 2026-10-01 card.

## 13. Invariants CR-11..CR-15

Added to `scripts/omnichannel_invariants.py` (`campaign_violations`; `verify` now runs 59 checks):

| ID | Counts | Injection test |
|---|---|---|
| CR-11 | pairs of accepted MARKETING templates (campaign or person, never a test) to one contact of one workspace less than 24 h apart, whose **later** send was made under the cap (its message carries `marketing_cap_claimed_at`) | a person's template 1 h after a capped campaign send → +1; a pre-cap pair half an hour apart and a test send → not counted |
| CR-12 | campaign ledger rows whose portfolio's distinct people in the 24 h ending at them (every source) exceed the `budget_at_send` recorded with them | a third person before a row of budget 2 → +1 |
| CR-13 | campaigns naming another workspace's segment (beside the composite key) | written with the key's trigger set aside → +1 |
| CR-14 | recipients linked to a `campaign_test` message, or campaign meters naming one | a recipient linked to a test message → +1 |
| CR-15 | sent recipients whose `rendered_parameters.contact_id` is not their contact | values naming another contact → +1 |

**CR-11's refinement** (Phase 14): the first definition counted every pair, so a database whose
history predates 0113 — when nothing capped anything — reads CR-11 > 0 forever, and the populated
upgrade showed exactly that (1 pair from 0107's code, nothing after). A pair is now judged when its
later message was sent under the cap; a pre-cap send followed by a post-cap one — the CPF-04 case —
still counts. The residual: a defect that skipped the claim altogether would not show in CR-11; the
cap's own tests and M-F10 / M-F11 cover that path.

## 14. Observability

- `wasla_campaign_waits_total{reason}` (quiet_hours, daily_limit, contact_cap, limit_unknown) per
  transition; `wasla_campaign_messaging_limit_unknown_total`; `wasla_messaging_limit_alerts_total{alert_type}`;
  scrape-time gauge `wasla_portfolio_limit_utilisation{bucket}` (`<50`, `50-80`, `80-100`, `>=100`,
  by Meta's **whole** limit and every source — campaigns stop at 80 % by themselves; the alert is for
  everything else reaching the ceiling); `wasla_campaign_events_total{event}` += `test_sent`,
  `duplicated`, `exported`, `plan_spread`; `wasla_campaign_stops_total{reason}` +=
  `meta_quality_review`.
- Alerts `PortfolioLimitReached` (≥ 100 % for 5 m), `CampaignMetaQualityReview`,
  `MessagingLimitUnknown`, each with promtool cases that fire, clear and stay silent on ordinary
  traffic. Labels are closed vocabularies (ADR-072): no portfolio id, number, contact, campaign
  name or value in any label or log line (`test_campaign_metrics.py`, APP-E2E V-8).

## 15. Mutation Matrix

Runner `audit/run_page_feature_mutations.py` (untracked; the header media stage's protocol): each
mutant is one or more exact textual replacements, every anchor found exactly once (else INVALID),
its named killer suites run with `-x`, every file restored byte for byte and checked by SHA-256;
KILLED only when the first failing test is a named killer (KILLED-OTHER when another test fails
first). Detached worktree `E:\wasla-cpf-mut`, own database `wasla_cpf_mut`, own Redis DB. Run at
`2160ee2` (2026-10-17 03:32–05:05 UTC); M-F10 and M-F01n re-run at `7f98e9f` after the test of
CPF-06 below. `git status` under `app/` clean after every run.

```text
control   222 passed in 751.88 s (every suite the matrix names, unmutated)
```

| Mutant | Change | Verdict | First failing test |
|---|---|---|---|
| M-F01 | the ledger keyed per number instead of per portfolio | KILLED-OTHER | `test_campaign_messaging_limit.py::test_600_people_on_a_portfolio_of_250_go_200_a_day_for_three_days[1]` |
| M-F02 | the budget is 100 % of the limit, not the share | KILLED | `test_campaign_messaging_limit.py::test_600_people_on_a_portfolio_of_250_go_200_a_day_for_three_days[1]` |
| M-F03 | the count is read before the portfolio lock | KILLED | `test_campaign_messaging_limit.py::test_the_last_place_in_the_window_goes_to_exactly_one_of_two_racing_campaigns` |
| M-F04 | only campaign rows counted | KILLED | `test_campaign_messaging_limit.py::test_follow_ups_and_manual_templates_use_the_window_too` |
| M-F05 | the open-window exemption reads any conversation of the person | KILLED | `test_campaign_messaging_limit.py::test_a_window_open_on_another_number_does_not_exempt_a_person` |
| M-F06 | resumes_at = now + 24 h instead of the oldest row + 24 h | KILLED | `test_campaign_messaging_limit.py::test_600_people_on_a_portfolio_of_250_go_200_a_day_for_three_days[1]` |
| M-F07 | a wait pauses the campaign | KILLED | `test_campaign_messaging_limit.py::test_600_people_on_a_portfolio_of_250_go_200_a_day_for_three_days[1]` |
| M-F08 | an unrecognised limit maps to unlimited | KILLED | `test_campaign_messaging_limit.py::test_an_unrecognised_limit_is_250_never_more` |
| M-F09 | a provably undelivered send keeps its ledger row | KILLED | `test_campaign_messaging_limit.py::test_a_send_meta_refused_gives_its_place_back_and_an_uncertain_one_keeps_it` |
| M-F10 | the cap's check and claim are two statements | SURVIVED | `8 passed (survived at 2160ee2 - see CPF-06)` |
| M-F11 | the cap skips instead of deferring | KILLED | `test_campaign_contact_cap.py::test_two_campaigns_scheduled_a_minute_apart_reach_a_person_once_a_day` |
| M-F12 | quiet hours end computed with today's fixed offset | KILLED | `test_campaign_quiet_hours.py::test_the_window_ends_at_nine_local_across_the_october_transition` |
| M-F13 | quiet hours checked per batch only | KILLED | `test_campaign_quiet_hours.py::test_a_batch_reaching_the_window_stops_at_the_next_person_and_resumes_in_the_morning` |
| M-F14 | the workspace's time zone ignored (UTC) | KILLED | `test_campaign_quiet_hours.py::test_a_batch_reaching_the_window_stops_at_the_next_person_and_resumes_in_the_morning` |
| M-F15 | a missing value sent empty instead of the fallback | KILLED | `test_campaign_personalisation.py::test_every_person_is_sent_their_own_values_and_the_fallback_where_they_have_none` |
| M-F16 | values resolved at send time instead of frozen | KILLED | `test_campaign_personalisation.py::test_values_are_frozen_when_the_audience_is_written` |
| M-F17 | one person's values written on the next person's row | KILLED | `test_campaign_personalisation.py::test_every_person_is_sent_their_own_values_and_the_fallback_where_they_have_none` |
| M-F18 | PATCH accepted on a scheduled campaign | KILLED | `test_campaign_drafts.py::test_only_a_draft_is_editable_or_deletable` |
| M-F19 | the version check removed | KILLED | `test_campaign_drafts.py::test_a_draft_is_edited_from_the_version_read_and_a_stale_edit_is_refused` |
| M-F20 | duplicate copies the written list | KILLED | `test_campaign_drafts.py::test_a_duplicate_is_a_new_draft_with_rules_and_values_and_nothing_else` |
| M-F21 | a segment edit rewrites the campaigns that used it | KILLED | `test_campaign_segments.py::test_a_segment_s_rules_are_copied_and_later_edits_change_nothing` |
| M-F22 | the contact picker drops its tenant filter | KILLED | `test_campaign_segments.py::test_the_picker_never_shows_another_workspace` |
| M-F23 | a test send is counted on the campaign's recipient | KILLED | `test_campaign_test_send.py::test_a_test_is_the_campaign_s_message_and_nothing_about_the_campaign_changes` |
| M-F24 | a reply credited to every campaign within 72 hours | KILLED | `test_campaign_results.py::test_a_reply_after_two_campaigns_counts_for_the_latest_only` |
| M-F25 | the summary computed from the first page of campaigns | KILLED | `test_campaign_results.py::test_the_summary_is_the_sum_of_every_campaign_not_a_page` |
| M-F26 | CSV cells not neutralised | KILLED | `test_campaign_results.py::test_the_export_is_excel_safe_filtered_bounded_and_audited` |
| M-F27 | the estimate multiplies floats | KILLED | `test_campaign_cost.py::test_an_estimate_prices_each_market_exactly_or_says_there_is_no_card` |
| M-F27b | the estimate prices with a default rate when no card is active | KILLED | `test_campaign_cost.py::test_an_estimate_prices_each_market_exactly_or_says_there_is_no_card` |
| M-F28 | send_until ignored | KILLED | `test_campaign_delivery_plan.py::test_people_still_pending_at_the_deadline_are_skipped_and_the_campaign_completes` |
| M-F29 | 135000 does not pause the campaign | KILLED | `test_campaign_contact_cap.py::test_a_held_message_dropped_after_review_pauses_the_campaign` |
| M-F30a | CR-11 made vacuous | KILLED | `test_campaign_invariants.py::test_cr11_two_marketing_messages_to_one_contact_within_a_day` |
| M-F30b | CR-12 made vacuous | KILLED | `test_campaign_invariants.py::test_cr12_campaign_send_past_the_portfolio_budget` |
| M-C01+M08 | per-recipient claim and per-recipient hold both removed | KILLED | `test_campaign_two_workers.py::test_a_batch_longer_than_its_lease_is_not_shared` |
| M-C09 | other live reservations read before the lock | KILLED | `test_campaign_reservation.py::test_two_admins_gated_at_the_old_decision_point_cannot_both_fit` |
| M-C15 | 132000 handled per recipient again | KILLED | `test_campaign_templates.py::test_a_parameter_refusal_costs_one_request_and_fails_the_campaign[132000]` |
| M-C21 | read timeout classified as not attempted | KILLED | `test_campaign_answer_classes.py::test_each_answer_class[read_timeout]` |
| M-C25 | resume re-arms a REQUESTED message | KILLED | `test_campaign_stop_resume.py::test_a_send_that_may_have_left_is_never_re_armed` |
| M-C31 | manual MARKETING ignores opt-out | KILLED | `test_manual_marketing_templates.py::test_an_admin_cannot_send_a_marketing_template_to_an_opted_out_contact` |
| M-H01 | the worker uploads per recipient | KILLED | `test_campaign_header_media.py::test_thirty_people_one_upload_thirty_sends_one_id` |
| M-H05 | the kind check at composition removed | KILLED | `test_campaign_header_media_api.py::test_a_wrong_missing_or_extra_file_is_refused_at_composition[local-IMAGE-pdf-template_parameters]` |
| M-H09 | 131053 on a send re-uploads without limit | KILLED | `test_campaign_header_media.py::test_media_unknown_twice_fails_the_campaign_after_two_sends` |
| M-H11 | the header file read drops its tenant filter | KILLED | `test_campaign_header_media_api.py::test_another_workspaces_file_is_not_found_anywhere[local]` |
| M-F10 (re-run) | the cap's check and claim are two statements | KILLED | `test_campaign_contact_cap.py::test_two_workers_racing_for_a_person_whose_window_is_open_send_once` |
| M-F01n | M-F01 against its named killers alone (the full file stops at an earlier decisive test) | KILLED | `test_campaign_messaging_limit.py::test_two_numbers_of_one_portfolio_share_the_200` |

**Result: 32 meaningful mutants of this stage (M-F01..M-F30 with M-F27b and M-F30a/b split) and
10 regression mutants — 0 survived at the final head.**

- **M-F01** was KILLED-OTHER in the matrix: with `-x` the file's first decisive test (600 people
  on one number) failed before the named one. Its named killers run alone (M-F01n): KILLED by
  `test_two_numbers_of_one_portfolio_share_the_200`. Both tests are non-vacuous for it.
- **M-F10 survived at `2160ee2` — a test gap (CPF-06), not an equivalent mutation.** Every cap
  test reached the claim with the person's service window closed, so the ledger had already taken
  the portfolio's advisory lock and serialised the two senders; splitting the cap's check and
  claim changed nothing observable. Where the ledger takes no lock — a person whose window is open
  (not counted toward Meta's limit) — only the cap's one conditional UPDATE decides. New test
  `test_two_workers_racing_for_a_person_whose_window_is_open_send_once` holds both workers at the
  claim until both have arrived (and asserts that they did): 3 / 3 green on the real code, KILLED
  M-F10 on `7f98e9f`. The portfolio lock is therefore a second guard for closed windows, not the
  only one.
- M-F17 and M-F23 are also caught by the CR-15 and CR-14 injection tests, which are independent
  of the application code they guard.
- The regression lane (M-C01+M08, M-C09, M-C15, M-C21, M-C25, M-C31, M-H01, M-H05, M-H09, M-H11)
  stays KILLED, each by the test the earlier stages named. M-H02 stays the proven redundant guard
  (not re-run; the header media stage's combined mutant proved it).

## 16. Model-Built Test Results

```text
MODEL-BUILT (WASLA_TEST_SCHEMA unset; wasla_cpf_models on wasla-cpf-pg; worktree E:\wasla-cpf-lane2)
head        b385527 (app.__file__ E:\wasla-cpf-lane2\app\__init__.py)
run         2026-10-17 06:45:27 -> 07:37:24 UTC, one uninterrupted run
collected   7,292
passed      7,274
failed      0
skipped     18   OpenAI real-provider x11, schema parity x4 (model-built only), tmpfs x1,
                 kept-data-only x2 (test_ai_invariants, test_tool_invariants)
xfailed     0
xpassed     0
```

## 17. Migration-Built Test Results

```text
MIGRATION-BUILT (WASLA_TEST_SCHEMA=migrations, fresh alembic upgrade head;
                 wasla_cpf_migrations on wasla-cpf-pg2; worktree E:\wasla-cpf-lane)
head        b385527 (app.__file__ E:\wasla-cpf-lane\app\__init__.py)
run         2026-10-17 06:45:27 -> 07:37:45 UTC, one uninterrupted run
collected   7,292
passed      7,278
failed      0
skipped     14   OpenAI x11, tmpfs x1, kept-data-only x2
xfailed     0
xpassed     0
```

Baseline at `752a9ba` (the merge verification): 7,053 collected — model-built 7,035 passed /
18 skipped, migration-built 7,039 / 14. Collected rises by **239**, exactly the tests this stage
added (section 24); skips are the same known set.

**Earlier full runs, recorded rather than hidden.** The two lanes were first run in parallel at
`7f98e9f`, then at `e0f71ca`:

| Head | Model-built | Migration-built | What it found |
|---|---|---|---|
| `7f98e9f` | 1 failed | 1 failed | `test_workspace_purge::test_the_classification_covers_every_tenant_scoped_table` — four tables of this stage unclassified (**CPF-07**, fixed in `e0f71ca`) |
| `e0f71ca` | 0 failed (exit 0) | 1 failed | `test_campaign_quiet_hours::test_a_batch_reaching_the_window…` — two requests reached Meta after 22:00 under the two lanes' load (**CPF-08**, fixed in `b385527`) |
| `b385527` | 0 failed | 0 failed | — |

Both earlier runs printed no final count line: the project's `addopts` already has `-q`, and the
`-q` added on the command line made it `-qq`, which suppresses it. Their verdicts were read from
the failure sections and the exit code; the final runs were made without the extra `-q`.

## 18. Kept-Data Sweep, Races, Alembic, Docker, CI Jobs

**Kept-data sweep** (CI's selection: the 28 AI-harness suites with `WASLA_TEST_KEEP_AI_DATA=1`,
then `test_ai_invariants` and `test_tool_invariants`; migration-built; a fresh database each run):
**301 passed, 0 skipped — 3 of 3** (7 minutes each).

**Race suites** (two workers, reservation, top-up concurrency, header media, and this stage's
messaging-limit, contact-cap and quiet-hours worker tests), a fresh database each run:
**80 passed — 3 of 3**.

**Re-verified probes of the earlier stages** (`wasla_cpf_probe`, migration-built at `b385527`; the
probe harness's workspaces now get a zone, quiet hours off and a read limit, so they measure what
they always measured):

```text
P01b  unscaled, 5 campaigns x 50 people, two replicas, lease 300 s   0 people twice; all completed
P07b  IMAGE header campaign                                          1 upload, 25 sends, one header id
P02   40 natural scheduling races at the period cap                  both accepted in 0 / 40
P12   subscription suspended after scheduling                        0 of 10 sent; paused
```

**Alembic** (`wasla_cpf_mig`, fresh, server 2): `alembic heads` → `0120 (head)`, single;
0001 → head in 120 steps, 9 s; `alembic check` "No new upgrade operations detected";
`scripts.db_preflight verify` ok; catalogue 0 NOT VALID constraints, 0 invalid indexes, 0 disabled
triggers; head → 0107 (13 downgrades) → head (13 upgrades) clean; `downgrade base` → `upgrade head`
→ check → preflight clean (CI's step). Refusing downgrades leave the stamp:
`test_migration_downgrade_atomicity.py` in the migration-built lane.

**Monitoring** (CI's `monitoring` job, locally with the pinned images): `promtool check config` →
78 rules SUCCESS; `promtool test rules` SUCCESS (the three new alerts fire, clear, stay silent);
`amtool check-config` valid (2 receivers).

**Docker** (CI's `docker` job): `docker build --target runtime` exit 0 (image 572 MB); the
container, started as `staging` with a generated secret, answered `/health/live` with
`{"status":"alive"}` on the third attempt and runs as uid 1001 (non-root).

**CI jobs locally:** `quality` (ruff, black, mypy — section 21) green; `tests` (the full
model-built run with its skip gate — every skip is one of the allowed reasons) green;
`migration-parity` (the migration-built run, parity not skipped, the kept-data sweep) green;
`monitoring` green; `docker` green. GitHub-only and not reproduced: the job's Ubuntu
tmpfs (the one tmpfs skip here) and `actions/checkout` itself.

## 19. Populated Upgrade

`audit/p24a_populate_base.py` (run from `E:\wasla-cpf-base` at `752a9ba`) built `wasla_cpf_upgrade`
with that revision's `alembic upgrade head` (0107) and populated it through that revision's own
services and a real `CampaignWorker` on the fake Meta: three workspaces, numbers with sealed tokens,
contacts with and without leads, conversations, synced templates, an opt-out, campaigns in every
status (draft, scheduled, running with an IMAGE header, paused, completed, failed, cancelled) with
recipients in every status, and the audit entries those services write. It recorded every table's
row count and an md5 over every column that existed at 0107. Its processes then stopped, and
`audit/p24b_upgrade_check.py` (feature worktree) ran `alembic upgrade head` (0108..0120).

Three runs, each on a freshly populated base:

1. Phase 14, before 0120: after the upgrade CR-11 = 1 — a customer reached by stage A minutes
   before the upgrade was reached again after it. That is **CPF-04** (section 1); 0120 is its fix.
2. At `2160ee2`, with 0120: no row lost — but in that base stage A's own code (0107, no cap) had
   itself sent one person two marketing messages minutes apart, and the first definition of CR-11
   read that pre-cap pair as 1. That is the reason for CR-11's refinement (section 13); after it,
   all 15 campaign checks read 0 on the same database.
3. **At the final head `b385527`:**

```text
upgrade             0119 -> 0120 last step; heads: 0120 (head)
tables compared     57; differences: none except alembic_version (0107 -> 0120)
workspaces          3, all Africa/Cairo, quiet hours on 22:00-09:00
numbers             2 linked by 0120 to an unknown WABA-keyed portfolio (they had sent templates
                    in the last day); 0 known limits
0120 backfill       ledger: 9 campaign rows / 9 people; 9 contacts with a marketing claim
invariants          after the upgrade: all 15 campaign checks 0
before deploy step  the running header campaign: running / waiting_reason limit_unknown, 0 sends
deploy step         scripts.read_messaging_limits.read_all: 3 numbers read, TIER_250; re-run: same
                    (idempotent)
after deploy step   the running campaign: running / contact_cap - 4 recipients sent, 1 deferred:
                    a person stage A had reached minutes before the upgrade, held by the claim
                    0120 restored rather than messaged twice
stage B sends       3, to nobody stage A had reached; 0 twice
invariants at end   all 0
```


What an operator sees right after 0110 without the deploy step: running campaigns with
`waiting_reason = limit_unknown`, nothing sent; `docs/RUNBOOK.md` "Campaign page features
(0108-0120)" says to run `python -m scripts.read_messaging_limits` after `alembic upgrade head` and
before starting the campaign workers; campaigns resume within five minutes of a successful read.

## 20. APP-E2E V-1..V-8

Harness: the merge verification's (`cmv/e2e/harness.py`, read in full) copied to `audit/e2e/`
(untracked) with one change of substance: a single counting fake Meta
(`tests/integration/campaign_harness.FakeMeta`) behind the API's sends, uploads and limit reads and
behind two real `CampaignWorker`s with their own `Database`s, stamping each request with the
workers' simulated clock. Real API under uvicorn on 127.0.0.1, real registration, login and
authorization, real webhook signatures, `wasla_cpf_e2e` built by `alembic upgrade head` from empty
on the second server, Paymob and the outbound-URL guard faked at the transport. First run at
`2160ee2` (all PASS); **re-run at the final head `b385527`** after CPF-08 changed when campaigns
stop, 2026-10-17 07:00 UTC; evidence `audit/e2e/evidence.jsonl` (the first run's in
`audit/e2e/run_2160ee2/`). The table is the final run.

Time: the customers' history was moved five days back and the campaign's schedule three days back
**in this throwaway database only, after the API had accepted it for the coming 21:30 Cairo**, so
the whole spread runs in the past on the workers' clock and the replies of V-6, written in real
time, fall within 72 hours of the sends. Cairo has no DST change between the two dates.

| # | Result | Evidence |
|---|---|---|
| V-1 | PASS | `GET /timezones` → Africa/Cairo first; register with it; workspace in Cairo; `pro` bought; number connected (201); its limit read once at connect on **v24.0** → `TIER_250`, campaign budget 200; a MARKETING template with an IMAGE header and body `{{1}}` synced |
| V-2 | PASS | 300 customers wrote (webhooks 310 × 200, every seventh without a profile name), 10 then wrote `STOP` → 10 opt-outs; history moved back so every window is closed |
| V-3 | PASS | segment "wrote in 30 days"; PNG header uploaded; campaign with `{{1}}` ← `contact.display_name`, fallback "عميلنا", created with an Idempotency-Key; audience from the segment: **290**; preview of 3 → each its own name, the nameless one the fallback; staff uploaded and activated the 2026-10-01 rate card; estimate **USD 18.2816** (Egypt 232, Saudi Arabia 58); test send to the admin's own contact → 201, the payload carried their name, statistics unchanged |
| V-4 | PASS | scheduled for 21:30 Cairo → plan: **2026-10-17 21:30–21:50, 150 people; 2026-10-18 09:00–21:45, 140 people**; notice `spreads_over_days`; completion 21:45 the next day |
| V-5 | PASS | two workers: **290 sends to 290 people, 0 twice, 0 inside quiet hours, at most 199 people in any moving 24 h, 0 payloads with another person's value, 0 to the opted out**; local days 145 / 145 (first send 21:30:00, the first evening's last before 21:59, the last 21:53 the next evening); 2 uploads (the test's and the campaign's: ADR-144 amendment); 290 delivered by status webhooks. The plan said 150 / 140: it is an estimate, and the first evening's last minute went to the morning |
| V-6 | PASS | 20 replies → `replied` 20; summary and progress equal the statistics (sent, delivered, replied, failed, skipped); progress ETag → 304; CSV export: BOM, 290 rows, Arabic names intact, 20 `replied_at` |
| V-7 | PASS | a member (membership written in the database, workspace switched through the API): PATCH, DELETE, test-send, export, cost → **403**; campaign, statistics, activity, progress, summary, messaging limit → 200; activity: created, audience_set, test_sent, scheduled, waiting × 2, completed, recipients_exported |
| V-8 | PASS | `omnichannel_invariants verify` exit 0, **59 checks all 0**; 9 run secrets, 30 customer names and Meta's media id searched in the run log (288,213 bytes), the evidence (2,537), `/metrics` (69,100) and every audit entry's metadata (1,332): **0 found** |

The harness, not the application, needed two corrections while it was written: the template list's
response key, and the time travel of V-2 — it first moved the inbound messages' `created_at` but not
their `sent_at`, and the window-anchor invariant counted 280 conversations whose anchor was older
than their newest inbound message (300 customers less the 20 who replied in real time). The invariant
was right about the data the harness had made; with both columns moved it reads 0.

## 21. Ruff / Black / MyPy

At `b385527`, in the clean lane worktree:

```text
ruff check .        All checks passed!
black --check .     1000 files would be left unchanged.
mypy app tests      Success: no issues found in 867 source files
mypy scripts        Success: no issues found in 12 source files
```

This stage added 16 narrow `# type: ignore[code]` comments and no broad one:
11 `[attr-defined]` on `result.rowcount` after a bulk UPDATE or DELETE (the repository's existing
convention for SQLAlchemy's untyped `CursorResult`), 2 `[attr-defined]` on a model's `__table__`
in tests, 2 `[arg-type]` in unit-test factories, 1 `[type-arg]` on a bare `Select` in the segment
repository.

## 22. Documentation Changes

- `DECISIONS.md`: ADR-145 (time zone, quiet hours), ADR-146 (Meta's messaging limit, ledger, 80 %,
  spreading), ADR-147 (per-customer cap; CR-11 as refined), ADR-148 (personalisation), ADR-149 (the
  page features), ADR-150 (cost estimate); amendments to ADR-025 (a test send stays inside "wrote
  first"), ADR-026 (waits are timestamps, never sleeps), ADR-134 (no lease while waiting), ADR-137
  (mappings), ADR-144 (a test uploads for itself).
- `docs/CAMPAIGNS.md`: "How a campaign sends" rewritten with the waits and the deadline; quiet hours,
  Meta's limit (with URLs), the per-customer cap, personalisation, the page features, cost; statistics,
  reason codes, stop reasons, API, configuration; "Known limits": the personalisation, limit and time
  zone entries closed; the WABA key, follow-ups at 100 %, the webhook subscription, the estimate's
  upper bound and polling added. "Who a campaign can reach" unchanged.
- `docs/CAMPAIGNS_PAGE_CONTRACT.md` (new): section 31's contract.
- `docs/API.md`: every new route, field, header and error code; the time zone on workspace creation
  and registration (237 operations).
- `docs/AUTHORIZATION.md`: the route table; the new composite keys and platform-level tables.
- `docs/WHATSAPP.md`: the limit read on v24.0, `business_capability_update`, `account_alerts`, held
  and 135000, the `campaign_test` origin, the WABA key as EXTERNAL VERIFICATION.
- `docs/RUNBOOK.md`: "Campaigns are not sending" gains every waiting reason, `meta_quality_review`
  and `messaging_limit_unknown`; "Campaign page features (0108-0120)" with the deploy step.
- `docs/OBSERVABILITY.md`: the counters, the gauge and the three alerts. `docs/SAAS.md`: the
  platform settings. `README.md`: migrations 0001–0120. `.env.example`:
  `CAMPAIGN_PORTFOLIO_LIMIT_SHARE`, `CAMPAIGN_MESSAGING_LIMIT_REFRESH_SECONDS`,
  `CAMPAIGN_CONTACT_MARKETING_INTERVAL_SECONDS`, `CAMPAIGN_PARAMETER_MAX_CHARS`,
  `CAMPAIGN_TEST_SENDS_PER_DAY`, `CAMPAIGN_EXPORTS_PER_HOUR`, `CAMPAIGN_REPLY_ATTRIBUTION_SECONDS`,
  `META_MESSAGING_LIMIT_API_VERSION`.

## 23. Before / After

Before: `audit/p21_page_before.py` read the application's OpenAPI document at `752a9ba`;
`audit/p20_limit_probe.py` ran the 600-person campaign there. After: the routes and tests named.

| Item | Before (`752a9ba`) | After (`b385527`) |
|---|---|---|
| 1 Search | no `q` | `GET /campaigns?q=` |
| 2 Duplicate | missing | `POST /campaigns/{id}/duplicate` |
| 3 Edit draft | `PATCH` → 405 | `PATCH` + `If-Match`, drafts only |
| 4 Delete draft | missing | `DELETE /campaigns/{id}`, drafts only |
| 5 Test send | missing | `POST /campaigns/{id}/test-send` |
| 6 Per-contact variables | constants only | mappings + fallbacks, frozen; `preview-messages` |
| 7 Segments | missing | `/campaign-segments` |
| 8 Quiet hours | none | `/workspace/campaign-settings`, enforced per recipient, a minute early |
| 9 Cost estimate | missing | rate cards + `GET /campaigns/{id}/cost-estimate` |
| 10 CSV export | missing | `GET /campaigns/{id}/recipients/export.csv` |
| 11 Activity | missing | `GET /campaigns/{id}/activity` |
| 12 Live updates | missing | `GET /campaigns/progress?ids=` + ETag |
| 13 Replied | absent | `statistics.replied`, summary |
| 14 Reasons | absent | `statistics.reasons` |
| 15 ETA | absent | `CampaignRead.eta`, progress |
| 16 Daily limit counter | missing | `GET /whatsapp/accounts/{id}/messaging-limit` |
| 17 Spread over days | 600 unique people in one 24 h window (p20) | 200 / 200 / 200; `delivery_plan`; `send_until` |
| 18 Contact picker | no listing | `GET /contacts` (extended) |
| 19 Four-step composer | a retried create made two drafts | Idempotency-Key; `audience_ready` |
| 20 KPI cards | missing | `GET /campaigns/summary` |
| 21 Timezone at onboarding | no zone anywhere | `GET /timezones`; required on registration and `POST /workspaces` |
| TZ-01..04 | — | applied (section 4) |
| LIM-01..08 | limit neither read nor enforced | applied |
| FREQ-01 | no cap | one marketing message per customer per 24 h |
| PER-01..05 | — | applied |
| PG-01..14 | — | applied |
| COST-01..02 | — | applied |

## 24. Required Numbers

| Number | Value | Where |
|---|---|---|
| Unique people per moving 24 h per portfolio at the transport, every limit test | ≤ 200 on TIER_250 (= 80 %) in every test; 200 / 200 / 200 per window for 600 people with 1, 2, 4 workers, two campaigns, two numbers, two workspaces; 2,000 in one window when unlimited | section 8 |
| People with two marketing messages within 24 h | 0 (CR-11 = 0 everywhere; the cap tests; APP-E2E) | sections 9, 13, 20 |
| Sends inside quiet hours at the transport | 0 (quiet-hours suite across both 2026 Cairo transitions; slow-send test; APP-E2E) | sections 6, 20 |
| People twice in any campaign | 0 | sections 8, 20, 23 |
| Attempts spent by waits | 0 (`sent/0`, `pending/0` in every wait test) | section 8 |
| Payloads carrying another contact's value | 0 (transport cross-check; APP-E2E 0 of 290) | sections 10, 20 |
| Mutants | 32 of this stage + M-F31 + 10 regression: all KILLED at the final head (M-F10 after CPF-06's test; M-F01 also by its named tests) | section 15 |
| p95 `GET /contacts`, 100,000 contacts | first page 6.3 ms; eligible 6.7 ms; name 8.7 ms; digits 83 ms; no number 4.2 / 15.7 ms | section 11 |
| p95 `GET /campaigns?q=`, 5,000 campaigns | 2.7 ms common term; 5.6 ms no match | section 11 |
| Collected tests | 7,053 → 7,292 (+239) | below |

Tests added per file (collected, `752a9ba` → `b385527`):

| File | Added |
|---|---|
| tests/integration/test_campaign_messaging_limit.py | +15 |
| tests/integration/test_campaign_drafts.py | +13 |
| tests/integration/test_messaging_limit_reads.py | +9 |
| tests/integration/test_campaign_contact_cap.py | +9 |
| tests/integration/test_campaign_quiet_hours.py | +8 |
| tests/integration/test_campaign_personalisation.py | +7 |
| tests/integration/test_campaign_results.py | +7 |
| tests/integration/test_campaign_segments.py | +7 |
| tests/integration/test_timezones_api.py | +7 |
| tests/integration/test_campaign_delivery_plan.py | +5 |
| tests/integration/test_campaign_test_send.py | +5 |
| tests/integration/test_campaign_invariants.py | +5 |
| tests/integration/test_campaign_cost.py | +3 |
| tests/integration/test_messaging_limit_api.py | +2 |
| tests/integration/test_campaign_metrics.py | +1 |
| tests/integration/test_campaign_page_migrations.py | +1 |
| tests/integration/test_campaign_settings_api.py | +1 |
| tests/integration/test_workspace_purge.py | +1 |
| tests/unit/test_messaging_limit_values.py | +23 |
| tests/unit/test_rate_card_parsing.py | +22 |
| tests/unit/test_request_field_bounds.py | +17 (the new request fields, parametrised) |
| tests/unit/test_request_text_storability.py | +13 (likewise) |
| tests/unit/test_delivery_plan.py | +11 |
| tests/unit/test_quiet_hours.py | +11 |
| tests/unit/test_timezones.py | +11 |
| tests/unit/test_body_allowances.py | +8 (the new routes, parametrised) |
| tests/unit/test_business_initiated_ledger.py | +8 |
| tests/unit/test_personalisation.py | +8 |
| tests/unit/test_inbound_metric_domains.py | +1 |
| **Total** | **+239** |

**Non-vacuity (section 28 of the brief).** Every decisive test of sections 14 and 15 either
names a feature absent at `752a9ba` (its route, column or module does not exist there — the test
cannot pass there; `p21_page_before.py` lists each as missing) or is killed by a mutant of
section 15: the per-window counts (M-F01/01n, M-F02, M-F06, M-F07), the 40-race test (M-F03), the
mixed-sources test (M-F04), the open-window-on-another-number test (M-F05), the unrecognised value
(M-F08), the release test (M-F09), the open-window cap race (M-F10), the deferral (M-F11), both DST
tests (M-F12), the next-person and slow-send tests (M-F13, M-F31), the zone test (M-F14), fallback,
frozen and own-values tests (M-F15..17), drafts (M-F18..20), segments and picker (M-F21, M-F22),
test send (M-F23), replies (M-F24), summary (M-F25), export (M-F26), cost (M-F27, M-F27b),
deadline (M-F28), 135000 (M-F29), CR-11 / CR-12 injections (M-F30a/b). The limit probe failed at
`752a9ba` (600 people in one window) and passes after.

## 25. Updated Score

The remediation's dimensions, re-scored by this stage. A self-score is not a measurement; the
audit measured 52 at `989584b`, the remediation and header media stages self-scored 95.

| Dimension | Before | After | Why it changed |
|---|---|---|---|
| Duplicate / concurrency safety | 19 / 20 | 20 / 20 | Meta's limit and the per-customer cap are decided under locks or one conditional statement, proven at the transport with 1/2/4 workers and forced races |
| Provider policy compliance (Meta's limits, opt-out, templates) | 16 / 20 | 19 / 20 | portfolio limit read and enforced at 80 %, per-customer cap, pacing 135000 / 131049; −1: the WABA key and Meta's behaviour past the limit are EXTERNAL VERIFICATION |
| Customer protection (quiet hours, opt-out, frequency) | 15 / 20 | 20 / 20 | quiet hours in the workspace's zone with a send lead, one marketing message a day, opt-out unchanged |
| Operability (observability, runbook, deploy) | 19 / 20 | 19 / 20 | new gauges and alerts; the deploy step is scripted; −1 the webhook subscription is an operator action |
| Product completeness (the page) | 12 / 20 | 19 / 20 | all 21 elements served and contracted; −1 per-recipient header pictures / URL values deliberately not supported |
| **Total** | **81 / 100** (this stage's reading of `752a9ba` against the page brief) | **97 / 100** | |

The "before" here is not the remediation's 95: that score measured the campaigns line against its
own brief, without the page or Meta's portfolio limit in scope.

## 26. Remaining External Actions

Not performed here; for the operator:

1. Subscribe `business_capability_update`, `account_alerts` and `message_template_status_update`
   (CMQ-05) in the Meta App.
2. Real-Meta lane, only when authorised: read `whatsapp_business_manager_messaging_limit` on the real
   number and compare with WhatsApp Manager; confirm whether any field names a WABA's portfolio
   (none is documented — LIM-02's WABA key); one small campaign to 1–3 numbers the owner names
   (personalised body, image header, sent / delivered / read, a reply attributed, rows reconciled);
   what Meta does past the portfolio limit on a test portfolio (EXT-CMP-01, undocumented); a new
   template's first send — `held_for_quality_assessment`? (EXT-CMP-08).
3. Download Meta's current rate card and activate it in production (COST-01); Egypt (+20) rates
   (EXT-CMP-10).
4. On the first deploy of 0108–0120: `python -m scripts.read_messaging_limits` after `alembic upgrade
   head`, before the campaign workers start.
5. CMQ-07 legal review ("wrote first" vs explicit opt-in, Egypt PDPL 151/2020), then the
   contact-import stage.
6. CMQ-08: retention of recipient rows, now including `rendered_parameters`.
7. The follow-up behaviour at 100 % of the portfolio limit (LIM-04).
8. The merge verification of this branch into `worktree-billing-google-auth`, then the push (a
   separate stage).
9. The campaigns page in the frontend, built against `docs/CAMPAIGNS_PAGE_CONTRACT.md`.
10. Remove this stage's infrastructure after review (`wasla-cpf-*` containers, `E:\wasla-campaign-features`,
    `E:\wasla-cpf-base`, `E:\wasla-cpf-lane`, `E:\wasla-cpf-mut`) with the infrastructure cleanup prompt.

## 27. Open Product Decisions

- What a follow-up or manual template does when the portfolio is at 100 % (today: recorded, sent,
  alerted — `PortfolioLimitReached`).
- CMQ-07 (explicit opt-in vs "wrote first"), CMQ-08 (recipient-row retention, now with a copy of the
  values sent), CMQ-10 (second-admin approval) — unchanged, still open.
- Whether a per-recipient header picture or URL / copy-code value is ever wanted (not supported).
- Whether to push live progress (WebSocket / SSE) once a frontend exists; polling fits bucket C today.

## 28. Final Verdict

**CAMPAIGNS PAGE FEATURES IMPLEMENTED WITH EXTERNAL VERIFICATION REMAINING**

Every condition of the brief's section 36 holds at `b385527` (the report commit after it changes
this file only):

| Condition | Evidence |
|---|---|
| No moving 24 h window of any portfolio carries more than floor(0.8 × limit) campaign people at the transport — 1, 2, 4 workers, two campaigns, two numbers, two workspaces | section 8; p20 after; APP-E2E V-5 (199) |
| A campaign at the budget waits and finishes by itself, 0 twice, 0 attempts | section 8 (`sent/0`), p20 (completed over 3 days), APP-E2E |
| Nobody gets two marketing messages within 24 h in a workspace (CR-11 = 0) | section 9; invariants after every run |
| No campaign send inside quiet hours at the transport, across DST | section 6; the slow-send test; APP-E2E V-5 |
| Every workspace has a time zone; creation requires it; `GET /timezones` serves the list it validates against | section 6 |
| Personalised payloads carry each person's own values, frozen, with fallbacks | section 10; APP-E2E V-5 (0 of 290 with another's value) |
| Every element of section 31 has a route doing what the page claims; the contract documents it | section 11; `docs/CAMPAIGNS_PAGE_CONTRACT.md` |
| Every meaningful mutant killed; regression mutants still killed | section 15 |
| Both full lanes 0 failed on the final head; kept-data 3/3; races 3/3; every CI job green locally | sections 16–18, 21 |
| CR-01..CR-15 at 0 | APP-E2E V-8 (59 checks), populated upgrade, every invariant suite |
| Populated upgrade loses no row; fail-closed deploy step documented and scripted | section 19; RUNBOOK; `scripts/read_messaging_limits.py` |
| Nothing pushed, merged or deployed; no real provider; `main` untouched; untracked files unchanged | `worktree-billing-google-auth` = origin = `752a9ba`; `main` = `8e3a1ba`; no remote branch for this stage; `E:\wasla`'s 11 documents and `docs/ui/` unchanged |

What remains is external (section 26): the webhook subscriptions, the real-Meta lane (above all
what Meta does past the limit, and whether two WABAs can share a portfolio), the production rate
card, the legal opt-in review, and the merge verification of this branch.

### The final question

*If a business on a brand-new portfolio (limit 250) scheduled a 600-person spring-sale campaign at
9:30 in the evening Cairo time, with each customer's name in the message, while a second campaign
and a few follow-ups were running on another number of the same portfolio — would Wasla send
nothing until 9 the next morning, never put more than 200 new people in any 24 hours across both
numbers, never message anyone twice in a day, give every customer their own name, finish the
campaign by itself over the following days, and show the owner on the page, at every moment, how
far it got, why it is waiting and when it will finish?*

Yes — with one correction to the premise. At 21:30 quiet hours have not begun, so Wasla **does
send** between 21:30 and 21:59 (a minute before the window, CPF-08), up to the portfolio's budget,
then nothing until 09:00. Across both numbers and every source the ledger holds the portfolio to
200 new people in any moving 24 hours — the follow-ups count against the same window and are never
refused, so they take part of the 200 (section 8: 30 follow-ups and manual templates → campaigns
170), and the second campaign shares the rest. A customer in both campaigns is reached once and the
other campaign waits 24 hours for them (`contact_cap`). Each payload carries that customer's own
name, frozen when the audience was written, or "عميلنا" where there is none. The campaign resumes by
itself at each window's end and completes over the following days without anyone touching it (600 people alone take three). The page has
`GET /campaigns/progress` (counts, `waiting_reason`, `resumes_at` in Cairo time, ETA) every 10
seconds, the delivery plan day by day, the messaging-limit counter and the activity log — as
estimates where they are predictions, which the contract tells the page never to present as
guarantees. APP-E2E ran this scenario (one campaign, 290 people after 10 opt-outs) through the
real API and two workers.
