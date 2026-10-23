# Follow-ups, Header Media & Realtime — Implementation (DRAFT)

Stage branch `followups-header-media-realtime-20261023`, worktree `E:\wasla-followups-realtime`,
based on `worktree-billing-google-auth` at `8752324`. Nothing pushed, merged or deployed.

This is the running draft (section 9 of the stage prompt). A resumed session reads the
**Phase status** table and `git log`, then continues from the first phase that is not committed.

## Phase status

| Phase | Scope | State | Commit | Suites run |
| --- | --- | --- | --- | --- |
| 0 | Baseline, facts, infrastructure | committed (with 1-2) | see 1-2 | follow-up ×4 suites green at base |
| 1 | RT-08 (CPM-06), TZ-05, TZ-06, TZ-07, TZ-09 | committed | (this commit) | timezone, quiet hours (pg + Cairo pg3), campaign API, invariants, docs truth, workspaces, unit |
| 2 | TZ-08 local-time resolution | committed | (this commit) | as above |
| 3 | FLIM-01..03, FQH-01..04 | not started | | |
| 4 | FUS-01 settings | not started | | |
| 5 | FUS-02/03/05/06 runs, stop rules, window | not started | | |
| 6 | FUS-04/07 AI-written step, charging | not started | | |
| 7 | FUS-09 what a person sees | not started | | |
| 8 | HMT-01..03 template header library | not started | | |
| 9 | HMR-01..03 per-recipient header media | not started | | |
| 10 | RT-03 publish points, RT-04 incremental reads | not started | | |
| 11 | RT-01/02/05 stream, RT-07 polling budget | not started | | |
| 12 | RT-06 nginx, shutdown, capacity | not started | | |
| 13 | Invariants, observability, docs, ADRs, contracts | not started | | |
| 14 | Mutation | not started | | |
| 15 | Full lanes, CI jobs, after-probes | not started | | |
| 16 | Final report | not started | | |

## Starting repository state (recorded 2026-10-23)

```text
canonical          worktree-billing-google-auth at 8752324 = origin (0 ahead / 0 behind)
git diff --stat 0f767ec 8752324   CAMPAIGNS_PAGE_FEATURES_MERGE_VERIFICATION.md only (838 +)
BASE_HEAD          8752324
BASE_MIGRATION_HEAD 0120 (single head)
last ADR           ADR-150
python             3.12.7 (venv E:\wasla-cpr-notes\venv), docker 29.8.0, compose v5.5.1
PostgreSQL         16.15 (pgvector/pgvector:pg16); server TimeZone Etc/UTC on wasla-fur-pg/pg2,
                   Africa/Cairo on wasla-fur-pg3; the dev stack's postgres is not used
Redis              7.4.11 (XADD MAXLEN ~ and XREAD BLOCK available)
tzdata             2026e: Cairo repeats 23:00-24:00 on 2026-10-29 (+03:00 then +02:00) and skips
                   00:00-01:00 on 2027-04-30 (the prompt's "2027-04-29 at 00:30" is 2027-04-30 00:30)
app.__file__       E:\wasla-followups-realtime\app\__init__.py (asserted by audit/env_fur.sh)
```

Infrastructure (this stage only; `wasla-cpf-*` left as they were, stopped):

```text
wasla-fur-pg      127.0.0.1:55981   model-built lane, probes (wasla_fur_models, _targeted, _probe, _mut, _upgrade, _e2e)
wasla-fur-pg2     127.0.0.1:55982   migration-built lane (wasla_fur_migrations)
wasla-fur-pg3     127.0.0.1:55983   server TimeZone=Africa/Cairo (wasla_fur_tz; TZ-06 proof)
wasla-fur-redis   127.0.0.1:56981   wasla-fur-redis2 127.0.0.1:56982 (TEST_REDIS_URL)   wasla-fur-redis3 127.0.0.1:56983
wasla-fur-minio   127.0.0.1:59981   bucket wasla-media
```

### Preamble facts verified at BASE_HEAD

```text
schedule_follow_up(delay_minutes, message) text only          app/agents/registry.py:745-766            true
LEDGER_ENFORCEMENT has CAMPAIGN and CAMPAIGN_TEST only;
    FOLLOW_UP and HUMAN are RECORD_ONLY by omission            app/services/messaging_service.py:178-188 true
FollowUpService.dispatch does not catch PortfolioLimitReachedError app/services/follow_up_service.py:885-916 true
follow-up worker sweep 30 s, no time-of-day check              app/workers/follow_up_worker.py:48,136-141 true
stale "no workspace time zone" comments                        campaign_service.py:1645, usage_repository.py:149-152,
                                                               docs/CRM.md:166                           true (fixed, TZ-07)
usage day buckets: date_trunc('day', timestamptz), unpinned session  usage_repository.py:155, db/session.py:93-98 true (fixed, TZ-06)
scheduled_at has no _local twin                                app/schemas/campaign.py:472               true (fixed, TZ-05)
```

The remaining facts (channels, ownership, lead vocabulary, AI charging, guardrails, header
media, realtime, rate buckets) are verified at the start of the phase that changes them and
recorded here as each phase lands.

## Phase 1 — RT-08, TZ-05, TZ-06, TZ-07, TZ-09

- **RT-08 (CPM-06).** `test_a_batch_reaching_the_window_stops_at_the_next_person_and_resumes_in_the_morning`
  now starts at 21:58:48 with Meta answering in 1.0 s: 12 s of real-time margin before the 21:59:00
  decision cut-off, and twenty sends need twenty seconds, so the batch still cannot finish before it.
  Every assertion is unchanged. M-F13 (quiet hours per batch only) and M-F31 (send lead removed) both
  let all twenty through, which the `< 20` assertion refuses; the strict runner confirms them in Phase 14.
- **TZ-05.** `CampaignRead` gains `scheduled_at_local`, `started_at_local`, `completed_at_local`,
  `cancelled_at_local`, `next_send_at_local`, `created_at_local`, `updated_at_local` (the campaign
  times still missing a twin). Follow-up twins come with Phase 3 (`requested_at_local`, `due_at_local`).
- **TZ-06.** Every connection runs with `TimeZone = 'UTC'` via asyncpg `server_settings`
  (connection-startup parameter, chosen over an on-connect `SET` so no statement can run before it):
  `Database` (API and workers), Alembic's `env.py`, and the scripts that open their own engines
  (`db_preflight`, `schema_catalog`, `omnichannel_invariants`, `provision_runtime_db_role`), through
  `app.db.session.utc_connect_args`. The only `date_trunc` / `::date` on a `timestamptz` in `app/`,
  `scripts/` and `alembic/` is `usage_repository.py:155`. Proven on a Cairo-zoned database (and the whole
  file again on `wasla-fur-pg3`); removing the pin fails the test (M-S38, checked by hand).
- **TZ-07.** The three stale statements now describe the code.
- **TZ-09.** 09:00+02:00 on 2026-10-30 is stored 07:00 UTC and first sent at 09:00 local; the stale
  +03:00 keeps its instant (06:00 UTC = 08:00 local): waits to 09:00 with quiet hours on, starts at
  08:00 local with them off.

## Phase 2 — TZ-08

`GET /api/v1/workspace/local-time?at=YYYY-MM-DDTHH:MM` (any member, workspace bucket) resolves a wall
time with the pinned tzdata: the October fold is `ambiguous` with both instants and `at_local` the
first (fold 0); the April gap is `nonexistent` with `at_local` the first instant after it; an offset,
seconds or any other shape is `422 local_time_invalid`. `docs/API.md` (238 operations),
`docs/AUTHORIZATION.md`.
