# Omnichannel Final Findings Remediation

Remediation of `OMNICHANNEL_READINESS_AUDIT_FINAL.md` (audit of `3473068`, verdict
*OMNICHANNEL READY WITH LIMITED REMEDIATION*, 71 / 100). Branch
`omnichannel-final-remediation-20261002`, worktree
`E:\wasla-omnichannel-final-remediation`. Nothing was pushed, merged or deployed;
no Meta App, webhook subscription, number or WABA was touched; no real provider
was contacted.

Finding IDs keep the existing series: OMNI-029..058 and backlog OMNI-R26..R46
are the final audit's; new decisions are ADR-124..130, with ADR-120, ADR-121 and
ADR-123 amended.

---

## 1. Executive Summary

Every P0 and P1 finding is closed in code, with regression tests, a runtime
before/after measurement and a killed mutant each:

- **A "Stop promotions" tap now opts the customer out and is not answered**;
  taps and list choices keep their words and payload as a neutral reply action
  the AI reads as `[tapped: ...]` (OMNI-030). Taps the old code lost can be
  replayed from retained evidence with a dry-run-first operator command, which
  must run on production **within 30 days of deploying 0085**.
- **Status resolution is an index seek** - 4 buffers, 0.031 ms on 210,000
  messages, against a parallel sequential scan of 4,999 buffers and 22.6 ms; a
  30,000-status campaign burst spends 27.8 s instead of 597.5 s in the lookup
  (OMNI-029). A plan regression test runs on the migration-built schema.
- **Window anchors only move forward** (OMNI-036); **Meta-sized deliveries are
  accepted** and any refusal is counted and alerts (OMNI-034); **the inbox renders
  a channel in any state** and a channel can be **paused by configuration**
  (OMNI-031); **Meta errors are classified by Meta's code** and sweeps wait or stop
  (OMNI-035).
- The adapter seam now carries **who sends and by which mechanism** - the AI can
  never use a human-agent tag (OMNI-033) - and **automation is disclosed** on
  channels whose policy requires it (OMNI-041); event/message identity is a
  stated contract (OMNI-032); colliding events are kept (OMNI-043); follow-ups are
  channel-aware with terminal policy refusals (OMNI-039); external echoes hand the
  conversation to a person (OMNI-037); media can be offered by short-lived
  single-object URL and per-type limits are checked before staging
  (OMNI-040/045); read watermarks compare provider time (OMNI-042).
- Low findings closed where small and safe (OMNI-044, 046, 048, 049, 050, 052,
  053 postbacks, 054 authentication templates); OMNI-047, 051, 055 are accepted or
  deferred with a dated plan.

Mutation matrix: **26 of 26 meaningful mutants killed**, one after adding a
policy-level test the matrix showed was missing (M-O16). Quality gates: ruff,
black and mypy clean; Alembic single head `0091`, `alembic check` clean, 0 NOT
VALID constraints, 0 invalid indexes; promtool and amtool pass.

**Verdict: OMNICHANNEL FINDINGS CLOSED WITH REQUIRED PRODUCT DECISIONS AND
EXTERNAL VERIFICATION** (section 39). Updated score **85 / 100** (section 38).

---

## 2. Starting Repository State

| Item | Value |
| --- | --- |
| `OMNI_REMEDIATION_BASE_HEAD` | `347306803a6b0d0582002b14c6aabadec885e34e` (`3473068`) - canonical `worktree-billing-google-auth`, unchanged since the audit |
| Branch base | `327b2d6` = the audit report commit on top of `3473068` (report only) |
| Remediation branch / worktree | `omnichannel-final-remediation-20261002` / `E:\wasla-omnichannel-final-remediation` |
| Migration head at base / now | `0084` (single) / `0091` (single) |
| origin vs canonical | `origin/worktree-billing-google-auth` = `354db53` after `git fetch --prune`; canonical is **12 ahead, 0 behind** (unpushed; includes the live-text-turn fix `aa11098`) |
| Canonical working tree | clean tracked tree; the user's nine untracked `*_AUDIT.md` / `*_REMEDIATION.md` / `FRONTEND_MASTER_PLAN.md` files left untouched |
| Stash | empty |
| `app.__file__` | `E:\wasla-omnichannel-final-remediation\app\__init__.py` (asserted in every lane; mutants in `E:\wasla-omni-frem-mut`, probes in `E:\wasla-omni-frem-probe`, each asserting its own) |
| Python / Docker / Compose | 3.12.7 / 29.8.0 / 5.5.1 |
| PostgreSQL / Redis | `pgvector/pgvector:pg16` = 16.15 / `redis:7-alpine` = 7.4.11 |

**Disposable infrastructure only**: `wasla-omni-frem-pg` (127.0.0.1:55911),
`wasla-omni-frem-redis` (56911), `-redis2` (56912), `-redis3` (56913),
`wasla-omni-frem-minio` (59911, the pinned GHCR mirror). Databases
`wasla_omni_rem_models`, `_migrations`, `_probes`, `_plans`, `_mut`. The
developer's Redis DB 0, the development database and the audit's
`wasla-omni-final-*` containers were never used. `TEST_DATABASE_URL` was explicit
in every lane (WQ-12).

---

## 3. Findings Revalidation

HEAD had not advanced past the audited revision, so every finding applied as
written. Each P0/P1 baseline was reproduced on the remediation's own PostgreSQL
before its fix by an out-of-repository probe file that records observations
without asserting them; the same file produced the "after" column (section 60
of the brief, table in section 27 below). Before:

| Probe | Before (exact) |
| --- | --- |
| P1 `button` "Stop promotions" / STOP-PAYLOAD | kind `interactive`, body `null`, action null, model sees `[interactive]`, opted_out `false`, agent jobs 1 |
| P1 `button_reply` "Yes, book it" / `list_reply` "Pro plan" | body `null`, model sees `[interactive]` |
| P1 control: text "Stop promotions" | opted_out `true` |
| E1 status lookup, 200 workspaces / 10,000 conversations / 210,000 messages | Parallel Seq Scan on messages, 4,999 buffers, 22.575 ms |
| P2 inbox, one WhatsApp + one Instagram-labelled conversation | `422 "Wasla cannot operate this channel."`; Instagram detail 422 |
| P3 split event/message ids, Redis refusing, recovery runs | `abandoned 1`, event `failed: projection_missing`, 0 agent jobs |
| P10 message now, then one a day earlier | `last_inbound_at` = the older timestamp |
| P11 signed deliveries 1,469,609 B / 2,571,644 B / 2,905,594 B / 3,500,025 B | 413 / 413 / 413 / 413, counted by no metric |
| P6 720 Arabic characters (1,320 bytes) on a 1,000-byte channel | accepted at scheduling; dispatch `pending`, attempts 1, retry scheduled |
| P9 "price?" then external echo "It is 500." | transcript inbound only; mode `ai`; agent job queued |
| P5 watermark 1 s before local `sent_at` | not read |
| P4 same provider id on a second connection of one workspace | stored 0, collisions 1, no evidence kept |

---

## 4. Locked Remediation Decisions

| Decision | Applied |
| --- | --- |
| OMNI-030 treated as P0 (present-day consent failure) | yes - fixed first |
| OMNI-029: predicate route over a new index | predicate route (tenant ids of the holders); no migration needed; the index route was not required |
| OMNI-032: option A (enforce `event_id == message_id`) or B (store the message id) | **A** - every Meta product identifies a message event by its `mid`; no planned adapter composes message ids; option B documented as the fallback (ADR-120 amended) |
| OMNI-043: keep legacy uniques, store collisions as evidence | yes; O8 prepared, not applied |
| Every change additive, nullable-first, `CONCURRENTLY` on hot tables, downgrades refuse rather than lose data | yes (section 25) |

## 5. Product Decisions Applied

| Item | Decision applied |
| --- | --- |
| ADR-122 (meters, quota, connection limit, price, opt-out scope) | **Not decided here.** `ChannelRegistry` still refuses an adapter whose meter is undecided - DEFERRED PRODUCT DECISION |
| OMNI-037 external echoes | Matching echo (provider id on the same connection, or the same words of a Wasla send still in flight): confirm, change nothing. Unmatched: project as outbound origin `external`, hand the conversation to a person, cancel pending agent nudges; a queued or composing agent turn is suppressed by the `human` mode it reads before it engages and again before it sends; never touch a window (ADR-129) |
| OMNI-041 automation disclosure | Required for Messenger/Instagram policies, off for WhatsApp; on the first AI reply, after a 24 h gap (configurable), after a human-to-AI hand-back; prepended to the same message; default en/ar wording exactly as briefed; workspace may override wording, never switch it off (ADR-127) |
| OMNI-040 URL media | Storage capability and adapter seam only; single-object SigV4 URL, default 10 min, hard maximum 1 h; privacy trade-off recorded in ADR-128; WhatsApp keeps uploading |
| OMNI-038 history ordering | DEFERRED TO COEXISTENCE TRACK |
| OMNI-051 global FIFO | DEFERRED (same decision as WQ-09) |
| OMNI-053 | Representation decided in ADR-125: postback implemented through OMNI-030's carrier; edit / unsend / reaction deferred to the adapter stage with a recorded direction |
| Opt-out tap and agent turns | an opt-out tap records the opt-out and **queues no agent turn**; a typed stop word is unchanged (honoured and answered) |

---

## 6. OMNI-030 Reply Actions and Button Opt-Out

**Change** (`f671b45`, migration 0085, ADR-124). `InboundEvent.action:
ReplyAction(source, id_or_payload, title)`; the WhatsApp parser
(`app/integrations/whatsapp/payload.py::reply_action`) maps `type: button`
(`button.text` -> text and title, `button.payload` -> payload, source `button`)
and `interactive.button_reply` / `list_reply` (`title`, `id`; sources
`button_reply` / `list_reply`); `context.id` is carried. Over-long ids are
dropped, never cut. Persisted as `messages.action_source / action_payload /
action_title`; exposed additively as `MessageRead.action`. `build_window`
renders `[tapped: <title>]`.

Opt-out: a tap whose words are a stop phrase (`app/services/opt_out.py`) or whose
payload a template on that number marks as the marketing opt-out
(`PUT /api/v1/templates/{id}/opt-out-payloads`, admins) goes through the one
opt-out writer with `opt_out_via = reply_action`; `answer=False`, so no agent
turn. Same `marketing_opt_out_at`, same counters (`wasla_opt_outs_total{via}`),
same audit path.

**Tests** (`tests/integration/test_omnichannel_reply_actions.py`, through
`WhatsAppIngestionService` on PostgreSQL with Meta-documented shapes - messages
webhook reference re-read 2026-10-02 - plus the adapter contract suite): stop
tap opted out with no turn; marked payload with unrelated words opts out; a
payload marked on another number means nothing; `button_reply` and `list_reply`
answered with the title in the model's window; API reports the action; typed
"Stop promotions" unchanged; location unchanged; double tap does not move the
timestamp; admin-only marking.

**Required numbers** (test `test_the_numbers_every_stop_tap_is_recorded_none_answered_none_unreadable`, five customers each tapping "Stop promotions"):

| Measure | Value |
| --- | --- |
| button taps ingested | 5 |
| opt-outs recorded | 5 |
| AI turns queued for opt-out taps | 0 |
| messages rendered `[interactive]` | 0 |

Runtime after (probe P1): stop tap -> body "Stop promotions", action
`{STOP-PAYLOAD, "Stop promotions", button}`, model sees `[tapped: Stop
promotions]`, opted_out `true`, opt-outs counted 1, agent jobs **0**;
`button_reply` / `list_reply` -> titles kept, `[tapped: ...]`, agent job 1;
location and typed-stop controls unchanged.

Mutants: M-O01, M-O02, M-O03 killed.

## 7. OMNI-030 Opt-Out Evidence Recovery Tool

`python -m scripts.omnichannel_invariants recover-button-opt-outs [--dry-run|--apply]`
(`50aee89`, `app/services/opt_out_recovery.py`). Reads retained `whatsapp_events`
message payloads of type `button` / `interactive`, matches stop phrases and each
workspace's marked payloads, dry-run by default and read-only, prints counts per
workspace id only (no phone, BSUID or text - asserted on captured output),
`--apply` writes through the normal writer with provenance `replay` dated by the
tap, idempotent, and a newer resume (`marketing_resumed_at` after the tap, or a
colleague clearing the opt-out) wins.

**Seeded proof** (two workspaces; 18 lost stop-phrase taps, 4 lost taps on a
marked payload with the words "Not now", 5 ordinary taps, 3 of the stop-phrase
contacts resumed after their tap):

| Run | candidates | applied | skipped (newer resume) | already opted out | contacts opted out afterwards |
| --- | --- | --- | --- | --- | --- |
| dry run | 22 | 19 (would) | 3 | 0 | **0** (nothing written) |
| first `--apply` | 22 | 19 | 3 | 0 | 19 |
| second `--apply` | 22 | **0** | 3 | 19 | 19 (no change) |

The 5 ordinary taps were never candidates. Mutant M-O04 (ignore a newer resume)
killed.

**Must run on production before the 30-day raw-payload redaction (DB-011)
removes the evidence** - section 36.

## 8. OMNI-029 Status Resolution and Query Plans

**Change** (`068143f`). `OutboundMessageDirectory.provider_message_lookup` now
issues `tenant_id IN (holders' tenants) AND connection_id IN (holders) AND
wa_message_id = ? AND direction = 'outbound'`, served by
`uq_messages_tenant_id_connection_id_wa_message_id`. Each holder belongs to one
workspace, so no answer changes and historical ownership (MSG-04) holds: the
holders are still every claim the connection has carried. The dead
`find_by_wa_message_id` is removed. No migration.

**Plans** (`wasla_omni_rem_plans`: 200 workspaces + 1, 10,000 conversations,
210,000 messages seeded through the real triggers, then migrated 0085 -> 0091 on
that populated data):

| Query | Before | After |
| --- | --- | --- |
| E1 status lookup as issued | Parallel Seq Scan on messages, 4,999 buffers, 22.575 ms | Index Scan `uq_messages_tenant_id_connection_id_wa_message_id`, **4 buffers, 0.031 ms** warm (first read after the migration: 4 buffers, 3 from disk) |
| E2 pre-foundation shape (reference) | Index Scan, 4 buffers, 0.157 ms | unchanged (0.72 ms cold) |
| E3 status for an id not ours | Parallel Seq Scan, 5,000 buffers, 16.156 ms | Index Scan, **3 buffers, 0.169 ms** |
| E7 E1 with `enable_seqscan = off` | Index Scan on the workspace-wide unique, 1,590 buffers (245 hit, 1,345 read) | Index Scan on the per-connection key, **4 buffers, 0.048 ms** |

Target met: E1 after (4 buffers) ≈ E2 before (4 buffers).

**Campaign status burst** (10,000 sent messages, 30,000 status events, 30
deliveries of 1,000 statuses, one transaction each through
`WhatsAppIngestionService`):

| Measure | Before (`3473068` tree) | After |
| --- | --- | --- |
| events stored | 30,000 | 30,000 |
| DB statements | 200,030 | 200,030 |
| total DB time | 869.93 s | **285.08 s** |
| status lookups: total | 597.48 s | **27.82 s** |
| lookup p50 / p95 | 19.093 ms / 26.231 ms | **0.831 ms / 1.445 ms** |
| wall time | 985.7 s | 393.4 s |

**Plan regression test** `tests/integration/test_status_lookup_plan.py` seeds
40,000 messages, first proves the old predicate *is* planned as a sequential scan
on that data (non-vacuity), then explains the statement compiled from the
repository and requires an index whose condition includes `tenant_id`; it runs in
the migration-built lane. The handover case and
`tests/integration/test_whatsapp_number_handover.py` pass. Mutants M-O05 (tenant
predicate removed) and M-O06 (historical holders ignored) killed.
`wasla_status_resolution_duration_seconds{channel}` now measures it in production
(section 22).

## 9. OMNI-036 Monotonic Anchors

`c8866c6`: `touch_inbound` and `touch_outbound` advance `last_inbound_at` and
`last_message_at` with `GREATEST(...)` in one `UPDATE ... RETURNING`; a closed
conversation still reopens on any customer message; outbound never touches the
window. Tests (`test_conversation_anchors.py`, through `WhatsAppIngestionService`,
the older message genuinely ingested second): newer-then-older stays newer,
older-then-newer moves, closed reopens, inbox order unaffected, a send never moves
the order back, and a real two-session race where the older delivery commits last
settles on the max. Probe P10 after: `anchor_is_newer true`,
`last_message_is_newer true`. The audit's Q4 is `verify`'s
`window_anchor_older_than_newest_inbound` (non-vacuous). M-O07 killed.

## 10. OMNI-034 Webhook Cap and Visibility

`287e3fc`: `WEBHOOK_MAX_REQUEST_BYTES` 3 MiB (configurable, still enforced before
the signature, far below the 32 MB general cap); the stale "a few kilobytes"
comment is gone; `wasla_http_body_too_large_total{route_group}` (`webhook` |
`api`); a streamed body cut off at the cap is answered 413 and counted instead of
escaping as `ClientDisconnect`; alert `WebhookBodyTooLarge` (critical).

Probe P11 after (correctly signed, through the real ASGI app): 1,469,609 B ->
**200**, 2,571,644 B -> **200**, 2,905,594 B -> **200**, 3,500,025 B -> **413,
counted**. promtool: one refusal fires, none and an api-only refusal do not.
M-O08, M-O09 killed. The email webhook's oversize test now posts 4 MiB
(`812284c`).

## 11. OMNI-031 Tolerant Inbox and Paused Channels

`886af4e`, ADR-126: `ChannelState` operational / paused / unavailable;
`PAUSED_CHANNELS` (configuration, no table). Presentation uses `state_for` and
`known_policy`, never raising: a non-operable conversation renders with
`service_window_open: false` and `reply_policy` with nothing sendable and
`state`. Acting (`adapter_for` / `policy_for`) still refuses: sends, agent turns,
tool calls, media fetches; follow-ups and campaigns wait `PAUSED_RECHECK` without
spending an attempt. `wasla_channel_state` gauge; `ChannelPausedForADay` alert.

Route tests through the real app and `_present_all`
(`test_channel_pause_and_inbox.py`): mixed page 200 with both listed; channel
filter and detail render; paused channel listed and every send refused; rolling a
channel operational -> paused -> removed leaves WhatsApp's rendering identical;
paused inbound kept, agent refused; send choke point refuses; follow-up and
campaign wait. Probe P2 after: page **200**, 2 items, Instagram `reply_policy.state
"unavailable"`, `?channel=whatsapp` 200 with 1, Instagram detail 200. M-O10,
M-O11 killed.

## 12. OMNI-035 Meta Error Classification

`64c925e`, ADR-130: `app/integrations/meta/errors.py`, code first then status -
throttled `4, 80007, 130429, 131056, 131057` (or bare 429) -> `RateLimitedError`,
health `rate_limited`, sweeps wait; credential `0, 190` (or 401) ->
`ProviderAuthError`; connection `3, 10, 200-299, 131005, 368, 131031, 133010` ->
`ProviderConnectionRefusedError`, health `permission_missing`, sweeps stop;
per-message otherwise. ADR-093 uncertainty unchanged.
`wasla_provider_errors_total{provider, class}`.

Tests: `tests/unit/test_meta_error_classes.py` (each code returned with HTTP 400,
bare 429, 401); `test_provider_error_sweeps.py` proves the sweeps: 130429 makes a
campaign wait and fails nobody; code 10 stops the campaign after one recipient
and records `permission_missing`; 131031 / 133010 stop it too; 131047 stays
per-recipient; a throttled follow-up waits with no attempt spent; a connection
refusal stops the follow-up sweep; a person sending into a throttle gets the
undelivered message back. M-O12, M-O13 killed.

## 13. OMNI-032 Event / Message Identity

Option A (`ad599ee`, ADR-120 amended): `InboundEvent.__post_init__` refuses a
`MESSAGE` / `ECHO` event whose `event_id` is not its `message_id`; statuses keep
composed ids. Contract cases refuse composed or missing ids; every message either
adapter produces is identified by its message id; two stranded-media sweeps on one
file requeue it once. Probe P3 after: split ids -> **refused by contract
(`ValueError`) at construction**, so no event is stored to strand; identical ids
-> 1 agent job, event `processed` (the probe makes Redis refuse the enqueue
before recovery runs). `verify` gained
`message_event_without_its_projected_message`. M-O14 killed.

## 14. OMNI-043 Collision Evidence

`cc94353`: when the legacy workspace-wide key refuses an event another connection
holds, the delivery is stored on its own connection as `failed` evidence, payload
intact, under `collision:<connection>:<event id>` (SHA-256 for an over-long id);
never projected, recovered or redacted; a replay writes nothing more; the
collision is still counted. Probe P4 after: stored 0, collisions 1, the second
connection holds 1 event `failed / event_id_collision`, payload kept. Tests: kept
as evidence; replay keeps one; another workspace simply stored; long-id digest.
Census `collision_evidence_events`, invariant `collision_evidence_not_failed`.
M-O15 killed. **O8 prepared, not applied** (runbook section *O8*).

## 15. OMNI-033 Send Context

`bccbf28`, ADR-121 amended: `SendDecision(allowed, reason, mechanism)`;
`SendMechanism` standard_window | template | human_agent_tag; `OutOfWindow.TAG`;
`human_tag_window`; `ChannelSender.send(recipient, prepared,
SendContext(origin, mechanism))`; `reply_policy` per origin (`free_text_mechanism`,
`agent_free_text_allowed`). The choke point refuses a tag for any non-human origin
as a backstop. WhatsApp payloads byte-identical (pinned) and it refuses a
human-agent tag.

Tests (`test_send_context.py`, synthetic channel with Messenger's rules): inside
the window every sender uses the standard mechanism; day 3 a person gets
`human_agent_tag`; day 3 no automated sender is ever tagged; day 8 even a person
is refused; reply_policy per origin. **Mutation found a gap**: M-O16 (policy
allows an AI the tag) survived because only the choke point was exercised.
`e885310` adds `test_only_a_person_is_allowed_the_human_agent_tag` on the policy
itself; M-O16 re-run is killed (section 27).

## 16. OMNI-041 Automation Disclosure

`b3077b7`, migration 0086, ADR-127: `ChannelCapabilities.disclosure_required`;
`conversations.automation_disclosed_at`, `ai_resumed_at` (stamped by
`release_to_ai`); `tenants.automation_disclosure`; `AUTOMATION_DISCLOSURE_GAP_HOURS`
(24). Due when never recorded, after the gap, or after a hand-back - read from the
columns after inference. Prepended to the same message, counted in the channel's
unit, the reply bounded so both fit without splitting a character;
`automation_disclosed_at` written only at `SENT`.

Tests (`test_automation_disclosure.py`, disclosure asserted in the bytes handed to
the fake provider): first AI reply discloses, second within the gap does not;
after the gap discloses; after a hand-back discloses; a person never carries it;
WhatsApp unchanged; Arabic disclosure + long Arabic reply fit a 1,000-byte policy
with no split character; undelivered reply records nothing; workspace wording.
Plus `tests/unit/test_disclosure_rules.py`. Invariant
`ai_reply_on_a_disclosure_channel_never_disclosed`. M-O17, M-O18 killed.

## 17. OMNI-039 Channel-Aware Follow-Ups

`1fc236d`: scheduling validates the body in the channel's unit and limit;
templates only where the channel has them (the WhatsApp registry consulted only
there); on a channel with no automated out-of-window mechanism a due time past the
window is refused; a dispatch-time `PolicyRefusalError` is terminal (`SKIPPED`,
attempts 1, no retry); the tool description is built from the conversation's
policy. Probe P6 after: 1,320-byte Arabic body -> **refused at scheduling**
(`PolicyRefusalError`), nothing stored, provider sends 0; next-week nudge on a
no-out-of-window channel -> **refused at scheduling**. Tests include WhatsApp's
next-week template nudge unchanged and a transient failure still retried. M-O19,
M-O20 killed.

## 18. OMNI-037 External Echoes

`365afb1`, migration 0087, ADR-129 (decision in section 5). Driven through
`ChannelIngestionService` on the synthetic channel. Probe P9 after: transcript
`[inbound customer "price?", outbound external "It is 500."]`, mode **human**,
`last_inbound_at` unchanged, agent turns 0. The agent job queued by the customer's
message stays in the queue and is **suppressed** when taken up (it reads mode
`human` before engaging and again before sending) - the same effect as cancelling
it, without reaching into Redis. Tests: own echo changes nothing (rows, mode,
turns); in-flight echo is Wasla's; external echo projected and hands over; never
opens or moves the window; replay projected once; an echo arriving while the
model composes stops its reply (real `AgentWorker` turn - engagement barrier, no
duplicate reply). M-O21, M-O22 killed.

## 19. OMNI-040 / OMNI-045 Media Seam and Limits

`403cd6d`, ADR-128. `SignedUrlStorage.signed_url` (S3/MinIO; SigV4, one object,
GET, `MEDIA_SIGNED_URL_TTL_SECONDS` 600, max 3,600); `MediaUrlGrant` built from
the message's own stored key, refusing another workspace's prefix or a malformed
key before the store is asked; `prepare(...) -> PreparedContent(reference,
reference_kind upload | url)`. `ChannelCapabilities.media_limits` checked by
`require_sendable_media` before staging - WhatsApp: image JPEG/PNG 5 MB,
video MP4/3GPP 16 MB, audio 16 MB, document 100 MB; the 16 MB route cap stays the
outer bound; inbound handles record their 7-day expiry.

Tests against the real MinIO container: the URL fetches its object only, expires,
another workspace's key refused, the URL absent from captured logs, rows and
events; WhatsApp payload byte-identical; 6 MB PNG refused before staging with 0
provider calls; 16 MB MP4 sent, 17 MB refused before staging. M-O23, M-O24
killed.

## 20. OMNI-042 Provider-Time Watermarks

`82e982c`, migration 0088: `messages.provider_sent_at` from the provider's own
time (send receipt, own echo, `sent` status, external echo);
`advance_to_watermark` compares it where present, else `sent_at` within
`WATERMARK_CLOCK_TOLERANCE_SECONDS` (5, max 60). Probe P5 after: watermark 1 s
before local `sent_at` -> **read**. Tests (synthetic watermark channel with its own
clock): watermark equal to provider time reads the newest; before it reads nothing;
bounded tolerance without provider time; an echo supplies provider time. WhatsApp
per-message statuses unchanged. **Evidence is synthetic; real Messenger payloads
are EXTERNAL VERIFICATION.** M-O25 killed.

## 21. Low-Severity Findings

| ID | Outcome |
| --- | --- |
| OMNI-044 | `ec0e6c6`: per-channel failure wording; `channel.outbound_failed / _uncertain / _replayed` log events carrying `legacy_event` (the old `whatsapp.*` name) for one release; API ceilings are neutral constants in `app.channels.policy`, a test holds every adapter within them; `prepare_channel_reply` has no WhatsApp default. CLOSED |
| OMNI-045 | section 19. CLOSED |
| OMNI-046 | `e593c3b`, migration 0089: `user_preferences` parsed into a neutral `PREFERENCE` event (stored, deduplicated); stop -> opt-out via `provider_preference`; resume lifts only a provider-preference opt-out; `131050` -> `RecipientOptedOutError`, contact opted out via `provider_refusal`. Tests `test_provider_consent_signals.py`. CLOSED in code; subscribing the field in the Meta App is EXTERNAL |
| OMNI-047 | Not changed. The `contacts.wa_id` trigger still writes `source = provider` for any writer; today's only writers fill a NULL from a provider delivery. ACCEPTED RESIDUAL until O8 (runbook) |
| OMNI-048 | `9af985d`, migration 0091: `TenantMetricsRepository.channels` and `TenantAnalyticsRead.by_channel` (additive); `ix_conversations_tenant_id_channel_last_message_at` built `CONCURRENTLY` with INVALID-leftover rebuild. E5 after: Index Scan, **5 buffers, 0.203 ms** (whatsapp) / **3 buffers, 0.048 ms** (instagram) vs **Seq Scan, 5,126 buffers, 71.8 ms** with the index dropped in a rolled-back transaction; the test proves the same both ways. CLOSED |
| OMNI-049 | `ec0e6c6` / `068143f`: `ContactRepository.upsert`, `get_by_wa_id`, `OutboundMessageDirectory.find_by_wa_message_id` removed; tests use the phone identity. CLOSED |
| OMNI-050 | `9af985d`, `ff6eeec`: `META_INSTAGRAM_APP_SECRET`, `META_MESSENGER_APP_SECRET` via `webhook_signing_secret()` (fallback `META_APP_SECRET`); WhatsApp's route reads through it; documented in `.env.example` and production compose. Runbook: 7-day WhatsApp vs ~36-hour Graph retry horizons. CLOSED; which secret signs Instagram-Login webhooks is EXTERNAL VERIFICATION |
| OMNI-051 | DEFERRED PRODUCT DECISION (as WQ-09); backlog and oldest-age gauges unchanged |
| OMNI-052 | `9af985d`, migration 0090: `channel_connections.sends_per_minute` (nullable, CHECK `> 0` added NOT VALID then validated); override in both directions and null-keeps-default proven; 0 refused by the database. CLOSED |
| OMNI-053 | ADR-125; postback represented and tested through ingestion (`5093783`, `test_postback_representation.py`). Postback CLOSED; edit / unsend / reaction DEFERRED TO ADAPTER STAGE |
| OMNI-054 | `9af985d`: an `AUTHENTICATION` template to a conversation pinned to a BSUID is refused (`PolicyRefusalError`) before staging, 0 rows, 0 provider calls; to a phone still sent. Refusal CLOSED; BSUID rotation via the `system` message DEFERRED, documented |
| OMNI-055 | Default stays `v21.0`; runbook plan: staging on `v24.0` by 2026-12-01, re-verify the listed contracts with recorded payloads, change the default and deploy before **2027-01-21**. ACCEPTED RESIDUAL with a dated plan |

## 22. Observability and Alerts

| Signal | Metric / alert | Status |
| --- | --- | --- |
| inbound outcomes by channel | `wasla_inbound_events_total{channel, outcome}` (+ `external_echo`) | existing, extended |
| inbound refusals by channel and reason | `wasla_inbound_entries_refused_total` | existing |
| webhook body-limit refusals | `wasla_http_body_too_large_total{route_group}`, `WebhookBodyTooLarge` | new (OMNI-034) |
| per-channel inbound stopped | `ChannelInboundStopped` (WhatsApp's alert kept) | new (`ea914dd`) |
| status resolution latency | `wasla_status_resolution_duration_seconds{channel}` | new (`ea914dd`) |
| Meta errors by class | `wasla_provider_errors_total{provider, class}` | new (OMNI-035) |
| connection health by state | `wasla_channel_connections{health}` incl. `rate_limited`, `permission_missing`; `ChannelConnectionUnusable` | new alert (`ea914dd`) |
| opt-outs by source | `wasla_opt_outs_total{via}`: message, reply_action, provider_preference, provider_refusal, replay, team | new (OMNI-030/046) |
| paused channels | `wasla_channel_state{channel, state}`; `ChannelPausedForADay` | new |

No connection id, tenant id, phone, BSUID or text in any label (ADR-072; the
existing label guard and metric catalogue tests pass).

## 23. Foundation Regression Probes

Re-proved by the repository suites, all passing in the lanes of sections 29-30:

| Guarantee | Proof |
| --- | --- |
| Instagram-labelled inbound never answered over WhatsApp; no default channel | `test_omnichannel_second_channel.py` (M-O26 killed) |
| WhatsApp template refused on a no-template channel | `test_follow_up_channels.py::test_a_template_is_refused_where_the_channel_has_none`, second-channel suite |
| same IGSID-like sender on two connections -> two contacts, two conversations (P4b) | `test_omnichannel_identity.py`, `test_omnichannel_second_channel.py` |
| same sender in two workspaces -> two contacts; cross-tenant reads refused | `test_omnichannel_identity.py`, tenancy suites |
| display name never links identities | `test_omnichannel_identity.py` |
| number handover: late inbound and late status reach the old owner | `test_whatsapp_number_handover.py` (M-O06 killed), `test_status_lookup_plan.py` |
| duplicate replay -> no re-projection; concurrent duplicate -> one row, no 500 | `test_omnichannel_concurrency.py`, `test_collision_evidence.py` |
| engaged agent job never blindly retried; REQUESTED never resent | `test_outbound_delivery_protocol.py`, worker suites |

Audit probes P7 (trigger provenance) and P8 (message kinds) were not re-run: the
code they exercise is unchanged (OMNI-047 accepted residual; reactions still
`unsupported` by ADR-125).

## 24. Database Invariant Sweep

On the populated migration-built `wasla_omni_rem_plans` (210,000 messages, at
`0091`): `python -m scripts.omnichannel_invariants verify` -> **all 26
invariants 0, `verify: ok`**, including the new
`window_anchor_older_than_newest_inbound` (OMNI-036),
`inbound_tap_without_its_words` (OMNI-030),
`message_event_without_its_projected_message` (OMNI-032),
`collision_evidence_not_failed` (OMNI-043),
`ai_reply_on_a_disclosure_channel_never_disclosed` (OMNI-041). `census`: q1-q7 0;
`q8_shared_keys_into_whatsapp_tables` 2 (the compatibility keys O8 removes);
`q5_inbound_interactive_without_text` 0; `q6_retained_stop_taps_without_opt_out`
0; `collision_evidence_events` 0. Each new invariant is proved non-vacuous by a
violation injected in a rolled-back transaction
(`tests/integration/test_omnichannel_final_invariants.py`, `test_conversation_anchors.py`).
The audit's Q4-Q6 on a production copy remain an external action.

## 25. Migrations and Alembic Gates

| Rev | Change | Online technique | Downgrade |
| --- | --- | --- | --- |
| 0085 | `messages.action_*`, `contacts.opt_out_via`, `marketing_resumed_at`, `whatsapp_templates.opt_out_payloads`, 2 enum types | metadata-only ADD COLUMN, `lock_timeout` 15 s | refuses while data exists |
| 0086 | `conversations.automation_disclosed_at`, `ai_resumed_at`, `tenants.automation_disclosure` | metadata-only | refuses while data exists |
| 0087 | `message_origin` + `external` | `ADD VALUE` in its own autocommit block | refuses while used; label cannot be dropped |
| 0088 | `messages.provider_sent_at` | metadata-only | refuses while data exists |
| 0089 | `whatsapp_event_kind` + `preference` | autocommit block | refuses while used; label cannot be dropped |
| 0090 | `channel_connections.sends_per_minute` + CHECK | metadata-only; CHECK `NOT VALID` then `VALIDATE` | refuses while data exists |
| 0091 | channel inbox index | `CREATE INDEX CONCURRENTLY`, INVALID leftover rebuilt | `DROP INDEX CONCURRENTLY` |

D1 (status index) and D3 (`whatsapp_events.message_id`) were not needed. No
table rewrite, no long lock on `messages`. O8 not applied.

Gates on `wasla_omni_rem_migrations`: `alembic heads` -> `0091 (head)` single;
fresh `0001 -> 0091`; `alembic check` -> *No new upgrade operations detected*;
downgrade `0091 -> 0084` across every new migration and re-upgrade to `0091`;
catalog **0 NOT VALID constraints, 0 invalid indexes**. 0085 -> 0091 also applied
on the populated 210,000-message plans database. (A first downgrade of 0090 failed
on a doubled constraint name from Alembic's naming convention; fixed before commit
by dropping the constraint by its full name.)

## 26. API Compatibility

Additive only: `MessageRead.action`; `MessageRead.origin` value `external`;
`reply_policy.state` (`operational | paused | unavailable`),
`free_text_mechanism`, `agent_free_text_allowed`; `TenantAnalyticsRead.by_channel`;
`PATCH /workspace` `automation_disclosure`; `PUT /templates/{id}/opt-out-payloads`
and `TemplateRead.opt_out_payloads`. Kept unchanged and deprecated until O8:
`account_id`, `wa_message_id`, `ContactOptOutRead.wa_id`, `service_window_open`
(WhatsApp meaning). OpenAPI regenerated by the app; `docs/API.md` updated (199
operations, asserted). `GET /connections` and the identities read API were not
added - adapter-stage prerequisites (section 37).

## 27. Mutation Matrix

Each mutant is one exact source replacement applied in a detached worktree
(`E:\wasla-omni-frem-mut`), its targeted suites run, and the file restored with
`git checkout`; the worktree was clean after the run. Baseline of every targeted
suite unmutated: **262 passed**.

| ID | Mutation | Verdict | Killing test |
| --- | --- | --- | --- |
| M-O01 | `button` / `interactive` mapped to no text | KILLED | `test_a_stop_promotions_tap_is_an_opt_out_and_is_not_answered` |
| M-O02 | payload-based opt-out dropped | KILLED | `test_a_payload_the_workspace_marked_opts_out_whatever_the_words` |
| M-O03 | opt-out tap queues a turn | KILLED | `test_a_stop_promotions_tap_is_an_opt_out_and_is_not_answered` |
| M-O04 | recovery ignores a newer resume | KILLED | `test_a_newer_resume_wins_over_older_evidence` |
| M-O05 | tenant predicate removed | KILLED | `test_the_status_lookup_as_issued_is_an_index_seek` |
| M-O06 | historical holders ignored (newest claim only) | KILLED | `test_whatsapp_number_handover.py` (late message of the previous owner's tenure) |
| M-O07 | `last_inbound_at = at` without GREATEST | KILLED | `test_a_late_older_message_leaves_the_anchors_at_the_newer_one` |
| M-O08 | 1 MiB cap restored | KILLED | `test_a_meta_sized_signed_delivery_reaches_ingestion[1468122]` |
| M-O09 | body-limit counter removed | KILLED | `test_a_delivery_beyond_metas_maximum_is_refused_and_counted` |
| M-O10 | render raises through `policy_for` | KILLED | `test_a_page_mixing_an_operable_and_an_unregistered_channel_renders` |
| M-O11 | paused channel allows outbound | KILLED | `test_a_paused_channel_is_listed_and_refuses_every_send` |
| M-O12 | throttling codes ignored (status only) | KILLED | `test_the_code_decides_and_the_status_is_the_fallback[400-4-throttled]` |
| M-O13 | connection codes treated as per-message | KILLED | `test_the_code_decides_and_the_status_is_the_fallback[400-3-connection]` |
| M-O14 | split ids accepted (option A) | KILLED | `test_a_message_event_with_a_composed_id_is_refused_at_construction` |
| M-O15 | colliding events discarded | KILLED | `test_a_collided_delivery_is_kept_as_failed_evidence_on_its_own_connection` |
| M-O16 | AI origin allowed `human_agent_tag` in the policy | **SURVIVED first** (test gap) -> **KILLED** after `e885310` | `test_only_a_person_is_allowed_the_human_agent_tag[agent]` |
| M-O17 | no disclosure after a hand-back | KILLED | `test_when_a_disclosure_is_due[after_a_hand_back]` |
| M-O18 | disclosure recorded without SENT | KILLED | `test_an_undelivered_reply_records_no_disclosure` |
| M-O19 | scheduling skips the channel's limit (4,096-char check only) | KILLED | `test_a_body_over_the_channels_byte_limit_is_refused_at_scheduling` |
| M-O20 | dispatch policy refusal retried | KILLED | `test_a_policy_refusal_at_dispatch_is_terminal` |
| M-O21 | unmatched echo not projected | KILLED | `test_a_reply_typed_in_the_providers_app_is_projected_and_hands_over` |
| M-O22 | own echo treated as external | KILLED | `test_an_echo_of_wasla_s_own_send_changes_nothing` |
| M-O23 | signed URL for another workspace's key | KILLED | `test_another_workspaces_object_or_a_malformed_key_is_refused` |
| M-O24 | per-type media limit not checked | KILLED | `test_a_6_mb_png_is_refused_before_anything_is_staged` |
| M-O25 | watermark compares local `sent_at` only | KILLED | `test_a_watermark_at_the_providers_time_reads_the_newest_message` |
| M-O26 | missing adapter falls back to a registered one | KILLED | `test_without_an_adapter_the_channel_is_refused_not_sent_as_whatsapp` |

**M-O16 classification and proof.** First run: SURVIVED - a *test gap*. The
suites reached the policy only through `MessagingService._dispatch`, whose second
check refuses a non-human tag regardless of the policy. Fixed by a policy-level
contract test. Additionally the backstop alone was mutated (`if False:` in
`_dispatch`): it survives, because the policy refuses - so each guard is
individually sufficient: a *redundant guard* by design (defence in depth), not a
gap.

**Exit criterion met: meaningful mutations survived = 0; all sources restored.**

## 28. Test Non-Vacuity

| Finding | How the test reaches its boundary |
| --- | --- |
| OMNI-030 | Meta-shaped payloads through `WhatsAppIngestionService` and the real parser; no hand-built `InboundEvent` |
| OMNI-029 | the test first asserts the old predicate is planned as a sequential scan on its 40,000 seeded messages; M-O05 then fails it |
| OMNI-031 | requests through the real ASGI app and `_present_all`; M-O10 fails the route test |
| OMNI-032 | the probe makes Redis refuse the enqueue before recovery runs (identical-id case recovers 1 job) |
| OMNI-034 | deliveries of 1.47-2.9 MB, correctly signed, through the app; 3.5 MB with and without `Content-Length` |
| OMNI-036 | the older message is ingested second, and in the race test committed last |
| OMNI-037 | echoes through `ChannelIngestionService`; the race through a real `AgentWorker` turn |
| OMNI-041 | the disclosure is asserted in the bytes handed to the fake provider |
| OMNI-048 | the index is dropped inside the test's rolled-back transaction and the plan loses its channel condition |
| invariants | each new one sees a violation injected in a rolled-back transaction |

## 29. Model-Built Test Results

All at final code `5b9c1a8`, `app.__file__` inside the worktree, database
`wasla_omni_rem_models`, CI's two deselects.

| Lane (MODEL-BUILT schema) | collected | passed | failed | skipped | xfailed | xpassed | deselected |
| --- | --- | --- | --- | --- | --- | --- | --- |
| full suite | 6,558 | **6,542** | **0** | 16 | 0 | 0 | 2 |
| omnichannel targeted (remediation and foundation suites, unit + integration) | 382 | 382 | 0 | 0 | 0 | 0 | - |
| WhatsApp targeted (`tests/*/test_*whatsapp*.py`) | 259 | 259 | 0 | 0 | 0 | 0 | - |

The 16 skips are the audit baseline's: schema parity ×4 (only meaningful on a
migration-built database, run in section 30), opt-in OpenAI real-provider ×11, and
the size-limited-filesystem test ×1 (`WASLA_TEST_TMPFS` not provided locally). No
new skip. Baseline at `3473068`: 6,337 passed, 16 skipped, 0 failed; the 205 more
passing tests are this remediation's.

**An earlier full run was not green and is reported here.** At `ea914dd`/`36d9576`
the model-built lane gave 6,524 passed, **10 failed**, and the migration-built
lane 3,201 passed, **6 failed**. The failures had two causes, both fixed before the
final runs: (1) the per-product secrets were missing from `.env.example` and the
production compose file (`ff6eeec`, `test_deployment_configuration.py`); (2) the
OMNI-041 hand-back test committed a platform-wide `User` that the AI harness
never deleted, so the Google login suites (which count every user) later saw two
(`5b9c1a8`; the harness now tracks and deletes users). That run also overlapped
with an orphaned pytest process against the same database, now killed.

## 30. Migration-Built Test Results

Fresh `alembic upgrade head` (`0001 -> 0091`) on `wasla_omni_rem_migrations`,
`WASLA_TEST_SCHEMA=migrations`, final code `5b9c1a8`, CI's two deselects.

| Lane (MIGRATION-BUILT schema) | collected | passed | failed | skipped | xfailed | xpassed | deselected |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `tests/integration` + `tests/e2e` | 3,211 | **3,211** | **0** | **0** | 0 | 0 | 2 |
| plan / EXPLAIN + schema parity (`test_status_lookup_plan.py`, `test_omnichannel_low_findings.py`, `test_schema_parity.py`) | 15 | 15 | 0 | 0 | 0 | 0 | - |

Baseline at `3473068`: 3,091 passed, 0 skipped. Schema parity (models vs
migrations catalog, enum labels) passes, so 0085-0091 match the models exactly.

**Audit probes (out-of-repository, after):** P1 (5 cases), P2, P3 (2), P4, P5, P6,
P9, P10, P11 (4 sizes) - 17 passed, observations in sections 6-20; P4b is covered
by the repository suites (section 23).

## 31. Ruff / Black / MyPy

| Gate | Result |
| --- | --- |
| `ruff check .` | All checks passed |
| `black --check .` | 845 files unchanged |
| `mypy app tests` | Success: no issues found in 745 source files |

No `# type: ignore` was added except the established `**overrides  # type: ignore[arg-type]` idiom in test settings helpers, copied from existing suites.

## 32. Prometheus / Alertmanager Gates

Official containers (`prom/prometheus:v3.1.0`, `prom/alertmanager:v0.28.0`):
`promtool check rules alerts.yml` -> **SUCCESS: 67 rules found**;
`promtool test rules tests/alerts_test.yml` -> **SUCCESS**; `amtool check-config
alertmanager.yml` -> SUCCESS (1 inhibit rule, 2 receivers). Every new rule has a
firing and a non-firing case (`ChannelInboundStopped`: a stopped channel fires, a
busy one and a never-active one do not; `ChannelConnectionUnusable`: active
`permission_missing` fires after 30 min, disabled and healthy do not;
`ChannelPausedForADay`: fires after 24 h, not at 12 h, not for an operational
channel; `WebhookBodyTooLarge`: one refusal fires, none and api-only do not). No
expression was weakened.

## 33. Documentation Changes

`DECISIONS.md` (ADR-124..130; ADR-120/121/123 amended); `ARCHITECTURE.md` (seam:
reply action, paused state, `SendContext`, prepared reference, error classes;
stale `UNIQUE(phone_number_id)` and media `UNIQUE(message_id)` corrected);
`docs/WHATSAPP.md` (taps and opt-outs, `user_preferences`, error classes, 3 MiB
cap, `external` origin); `docs/API.md` (additive fields, `reply_policy` states,
opt-out-payloads route, `by_channel`, disclosure wording, 199 operations);
`docs/MEDIA.md` (per-type limits, signed URLs, TTL setting);
`docs/OBSERVABILITY.md` (new metrics and alerts); `docs/RUNBOOK.md` (0085-0091,
time-bound opt-out recovery, pausing a channel, unusable connections, retry
horizons and secrets, Graph upgrade plan, O8 conditions); `README.md` (migrations
0001-0091); `.env.example` and `docker-compose.prod.yml` (per-product secrets).

## 34. Findings Ledger

| ID | Severity | Status |
| --- | --- | --- |
| OMNI-029 | High | CLOSED |
| OMNI-030 | High | CLOSED (production recovery run: EXTERNAL VERIFICATION, before the 30-day redaction) |
| OMNI-031 | High | CLOSED |
| OMNI-032 | Medium | CLOSED |
| OMNI-033 | Medium | CLOSED |
| OMNI-034 | Medium | CLOSED |
| OMNI-035 | Medium | CLOSED |
| OMNI-036 | Medium | CLOSED |
| OMNI-037 | Medium | CLOSED (section 5 decision applied) |
| OMNI-038 | Medium | DEFERRED TO COEXISTENCE TRACK |
| OMNI-039 | Medium | CLOSED |
| OMNI-040 | Medium | CLOSED (seam and capability; Instagram use at the adapter stage) |
| OMNI-041 | High | CLOSED |
| OMNI-042 | Medium | CLOSED (synthetic proof); real Messenger payloads EXTERNAL VERIFICATION |
| OMNI-043 | Medium | CLOSED for evidence retention; legacy uniques removal = O8 |
| OMNI-044 | Low | CLOSED |
| OMNI-045 | Low | CLOSED |
| OMNI-046 | Low | CLOSED in code; Meta field subscription EXTERNAL VERIFICATION |
| OMNI-047 | Low | ACCEPTED RESIDUAL until O8 |
| OMNI-048 | Low | CLOSED |
| OMNI-049 | Low | CLOSED |
| OMNI-050 | Low | CLOSED (runbook + setting); Instagram-Login secret EXTERNAL VERIFICATION |
| OMNI-051 | Low | DEFERRED PRODUCT DECISION |
| OMNI-052 | Low | CLOSED |
| OMNI-053 | Low | CLOSED for postbacks; edit / unsend / reaction DEFERRED TO ADAPTER STAGE |
| OMNI-054 | Low | CLOSED for the authentication-template refusal; BSUID rotation DEFERRED TO ADAPTER STAGE (documented) |
| OMNI-055 | Low | ACCEPTED RESIDUAL with a dated plan (deadline 2027-01-21) |
| OMNI-056 | Info | CLOSED BY DESIGN (re-verified, section 23) |
| OMNI-057 | Info | CLOSED BY DESIGN (provider conformance; error codes and webhook size now match Meta's documents) |
| OMNI-058 | Info | CLOSED BY DESIGN (WhatsApp-only where it should be: templates, BSUID, 24 h window) |
| ADR-122 | Product gate | DEFERRED PRODUCT DECISION |

## 35. Backlog Mapping (OMNI-R26..R46)

| Backlog | Findings | Status |
| --- | --- | --- |
| OMNI-R26 | OMNI-029 | done (predicate route, EXPLAIN test, dead lookup removed) |
| OMNI-R27 | OMNI-030 | done (+ recovery tool) |
| OMNI-R28 | OMNI-031 | done (tolerant render, paused state, route tests, rollback rehearsal) |
| OMNI-R29 | OMNI-041 | done |
| OMNI-R30 | ADR-122 | open - product decision |
| OMNI-R31 | OMNI-032 | done (option A) |
| OMNI-R32 | OMNI-033 | done |
| OMNI-R33 | OMNI-034 | done |
| OMNI-R34 | OMNI-036 | done |
| OMNI-R35 | OMNI-035 | done |
| OMNI-R36 | OMNI-039 | done |
| OMNI-R37 | OMNI-040 | done (seam); Instagram use at adapter stage |
| OMNI-R38 | OMNI-037 | done |
| OMNI-R39 | OMNI-042 | done (synthetic) |
| OMNI-R40 | OMNI-043, 044, 045, 048 | done |
| OMNI-R41 | OMNI-038 | deferred - Coexistence track |
| OMNI-R42 | OMNI-046 | done in code; field subscription external |
| OMNI-R43 | OMNI-047, 049 | 049 done; 047 accepted until O8 |
| OMNI-R44 | OMNI-050, 055 | secrets setting and runbook done; upgrade planned for 2027-01-21 |
| OMNI-R45 | OMNI-051, 052 | 052 done; 051 deferred |
| OMNI-R46 | OMNI-053, 054 | postback and authentication-template refusal done; edit/unsend/reaction and BSUID rotation deferred |

## 36. Remaining External Actions

Not performed; for the operator:

1. Review, then push this branch **and the 12 earlier unpushed canonical
   commits** - `origin` (`354db53`) still lacks the live-text-turn fix `aa11098`.
2. Deploy 0085, then run
   `recover-button-opt-outs --dry-run` and `--apply` on production **within 30
   days**, before the raw-payload redaction (DB-011) removes the evidence.
3. Run the audit's Q4 and Q6 and the extended `verify` / `census` on a production
   copy; run the Q1 BSUID pairing census there too (same 30-day deadline).
4. Real-Meta checks: a template quick-reply tap and an interactive reply arrive as
   documented; a signed delivery above 1 MiB is accepted.
5. Subscribe the `user_preferences` webhook field in the Meta App (OMNI-046).
6. Confirm which secret signs Instagram-Login webhooks (OMNI-050).
7. Plan and execute the Graph API upgrade before 2027-01-21 (OMNI-055; runbook).
8. Take the ADR-122 product decisions.
9. Real Messenger watermark and echo payloads, for the OMNI-042/037 evidence that
   is synthetic here.

## 37. Adapter-Stage Prerequisites

The input to the Instagram (or Messenger) adapter stage:

- ADR-122 decisions (the registry refuses an adapter whose meter is undecided).
- Instagram route, parser, connect flow and credential storage; sender rendering
  `MESSAGE_TAG` + `HUMAN_AGENT` from `SendContext`; policy with
  `disclosure_required`, a 24 h window and a 7-day human tag; per-type media limits.
- URL media: Instagram CDN host roots for inbound fetches; outbound through
  `MediaUrlGrant` (requires the `s3` store).
- `GET /connections` and the identities read API.
- Edit / unsend / reaction representation per ADR-125; BSUID-style rotation if
  the product's ids rotate.
- Per-product signing secret confirmed against a real delivery.
- Recorded, redacted real Meta payloads (messages, echoes, reads, postbacks,
  quick replies, errors) for the contract suites, replacing the synthetic shapes.

## 38. Updated Readiness Score

| Dimension | Audit | Now | Remaining deductions |
| --- | --- | --- | --- |
| Domain neutrality | 11 | **13 / 15** | meters undecided (ADR-122); imported-history ordering (OMNI-038) |
| Identity model | 12 | **12 / 15** | BSUID scoped by WABA (over-splits); BSUID rotation deferred (OMNI-054); no merge/unlink/erasure; trigger provenance (OMNI-047) |
| Conversation / message model | 10 | **12 / 15** | OMNI-038; legacy uniques until O8 (OMNI-043); edit/unsend/reaction deferred (OMNI-053) |
| Provider adapter boundaries | 10 | **13 / 15** | watermark and echo behaviour proved on synthetic shapes only (OMNI-042/037); Instagram-Login secret unconfirmed (OMNI-050) |
| Workers / idempotency | 6 | **9 / 10** | one global agent FIFO (OMNI-051) |
| API compatibility | 7 | **9 / 10** | no connections / identities read API |
| Database migration readiness | 8 | **9 / 10** | O8 pending, with production Q1/Q4/Q6 and the opt-out replay still to run |
| Security / tenancy | 4 | **4 / 5** | `v1` credentials not yet re-sealed (unchanged) |
| Observability / testing | 3 | **4 / 5** | provider conformance proven against Meta's documents and synthetic payloads, not recorded real ones |
| **Total** | **71** | **85 / 100** | |

The previous remediation self-scored 82 and the audit measured 71; this score is
earned by measurements reported above, and every remaining point is a named
finding or a deferred item.

## 39. Final Verdict

**OMNICHANNEL FINDINGS CLOSED WITH REQUIRED PRODUCT DECISIONS AND EXTERNAL VERIFICATION**

Each condition of the brief's section 66 holds: OMNI-030 taps keep their content
and opt-out taps are honoured, and the recovery tool is proven on seeded data;
OMNI-029 is an index path on a migration-built schema with a plan regression test;
OMNI-031 renders non-operable channels and the paused state is tested; OMNI-034
accepts 3 MB and counts and alerts refusals; OMNI-036 anchors are monotonic;
OMNI-035 classifies Meta codes and sweeps wait or stop; OMNI-032 identity is
explicit and tested; OMNI-033 carries origin and mechanism and the AI can never
tag; OMNI-041 disclosure is modelled and tested; OMNI-039 is channel-aware with
terminal refusals; OMNI-037 external echoes follow the applied decision; OMNI-043
retains collisions; all meaningful mutants are killed; both suites are green and
reported separately (sections 29-30); the foundation probes show no regression;
no real provider was contacted; nothing was pushed, merged or deployed. What
remains open is ADR-122, the deferred tracks and the external actions of
section 36.

To the brief's closing question: if Wasla switched on its second Meta channel
tomorrow and had to roll it back, it can now prove - by the tests, probes,
plans and mutants above - that taps and opt-outs are understood and honoured,
that a status costs an index lookup, that no late delivery closes a window, that
WhatsApp's inbox keeps rendering while the new channel is paused (a
configuration change, not a deploy), and that the AI on a disclosure-required
channel says it is automated and can never borrow a human agent's tag. It cannot
yet prove those behaviours against *real* Instagram and Messenger payloads, nor
count that channel's usage: those are the external verifications and the ADR-122
decisions above.
