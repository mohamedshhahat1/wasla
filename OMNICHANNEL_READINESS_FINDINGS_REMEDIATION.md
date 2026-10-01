# Wasla Omnichannel Readiness Findings Remediation

| | |
| --- | --- |
| Date | 2026-09-29 to 2026-10-01 |
| Canonical branch | `worktree-billing-google-auth` |
| Canonical base HEAD | `354db5398d553f7e9ea9fee4377b99a84d9f3f3d` (equal to `origin/worktree-billing-google-auth` after `git fetch --prune`) |
| Audit | `OMNICHANNEL_READINESS_AUDIT.md`, commit `49e2e98`, branch `omnichannel-readiness-audit-20260928` |
| Remediation branch / worktree | `omnichannel-findings-remediation` at `E:\wasla-omnichannel-remediation` |
| Remediation HEAD | code complete at `5f8a788`; this report is the one commit on top of it (`docs(omnichannel): record readiness findings remediation`) |
| Alembic | `0081` before, `0084` after (single head throughout) |
| Merged / pushed / deployed | No / No / No. No production system, Meta account or real customer was touched |

---

## 1. Executive summary

**Verdict: OMNICHANNEL FOUNDATION READY WITH NAMED NON-BLOCKING DEFERRED ITEMS.**

The audit found that Wasla's business layer was already channel-neutral and that three primitives underneath it - customer identity, the connection, and provider message identity - were WhatsApp's (49/100, eight second-channel blockers, one present-day WhatsApp defect). This remediation builds those primitives, moves WhatsApp onto them without changing its behaviour, and proves the seam with contract suites, oracles, races and a mutation campaign. It implements no second channel.

**All eight second-channel blockers are closed** (OMNI-001, 003-009), and so is the present-day defect **OMNI-002**: a WhatsApp username sender - Meta's business-scoped user id (BSUID) with no phone number - is now stored, answered, and addressed by `recipient`, where before the message was answered 200 and lost.

**A second present-day defect was found during the remediation and is fixed in its own cherry-pickable commit (`aa11098`).** On the canonical branch every live inbound WhatsApp *text* message is stored but never answered: the webhook hands the agent queue the new message's id before the row is flushed, so the job carries no trigger; since TOOL-17 the worker refuses an unkeyed job as malformed and dead-letters it without a retry, and the event is already `processed`, so recovery never sees it. Reproduced through the signed route and the real worker (section 5.4). Media messages were unaffected. **Canonical still has this defect until `aa11098` (or an equivalent) is merged.**

**A test-hermeticity hole was found and closed.** After the send seam moved into the adapter, one delivery-protocol suite's provider double stopped intercepting, and its sends reached the real Graph API with a fixture token (Meta answered 401/code 190; nothing was delivered; no real credential exists in tests). The suite now refuses every live DNS lookup (`tests/conftest.py`), so a double that stops intercepting fails on the lookup instead.

**Verified at `5f8a788`, the code-complete commit, on a clean tree:**

| Check | Result |
| --- | --- |
| static gates | clean |
| model-built `pytest tests/` | 6,337 passed, 0 failed |
| migration-built integration and e2e | 3,091 passed, 0 failed (section 32 records one earlier run's infrastructure error) |
| kept-data sweep | 218 passed |
| migration matrix | passed |
| single Alembic head | at every commit |
| mutation campaign | 18 of 18 killed |
| races | three consecutive green runs |
| promtool and amtool | clean |
| runtime image | live |
| secret leaks | 0 |

**Readiness: 49 -> 82 / 100.** Second-channel blockers remaining: **0**. Named deferred items (section 24, 38): the compatibility cleanup O8 (legacy workspace-wide uniques and `wa_*` fields), product decisions on meters/quotas/connection limits/opt-out scope (ADR-122), person-level linking and erasure (R20/R21), Coexistence (R22), BSUID rotation, and a production-copy run of the Q1 census.

| Severity | Audit | Closed | Closed by design/ADR | Deferred | Open |
| --- | --- | --- | --- | --- | --- |
| Critical | 1 | 1 | 0 | 0 | 0 |
| High | 8 | 8 | 0 | 0 | 0 |
| Medium | 10 | 6 | 3 (OMNI-013, 016, 018) | 1 (OMNI-019) | 0 |
| Low | 6 | 5 | 1 (OMNI-025) | 0 | 0 |
| Info | 3 | preserved and documented | | | |

---

## 2. Starting repository state

```
git fetch origin --prune                      # nothing new
branch                                        worktree-billing-google-auth
HEAD = origin/worktree-billing-google-auth    354db5398d553f7e9ea9fee4377b99a84d9f3f3d
tracked tree                                  clean
git branch --contains 49e2e98                 omnichannel-readiness-audit-20260928 only
alembic heads                                 0081 (single)
```

Nine untracked user documents in `E:\wasla` were left untouched (`AI_FINDINGS_REMEDIATION.md`, `AI_SUBSYSTEM_AUDIT.md`, `AUTHORIZATION_MULTI_TENANCY_AUDIT.md`, `FINAL_AUTH_ACCOUNT_SECURITY_AUDIT.md`, `FRONTEND_MASTER_PLAN.md`, `RAG_SUBSYSTEM_AUDIT.md`, `REAL_META_WHATSAPP_E2E_VERIFICATION.md`, `WORKERS_QUEUES_AUDIT.md`, `WORKERS_QUEUES_FINDINGS_REMEDIATION.md`).

The remediation worktree had been created by an earlier session from `354db53`, which had written most of the implementation but committed nothing and run no database-backed test (its container start was refused). This session verified, corrected, completed and committed that work. The canonical checkout stayed clean throughout.

**Test infrastructure.** Containers of this remediation only, on ports nothing else uses: `pgvector/pgvector:pg16` (55711), four `redis:7-alpine` (56711-56714), MinIO from the pinned GHCR mirror (59711). Every suite ran against databases of its own (`TEST_DATABASE_URL`); the development stack (`wasla-postgres-1`, `wasla-redis-1`) and every other worktree's containers were not touched. Each test lane asserted `app.__file__` points into the worktree under test.

**Left in place**, for the independent verification to inspect or remove:

- Scratch worktrees `E:\wasla-omni-rem-baseline`, `-c1`, `-snap`, `-stage`, `-stage2` and `-stage3`. All are detached. `-c1` and `-snap` hold uncommitted copies that `aa11098` and `5f8a788` supersede; the other four are clean.
- Containers `wasla-omni-rem-*` (PostgreSQL, four Redis, MinIO). They are running and hold test data only.

---

## 3. Audit findings baseline

**Static gates at the base** (`75fbe51` = `354db53` plus the test-harness commit below, no application change): ruff clean; black 769 files unchanged; mypy `app tests` clean, 681 files.

| Lane | Result | Duration |
| --- | --- | --- |
| Model-built `pytest tests/` (CI's two kept-run checks deselected) | **6,091 passed, 16 skipped, 2 deselected, 0 failed, 0 errors** | 28m21s |
| Migration-built: schema parity | 4 passed | |
| Migration-built: `tests/integration tests/e2e` | **2,991 passed, 2 deselected, 0 skipped, 0 failed** | 28m45s |
| Kept-data AI/tool sweep | **218 passed** | 5m09s |

The 16 skips are the three reasons CI allows (schema parity in a model-built run; real-provider suite without an OpenAI key) plus the Linux-tmpfs storage test, which needs `WASLA_TEST_TMPFS` and cannot run on Windows.

**Why the base needed one test-harness commit first (`75fbe51`).** Fifteen suites hard-coded `redis://localhost:6379/<n>` and `FLUSHDB` it; on this machine that is the running development stack's Redis. `tests/redis_url.py` lets `WASLA_TEST_REDIS_HOST` move them to an isolated server; unset, every URL is byte-for-byte what it was.

**Reproduced at the base before anything changed** (synthetic identifiers; every database probe in one rolled-back transaction; `logs/probes_base.json`):

| Probe | Base behaviour |
| --- | --- |
| P2 username sender (`from_user_id`, no `from`/`wa_id`) | `stored 0, ignored 1`; no event, no message - **lost** |
| P3 phone + BSUID | stored; BSUID kept nowhere (no table to hold it) |
| P5 / P6 `object: page` / `instagram` with a WhatsApp-shaped value | **`stored 1` - parsed and stored as a WhatsApp message** (worse than the audit's empty-envelope probe) |
| Unknown field (`smb_message_echoes`) | `stored 0, ignored 0` - silent |
| Y1 our outbound id P on number 1 arriving inbound on number 2 | conversation opened on number 2 with its window set and no message; agent job queued on number 1's conversation **triggered by our own outbound message** |
| X3 same provider id, two numbers, one workspace | refused (`uq_messages_tenant_id_wa_message_id`) |
| X7 second file on one message | refused (`uq_message_media_message_id`) |
| X8 file in workspace B naming workspace A's message | **accepted** |
| T0 a plain text message | agent job enqueued **without `trigger_message_id`** (section 5.4) |

---

## 4. Remediation architecture

```
Provider webhook (one route per product; Meta signature shared)
      |
Provider adapter: parse -> InboundEvent (refusals counted by reason)
      |
ChannelIngestionService: owner at event time -> event log -> screen
      |
channel_connections  +  contact_identities  ->  contacts
      |
conversations (connection + pinned participant identity)
      |
messages (provider id unique per connection) / message_media (ordered, located)
      |
AgentTurn, orchestrator, tools, InboxService, CRM, follow-ups, analytics   (unchanged)

API / AI / follow-up / campaign
      |
MessagingService._dispatch   (ADR-093 delivery protocol, ADR-103 idempotency: unchanged)
      |
conversation -> connection -> ChannelPolicy -> participant identity -> adapter.sender
```

**What stays WhatsApp's, behind its adapter:** WABA, `phone_number_id`, the ownership proof and connect flow, template sync/category/approval and withdrawal, the 24-hour window with the template escape, the Graph media fetch and Meta host roots, the status payload format, the webhook parser, the platform-token fallback. **What is neutral now:** customer identity storage, connection identity and tenure, the conversation's destination, provider message identity, inbound orchestration, the event log and its recovery and retention, AI trigger validity, send routing, policy lookup, attachment cardinality, the outcome taxonomy, observability dimensions, inbox filtering.

**Decisions recorded** as ADR-117 (neutral connection, shared ids), ADR-118 (scoped identities, provider-asserted linking only), ADR-119 (one conversation per contact and connection, pinned participant), ADR-120 (per-connection provider identity; only a new customer message has consequences), ADR-121 (the channel seam: InboundEvent, adapters, policy/capabilities), ADR-122 (commercial and consent policy not inferred), ADR-123 (per-connection sending allowance).

---

## 5. OMNI-R1 — BSUID

### 5.1 Provider facts, re-checked

Checked 2026-10-01 against Meta for Developers, *Business-scoped user IDs* (`developers.facebook.com/documentation/business-messaging/whatsapp/business-scoped-user-ids/`, page dated 2026-06-29). No difference from the audit's 2026-09-28 facts.

| Contract | Use |
| --- | --- |
| `messages[].from_user_id` and `contacts[].user_id` carry the BSUID on every inbound message; `from` and `wa_id` are omitted when Meta may not share the number (username on, no exchange in 30 days, not in the contact book) | parser accepts either identifier and keeps both |
| BSUID = ISO country code + `.` + up to 128 alphanumerics (131); parent BSUID adds `ENT.` | identifiers bounded at 255; longer refused and counted |
| unique per business-portfolio and user; regenerated on phone change (`user_id_update` field, `system` message) | scoped by the WABA it arrived through (a WABA is in one portfolio); rotation not processed - documented |
| `statuses[].recipient_user_id` on sent/delivered/read | kept on the parsed status |
| send by `recipient` (from July 2026); `to` wins if both | exactly one of `to`/`recipient` |
| a BSUID send answers `contacts[].user_id`, no `wa_id` | client reads either |
| request body: `messaging_product`, `recipient_type: individual`, `recipient`, `type`, ... | matches `WhatsAppClient._send` |

### 5.2 Fix

`app/integrations/whatsapp/payload.py` accepts a message with `from` and/or `from_user_id`, keeps `from_parent_user_id` and `context.id`, keys profile names by both identifiers, and refuses with a bounded reason (`missing_sender`, `identifier_too_long`) only when no usable identifier remains. The sender's identifiers, phone first, become the neutral event's `sender`; `ContactIdentityService` resolves them (section 6). The conversation is pinned to the phone when Meta named one, else to the BSUID; the WhatsApp adapter addresses a BSUID participant as `recipient` and a phone as `to`, never both.

### 5.3 Evidence

- Regression of P2 (`tests/unit/test_whatsapp_identity_parsing.py::test_p2_a_username_sender_is_parsed_not_dropped`, `tests/integration/test_omnichannel_identity.py::test_a_username_sender_is_stored_and_answered_by_business_scoped_id`); after: P2 `stored 1, queued 1`, one BSUID identity, reply body `{"recipient": <BSUID>}` with no `to`.
- Pairing (P3): `test_a_phone_and_a_business_scoped_id_named_together_are_one_person`, `..._arriving_later_joins_...` (both directions), `test_identifiers_held_by_two_contacts_are_never_merged`.
- Long BSUID: 131-character BSUID stored; 256 refused as `identifier_too_long`, counted (`test_an_identifier_past_any_documented_form_is_refused_and_counted`).
- A valid username message can no longer be accepted with 200 and discarded: mutant **M13** (drop BSUID-only) is killed.

### 5.4 The live text turn defect (found during this remediation; `aa11098`)

| Step (through `POST /api/v1/webhooks/whatsapp`, signed, real `AgentWorker`) | `354db53` | after |
| --- | --- | --- |
| HTTP | 200 | 200 |
| message stored | yes | yes |
| job body | `{conversation_id, tenant_id}` - **no trigger** | `{..., trigger_message_id}` |
| worker | `agent.turn_unkeyed`, dead-lettered `malformed`, NO_RETRY | turn claimed; outcome `suppressed_agent` (the probe workspace has no active agent: `agent.no_active_default`); no dead letter |
| event state after the worker | `processed` (recovery never retries it) | `processed` |

Cause: `record_inbound` staged the row and the hand-off read `message.id` before any flush; ids are generated at insert. TOOL-17 (`78a6fd5`, later than the real-Meta E2E record, which stopped at job dispatch) made the worker refuse unkeyed jobs. Fix: name the id at staging. Regression `tests/integration/test_live_turn_identity.py` (both halves; both fail at the base). Mutant B2 (remove the id) is killed.

### 5.5 Historical BSUID pairing evidence (Q1)

`scripts/omnichannel_invariants.py census` counts, read-only, the message payloads still retained that name a phone only, a BSUID only, or both (pairable), and the contacts that already hold a phone and a BSUID. Validated on synthetic and migrated fixtures, including under `default_transaction_read_only = on` (`test_the_operator_checks_run_on_a_read_only_replica`). **Running it on a production copy is DEPLOYMENT VERIFICATION**: raw payloads are redacted 30 days after processing (DB-011), so pairs in older deliveries are already unrecoverable and the census must run before that window passes for any pairs that matter.

---

## 6. OMNI-R2 — Contact identities

`contact_identities` (migration 0082, model `app/db/models/channel.py`): `tenant_id`, `contact_id`, `channel`, `kind` (`phone`/`bsuid`/`psid`/`igsid`), `scope` (`workspace`/`provider_account`/`connection`), `scope_ref`, nullable `connection_id`, `value` (<=255), `source` (`backfill`/`provider`/`provider_pairing`), timestamps.

| Rule | Mechanism |
| --- | --- |
| Uniqueness | `UNIQUE(tenant_id, channel, kind, scope, scope_ref, value)` - never the value alone, never per workspace alone |
| Scope agrees with its reference | CHECK `scope_shape`: workspace -> `scope_ref=''`, no connection; provider_account -> non-empty ref; connection -> `connection_id` set and equal to `scope_ref` |
| Tenant agreement | `(tenant_id, contact_id) -> contacts`, `(tenant_id, connection_id) -> channel_connections`; targets `UNIQUE(tenant_id, id)`, `(tenant_id, contact_id, id)`, `(tenant_id, contact_id, id, channel)` |
| One WhatsApp phone per contact, equal to `wa_id` | partial unique index; trigger `trg_contacts_phone_identity` keeps them equal for every writer and refuses a phone held by another contact |
| Linking | provider assertion in one signed payload only; conflicts counted and routed to the anchor kind's contact (BSUID), never merged |
| Races | `ON CONFLICT DO NOTHING` on the scoped-value rule; a losing creation unwound in a savepoint and resolved again |

**Backfill (0082):** exactly one `phone` identity per contact, from its own `wa_id`, `source = backfill`; nothing else consulted (no name, email, lead phone, profile name). Post-check in the migration itself: contacts with `wa_id` and no matching identity must be 0, or the migration refuses. Section 26 has the counts.

`contacts.wa_id` stays (deprecated, nullable for username senders, kept equal to the phone identity by trigger); the deprecation path is O8 (section 24).

Tests: `test_omnichannel_identity.py` (14), `test_omnichannel_schema.py` identity tests, the identity oracle (section 27), races (section 28). Mutants M1 (ignore scope/connection), M4 (accept another workspace's identity), M8 (merge by display name): killed.

---

## 7. OMNI-R3 — Channel connections

`channel_connections`: id (shared with `whatsapp_accounts.id` for every number), tenant, `channel`, `external_account_id` (`phone_number_id` for WhatsApp), `status`, tenure `ownership_started_at`/`ownership_verified_at`/`released_at`, `health`/`health_reason`/`health_changed_at`, `credential_expires_at`, send window. No provider-specific columns; WABA, display number, verified name, proof and token stay on `whatsapp_accounts`.

- **Shared-id backfill (0082)**: `INSERT ... SELECT` from `whatsapp_accounts` with the same id, tenant, status and tenure; post-check refuses the migration if any number lacks an identical connection.
- **Mirror** `trg_whatsapp_accounts_channel_connection` (AFTER INSERT/UPDATE/DELETE on `whatsapp_accounts`): every writer of a number keeps its connection current in the same statement; health, credential expiry and the send window are never touched by it.
- **Live claim** `UNIQUE(channel, external_account_id) WHERE released_at IS NULL`; **tenure index** `(channel, external_account_id, ownership_started_at)` (named `ix_channel_connections_external_account_tenure` - the earlier name exceeded PostgreSQL's 63-character identifier limit and broke 0082 on a fresh database; found and fixed here).
- **Re-pointed keys (0084)**: `conversations (tenant_id, account_id, channel) -> channel_connections (tenant_id, id, channel)` and the same for `whatsapp_events`, added `NOT VALID`, validated in their own transactions; the old `fk_conversations_tenant_account` and `fk_whatsapp_events_account_id_whatsapp_accounts` dropped after. No conversation or event row was rewritten.
- **Routing** (`ConnectionDirectory`): live claim fast path, then ADR-101's "who held it at the event's instant" over the tenure index; never the customer's identifier (mutant M14 killed).
- **Fail closed**: `ChannelRegistry` operates WhatsApp only; any other channel raises `ChannelUnavailableError` on send, fetch or policy lookup.

`LimitKey.WHATSAPP_NUMBERS` is unchanged and still counts WhatsApp numbers (ADR-122).

---

## 8. OMNI-R4 — Participant identity

`conversations.participant_identity_id` (0082 nullable, 0083 backfilled from the contact's phone identity in batches, 0084 NOT NULL via a validated CHECK) with `(tenant_id, contact_id, participant_identity_id, channel) -> contact_identities (tenant_id, contact_id, id, channel)`: the participant is the conversation's own contact's identity, in its workspace, on its channel. A writer that names none gets the contact's only identity on the channel from `trg_conversations_default_participant`, or a refusal by the key - never a guess.

**Routing rule** in `MessagingService._dispatch`: conversation -> connection (same channel, active) -> `ChannelPolicy` -> participant identity -> `adapter.address` -> `adapter.sender`. Nothing reads `contacts.wa_id` to send. `adapter.address` refuses an identity of another channel or an unaddressable kind before anything is staged.

**Campaigns** (WhatsApp-only, as before): `campaign_recipients.participant_identity_id` is recorded when the audience is materialised, keyed `(tenant_id, contact_id, participant_identity_id) -> contact_identities (tenant_id, contact_id, id)`. (The earlier draft keyed it to the workspace only, so a recipient could name another contact's identity; the schema test caught it and the key now includes the contact.) `_deliver` sends only through the campaign's own connection and refuses a conversation whose pin no longer matches. Opt-out, template validation, idempotency and ADR-026 rates unchanged.

Tests: reply to username sender -> `recipient`; reply after the contact gains a phone stays on the BSUID pin (M3 killed); campaign through another number refused (M10 killed); legacy recipient records its identity; wrong-channel adapter refused (M6 killed). Wrong-channel oracle: section 27.

---

## 9. OMNI-R5 — Echo-safe dedup

- `messages.connection_id` (assigned by the sequence trigger from the conversation; `(tenant_id, conversation_id, connection_id) -> conversations (tenant_id, id, account_id)`), and `UNIQUE(tenant_id, connection_id, wa_message_id)` as the neutral identity. The workspace-wide `uq_messages_tenant_id_wa_message_id` stays until O8 (ADR-120).
- **The projection screens before anything is resolved or created**: same customer message on the same connection -> duplicate; an id naming Wasla's own send or another connection's message -> **collision**: event kept `failed` (`provider_id_collision`), nothing opened, touched, queued, cancelled or opted out. `echo` is an event kind, stored and never projected.
- **Hand-off contract**: only a newly stored customer message hands off an agent or media job, cancels follow-ups, applies opt-out, records customer analytics and meters `whatsapp_message_received`.
- **Backstop**: `AgentTurnRepository.claim` and `owe` (the only two insert paths into `agent_turns`) refuse a trigger that is not an inbound customer message of the job's conversation in its workspace (`TriggerNotAnswerableError`); a trigger not yet visible is `NotFoundError` and retried (ADR-089). Not a foreign key, deliberately: a turn is evidence that outlives its message under retention (existing `AgentTurn` design).

**Y1 as a regression** (`test_an_inbound_event_carrying_our_own_sent_id_on_another_number_is_a_collision`, and the same-number variant): after - `collisions 1, duplicates 0, queued 0`, no conversation on number 2, number 1's window untouched, event `failed`, no turn for our reply. Mutants M2, M9, M11, B1 killed; M9 is killed by the Y1 regression (a customer's id repeated on another number is intercepted earlier, by the event log's workspace-wide key).

---

## 10. OMNI-R6 — Neutral inbound

`app/channels/inbound.py`: `InboundEvent` (channel, connection key, kind message/status/echo, event id, message id, provider time, sender identifiers asserted together, message kind, text, ordered `AttachmentLocator`s, reply-to, status per message or watermark, profile name, raw evidence) and `ParsedDelivery` (events plus refusals by `RefusalReason`). `ChannelIngestionService` is `WhatsAppIngestionService`'s logic moved, not copied: ADR-101 ownership, per-event savepoints (MEDIA-05), event storage, screening, identity resolution, projection, hand-offs, settlement (ADR-102). The WhatsApp adapter (`app/integrations/whatsapp/adapter.py`) holds the parser, the Meta type/status maps, BSUID handling and identity scoping; `WhatsAppIngestionService` is now a thin facade (`adapter.parse` -> ingestion). The shared projection imports no WhatsApp DTO.

Behaviour preserved: the WhatsApp suites pass unchanged except where a fixture lacked `"field": "messages"`, which every real Meta change carries (section 33).

---

## 11. OMNI-R7 — Neutral event recovery

Decision: **evolve `whatsapp_events` in place** rather than introduce a second table. The ORM names it `ChannelEvent` (`WhatsAppEvent` remains an alias), it gains `channel`, the event kind `echo`, `UNIQUE(tenant_id, account_id, event_id)` (the workspace-wide key stays until O8) and the connection key above. Renaming a hot table beside a behaviour change was riskier than the name. `InboundRecoveryWorker`, `InboundEventSweep` and `WebhookPayloadRetention` read it for every channel: oldest-first claiming with leases, bounded batches, settled states and DB-011 redaction are unchanged; recovery finds the projected message by `(connection, provider id)` and refuses a non-customer row; every pending file of a message is re-queued.

Tests: another channel's owed event finished by the same sweep; recovery never answers a message that is not a customer's; another channel's payloads age out like WhatsApp's; the sweep racing a redelivery queues one turn (section 28); the existing `test_whatsapp_inbound_recovery.py` suite unchanged.

---

## 12. OMNI-R8 — Channel policy and capabilities

`app/channels/policy.py`: `ChannelPolicy` (`may_send(conversation, origin, kind, now)`, `standard_window_open`, `follow_up`, `reply_policy`, `agent_instructions`) and `ChannelCapabilities` (text limit **and unit**, reply budget, attachments per message, media families, receipt model, echoes, reply-to, reactions, unsend, templates, out-of-window mechanism, message-id scope). `WhatsAppChannelPolicy` holds the 24-hour window, the template escape, 4,096 characters and the exact refusal and prompt sentences that used to be constants in shared code (pinned by `test_whatsapp_*` parity tests).

- **Agent instructions** come from the conversation's channel policy (`_reply_instructions`); "You are replying over WhatsApp" is now only WhatsApp's.
- **Byte-aware bounding**: `text_length`/`longest_prefix` measure in characters or UTF-8 bytes and never split a character; `prepare_channel_reply` bounds in the channel's unit. Arabic tests: 700 characters (~1,400 bytes) accepted by WhatsApp and refused by a 1,000-byte policy; a reply of five times the byte limit shortened under 1,000 bytes with valid UTF-8; 1,500 Arabic characters (~3,000 bytes) accepted by a 2,000-*character* policy (M12 killed).
- **Follow-ups** ask the policy (`FREE_TEXT`/`TEMPLATE`/`SKIP` with a reason); WhatsApp decisions are equivalent to the old branches; a synthetic policy can forbid free text or offer nothing out of window without touching WhatsApp templates.
- **API**: `service_window_open` keeps its WhatsApp meaning; `reply_policy` is the additive, channel-stated rule.

Mutant M5 (WhatsApp policy on a non-WhatsApp conversation) killed.

---

## 13. OMNI-R9 — Media

- `message_media.position` (0 for every existing row) and `UNIQUE(message_id, position)` replacing `UNIQUE(message_id)`; every file of a message queued; the message answered once all resolve; recovery re-queues every pending file.
- Neutral locator: `locator_kind` (`handle`/`url`), `locator TEXT` (<=4,096, CHECK), `locator_expires_at`; 0083 copied every handle into it; `wa_media_id` stays and a row with only it is still fetched by that handle (the rolling-deploy case - a suite writing rows the old way exposed it).
- **OMNI-022 closed**: `(tenant_id, conversation_id, message_id) -> messages (tenant_id, conversation_id, id)` and `(tenant_id, conversation_id) -> conversations`. X8 after: refused by `fk_message_media_tenant_message` (`test_probe_x8_a_file_cannot_name_another_workspaces_message`).
- Fetch through the adapter: `ChannelMediaFetcher`; WhatsApp's two-step handle fetch; `UrlMediaFetcher` for URL-locating providers - SSRF guard on every hop, provider-scoped host roots, credential only to allowed hosts, expired links refused without a request, the byte cap enforced mid-stream. Hashing, sniffing, size caps, AI extraction, storage and retention unchanged and shared.

---

## 14. OMNI-R10 — Contract and mutation framework

| Contract | Suite | Tests |
| --- | --- | --- |
| Inbound | `tests/unit/test_channel_adapter_contract.py` (WhatsApp and a synthetic adapter): parse never raises on hostile input; foreign object refused; refusals counted by closed reason; echo explicit; identifier pairs, provider timestamps, connection key and ordered attachments survive; unmapped types kept as `unsupported` | 13 functions |
| Outbound | `tests/unit/test_channel_outbound_contract.py`: participant is the address (`to` vs `recipient`, never both); unaddressable/other-channel identity refused before any request; every Meta answer maps to a neutral outcome (auth, permanent, uncertain, rate limit); uncertain sends asked exactly once; the WhatsApp names are the neutral classes | 9 functions |
| Status | `tests/integration/test_omnichannel_status_contract.py`: mapping in order; duplicate and late receipts harmless; unmapped status is evidence; unknown message creates nothing; another connection's or workspace's receipt moves nothing; a stale watermark never moves a message backwards | 7 |
| Media | `tests/unit/test_channel_media_contract.py`: provider host with token; long signed link; foreign host, lookalike host, off-host redirect, private resolution, plain http, expired link refused before any request; mid-stream cap; 401/403/404/410/5xx classified; redirect loop bounded; handle vs URL | 18 functions |
| Policy | `tests/unit/test_channel_policy_contract.py`: WhatsApp parity; byte-bound and character-bound synthetic policies; answers depend on channel, origin, kind and time | 18 functions |

The synthetic adapter (`tests/channel_fakes.py`) is never registered by the application; `ChannelRegistry` refuses a channel without a decided meter unless a test opts out explicitly. Watermark projection (OMNI-011) is a generic primitive tested through it (section 16). Mutation campaign: section 29.

---

## 15. OMNI-R11 — Meta discrimination

`/webhooks/whatsapp` is preserved and is still the only Meta route; no Instagram or Messenger endpoint is registered. The HMAC lives in `app/integrations/meta/signature.py` (shared by any future Meta route; `app/integrations/whatsapp/signature.py` re-exports it). The parser requires `object == whatsapp_business_account` and `field == messages`; another product's delivery is refused as `foreign_object`, any other field as `unsupported_field` (template updates, `user_id_update`, Coexistence `history`, `smb_app_state_sync`, `smb_message_echoes`) - counted in `wasla_inbound_entries_refused_total` and in the webhook log line, never an empty success. Before/after: P5/P6 went from *stored as WhatsApp messages* to `refused {foreign_object: 1}`; the unknown field from silent to `refused {unsupported_field: 1}`. Mutant B4 killed.

---

## 16. OMNI-R12 — Watermarks

`MessageRepository.advance_to_watermark` advances only this conversation's outbound messages on this connection with `sent_at <= watermark`, each through the monotonic `advance_status`. The ingestion path routes a watermark status to it after finding (never creating) the conversation from the reader's identity. Tests: a read watermark advances only what was sent before it, not other connections' or workspaces'; a stale watermark never moves a message backwards. No Messenger code exists; a partial index for watermark reads is left for the Messenger adapter (O6), where its query shape will be known.

---

## 17. OMNI-R13 — Credentials and health

- **Envelope v2** (`app/core/crypto.py`): AAD `tenant:connection:channel` for every credential sealed from now on (`CredentialService.seal`), so a ciphertext moved to another number of the same workspace no longer decrypts; `v1` (tenant-only AAD) still reads. Tests: moved between connections fails; v1 still readable; connect and re-verify store v2. Re-sealing existing v1 values is a later explicit sweep (`needs_rotation` exists).
- **Platform fallback** is decided inside the WhatsApp adapter only; no other channel can inherit it.
- **Health**: a provider credential refusal records `auth_failed` on the connection (conditional update - a healthy connection writes nothing per send); the next successful send clears it. Other health values are vocabulary for future adapters; nothing writes them, so transient failures never become states. `credential_expires_at` exists for providers that expire tokens.

---

## 18. OMNI-R14 — Inbox and analytics

`GET /api/v1/conversations?channel=&connection_id=` - filters on the one inbox, tenant-scoped (a foreign connection id matches nothing). Index `ix_conversations_tenant_id_account_id_last_message_at (tenant_id, account_id, last_message_at DESC NULLS LAST, id DESC)` built `CONCURRENTLY`. Platform overview gains `connections_by_channel {total, active}` (counts only). Metrics carry `channel` (closed enum), never connection, page, number or customer identifiers (ADR-072). Mutant M7 killed.

---

## 19. OMNI-R15 — API compatibility

Additive only: `ConversationRead.connection_id` (= `account_id`), `channel`, `participant {id, channel, kind}` (no identifier value), `reply_policy`; `MessageRead.provider_message_id` (= `wa_message_id`); `ContactOptOutRead.identities[]`. Deprecated and kept (OpenAPI `deprecated`, docs/API.md): `MessageRead.wa_message_id`, `ContactOptOutRead.wa_id` (null for a username customer). `service_window_open` keeps its WhatsApp meaning. Existing endpoint suites pass; `test_conversation_endpoints.py` and `test_contact_endpoints.py` cover the new fields and cross-workspace filters.

---

## 20. OMNI-R16 — Throughput

`CONNECTION_SENDS_PER_MINUTE` (unset by default - no behaviour change) is a per-connection allowance on the connection row: a one-minute window and count, taken by one conditional UPDATE before anything is staged. Campaigns and follow-ups are refused (`ConnectionThrottledError`, `Retry-After`) and wait without spending an attempt; agent and human replies are counted and never refused. Tests: two campaigns one number; two numbers one workspace (no shared bottleneck); retries; another workspace cannot spend it; concurrent senders never exceed it; replies never held back (`tests/integration/test_connection_throughput.py` 8, unit 6).

---

## 21. OMNI-R17 — Outcome taxonomy

`app/channels/outcomes.py`: `SendNotAttemptedError`, `UncertainDeliveryError`, `ProviderAuthError` (with `RateLimitedError` from core). The WhatsApp client re-exports them as the same classes and keeps `TemplateWithdrawnError`. The refused-credential text is unchanged. Behaviour pinned by the existing provider-outcome and delivery-protocol suites and the outbound contract.

---

## 22. OMNI-R18 — Observability

| Signal | Labels (closed) |
| --- | --- |
| `wasla_inbound_entries_refused_total` | `channel`, `reason` (8 values) |
| `wasla_inbound_events_total` | `channel`, `outcome` (9 values) |
| `wasla_unprocessed_inbound_events_by_channel` | `channel` |
| `wasla_channel_connections` | `channel`, `status`, `health` |

Alerts (60 -> 63 rules; rule tests 167 -> 175 assertions): `InboundEntriesRefused` (message-loss reasons, 15m), `InboundForeignPayloads`, `ChannelConnectionCredentialRefused`; every one has promtool cases that fire and clear. Existing WhatsApp alerts unchanged (`WhatsAppInboundStopped` stays WhatsApp's; the unprocessed-inbound alert reads the now-neutral event log). Unknown label values collapse to `other`; tests hold the domains equal to the enums.

---

## 23. Product-policy items / OMNI-R19

Searched `DECISIONS.md`, `docs/PRODUCT.md`, the billing docs and earlier ADRs: nothing decides any of these, so ADR-122 records safe architectural defaults and leaves commercial policy to the product:

| Question | State |
| --- | --- |
| Conversation continuity | **Decided architecturally**: separate thread per connection and identity; unified inbox yes; unified thread no (ADR-119) |
| AI memory | per conversation (unchanged) |
| Identity linking | provider assertion only; never automatic otherwise (ADR-118) |
| Default agent | workspace default kept; per-connection default not built |
| Billing | unchanged; only WhatsApp meters decided; a channel without a decided meter cannot be registered |
| Connection limits / pricing | **PRODUCT DECISION DEFERRED**; `WHATSAPP_NUMBERS` not reinterpreted |
| Opt-out scope across channels | **PRODUCT DECISION DEFERRED**; person-level behaviour preserved; identities listed |
| Message quotas across channels | **PRODUCT DECISION DEFERRED** |

Status: **RESOLVED ARCHITECTURALLY / PRODUCT POLICY DEFERRED** (OMNI-013, OMNI-016, OMNI-025).

---

## 24. Deferred R20-R25

| Item | State |
| --- | --- |
| R20 cross-channel linking UI | Not built. The schema permits it: identities move between contacts by `contact_id`; `source` records provenance |
| R21 person-level export/erasure | Not built; **required before R20**. New tables participate in workspace purge (`contact_identities` before `contacts`; `channel_connections` after `whatsapp_accounts`; events and media by cascade and explicit purge order) and the purge-partition test |
| R22 Coexistence | Not built. Prerequisites in place: BSUID identity, echo kind, field discrimination, neutral event path; the Coexistence fields are refused and counted |
| R23 vocabulary | No historic label renamed; new names neutral (`channel_*`, `contact_identit*`, `ChannelEvent`); new audit actions not needed by this scope |
| R24 hermetic tests | **Closed**: injectable resolver; the whole suite refuses live DNS; three DNS-dependent tests made hermetic |
| R25 compatibility cleanup (O8) | Not done. Conditions: neutral path authoritative for at least one release; backfills verified by `omnichannel_invariants verify` on production; API consumers off `wa_message_id`/`wa_id`; PITR restore of the pre-cleanup schema rehearsed. Then: drop the workspace-wide message and event uniques, the mirror trigger and duplicated lifecycle columns, `wa_media_id`; rename at leisure |

Other named deferred items: BSUID rotation (`user_id_update`), persisting `reply_to` (carried on the event, not stored), per-connection fairness in the recovery sweep, re-sealing v1 credentials, a per-channel "inbound stopped" alert.

---

## 25. Database migrations

| Migration | Content | Safety |
| --- | --- | --- |
| `0082` | enums; `channel_connections` + shared-id backfill; `contact_identities` + phone backfill; mirror, phone-identity and participant-default triggers (each followed by a catch-up pass); `contacts.wa_id` nullable; `conversations.channel` + nullable `participant_identity_id`; nullable `messages.connection_id` via the sequence trigger; media position/locator + CHECKs `NOT VALID`; `whatsapp_events.channel`; `campaign_recipients.participant_identity_id` | prechecks refuse ambiguous data unchanged; post-checks inside the transaction; `ALTER TYPE ... ADD VALUE` first in its own block; metadata-only ALTERs last; `lock_timeout 15s` |
| `0083` | batched backfills (5,000 ids per transaction, keyset): participants, message connections, media locators, recipient identities | idempotent; precheck refuses a conversation whose contact has no phone identity; post-checks |
| `0084` | uniques `CONCURRENTLY` (rebuilding an INVALID leftover), keys `NOT VALID` then `VALIDATE` each in its own transaction, NOT NULL through validated CHECKs, superseded keys dropped last | refuses before anything changes if a row would violate a new key |

**Downgrades refuse rather than lose**: 0084 (one transaction, deliberately) refuses while a conversation or event sits on a non-WhatsApp connection or a message has several files; 0082 refuses while any contact lacks a phone, an identity is not a contact's own phone, a connection is not a number, a file is above position 0 or URL-located, or an echo event exists.

Single head at every commit, checked commit by commit: `0081` through `e0d81c5`, `0084` from `8ea574b` on. Found and fixed during verification: an over-length index name (fresh 0082 failed), and a `position` default whose catalog text differed between migration and model (`'0'::smallint` vs `0`) - caught by the migration-built schema-parity lane.

| Check | Result |
| --- | --- |
| fresh `0001 -> head` | pass at `5f8a788`: empty database to `0084` in 9 s; single head |
| `0081 -> head -> 0081 -> head` (empty and populated) | pass: empty (`final_migmatrix.txt`) and populated (`test_omnichannel_migrations.py`: real 0081 rows seeded, upgraded, inspected, downgraded, re-upgraded, counts unchanged) |
| `head -> base -> head` | pass |
| downgrade refusal with a username contact; nothing changed, still at 0084 | pass (`test_omnichannel_migrations.py`) |
| `alembic check` / `db_preflight verify` | no drift / ok |
| schema parity (migration-built vs model-built) | 4 passed at `5f8a788` (it failed once on the schema commit's first run - the `position` default below - and passed after the fix, before that commit was made) |

---

## 26. Backfill evidence

`tests/integration/test_omnichannel_migrations.py` builds a database to `0081`, seeds two workspaces (a live and a released number, contacts, conversations on both, inbound and outbound messages, a file, events, a campaign with a recipient), upgrades, inspects, downgrades and re-upgrades.

| Invariant | Result |
| --- | --- |
| numbers = connections with the same id, tenant, status, tenure | equal |
| contacts with `wa_id` = phone identities from that `wa_id` | equal; 0 heuristic links |
| conversations with a participant that is their own contact's phone | all |
| messages with their conversation's connection | all |
| files with a handle locator | all |
| events on the WhatsApp channel; recipient identities | all |
| row counts before/after/after round trip | unchanged; 0 missing contacts, conversations, messages or files; 0 wrong tenant; 0 duplicate scoped identities |

`scripts/omnichannel_invariants.py verify` encodes these checks for operators (section 27).

---

## 27. Tenant and identity invariants

`scripts/omnichannel_invariants.py` - `census` (Q1-Q8 restated) and `verify` (21 invariants), SELECT-only in a `READ ONLY` transaction, counts only. Held at zero over a population built only through production paths (`test_omnichannel_oracles.py::test_no_write_path_breaks_an_invariant`), in the model-built and the migration-built lanes at `5f8a788`, and proven non-vacuous by injecting one violation each (`test_the_oracles_count_a_violation_when_one_exists`). The operator command itself (`python -m scripts.omnichannel_invariants verify`) also ran at `5f8a788` against the migration matrix's database: `ok`, every invariant 0. That database held no rows, so this run proves only that the queries run read-only against the migrated catalog; the populated evidence is the oracle suite's, because the test harness drops its schema at the end of every session.

| Oracle / invariant | Violations |
| --- | --- |
| Wrong-channel: outbound message -> conversation -> connection -> participant agree on tenant and channel, kind addressable | 0 |
| Echo: no agent turn triggered by a non-customer message; no echo projected as a customer message | 0 |
| Identity: database partition equals the independently written expectation (same value across tenant, channel, connection, WABA; phone+BSUID; same display name) | 0 mismatches |
| Number <-> connection parity; contact <-> phone identity | 0 |
| Conversation connection/participant agreement; message connection agreement | 0 |
| Provider id twice on one connection; identity scoped twice; cross-tenant identity | 0 |
| File in another workspace/conversation; position reused | 0 |
| Event on a connection not its own; recipient identity not its contact's; recipient conversation on another connection | 0 |

Cross-tenant probes: X1, X4, X5, X6 behave as before; X8 now refused; the new tables refuse cross-tenant rows by key (`test_omnichannel_schema.py`, 20 tests). Routes reuse the existing tenant dependencies; the new filters and the participant summary are tenant-scoped; platform responses carry counts only.

---

## 28. Concurrency

Real PostgreSQL, one session and transaction per actor, released by a barrier, committed (`test_omnichannel_concurrency.py`, 7 tests, with the existing same-message, same-customer and same-status races in `test_whatsapp_inbound_concurrency.py`): three consecutive green runs at `5f8a788`, 13 passed each.

| Race | Result |
| --- | --- |
| a new username sender's four first messages together | 1 contact, 1 identity, 1 conversation pinned to it, 4 turns each keyed on its own message |
| a known phone's BSUID named by four concurrent deliveries | attached once; 0 conflicts; 1 conversation |
| a new sender named both ways, four at once | 1 contact, conversation pinned once to the phone, 4 messages |
| one provider id on two numbers at once | 1 message, 1 collision, 1 turn |
| echo-shaped event racing a status | status applied; no conversation, window or turn |
| three files delivered three times at once | positions 0,1,2 once |
| two recovery sweeps racing a redelivery | exactly 1 turn; event processed |

No deadlock, no duplicate turn or usage, no cross-tenant row in any run.

---

## 29. Mutation campaign

`scripts/run_omnichannel_mutations.py`: unmutated control first (all killer tests must pass); each edit must match exactly once; the source's SHA-256 and `git status --porcelain` are checked after the restore; a run that failed to collect, import or parse is `invalid`, not a kill; each kill records the failing test and its first assertion.

Run at `5f8a788` (`logs/final_mutations.jsonl`): the unmutated control ran all 23 killer tests (24 cases) green; **18 of 18 mutants killed, 0 survived, 0 invalid**; every edited file's SHA-256 restored; `git status --porcelain` empty before and after; 3m33s.

| Mutant | Property removed | Mutated file | Verdict | First failing test |
| --- | --- | --- | --- | --- |
| M1 | ignore the connection (the provider scope) in identity lookup | `channel_repository.py` | **killed** | `test_omnichannel_identity::test_a_business_scoped_id_is_scoped_by_the_business_account` |
| M2 | ignore the channel/connection in provider-message dedup | `conversation_repository.py` | **killed** | `test_omnichannel_status_contract::test_a_receipt_on_one_connection_never_moves_anothers_message` |
| M3 | route outbound to the contact's phone instead of the conversation's participant | `messaging_service.py` | **killed** | `test_omnichannel_identity::test_a_reply_goes_to_the_participant_even_after_the_contact_gains_a_phone` |
| M4 | accept an external identity from another workspace | `channel_repository.py` | **killed** | `test_omnichannel_identity::test_an_identifier_is_never_matched_across_workspaces` |
| M5 | apply the WhatsApp policy to a non-WhatsApp conversation | `messaging_service.py` | **killed** | `test_omnichannel_second_channel::test_another_channels_window_is_its_own_not_whatsapps` |
| M6 | let a non-WhatsApp conversation use the WhatsApp outbound adapter | `messaging_service.py` | **killed** | `test_omnichannel_second_channel::test_a_reply_goes_through_the_conversations_own_adapter` |
| M7 | drop the connection filter from the inbox query | `conversation_repository.py` | **killed** | `test_omnichannel_second_channel::test_the_inbox_narrows_to_one_channel_and_one_connection` |
| M8 | merge identities by display name | `contact_identity_service.py` | **killed** | `test_omnichannel_identity::test_the_same_display_name_never_links_two_senders` |
| M9 | reuse a provider message id across connections | `conversation_service.py` | **killed** | `test_omnichannel_echo_and_dedup::test_an_inbound_event_carrying_our_own_sent_id_on_another_number_is_a_collision` |
| M10 | send a campaign through a connection other than its own | `campaign_service.py` | **killed** | `test_omnichannel_operations::test_a_campaign_never_sends_through_another_numbers_conversation` |
| M11 | treat an echo as a customer message and hand off an AI turn | `channel_ingestion_service.py` | **killed** | `test_omnichannel_second_channel::test_an_echo_of_our_own_send_is_evidence_not_a_turn` |
| M12 | count characters instead of UTF-8 bytes for a byte-limited policy | `policy.py` | **killed** | `test_channel_policy_contract::test_a_byte_channel_refuses_arabic_that_fits_whatsapp` |
| M13 | drop a WhatsApp message whose sender carries only a business-scoped id | `payload.py` | **killed** | `test_whatsapp_identity_parsing::test_p2_a_username_sender_is_parsed_not_dropped` |
| M14 | resolve the workspace from the customer's identifier instead of the connection | `channel_ingestion_service.py` | **killed** | `test_omnichannel_identity::test_the_workspace_is_the_numbers_not_the_customers` |
| B1 | let an agent turn be claimed for a message that is not a customer's | `agent_turn_repository.py` | **killed** | `test_omnichannel_echo_and_dedup::test_a_turn_is_never_claimed_for_our_own_message` |
| B2 | hand a live text turn off before its message has an id | `conversation_repository.py` | **killed** | `test_live_turn_identity::test_a_text_message_hands_off_a_turn_naming_that_message` |
| B3 | address a business-scoped participant with `to` | `adapter.py` | **killed** | `test_channel_outbound_contract::test_a_business_scoped_participant_is_sent_as_recipient_and_never_to` |
| B4 | read another Meta product's payload as WhatsApp's | `payload.py` | **killed** | `test_whatsapp_identity_parsing::test_p5_p6_another_products_delivery_is_refused_and_counted[page-entry0]` |

M4 is killed by the tenant-scoped contact read refusing the foreign row (`TenantIsolationError`, a `NotFoundError` surfaced as 404) - the mutated lookup itself, stopped by the second wall; M9 needs the Y1 regression (section 9). No mutant was equivalent or inapplicable.

---

## 30. Static gates

At `5f8a788`, clean tree (`logs/final_static.txt`): `ruff check .` - all checks passed; `black --check .` - 812 files unchanged; `mypy app tests` - no issues in 719 source files; `mypy` on the two new scripts - no issues. Found and fixed while committing: one unused `type: ignore` in a new test (mypy `unused-ignore`).

No blanket suppression or formatting exemption was added, and every new narrow one names its rule:

| Where | Added |
| --- | --- |
| application code (`app/`) | no `noqa`, no `type: ignore`; four `pragma: no cover` on defensive branches, each saying what makes it unreachable |
| migration `0083` | one `noqa: S608` - the keyset walk interpolates a module-constant table name |
| `scripts/` | the mutation runner's `S603`/`S607` (fixed interpreter, `git` on PATH) and `T201` (CLI output) - the codes `run_security_mutations.py` already uses; one `pragma: no cover` on the invariants script's entry point |
| tests | one `noqa: S608` (fixed table names in a count); four code-scoped `type: ignore[...]` - a pytest fixture parameter, an Optional lookup, a test double and a keyword-override mapping |

---

## 31. Model-built whole suite

`pytest tests/` at `5f8a788` on a clean scratch worktree, model-built schema, its own PostgreSQL database and Redis server, CI's two kept-run checks deselected exactly as at the base (`logs/final_models.txt`):

| | Base `75fbe51` | Final `5f8a788` |
| --- | --- | --- |
| passed | 6,091 | **6,337** (+246) |
| skipped | 16 | 16 - the same three reasons: schema parity in a model-built run (4), real-provider suite without an OpenAI key (11), the Linux-tmpfs storage test (1) |
| deselected | 2 | 2 |
| failed / errors | 0 / 0 | **0 / 0** |
| warnings | 4 | the same 4 (third-party deprecations; two deliberate weak-key token tests) |
| duration | 28m21s | 32m39s |

Every test ran under the suite-wide guard that refuses a live DNS lookup.

---

## 32. Migration-built whole lane

Migration-built schema (`WASLA_TEST_SCHEMA=migrations`: every table, key, trigger and index built by Alembic `0001`-`0084`), CI's migration-parity job in its order, at `5f8a788` on clean scratch worktrees with databases and Redis servers of their own:

| Step | Base `75fbe51` | First run | Re-run |
| --- | --- | --- | --- |
| schema parity | 4 passed | 4 passed | 4 passed |
| `tests/integration tests/e2e` (two kept-run checks deselected) | 2,991 passed | 3,090 passed, **1 error** | **3,091 passed, 0 failed, 0 errors** (27m11s) |
| kept-data AI/tool sweep | 218 passed | 218 passed | 218 passed |

The first run's one error happened at fixture setup, before any test code ran: `OSError: [WinError 121] The semaphore timeout period has expired` while the `db_connection` fixture opened a TCP connection to the local PostgreSQL container through Docker Desktop's port proxy (`test_whatsapp_persistence.py::test_recording_the_same_event_twice_stores_one_row`). The file then passed in the same configuration (9 passed), and the whole lane was re-run from scratch on a fresh database: green. Both runs are kept (`logs/final_mig_*`, `logs/final_mig2_*`); nothing was retried in place.

In the re-run the omnichannel suites ran on the migrated schema too: oracles 4, schema 20, migrations 1, races 7, schema parity 4 - all passed.

---

## 33. Targeted suites

Counts at `5f8a788`, from the model-built whole run (`logs/final_models.xml`) unless stated:

| Suite | Cases | Result |
| --- | --- | --- |
| adapter contract (`test_channel_adapter_contract.py`) | 39 | passed |
| outbound contract | 21 | passed |
| media contract | 21 | passed |
| policy contract | 19 | passed |
| status contract | 7 | passed |
| identity (`test_omnichannel_identity.py`) | 14 | passed |
| echo and dedup | 9 | passed |
| second channel (synthetic adapter end to end) | 10 | passed |
| operations (recovery, retention, campaigns, health) | 9 | passed |
| oracles | 4 | passed; and five consecutive runs on the commit that added them |
| races (`test_omnichannel_concurrency.py` + `test_whatsapp_inbound_concurrency.py`) | 7 + 6 | passed; three consecutive runs at `5f8a788` |
| schema / migrations | 20 / 1 | passed |
| WhatsApp identity parsing (P2, P3, P5, P6, unknown field, long BSUID) | 20 | passed |
| live text turn (`aa11098`) | 2 | passed (both fail at the base) |
| per-connection allowance | 7 unit + 8 integration | passed |
| WhatsApp webhook, delivery protocol, inbound recovery, credential encryption | 9, 11, 12, 31 | passed |
| documentation truth, metric catalogue, inbound metric domains | 27, 12, 4 | passed |
| conversation and contact endpoints, platform analytics | 21, 7, 13 | passed |
| campaigns, follow-ups, media projection | 36, 36, 11 | passed |
| probes P2-P6, Y1, X3, X7, X8, T0 and the HTTP live turn, re-run at `5f8a788` | 11 + 1 | identical to the after-remediation results in sections 5, 9, 13 and 15 (`logs/probes_final.json`, `logs/probe_http_turn_final.json`) |

**Found while committing, and fixed (`e8d67de`).** The oracle suite's echo-shaped probe reused `meta.ids[0]` - the id of whichever reply was sent first - while the replies were sent in the order of an unordered `SELECT`. When that first reply belonged to the other workspace, acme's screen correctly could not see the id (tenant isolation), treated the event as a new customer message, and queued a turn; the assertion failed. It passed on one run and failed on the next with identical code. Reproduced deterministically by forcing the other workspace first; fixed in the test by naming acme's own sends - one on the same number and one on another - and ordering the replies. The forced order passes after the fix, and the suite passed five runs in a row. The product behaviour was right in both cases; the test had been order-dependent.

Existing WhatsApp fixtures that changed: payloads missing `"field": "messages"` gained it (every real Meta change carries it); suites that read a moved class import it from its new module or the re-export; the delivery-protocol double moved from `MessagingService._client` to the adapter's sender (section 1); the ownership test expects envelope `v2`; the plan-price test asserts the head is unchanged by a refused downgrade rather than naming `0081`; a media suite's fake download gained `declared_size`, a field of the real type.

---

## 34. Docker and monitoring

At `5f8a788`, clean tree:

- **Runtime image** - `docker build --target runtime`, as CI builds it: built in 144 s; runs as the non-root `wasla` user. Started as `ENVIRONMENT=staging` with a `JWT_SECRET` generated for the run and never written down, `GET /health/live` answered `{"status":"alive"}` on the fourth poll; container removed (`logs/final_docker.txt`).
- **Monitoring** - `promtool check config`: 63 rules, SUCCESS; `promtool test rules`: SUCCESS (175 alert expectations, 167 at the base); `amtool check-config`: valid (`logs/final_monitoring.txt`).

---

## 35. Security and secret scan

Review of the diff for tenant bypass, IDOR, credential leakage, SSRF, payload logging, unbounded identifiers and cross-connection lookups: identifiers bounded (connection 128, identity 255, scope 128, locator 4,096, health reason 64); every new lookup tenant-scoped except the two deliberate directories (connection routing, health census) which return no customer data; URL locators only through the guarded client and host roots; no new log line carries a phone, BSUID or message text; platform reads carry counts only.

**Secret scan - secret leaks: 0.** Counts only, never values, over the 17,510 added lines of `git diff 354db53..HEAD`, this report, and all 197 scratch log, JUnit and mutation files. Patterns: Meta, OpenAI, Paymob, Resend, JWT, private-key and AWS key shapes; credentialed PostgreSQL and Redis URLs; ngrok hosts; and this run's own local container and settings values. 17 matches are fixtures already committed in the base test suite, not secrets.

One thing was cleaned up first. pytest had printed a test database URL, with the throwaway password of this remediation's local container, in the failure output of 14 scratch logs. Those were redacted in place before the final scan. The value was never in the repository, a commit or this report, and the container is local and holds only test data.

No real Meta credential was used, no message was sent to Meta, and no production system was touched. The one live request in the whole remediation is the hermeticity hole described in section 1: a fixture token answered 401/190. The suite now refuses live DNS, so that cannot happen again.

---

## 36. Documentation and ADR changes

`DECISIONS.md` ADR-117 to ADR-123; `ARCHITECTURE.md` 5.4 (the foundation, what stays WhatsApp's) and the webhook flow; `docs/WHATSAPP.md` (senders and BSUIDs with the dated provider facts, parsing discrimination, per-connection idempotency, unsupported items); `docs/API.md` (filters, additive fields, deprecations, platform counts); `docs/MEDIA.md` (several files, locators, adapter fetch, tenant keys); `docs/OBSERVABILITY.md` (metrics, alerts); `docs/RUNBOOK.md` (refused entries, refused connection credentials, "Omnichannel foundation (0082-0084)" with prechecks, post-checks, rolling deploy and downgrades); `README.md` migration range. Nothing claims Instagram or Messenger support.

---

## 37. Before / after readiness

| Dimension | Before | After | Remaining deductions |
| --- | --- | --- | --- |
| Domain neutrality /15 | 8 | 13 | usage meters still WhatsApp-named pending ADR-122 decisions; legacy names (`whatsapp_events`, `account_id`, `wa_message_id`) until O8 |
| Identity model /15 | 3 | 12 | BSUID scoped by WABA, not portfolio (over-splits); rotation not processed; no merge/unlink or person-level erasure |
| Conversation / message model /15 | 7 | 12 | workspace-wide message and event uniques still active until O8; `reply_to` not persisted |
| Provider adapter boundaries /15 | 6 | 13 | the template registry check still sits in shared `MessagingService`; connect/claim flows are WhatsApp-only by design |
| Workers / idempotency /10 | 6 | 8 | recovery sweeps oldest-first across all channels (no per-connection fairness); agent queue still one global FIFO (WQ-09) |
| API compatibility /10 | 6 | 8 | no contacts/identities read API or connections listing yet |
| Database migration readiness /10 | 6 | 8 | O8 cleanup pending; Q1 not yet run on a production copy |
| Security / tenancy /5 | 4 | 4 | existing v1 credentials not yet re-sealed |
| Observability / testing /5 | 3 | 4 | no per-channel inbound-stopped alert |
| **Total** | **49** | **82** | |

---

## 38. Remaining provider verification

Deterministic fixtures and Meta's documentation are the authority here; no real Meta message was sent. The provider-facing WhatsApp behaviour that changed since the real-Meta E2E record (`REAL_META_WHATSAPP_E2E_VERIFICATION.md`, ending `fa02b52`), and so needs a later authorised real-provider check:

1. an inbound message from a username user (BSUID, no `from`) - stored and answered;
2. a send by `recipient` to a BSUID (supported from July 2026) and its response shape;
3. a status carrying `recipient_user_id`;
4. a delivery with a foreign `object` or an unsubscribed `field` - refused and counted;
5. a credential stored as envelope `v2` sending successfully;
6. an end-to-end AI reply to a live text message (the TOOL-17 path that was broken; the earlier record stopped at dispatch).

Unchanged and still covered by the existing record: signature verification, phone sends, template and media sends, two-step media fetch, error-envelope classification.

---

## 39. Commit ledger

Branch `omnichannel-findings-remediation`, on `354db53`, local only. One commit per invariant group; each commit below was materialised in a clean scratch worktree, verified there, and committed with exactly what that worktree held - the remediation working tree was never staged by hand.

| # | Commit | Subject | Closes / scope | Verified on that commit |
| --- | --- | --- | --- | --- |
| 1 | `75fbe51` | test: let the Redis-backed suites run against an isolated server | test harness only; every URL byte-identical when unset | the baseline lanes (section 3) |
| 2 | `aa11098` | fix(messaging): name the customer's message on every live agent job | live text turn defect (section 5.4); cherry-pickable | regression pair: 2 failed at the base, passing after; affected suites 242 passed, AI suites 174 passed |
| 3 | `e0d81c5` | test: resolve no live host names in the test suite | OMNI-024 / R24 | static; unit 3,104 passed; integration + e2e + real-provider 2,989 passed, 15 skipped |
| 4 | `8ea574b` | feat(omnichannel): add channel connections, contact identities and neutral keys | schema for OMNI-001, 003, 004, 005, 007, 009, 022 | static; unit 3,107; model-built integration + e2e + real-provider 3,010 passed, 15 skipped; migration-built 3,013 passed + parity (one failure, fixed before the commit, then 4/4) + kept sweep 218; migration and schema suites 24 passed; fresh head, `alembic check`, preflight |
| 5 | `2c30041` | refactor(channels): move the send-outcome taxonomy and Meta signatures to neutral modules | OMNI-020; the shared Meta signature (OMNI-010) | static; unit 3,107; affected integration suites 420 (pre-check) and 75 passed; security mutants S04/S04b killed at the new path |
| 6 | `5c66a66` | feat(omnichannel): run WhatsApp on the channel-neutral path | OMNI-001 to 009, 011, 012, 014, 015; OMNI-002 | static; unit 3,143; integration + e2e + real-provider 3,060 passed, 15 skipped, 0 failed (on a byte-identical tree) |
| 7 | `70c9826` | feat(channels): share each connection's sending allowance | OMNI-017 | static; unit 3,150; affected integration suites 292 passed |
| 8 | `8b348d5` | feat(observability): alert on refused inbound entries and gauge connection health | OMNI-021 | static; unit 3,150; affected integration suites 166 passed; promtool and amtool |
| 9 | `e8d67de` | test(omnichannel): adapter contracts, oracles, races and the mutation campaign | OMNI-R10 | static (with both scripts); unit 3,250; omnichannel integration suites 60 passed; the oracle suite five times in a row (section 33) |
| 10 | `5f8a788` | docs(omnichannel): record the foundation's decisions and operations | ADR-117 to 123, guides, runbook | the documentation-truth and documentation-reading suites, 172 passed; then the whole final verification, sections 25-35 |
| 11 | this commit | docs(omnichannel): record readiness findings remediation | this report | - |

Every commit message ends with the `Co-Authored-By` line. Nothing was merged, pushed or deployed.

---

## 40. Final verdict

**Can Wasla now add a second customer messaging channel as an adapter without duplicating core business logic or risking identity/routing corruption?** Yes, for the engineering: identity, connection, provider message identity, inbound orchestration, recovery, policy, media and routing are channel-neutral, WhatsApp runs on them unchanged, and the seam is held by contract suites, oracles, races and 18 killed mutants. Before such a channel reaches customers the product must decide its meters, connection limits and opt-out scope (ADR-122), and the workspace-wide uniques should be dropped (O8) once the neutral path has run a release. Instagram, Messenger and WhatsApp Coexistence are **not** implemented or verified.

**FINAL VERDICT: OMNICHANNEL FOUNDATION READY WITH NAMED NON-BLOCKING DEFERRED ITEMS**

### Finding closure table

| ID | Sev. | Original reproduction | Remediation | Migration | Permanent test | Mutant | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| OMNI-001 | Critical | identity = `contacts.wa_id`, workspace-scoped (P7, catalog S3) | `contact_identities`, scoped; provider-asserted linking (R2) | 0082 | identity, schema, oracle suites | M1, M4, M8 | CLOSED |
| OMNI-002 | High | P2: `stored 0, ignored 1` | parser + identities + `recipient` (R1) | 0082 | P2 unit + integration | M13, B3 | CLOSED |
| OMNI-003 | High | FK into `whatsapp_accounts` (S2/Q8) | `channel_connections`, shared ids, keys re-pointed (R3) | 0082, 0084 | schema, second-channel | M14 | CLOSED |
| OMNI-004 | High | `_dispatch` addressed `contact.wa_id` | pinned participant; campaign recipient identity (R4) | 0082-0084 | identity, operations, oracle | M3, M6, M10 | CLOSED |
| OMNI-005 | High | Y1: empty conversation, turn on our own outbound | per-connection screen, collisions, backstop (R5) | 0082-0084 | echo/dedup suite, concurrency | M2, M9, M11, B1 | CLOSED |
| OMNI-006 | High | ingestion inside `WhatsAppIngestionService` | `InboundEvent`, `ChannelIngestionService`, adapter (R6) | - | inbound contract, second-channel | M11 | CLOSED |
| OMNI-007 | High | WhatsApp-only log/recovery/retention | neutral event log in place; recovery and retention for all (R7) | 0082, 0084 | operations, recovery suites | - | CLOSED |
| OMNI-008 | High | 24h/4096/"over WhatsApp" in shared code | `ChannelPolicy`/`ChannelCapabilities`, byte-aware bounding (R8) | - | policy contract, second-channel | M5, M12 | CLOSED |
| OMNI-009 | High | X7: second file refused | position, locators, adapter fetch (R9) | 0082-0084 | X7, media contract, concurrency | - | CLOSED |
| OMNI-010 | Medium | P5/P6 stored as WhatsApp; unknown field silent | object/field discrimination, counted (R11) | - | parsing, inbound contract | B4 | CLOSED |
| OMNI-011 | Medium | per-message only | watermark primitive (R12) | - | second-channel, status contract | - | CLOSED |
| OMNI-012 | Medium | tenant-only AAD; fallback not scoped; no health | v2 AAD, WhatsApp-only fallback, health (R13) | 0082 | credential encryption, operations | - | CLOSED |
| OMNI-013 | Medium | WhatsApp meters/limit; no decision | ADR-122; registry refuses undecided meter | - | policy contract | - | CLOSED BY DESIGN/ADR (product decision deferred) |
| OMNI-014 | Medium | no channel/connection filter or index | filters, index, channel labels (R14) | 0084 | second-channel, endpoints | M7 | CLOSED |
| OMNI-015 | Medium | `wa_*` fields only | additive fields, deprecations (R15) | - | endpoint suites | - | CLOSED |
| OMNI-016 | Medium | opt-out by `wa_id` | resolved from the sender's contact; scope ADR-122 | - | opt-out ingestion | - | CLOSED BY DESIGN/ADR (scope decision deferred) |
| OMNI-017 | Medium | per-campaign rate only | per-connection allowance (R16) | 0082 | throughput suites | - | CLOSED |
| OMNI-018 | Medium | coexistence prerequisites missing | BSUID, echo kind, field discrimination in place; Coexistence itself R22 | 0082 | parsing, echo | - | CLOSED BY DESIGN/ADR (prerequisites; feature deferred) |
| OMNI-019 | Medium | no person-level erasure/export | new tables in workspace purge; person-level R21 | - | purge partition | - | DEFERRED |
| OMNI-020 | Low | taxonomy in WhatsApp client | `app.channels.outcomes` (R17) | - | outbound contract | - | CLOSED |
| OMNI-021 | Low | no refused-entry signal or channel labels | counters, gauge, 3 alerts (R18) | - | metric domains, promtool | - | CLOSED |
| OMNI-022 | Low | X8 accepted | composite tenant keys | 0084 | X8 regression | - | CLOSED |
| OMNI-023 | Low | WhatsApp-named vocabularies | neutral names for new things; nothing renamed | 0082 | - | - | CLOSED (by policy, R23) |
| OMNI-024 | Low | DNS-dependent unit test | resolver seam; suite refuses live DNS (R24) | - | whole suite under the guard | - | CLOSED |
| OMNI-025 | Low | no per-connection agent | workspace default kept (ADR-122) | - | - | - | CLOSED BY DESIGN/ADR |
| OMNI-026 | Info | verified neutral foundations | preserved (business layer unchanged) | | | | PRESERVED |
| OMNI-027 | Info | single outbound choke point | preserved: every send through `_dispatch` | | | | PRESERVED |
| OMNI-028 | Info | prior "identity unaffected" superseded | documented in docs/WHATSAPP.md and ADR-118 | | | | DOCUMENTED |
