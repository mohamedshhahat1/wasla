# Follow-ups, Header Media & Realtime — Implementation (DRAFT)

Stage branch `followups-header-media-realtime-20261023`, worktree `E:\wasla-followups-realtime`,
based on `worktree-billing-google-auth` at `8752324`. Nothing pushed, merged or deployed.

This is the running draft (section 9 of the stage prompt). A resumed session reads the
**Phase status** table and `git log`, then continues from the first phase that is not committed.

## Phase status

| Phase | Scope | State | Commit | Suites run |
| --- | --- | --- | --- | --- |
| 0 | Baseline, facts, infrastructure | committed (with 1-2) | see 1-2 | follow-up ×4 suites green at base |
| 1 | RT-08 (CPM-06), TZ-05, TZ-06, TZ-07, TZ-09 | committed | a058bb1 | timezone, quiet hours (pg + Cairo pg3), campaign API, invariants, docs truth, workspaces, unit |
| 2 | TZ-08 local-time resolution | committed | a058bb1 | as above |
| 3 | FLIM-01..03, FQH-01..04 | committed | 8113a5f | new file 10/10; send-path sweep (campaign*, follow_up*, crm*, agent*, tool*, messaging*, manual*, ai_*, timezone); migration-built parity/downgrade; alembic head->0120->head |
| 4 | FUS-01 settings | committed | 705bd34 | settings API 17/17; workspace, purge, auth, audit, docs truth, route auth; migration-built parity + downgrade; alembic head->0120->head |
| 5 | FUS-02/03/05/06 runs, stop rules, window | committed | fb7124b | sequences 18/18 (+ reply/silent 5x); settings + limit files; send-path sweep (see below); migration-built parity + downgrade; alembic head->0120->head |
| 6 | FUS-04/07 AI-written step, charging | committed | 980868a | ai steps 16/16, tool offer 3/3, sequences 18/18, AI invariants; send-path sweep |
| 7 | FUS-09 what a person sees | committed | 888f2c7 | visibility 3/3; conversation/inbox/follow-up/CRM/schema suites 255 passed; docs truth, route auth |
| 8 | HMT-01..03 template header library | committed | 4ffbad0 | template header media 8/8; sweep 162 suites 0 failed; migration-built parity + downgrade; alembic head->0120->head |
| 9 | HMR-01..03 per-recipient header media | committed | 324d435 | per-recipient 7/7 (1/2/4 workers); sweep 163 suites: 7 failures in 2 files fixed (below), re-run green; migration-built parity + downgrade; alembic round trip |
| 10 | RT-03 publish points, RT-04 incremental reads | committed | a100716 | publish 5/5, incremental 2/2, CRM integrity, conversation endpoints; sweep 140 suites 1,569 passed / 2 skipped / 0 failed; ruff, black, mypy app tests scripts clean |
| 11 | RT-01/02/05 stream, RT-07 polling budget | committed | (this commit) | stream 14/14, progress budget 3/3, route authorization, rate limits; sweep 126 suites 1,543 passed / 1 failed (documented operation count, fixed: 245 -> 247) / 2 skipped; documentation and config suites 205 passed; ruff, black, mypy clean |
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

## Phase 3 — FLIM-01..03, FQH-01..04

Facts verified first: `LEDGER_ENFORCEMENT` held only CAMPAIGN and CAMPAIGN_TEST
(messaging_service.py:178-188), so FOLLOW_UP and HUMAN were RECORD_ONLY by omission;
`dispatch` caught no `PortfolioLimitReachedError` (follow_up_service.py:885-916);
`_defer` overwrote `scheduled_at`; nothing in the follow-up path read quiet hours.

- **FLIM-01/02.** New `Enforcement.DEFER_AT_LIMIT`: the whole limit, but not
  fail-closed - with the limit unknown the send proceeds and the row records
  `limit_at_send` null. `LEDGER_ENFORCEMENT[FOLLOW_UP] = LEDGER_ENFORCEMENT[HUMAN] =
  DEFER_AT_LIMIT`. `FollowUpService.dispatch` catches `PortfolioLimitReachedError` and
  defers to `resumes_at` (`deferred_reason = portfolio_limit`, no attempt).
  `MessagingService.send_manual_template` turns it into `409 portfolio_limit_reached`
  with `available_at` / `available_at_local`, and answers a send made with the limit
  unknown with `notices: ["messaging_limit_unknown"]` (`TemplateSentRead`, additive).
- **FLIM-03.** Unchanged code path - the ledger is reserved only for
  `kind is TEMPLATE` on WhatsApp - now asserted: a free-form follow-up at 100 % is sent
  and writes no ledger row. The ledger docstring, the `PortfolioLimitReached` alert,
  `docs/CAMPAIGNS.md` and ADR-146 section 4 now state the decision.
- **FQH-01/02.** A template follow-up to a closed window is bound by the workspace's
  quiet hours, with the campaigns' minute of lead (`closed_to_sends`, deferred to
  `reopens_after`; the prompt says `ends_after` - identical inside the window, and the
  lead keeps a send decided at 21:59:59 out of it). A free-form follow-up is not bound.
- **FQH-03.** `release_follow_up_quiet_waits` (follow-ups only; the campaign function
  is untouched) runs in the quiet-hours PUT and in a zone change.
- **FQH-04.** 0121: `requested_at` (set at scheduling and at a reschedule - a new
  intention - never by a wait or a retry), `deferred_reason`, and `deferrals`
  (every wait in order, at most 20). An ORM default fills `requested_at` from
  `scheduled_at` for inserts that do not name it, so existing tests and Core inserts
  are unchanged.
- The follow-up read gains `requested_at`, `deferred_reason`, `due_at_local`,
  `requested_at_local` (TZ-05).

Tests (`test_follow_up_limit_and_quiet_hours.py`, 10): the 251st person's template
follow-up waits, spends nothing and is sent when the window frees; unknown limit sent
and recorded null; free-form at 100 % sent with no ledger row; manual template 409 with
when (HTTP and service, nothing staged); unknown-limit notice; a template follow-up
waits out quiet hours while a free-form one is sent; switching quiet hours off and a
new zone both release it; night then limit recorded in order. The file is marked
`quiet_hours` (the conftest switch that otherwise keeps every window shut); its windows
are built around the real now with hours of margin.

Send-path sweep after Phase 3 (70 suites: every `test_campaign*`, `test_follow_up*`, `test_crm*`,
`test_agent*`, `test_tool*`, `test_messaging*`, `test_manual*`, `test_ai_*`, the timezone and
invariant files): first run 15 failures, all `[s3]` cases of `test_campaign_header_media_api.py`
answering 503 `dependency_unavailable` - an **infrastructure** failure: the `wasla-media` bucket had
been made with `mkdir` in MinIO's data directory, which MinIO does not register as a bucket. Fixed
with `mc mb`; every swept suite that uses the object store re-run green. No test expectation of
an earlier stage changed in Phase 3.

## Phase 4 — FUS-01 follow-up settings

Facts verified first: only WhatsApp has an adapter (`app/channels/registry.py:116-124`,
`Channel` docstring, ADR-117); Messenger and Instagram are vocabulary only. So
`available_channel_types = ["whatsapp"]` and a PUT naming messenger or instagram is
`422 channel_type_unavailable`. The Messenger and Instagram parts of Part 1 are therefore
not built: no adapter exists to send on (section 34 is not triggered - this is absence of a
channel, not a platform rule).

- **Storage: a table, not tenant columns** (`follow_up_settings`, 0122) - unlike 0109's
  quiet hours - because the reminders are a list with a version and an editor, and the
  `If-Match` edit counter belongs to the settings, not to the workspace row.
- **Default ON** for every workspace that existed (0122 writes the row) and for every
  workspace created through `AuthService` registration and `WorkspaceService` creation
  (`default_settings_row`, in the creating transaction). **A workspace with no row is "not
  configured" and runs nothing** (`enabled: false, version: 0`): that is a workspace written
  directly - as test fixtures do - and it keeps every existing suite on today's behaviour
  without editing it (FUS-03's "while sequences are disabled, everything stays as today").
- Validation and codes as FUS-01; guidance normalised with PER-05's `normalise`, then
  bounded to 500. Audited `follow_up_settings_updated`, `{previous, current}`.
- `follow_up_settings` joins `PURGED_TABLES` (the classification test requires every
  tenant table in one list).

## Phase 5 — FUS-02, FUS-03, FUS-05, FUS-06 (runs, stop rules, window)

Facts verified first: a conversation's owner is `conversations.mode` (AI / HUMAN) plus
`assigned_to_id`; a takeover is `InboxService.set_mode` (mode HUMAN, the agent's
follow-ups cancelled, a colleague's kept - PD-CRM-1) and a person may send on an
AI-owned conversation without taking it over (`POST /messages`, origin `human`). The lead
vocabulary is NEW, CONTACTED, QUALIFIED, PROPOSAL, WON, LOST (`app/db/models/lead.py:49-62`),
changed only by `LeadService.change_status`. **Wasla records no purchase or order**
anywhere (no order tool, no Shopify event): `purchased` is the AI's skip reason and
`lead_closed` (won / lost) the recorded stop. A conversation is closed only by
`InboxService.close`; an opt-out only by `opt_out.record_opt_out`.

- **0123**: `follow_up_sequence_runs` (anchor, state, closed `end_reason`, `steps_sent`;
  tenant-composite key to its conversation; partial unique index: one active run per
  conversation) and on `follow_ups` `origin` (`one_off` / `sequence`), `sequence_run_id`
  (tenant-composite key), `step_index` (1-based), `anchor_at`, `skip_reason` (closed),
  with CHECKs that a step names all three. **No steps snapshot on the run**: FUS-06 says a
  step's delay is read when that step is scheduled, so a snapshot would only be a second,
  stale copy of the settings.
- **Start** (`SequenceService.on_outbound_reply`), in the send-intent transaction of an
  AGENT or HUMAN non-template message (`MessagingService`, right after the message is
  staged): settings cover the channel; mode AI; open; the customer wrote since the last
  run's anchor (by Wasla's stored `created_at` of an inbound message - **not**
  `last_inbound_at`, which is the platform's whole-second timestamp: a reply in the same
  second as the anchor read as older and blocked the next run; found by a flaky test and
  fixed before commit); no colleague one-off waiting; no stop rule. An active run is
  restarted (`sequence_restarted`); an agent one-off is replaced (`replaced_by_sequence`).
- **Steps** are `follow_ups` rows (origin `sequence`, `created_by_kind` SYSTEM); step n+1 is
  scheduled in the transaction that settles step n as SENT, from its `sent_at`.
- **Window** (FUS-05): `window_end = last_inbound_at + policy.window - margin`
  (`FOLLOW_UP_WINDOW_MARGIN_SECONDS`, 1800), read from the channel policy's `window`; a
  step due later is never scheduled (`window_closing`); at dispatch a step past it is
  skipped `window_closed`. A step is only ever sent with `send_text` (free-form); there is
  no template or tag path for it.
- **Stop rules** (FUS-06) in the causing transaction: inbound (`customer_replied`, or
  `opted_out` for a STOP), `set_mode` HUMAN and an assignment to a person
  (`human_took_over`), `close` (`conversation_closed`), `record_opt_out` (`opted_out`, that
  channel), `change_status` to WON/LOST (`lead_closed`), the settings PUT
  (`settings_changed`: off, platform removed, or the waiting step's reminder deleted).
  The stop paths never flush: a stop runs inside somebody else's write, and an early
  flush expired the lead's server-side `updated_at` before the route serialised it
  (`test_crm_integrity` caught it). Dispatch re-checks every rule plus serving, plan and
  channel.
- **FUS-03**: the orchestrator does not describe `schedule_follow_up` while sequences cover
  the conversation; `FollowUpService.schedule` refuses an agent's call with
  `SEQUENCES_AUTOMATIC`; a person's one-off cancels the waiting step
  (`replaced_by_person`) and ends the run.
- Phase 5's step text is `FOLLOW_UP_SEQUENCE_PLACEHOLDER_TEXT` (test-only); Phase 6
  replaces it with the AI's.

Send-path sweep after Phase 5 (113 suites: campaign*, follow_up*, crm*, agent*, tool*, messaging*,
manual*, ai_*, lead*, inbox*, conversation*, opt_out*, channel*, whatsapp*, omnichannel*, timezone,
workspace*): one failure, `test_workspace_purge::test_the_classification_covers_every_tenant_scoped_table`
- the new `follow_up_sequence_runs` table was not classified. Added to `PURGED_TABLES`; both purge
files re-run green. No earlier expectation changed.

## Phase 6 — FUS-04, FUS-07 (the AI-written step, the skip, charging)

Facts verified first: an agent turn's charge is a hold taken in the engaging transaction
under the workspace's advisory lock (`EntitlementService.hold_ai_turn`) and settled by
`AITurnCharge` into one `ai_turn` usage event naming the agent turn (ADR-131); the
orchestrator's guardrails are the channel's length (`prepare_channel_reply`, which
*shortens*) and the automation disclosure (`ai_worker._with_disclosure`). There is no
existing "secret" or "other customer's data" guard on agent replies.

- **Three parts in the worker** (`FollowUpWorker._dispatch_step`): `prepare_step`
  (decide, charge, read the request) commits; `StepWriter.write` runs with no session;
  `finish_step` re-takes the row with `claim_by_id(id, claim_token)` - a row the customer,
  a colleague or a restart touched meanwhile is not found and the text is dropped
  (`follow_up.step_discarded`). One-off follow-ups keep the old single-transaction path.
- **The writer** (`app/services/follow_up_writer.py`): Responses API, strict JSON schema,
  no tools, operation `respond_follow_up`, a `ResponsesClient` with `max_attempts=1`; the
  one retry is the worker's after `FOLLOW_UP_AI_RETRY_SECONDS`.
- **Guardrails for a step**: never shortened (a shortened nudge is a different message) -
  over the channel limit is skipped `guardrail`; a credential shape or any phone number is
  skipped `guardrail`. **Disclosure is not composed onto a step**: a run starts only after an
  AI reply, which already carried the disclosure when due (OMNI-041); recorded.
- **Charging without an agent turn**: the step's `ai_turn` event names
  `metadata.follow_up_id`. AI invariant B7 ("every ai_turn names an agent turn") is amended
  to accept a charge naming a sequence step of its own workspace - an expectation of an
  earlier stage changed on purpose (ADR-131 amended by ADR-151).
- The Phase 5 placeholder setting is removed; the harness fakes OpenAI at its transport
  (`ScriptedModel`).

Send-path sweep after Phase 6: 132 suites (Phase 5's set plus metric*, rag*, entitlement*,
usage*): 0 failed.

## Phase 7 — FUS-09 (what a person sees)

- `GET /conversations/{id}` gains `follow_up` (`none` / `scheduled` / `ended`, step of total,
  due time with its local twin, end reason and time); the list leaves it null (no per-row
  query on the inbox list).
- `GET /conversations/{id}/follow-ups`: the conversation's history through the existing
  cursor-paged follow-up list (latest due first): origin, step, status, requested and due
  times, closed skip / cancel / defer reasons, and the text of a **sent** one only - the
  model's text for a skipped step is never stored, so it cannot be shown.
- `DELETE /conversations/{id}/follow-up` (any member, as replying): ends the run
  `stopped_by_person`, audited `follow_up_stopped_by_person`, returns the state.
- **The "activity / timeline"**: Wasla has no conversation timeline endpoint (the campaign
  activity feed is campaigns-only), so the history route *is* the follow-up timeline; the
  contract says so.
- 242 operations.

## Phase 8 — HMT-01..03 (template header library; manual and follow-up headers)

Facts verified first: follow-ups and the manual template route passed raw `components` to Meta
(`conversations.py` send_template; `follow_up_service.py` TEMPLATE branch); Wasla never
uploaded or checked a header for them. Header retention defaults to 0 (never purged).

- **HMT-01: nothing renamed.** `campaign_header_media` holds every template header file; the
  library is `/template-header-media` (POST admin, GET member, DELETE admin; the campaign
  upload route stays). Deleting sets `deleted_at` (0124): unlisted, unusable, and purged by the
  next sweep **whatever the retention** (`deleted_for_purge`); refused `409 header_media_in_use`
  while a live campaign or a waiting follow-up names it. Retention also never claims a file a
  waiting follow-up names.
- **HMR-02 / HMT-02 key**: `template_header_uploads` - a person's and a follow-up's template
  share one Meta id per (workspace, file, connection) (`campaign_id` null, partial unique index);
  a campaign's own uploads are keyed (campaign, file, connection) for Phase 9.
- **HMT-02**: `TemplateHeaderMediaService.resolve` refuses raw media header parameters
  (`header_media_must_be_uploaded`), a missing file for a media header (`header_media_required`),
  a wrong kind (`header_media_kind_mismatch`), another workspace's file (404). The header
  component comes from the one builder, `template_parameters.header_file_component`, now shared
  by `send_components`. Uploads go through `MessagingService.upload_header_file`.
- **HMT-03**: `follow_ups.header_media_id` (tenant-composite key, ON DELETE RESTRICT), checked
  at scheduling and uploaded at dispatch; the worker and the service take the media store by
  injection (`storage=`).
- Counter `wasla_template_header_uploads_total{origin}`. 245 operations.

## Phase 9 — HMR-01..03 (per-recipient header media)

Facts verified first: one file per campaign (`campaigns.header_media_id`), uploaded once per
campaign (`_header_file` under the lease), and `_personal()` / `send_components` always took
the campaign's file.

- **0125**: `campaigns.header_media_mode` (`campaign` default / `per_recipient`),
  `header_media_rules`, `header_media_refusals`; `campaign_recipients.header_media_id`
  (tenant-composite, RESTRICT); reason `header_media_refused`.
- **Composition**: rules `{when: {field, key?, equals}, header_media_id}` on a PER-01 field;
  checked by the one composition validator (create and draft edit): workspace (404), stored,
  the default file's kind (`header_media_kind_mismatch`), at most
  `CAMPAIGN_HEADER_MEDIA_MAX_FILES` distinct files (`header_media_too_many_files`); rules
  without the mode are refused, the mode without a default file too.
- **Materialisation**: `header_file_for(campaign, facts)` - first matching rule, compared as
  PER-05 normalises, ignoring case - frozen on the recipient. Preview shows each person's file.
- **Worker** (`_deliver_personal_header`): per batch a cache of each file's parameter; a file
  not yet fresh is uploaded under the lease (`template_header_uploads` keyed by campaign, file
  and connection, written under the lease in its own transaction), at most
  `CAMPAIGN_HEADER_UPLOADS_PER_MINUTE` uploads per batch; a refused upload or a 131053 on send
  counts a refusal for that file (`header_media_refusals`) and defers the person (no attempt);
  three refusals skip that file's people. Meta unreachable or a credential stops/waits the
  campaign as in campaign mode. The campaign-mode path is unchanged.
- **HMR-03**: retention and the library delete treat a file named by any recipient of a live
  campaign as in use.
- **Sweep findings** (fixed before commit): `CampaignRead.header_media_mode` rejected an
  unflushed `Campaign` (attribute None before the column default applies) - 6 cases of
  `test_campaign_endpoints.py` and the TZ-05 unit test; now defaulted like `version`.

- **Style fix** (1a3b262): 0124 had been committed without `black`; formatting only.

## Phase 10 — RT-03 publish points, RT-04 incremental reads

- **Publish points** (`app/core/events.py`): ORM collectors on `Message` (insert:
  `message.created` with `conversation_id`, `message_id`, `direction`, `channel_type`,
  `created_at`; status change: `message.status`), `Conversation` (status / owner / assignee /
  priority: `conversation.updated` with the field names), `FollowUp` (insert, status or time
  change: `follow_up.updated`) and `Campaign` (status or waiting reason: `campaign.changed`,
  per transition, never per recipient). Hints wait on `session.info` and are written by
  `after_commit`; `after_rollback` drops them. One stream per workspace,
  `wasla:rt:events:{tenant_id}`, `XADD MAXLEN ~ RT_STREAM_MAXLEN`, `XTRIM MINID` to
  `RT_STREAM_RETENTION_SECONDS`, expiring when idle. Collecting at the ORM covers every path
  that writes these rows (inbound webhook, send path, status webhook, takeover, follow-up and
  campaign workers) without each remembering to; a Core bulk `UPDATE` publishes nothing and
  clients converge through `resync` and `updated_since`. A failed publish is logged with its
  type only and counted (`wasla_events_publish_failed_total`); the write already committed.
  The API lifespan and the worker runner set the sink and drain it on shutdown.
- **`channel_type`**: a message has no channel column; the collector reads its
  conversation's from the session's identity map, or with one select on the flush's own
  connection.
- **Incremental reads** (RT-04): `GET /conversations?updated_since=` (offset required, 422
  `updated_since_needs_offset`), inclusive, oldest change first, cursor-keyed by
  `(updated_at, id)`, closed conversations included; the first page carries an `ETag` and an
  unchanged inbox answers 304; `poll_after_seconds` is 30 without a stream and 60 with one
  (open streams counted in Redis per person). `GET /conversations/{id}/messages?after=` lists
  the later messages, oldest first; an anchor of another conversation or workspace names no
  position.
- **0126**: `ix_conversations_tenant_id_updated_at (tenant_id, updated_at, id)`,
  `CONCURRENTLY`. `updated_at` is maintained by the application's `onupdate`, not a trigger:
  every writer of the table is the application, and a trigger would be a second definition of
  "changed" for the two schema modes to agree on.
- **The list's response** gained `poll_after_seconds` (additive; the one test asserting the
  exact key list was updated on purpose).

## Phase 11 — RT-01, RT-02, RT-05 (the stream), RT-07 (the polling budget)

- **Routes**: `POST /events/ticket` (any member, workspace bucket) and
  `GET /events/stream?ticket=` (`app/api/v1/events.py`). The stream resolves no session -
  a browser's EventSource cannot send an Authorization header (WHATWG HTML, server-sent
  events) - so it is on the open-route list (now 19, `test_route_authorization.py`) with the
  ticket as its authorization. Tickets are `secrets.token_urlsafe(32)`, stored as a SHA-256
  hash for `RT_TICKET_TTL_SECONDS`, redeemed with `GETDEL`, bound to (person, workspace,
  token version), never logged (asserted against the captured log). The route is exempt from
  the request timeout (`TIMEOUT_EXEMPT_PREFIXES`) and from per-request buckets; it is bounded
  by `RT_STREAMS_PER_USER` / `RT_STREAMS_PER_WORKSPACE`, counted in Redis sorted sets across
  containers, with members that miss three keep-alives dropped (a dead process never locks a
  person out). Over the limit: `429 stream_limit`.
- **One reader per workspace per process** (`StreamHub`): one `XREAD BLOCK` loop per
  workspace with a subscriber, fanned out to that process's queues; 200 streams of one
  workspace are one blocking read.
- **Lifetime** (RT-05): every keep-alive re-checks the token version (`token_changed`), the
  membership (`membership_revoked`), the workspace being served (`workspace_unavailable`) and
  the person's active workspace (`workspace_switched`: one active workspace per person, set
  when a stream opens); each closes with `event: closed` and its reason, then the response
  ends. `may_read` is the seam for a narrower read rule: today every member reads every
  conversation of the workspace (the route table), so the stream delivers the workspace's
  hints, which are ids only.
- **Defects found and fixed before commit** (in this phase's own uncommitted code, never
  committed or run before):
  1. `_Subscriber` was an `eq=True` dataclass, so unhashable: every stream failed on open.
  2. A new reader started at `$`, so a hint committed between subscribing and its first
     `XREAD` reached nobody. Now every open records the newest id before subscribing, a new
     reader starts there, and the stream catches up from that floor (or from Last-Event-ID)
     after subscribing, deduplicating by id
     (`test_an_event_right_after_the_stream_opens_is_never_missed`).
  3. Keep-alives and the RT-05 checks ran only after a full idle interval, so a busy
     workspace's streams were never re-checked; they now run on a fixed schedule.
  4. A full subscriber queue dropped hints silently; it now sends `resync`.
  5. After a `resync`, the stream carries on from the newest hint instead of replaying the
     whole retained stream.
- **Graceful shutdown** (RT-06, built here because the stream needs it): uvicorn waits for
  open connections before the lifespan's shutdown, and a stream never finishes by itself.
  `app.main.install_stream_shutdown` wraps `uvicorn.Server.handle_exit` once per process so
  the exit signal first makes every stream say `closed` (`server_restart`) and end.
- **RT-07**: a route marks buckets it charges only for a changed answer
  (`CHARGED_AFTER_ETAG`); with `If-None-Match` the guard defers the charge and the handler
  calls `charge_deferred` after its ETag check, so a 304 costs no campaign token; the
  workspace bucket still counts it. `GET /campaigns/progress` uses it. Proved with a campaign
  bucket of 2: 60 conditional 304s then two 200s, the third 429; a stale ETag is charged.
- **Tests added**: `test_events_stream.py` (14, a real uvicorn in-process and a real Redis;
  httpx's ASGI transport buffers a response to its end, so it cannot read a stream),
  `test_campaign_progress_budget.py` (3). The shutdown test drops uvicorn's captured signal,
  which uvicorn otherwise re-raises after stopping - it ended the pytest session on the
  first run.
- **Numbers**: p95 delivery of `message.created` over 100 inbound messages on one stream,
  measured from before the ingestion call: < 1 s (asserted; recorded by Phase 15's run with
  `WASLA_RT_NUMBERS_FILE`).
- **Docs**: `docs/API.md` (the two routes, the event table, reconnects, the polling
  budget; operation count 247, 19 unauthenticated), `docs/AUTHORIZATION.md` (route rows and
  the open-route rationale), `.env.example` (`RT_*`).
