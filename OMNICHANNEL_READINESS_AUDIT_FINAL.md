# Wasla Omnichannel Readiness Audit

| | |
| --- | --- |
| Date | 2026-10-02 |
| Type | Independent readiness audit (audit only - no remediation in this branch) |
| Audit base branch | `worktree-billing-google-auth` |
| Audit base HEAD | `347306803a6b0d0582002b14c6aabadec885e34e` (`3473068`, merge of the omnichannel foundation) |
| `origin/worktree-billing-google-auth` | `354db5398d553f7e9ea9fee4377b99a84d9f3f3d` - the audit base is **12 commits ahead and unpushed** |
| Audit branch / worktree | `omnichannel-readiness-audit-20261002` at `E:\wasla-omnichannel-audit-final` |
| Alembic head | `0084` (single head) |
| Prior stages | audit `49e2e98` (2026-09-28, 49/100) -> remediation `4035642` (self-scored 82/100) -> merged as `3473068` **without the planned independent verification**. This audit is that independent look, on the merged tree. |
| Finding IDs | Continue the 2026-09-28 series (`OMNI-001`..`OMNI-028`, `OMNI-R1`..`R25`) so the IDs already cited in code comments and ADR-117..123 keep one meaning. New findings are `OMNI-029`..`OMNI-058`; new backlog items `OMNI-R26`..`OMNI-R46`. |
| Production / Meta / customers | Not touched. No real message sent, no webhook or Meta configuration changed. All probes used synthetic identifiers on this audit's own containers. |

---

## 1. Executive summary

**Verdict: OMNICHANNEL READY WITH LIMITED REMEDIATION.**

**Readiness score: 71 / 100** (the remediation's self-assessment was 82; deductions in section 50).

| Severity | Count | IDs |
| --- | --- | --- |
| Critical | 0 | - |
| High | 3 | OMNI-029, OMNI-030, OMNI-031 |
| Medium | 12 | OMNI-032 .. OMNI-043 |
| Low | 12 | OMNI-044 .. OMNI-055 |
| Info | 3 | OMNI-056 .. OMNI-058 |

**Answer to the core question.** *Can Wasla add Instagram Direct, Facebook Messenger and future channels without redesigning or duplicating its core conversation, customer, AI, CRM, media, campaign, notification, worker and authorization systems?* - **Yes. The redesign is done and it holds.** The merged foundation gives every channel a neutral connection (`channel_connections`), scoped customer identities (`contact_identities`), a conversation pinned to the identity that wrote, per-connection provider message identity, one neutral inbound pipeline with the event log, recovery and retention shared by all channels, a channel policy chosen from the conversation, ordered multi-attachment media with neutral locators, and one outbound choke point that routes `conversation -> connection -> policy -> participant -> adapter`. Every send path in the application goes through it; nothing reads `contacts.wa_id` to address anybody; tenancy holds in every cross-tenant probe. A second channel is an adapter, a connect flow and a route - **not** a rewrite.

**It is not yet safe to switch a second channel on**, for two concrete reasons and one policy gate (section 41), and there is one present-day WhatsApp defect to fix first:

1. **OMNI-029 (High, regression introduced by the remediation `5c66a66`).** Every delivery-status webhook - WhatsApp sends three per outbound message - now resolves its message with `wa_message_id = ? AND connection_id IN (...)` and no tenant predicate. No index leads with either column. On a migration-built database seeded with 200,000 messages the production query is a **parallel sequential scan of `messages`** (4,878 buffers, 22.7 ms); with sequential scans disabled the best plan is a full index scan; the pre-remediation tenant-scoped form used an index (4 buffers, 0.07 ms). The cost grows with the platform's total message count, on the hottest webhook path, for WhatsApp today and every channel after it.
2. **OMNI-031 (High).** The unified inbox fails closed. `GET /api/v1/conversations` renders each conversation's channel policy, and the registry raises for a channel without an adapter - so one Instagram conversation in a workspace whose Instagram adapter is disabled turns the whole inbox page into **422 "Wasla cannot operate this channel."**, WhatsApp threads included (probe P2). The registry is the only on/off switch a channel has, so the rollback plan for a bad second-channel release breaks WhatsApp's inbox. That violates the rollout principle *an Instagram outage must not break WhatsApp*.
3. **Policy gate (OMNI-041 and ADR-122).** Meta's Messenger/Instagram policy requires an automated experience to **disclose that it is automated** at the start, after long gaps and when moving from a person back to automation; nothing models that obligation. ADR-122's product decisions (meters/quota, connection limits/price, opt-out scope) are still open - and `ChannelRegistry` already refuses to register an adapter whose meter is undecided, so this gate is enforced in code.
4. **Fix first, today, regardless of any second channel - OMNI-030 (High, present-day WhatsApp).** Customers' **button taps lose their content.** Meta delivers a template quick-reply as `"type": "button", "button": {"payload", "text"}` and an interactive reply as `interactive.button_reply|list_reply {id, title}` (Meta docs, re-checked today); the parser maps both to `INTERACTIVE` with **no text**, so the AI is billed to answer the literal string `[interactive]`, and a customer who taps a marketing template's **"Stop promotions"** button is **not opted out** - although `app/services/opt_out.py` lists that exact phrase as "the wording WhatsApp's own opt-out button sends", and Meta's WhatsApp Business Policy requires respecting every opt-out request (probe P1).

None of these is an architectural integrity failure: no path leaks across tenants, sends to the wrong recipient or channel, or merges identities. The fixes are local (a predicate or an index, a tolerant renderer, a parser mapping, a policy hook) - hence *limited remediation*, not *foundation required*.

**Provider conformance (reviewed at the user's request against Meta's current documentation, section 4a).** 38 provider contracts were checked (27 WhatsApp, 7 Instagram, 4 Messenger). **19 conform** - signature and verification handshake, retry/duplicate handling, field and object discrimination, BSUID fields and `recipient` addressing, the text limit, voice notes, statuses, the media two-step fetch and upload, template shape and withdrawal codes, the 24-hour window, escalation paths, Graph API version dates, IGSID and PSID scoping. **4 mismatch**: the webhook body cap is 1 MiB where Meta documents payloads up to 3 MB (OMNI-034; 1.47 MB and 2.57 MB well-formed deliveries refused 413), template quick-reply and interactive replies lose their content (two contracts, OMNI-030), and opt-out requests are not all respected (OMNI-030, OMNI-046). **10 are gaps or partial**: Meta's throttling and connection-level error codes (OMNI-035), outbound per-type media limits (OMNI-045), BSUID scope/rotation and the authentication-template limit (OMNI-054), Instagram media by URL only (OMNI-040), `messaging_type`/`HUMAN_AGENT` (OMNI-033), Messenger read watermarks (OMNI-042) and the automation-disclosure obligation (OMNI-041). **3 are adapter work not yet built** (Instagram events and fields; Coexistence) and **2 are notes or unverified** (the 36-hour Graph retry horizon; the Instagram-Login signing secret).

**Other present-day WhatsApp defects found** (not second-channel blockers, fix first anyway): an out-of-order delivery moves `last_inbound_at` *backwards*, shrinking or closing the 24-hour window (OMNI-036, probe P10; since `69703d6`); the 413 above is logged and never counted or alerted (OMNI-034).

**Channel verdicts.**

| Channel | Readiness | Why |
| --- | --- | --- |
| WhatsApp (current) | Production-grade on the neutral path, with two present-day defects to fix first (OMNI-029 at scale, OMNI-030) and two medium ones (OMNI-034, OMNI-036) | |
| Instagram Direct | **NEEDS REMEDIATION** | OMNI-029, OMNI-031, OMNI-041 and ADR-122 must land; text and images can launch on the existing seams; video/audio/PDF sends need a URL mechanism (OMNI-040) |
| Facebook Messenger | **NEEDS REMEDIATION** | as Instagram, plus read watermarks compared against a local clock (OMNI-042) and `messaging_type`/`HUMAN_AGENT` plumbing (OMNI-033) |
| WhatsApp Coexistence | **BLOCKED** | needs history import ordered by provider time (OMNI-038), projection of Business-app echoes (OMNI-037), the three refused webhook fields and Meta's 24-hour history-sync deadline, payloads above 1 MiB (OMNI-034), a monotonic window (OMNI-036) and per-connection throughput (OMNI-052) - none exists |

**Regression baseline at the audit HEAD:** ruff clean; black 812 files unchanged; mypy `app tests` clean (719 source files). Test lanes: see section 3 and Appendix C.

---

## 2. Scope and methodology

**Scope.** The whole backend at `3473068` from the perspective of adding a second customer messaging channel: domain model, schema and migrations, inbound webhook to projection, outbound routing to provider, status lifecycle, media, AI and tools, CRM and follow-ups, campaigns, workers and queues, API contracts, authorization, observability, privacy. Billing was read only where messages are metered.

**Method.**

1. Repository state, isolated worktree from the exact canonical HEAD, the user's nine untracked documents in `E:\wasla` left untouched.
2. Read the prior audit (`49e2e98`), the remediation report, ADR-117..123, `ARCHITECTURE.md`, `docs/WHATSAPP.md`, `docs/API.md`, `docs/CAMPAIGNS.md`, `docs/MEDIA.md`, and judged none of them correct until code, schema or a probe agreed.
3. Read every module on the message path end to end (Appendix A lists them), and swept the whole application for `wa_id`, `wa_message_id`, `phone_number_id`, `waba`, `whatsapp` and WhatsApp-client imports outside the adapter.
4. **Schema census on a database built only by migrations** (`alembic upgrade head` from empty: 11 s, single head `0084`), read-only catalog queries C1-C11 (Appendix B).
5. **Query plans on seeded volume**: 200 workspaces, 10,000 conversations, 200,000 messages built through the real triggers, then `EXPLAIN (ANALYZE, BUFFERS)` of the production queries E1-E7 (Appendix B).
6. **Seventeen deterministic probes** (P1-P11 and P4b, Appendix D) through production code paths on a real PostgreSQL - ingestion, the HTTP inbox route, the recovery worker, the messaging and follow-up services - with a synthetic second channel (`tests/channel_fakes.py`, never registered by the application). The probe file was kept out of the tracked tree and is not committed.
7. **Provider conformance review** of the WhatsApp client, parser, signature and media code against Meta's current developer documentation, and of the Instagram/Messenger assumptions behind the verdicts (section 4a), each contract quoted and dated.
8. Static gates and the full test suite on the audit HEAD in this audit's own containers (section 3).

**Severity rubric** (as briefed): *Critical* - cross-tenant leakage, wrong-recipient sends, identity collision or irreversible corruption when a channel is added; *High* - blocks a safe second channel or creates major duplicated logic or inconsistent message state; *Medium* - significant maintainability, migration or operational issue without immediate integrity risk; *Low* - cleanup and ergonomics; *Info* - correct architecture or intentional provider specificity. Findings that rest on inference rather than a reproduction say so (*Architectural risk / inference*).

---

## 3. Repository / commit state

```
git fetch origin --prune                     # nothing new
branch                                       worktree-billing-google-auth
HEAD                                         347306803a6b0d0582002b14c6aabadec885e34e
origin/worktree-billing-google-auth          354db5398d553f7e9ea9fee4377b99a84d9f3f3d   (local is 12 ahead)
tracked tree                                 clean; 9 untracked user documents, untouched
alembic heads                                0084 (single), fresh 0001 -> 0084 in 11 s
audit worktree                               E:\wasla-omnichannel-audit-final, branch omnichannel-readiness-audit-20261002
```

The 12 unpushed commits are the remediation (`75fbe51`..`4035642`) and its merge `3473068`. **`origin` does not have them**, so origin also still lacks the live-text-turn fix `aa11098` (every live inbound WhatsApp text stored and never answered); anything deployed from `origin` today has that defect.

**Test infrastructure.** This audit's own containers only, on ports nothing else uses: `pgvector/pgvector:pg16` (127.0.0.1:55811), two `redis:7-alpine` (56811, 56812), MinIO from the pinned GHCR mirror (59811), named `wasla-omni-final-*`. Separate databases for the model-built lane, the migration-built lane, the probes and the catalog census. The development stack and every other worktree's containers were not touched. Each lane asserted `app.__file__` resolves inside the audit worktree.

| Gate / lane | Result |
| --- | --- |
| `ruff check .` | All checks passed |
| `black --check .` | 812 files would be left unchanged |
| `mypy app tests` | Success: no issues found in 719 source files |
| Model-built `pytest tests/` (CI's invocation and its two deselects) | **6,337 passed, 16 skipped, 2 deselected, 0 failed, 0 errors** (30 m 46 s). The 16 skips are CI's three sanctioned reasons: schema parity in a model-built run (4), real-provider suite without an OpenAI key (11), the Linux tmpfs test (1) |
| Migration-built `tests/integration tests/e2e` (`WASLA_TEST_SCHEMA=migrations`) | **3,091 passed, 2 deselected (CI's two), 0 skipped, 0 failed, 0 errors** (29 m 09 s), after schema parity 4 passed |
| Audit probes P1-P11, P4b (17 cases) | 17 passed (assertions record observed behaviour; Appendix D) |
| Catalog census C1-C11 on a migration-built database | no NOT VALID constraint, no invalid index, single head |

---

## 4. Current channel architecture

```
POST /api/v1/webhooks/whatsapp            (Meta HMAC, app/integrations/meta/signature.py)
      |  1 MiB body cap (OMNI-034)
WhatsAppIngestionService (facade)  ->  WhatsAppAdapter.parse  ->  ParsedDelivery(InboundEvent..., refused{reason})
      |
ChannelIngestionService (neutral, one savepoint per event)
      |-- ConnectionDirectory.owner_at  (channel, external_account_id, instant)  -> channel_connections   (ADR-101)
      |-- ChannelEventRepository.store  -> whatsapp_events (ORM: ChannelEvent)   (ADR-102)
      |-- ConversationProjectionService.screen  (duplicate | collision | new)    (ADR-120)
      |-- ContactIdentityService.resolve_sender -> contact_identities -> contacts (ADR-118)
      |-- project_message -> conversations (pinned participant) / messages / message_media
      |-- hand-offs: AgentJob(trigger) | MediaJob per file; cancel follow-ups; opt-out; meter
      `-- statuses: _status_owner -> OutboundMessageDirectory (OMNI-029) -> advance_status | watermark

API / AI worker / follow-ups / campaigns
      `-> MessagingService._dispatch : conversation -> channel_connections -> ChannelPolicy
              -> participant identity -> adapter.address -> allowance -> TX1 CLAIMED -> prepare
              -> TX2 REQUESTED -> adapter.sender.send -> TX3 SENT | UNDELIVERED | (REQUESTED = uncertain)
```

`app/channels` (adapter protocol, inbound events, policy and capabilities, outcomes, metering, throughput, URL media fetcher, registry) imports no provider. `ChannelRegistry` (`app/channels/registry.py:33`) holds WhatsApp only and refuses any other channel (`ChannelUnavailableError`) rather than falling back. Channel *labels* for Instagram and Messenger exist in `channel_kind`, `identity_kind` (`psid`, `igsid`) and `identity_scope` (`connection`); nothing writes them.

---

## 4a. Provider conformance: code against Meta's documentation

Requested during the audit. Each row quotes or paraphrases Meta's documentation **as read on 2026-10-02** (sources at the end of the section) and the code that implements it. Meta changes these pages without a repository changing; re-read before implementing.

**WhatsApp Cloud API (implemented)**

| # | Contract (Meta) | Code | Verdict |
| --- | --- | --- | --- |
| M1 | Verification: `hub.mode=subscribe`, `hub.verify_token` must match, respond with `hub.challenge` | `app/api/v1/webhooks.py:138` constant-time token compare; echoes the challenge only on a match, 403 otherwise; 503 when unconfigured | Conforms |
| M2 | Signature: `X-Hub-Signature-256: sha256=<HMAC-SHA256(payload, App Secret)>` | `app/integrations/meta/signature.py:24-44` over the raw bytes, constant-time | Conforms |
| M3 | Non-200 is retried "for up to 7 days"; "retries can result in duplicate webhook notifications" | 200 for anything unprocessable, 5xx only for infrastructure; per-connection event dedup | Conforms |
| M4 | "Webhook payloads can be up to 3 MB"; Graph batches "a maximum of 1000 updates" | `webhook_max_request_bytes = 1 MiB` (`app/core/config.py:650`), enforced before the signature (`app/core/limits.py:188`) -> 413 | **Mismatch** - OMNI-034 (P11: 1,468,122 B and 2,568,972 B refused) |
| M5 | Subscribable fields include `messages`, `history`, `smb_message_echoes`, `smb_app_state_sync`, `user_preferences`, `message_template_status_update` | only `messages` processed (`payload.py:57`); every other field refused and counted `unsupported_field` | Conforms as explicit non-support; consequences in OMNI-046 and the Coexistence verdict |
| M6 | `object = whatsapp_business_account`; `changes[].field`; `value.metadata.phone_number_id` | enforced (`payload.py:300-329`) | Conforms |
| M7 | BSUID inbound: `contacts[].user_id` and `messages[].from_user_id` always; `wa_id`/`from` omitted for some username users; `parent_user_id`, `from_parent_user_id`; `profile.username` | parsed, bounded at 255, both identifiers kept as one provider assertion (`payload.py:278-290`, `adapter.py:114-127`) | Conforms |
| M8 | BSUID = ISO country code + `.` + up to 128 alphanumerics; parent adds `ENT.`; "unique to each business portfolio-user pair"; regenerated on phone change, "which triggers a system messages webhook" | scoped by WABA (a portfolio-safe over-split); `system` messages stored as `unsupported`, rotation not processed | Partial, documented deferral (ADR-118 item 5) |
| M9 | Send to a BSUID with `recipient`; "If you do [send both], `to` ... will take precedence"; response `contacts[].input/user_id`; statuses carry `recipient_user_id` always | exactly one of `to`/`recipient` (`client.py:915-917`); response read for `wa_id` or `user_id` | Conforms |
| M10 | BSUIDs cannot receive "one-tap, zero-tap, and copy code authentication templates" | not checked before staging; Meta's refusal is recorded as undelivered | Gap (Low) - OMNI-054 |
| M11 | Text: `messaging_product`, `recipient_type: individual`, `to`, `type: text`, `text.body` "Maximum 4096 characters", `preview_url` | `client.py:325-339`; `WHATSAPP_TEXT_MAX_CHARS = 4096` counted in characters | Conforms |
| M12 | Template quick-reply tap arrives as `"type": "button"`, `"button": {"payload", "text"}` | mapped to `INTERACTIVE` (`adapter.py:93`) and `_message_text` returns nothing for it (`payload.py:185-199`) | **Mismatch** - OMNI-030 |
| M13 | Interactive reply arrives as `"type": "interactive"`, `interactive.button_reply {id, title}` (or `list_reply`), with `context` | same: no text, AI reads `[interactive]` (`agents/memory.py:209`) | **Mismatch** - OMNI-030 |
| M14 | Voice: `type: audio` with `audio.voice: true`; OGG/OPUS; audio max 16 MB | `voice` flag read (`payload.py:238`); a `voice` type would also be accepted, harmlessly | Conforms |
| M15 | Statuses `sent`, `delivered`, `read` (+ `failed`); "played" for voice messages from 2026-03-17 | four mapped (`adapter.py:101-106`); others stored, never projected | Conforms (Wasla sends no voice notes, so `played` never applies) |
| M16 | Media: `GET /<MEDIA_ID>` -> `url`, `mime_type`, `sha256`, `file_size`; "Media URLs expire after 5 minutes"; download "will fail" without the token; optional `phone_number_id`; inbound media ids "expire after 7 days" | two-step fetch, token sent only to Meta hosts on every hop, SSRF guard, capped streaming (`client.py:514-567, 639-670`) | Conforms; `phone_number_id` binding and the 7-day expiry are not used (Low, OMNI-045) |
| M17 | Upload: `POST /<PHONE_NUMBER_ID>/media` with `file`, `type`, `messaging_product` -> `{"id"}`; uploads persist 30 days | `client.py:584-620` | Conforms |
| M18 | Outbound limits: image JPEG/PNG <= 5 MB, video MP4/3GPP <= 16 MB, audio <= 16 MB, documents <= 100 MB, stickers WebP | route cap 16 MB, family check only | Gap (Low) - OMNI-045 |
| M19 | Template: `template {name, language {code}, components}` | `client.py:454-474` | Conforms |
| M20 | 132001 "does not exist in the specified language or ... has not been approved"; 132015 "paused due to low quality"; 132016 "permanently disabled" | the three codes write `PAUSED` back to the registry (`client.py:108-117`) | Conforms (132001 also means "not approved yet"; `PAUSED` is the conservative label and a sync corrects it) |
| M21 | Throttling codes 4, 80007, 130429, 131056 - HTTP status **not specified** by Meta; the throughput page: "the API will return error code 130429" (80 mps per number by default, up to 1,000 by automatic upgrade; 131057 while an upgrade is in progress) | only HTTP 429 is a rate limit (`client.py:992-999`) | **Gap** - OMNI-035 |
| M22 | Connection-level codes 0, 3, 10, 190, 200-299, 131005, 368, 131031 | only HTTP 401 or code 190 is a credential failure (`client.py:1035-1049`) | **Gap** - OMNI-035 |
| M23 | Customer-service window: opens when the user messages or calls, resets on each, 24 h; outside it "you can only send pre-approved template messages" | `WhatsAppChannelPolicy` 24 h from `last_inbound_at`, templates the escape (`integrations/whatsapp/policy.py`) | Conforms; but the anchor can move backwards (OMNI-036); calls not handled (no calling) |
| M24 | Business Policy: "You must respect all requests (either on or off WhatsApp) by a person to block, discontinue, or otherwise opt out of communications from you via WhatsApp"; `user_preferences` `stop`/`resume`; 131050 "Don't retry" | stop words in *text* only; button taps lost; `user_preferences` refused; 131050 recorded per message | **Mismatch** - OMNI-030, OMNI-046 |
| M25 | Automation allowed in the window "but must also have available prompt, clear, and direct escalation paths" | handoff tool and human mode | Conforms |
| M26 | Graph API: v21.0 available until **2027-01-21**; v22.0 2027-05-20; v23.0 2027-10-08; v24.0 2028-02-18; v25.0 2028-07-29; v26.0 current (TBD) | `META_API_SUNSETS` matches exactly (`versions.py`); default `meta_api_version = "v21.0"` | Conforms; plan the upgrade (OMNI-055) |
| M27 | Coexistence: subscribe `history` (180 days, phases, `chunk_order`, `progress`; "you have 24 hours to synchronize ... otherwise they must be offboarded"), `smb_app_state_sync`, `smb_message_echoes`; fixed 20 mps; Business-app messages "do not create, extend, or affect Cloud API conversation windows" | all three fields refused; no history import, echo projection or per-connection throughput | Not supported - Coexistence **BLOCKED** |

**Instagram messaging (not implemented; assumptions behind the verdict)**

| # | Contract (Meta) | Current model | Verdict |
| --- | --- | --- | --- |
| I1 | Webhook `object: "instagram"`; `entry[].messaging[]` with `sender.id`, `recipient.id` (the professional account), `timestamp`, `message {mid, text, attachments[{type, payload.url}], is_echo, is_deleted, reply_to{mid} or reply_to{story{url,id}}}`; `read {mid}`; `reaction {mid, action, emoji}`; `message_edit {mid, text, num_edit}`; `postback {mid, title, payload}` | `InboundEvent` holds message, echo, URL attachments, per-message status; no edit, unsend, reaction or postback representation; `reply_to` is a string and not persisted | Ready with adapter; gaps OMNI-053 |
| I2 | Fields `messages`, `messaging_seen`, `message_reactions`, `messaging_postbacks`, `messaging_referral`, `standby`, `messaging_handover` | none | Adapter work |
| I3 | IGSID is unique "per Instagram professional account" | `identity_scope = connection` | Conforms |
| I4 | Send: `graph.instagram.com/<IG_ID>/messages` (or `/me/messages`), Instagram User token; `recipient {id}`; text "1000 bytes or less", UTF-8; image by `url` or `attachment_id`, **video, audio, file by `url` only**; up to 10 images per message; image 8 MB, audio/video/PDF 25 MB | byte-aware bounding exists (`app/channels/policy.py:132-155`); outbound media is upload-only and storage issues no URLs | Partial - OMNI-040 |
| I5 | Reply only after the user writes; 24 h; `HUMAN_AGENT` "required for Instagram Messaging API", 7 days | `WindowedPolicy` can refuse; no way to tell the adapter to tag | Partial - OMNI-033 |
| I6 | Graph webhooks retried "over the next 36 hours" | recovery assumes the provider retries a 5xx; WhatsApp's horizon is 7 days | Note - OMNI-050 |
| I7 | Signed with `X-Hub-Signature-256` and "your app's App Secret" - which secret for an Instagram-Login app is not stated | one `META_APP_SECRET` | Unverified - OMNI-050 |

**Messenger (not implemented)**

| # | Contract (Meta) | Current model | Verdict |
| --- | --- | --- | --- |
| F1 | `object: "page"`; `messaging[].sender.id` = PSID ("The Page-scoped ID"), `recipient.id` = Page; `message {mid, text, quick_reply.payload, reply_to.mid, attachments[]}` | PSID kind, connection scope | Ready with adapter |
| F2 | `message_reads.read.watermark` (epoch ms): "All messages that were sent before or at this timestamp were read"; no `mid` | `advance_to_watermark` compares Wasla's *local* `sent_at`, stamped after the Send API answers | Risk - OMNI-042 (P5) |
| F3 | `POST /<PAGE_ID>/messages` with a Page access token, `recipient {id: PSID}`; a messaging type is required - a response, a proactive update (both inside the 24 h window) or a tagged message outside it; `HUMAN_AGENT` "allows a business representative to manually respond to a person's messages within a 7-day period"; attachments by `url` (`is_reusable`) **or through the Attachment Upload API**; response `{recipient_id, message_id}`; text "UTF-8 and less than 2000 characters" (search-indexed Send API reference) | the policy decision cannot reach the adapter; upload-based media fits Messenger (OMNI-040 is Instagram's only) | Partial - OMNI-033 |
| F4 | Messenger and IG policy: "Automated chat experiences must disclose that a person is interacting with an automated service" at the start, after significant lapses, and on transition from human to automated; promotion outside the window only by tags or Sponsored Messages | not modelled | **Gap** - OMNI-041 |

**Sources** (Meta for Developers unless noted, read 2026-10-02): [Graph API webhooks - getting started](https://developers.facebook.com/docs/graph-api/webhooks/getting-started); [WhatsApp webhooks overview](https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/overview/); [messages webhook reference](https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/reference/messages); [Business-scoped user IDs](https://developers.facebook.com/documentation/business-messaging/whatsapp/business-scoped-user-ids/); [Text messages](https://developers.facebook.com/documentation/business-messaging/whatsapp/messages/text-messages); [Audio messages](https://developers.facebook.com/documentation/business-messaging/whatsapp/messages/audio-messages); [Interactive message templates](https://developers.facebook.com/docs/whatsapp/api/messages/message-templates/interactive-message-templates/); [Interactive reply buttons](https://developers.facebook.com/docs/whatsapp/cloud-api/messages/interactive-reply-buttons-messages/); [Media](https://developers.facebook.com/documentation/business-messaging/whatsapp/business-phone-numbers/media); [Error codes](https://developers.facebook.com/documentation/business-messaging/whatsapp/support/error-codes); [Throughput](https://developers.facebook.com/documentation/business-messaging/whatsapp/throughput); [Service messages / window](https://developers.facebook.com/documentation/business-messaging/whatsapp/messages/send-messages); [WhatsApp changelog](https://developers.facebook.com/documentation/business-messaging/whatsapp/changelog) (2024-11-18: `user_preferences`, 131050); [user_preferences reference](https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/reference/user_preferences); [Onboard WhatsApp Business app users (Coexistence)](https://developers.facebook.com/documentation/business-messaging/whatsapp/embedded-signup/onboarding-business-app-users/); [Graph API changelog / versions](https://developers.facebook.com/docs/graph-api/changelog/); [WhatsApp Business Policy](https://whatsappbusiness.com/policy/) (redirect target of business.whatsapp.com/policy); [Instagram webhooks](https://developers.facebook.com/docs/instagram-platform/webhooks) and [examples](https://developers.facebook.com/docs/instagram-platform/webhooks/examples); [Instagram API with Instagram Login - Messaging API](https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/messaging-api); [Messenger webhook `messages`](https://developers.facebook.com/docs/messenger-platform/reference/webhook-events/messages) and [`message_reads`](https://developers.facebook.com/docs/messenger-platform/reference/webhook-events/message-reads); [Messenger - Send a message](https://developers.facebook.com/documentation/business-messaging/messenger-platform/send-messages) and the [Send API reference](https://developers.facebook.com/docs/messenger-platform/reference/send-api/) (the latter through a search-indexed extract); [Messenger Platform and IG Messaging API policy](https://developers.facebook.com/documentation/business-messaging/messenger-platform/policy). Pages were read through an automated fetcher; quoted text is Meta's, and two facts are marked where Meta's pages did not state them (the HTTP status of throttling codes; which secret signs Instagram-Login webhooks), and Messenger's 2,000-character limit comes from a search-indexed extract of the Send API reference.

---

## 5. Dependency map

**Inbound** (WhatsApp today; a second channel enters at step 2 with its own adapter and route):

| Step | Domain object | Service / function | Repository | Table | Tests |
| --- | --- | --- | --- | --- | --- |
| 1 webhook + signature | raw bytes | `receive_events`, `_require_signature` | - | - | `test_whatsapp_webhook`, `test_whatsapp_signature` |
| 2 normalisation | `InboundEvent`, `ParsedDelivery` | `WhatsAppAdapter.parse` | - | - | `test_channel_adapter_contract`, `test_whatsapp_identity_parsing` |
| 3 tenant resolution | `ChannelConnection` | `_owner_at`, `_status_owner` | `ConnectionDirectory` (unscoped by design) | `channel_connections` | `test_whatsapp_number_handover`, identity M14 |
| 4 event log / dedup | `ChannelEvent` | `ChannelEventRepository.store` | same | `whatsapp_events` | `test_whatsapp_persistence`, echo/dedup |
| 5 screen | `ProjectedMessage` | `ConversationProjectionService.screen` | `MessageRepository.find_provider_message` | `messages` | `test_omnichannel_echo_and_dedup` |
| 6 identity | `ContactIdentity`, `Contact` | `ContactIdentityService.resolve_sender` | `ContactIdentityRepository` | `contact_identities`, `contacts` | `test_omnichannel_identity`, races |
| 7 conversation + message | `Conversation`, `Message`, `MessageMedia` | `project_message` | `ConversationRepository.get_or_create`, `record_inbound`, `MediaRepository.record` | `conversations`, `messages`, `message_media` | projection, live-turn identity |
| 8 hand-offs | `AgentJob`, `MediaJob` | `_enqueue`, `_enqueue_media`, follow-up cancel, opt-out, meter | queues (Redis) | `usage_events`, `follow_ups` | live turn, recovery |
| 9 AI / human | `AgentTurn` | `AgentWorker`, orchestrator, tools | `AgentTurnRepository` | `agent_turns`, `tool_executions` | AI suites |
| 10 statuses | `StatusUpdate` | `_status_owner` -> `project_status` / `project_watermark` | `OutboundMessageDirectory` (OMNI-029) | `messages` | status contract |

**Outbound**:

| Step | Where | Evidence |
| --- | --- | --- |
| intent | API route, `AgentWorker`, `FollowUpService.dispatch`, `CampaignService._deliver` | the only `MessagingService` constructors (`api/dependencies.py:432`, `ai_worker.py:766`, `follow_up_service.py:622`, `campaign_worker.py:208`) |
| channel selection | `conversation.channel` -> `ChannelRegistry.adapter_for` | `messaging_service.py:697-699` |
| policy | `require_sendable_text`, media family, `policy.may_send(origin, kind, now)` | `messaging_service.py:700-710` |
| connection / participant | `ChannelConnectionRepository.require_by_id` (fresh), `ContactIdentityRepository.require_by_id`, `adapter.address` | `messaging_service.py:712-718` |
| allowance | `take_send_allowance` (off by default) | `messaging_service.py:721`, ADR-123 |
| provider rendering + request | `adapter.sender(...).prepare/send` | `adapter.py:193-234` |
| provider identity | `mark_sent(wa_message_id=receipt.message_id)` | `messaging_service.py:864-868` |
| lifecycle | `CLAIMED -> REQUESTED -> SENT / UNDELIVERED`; statuses advance `status` monotonically | ADR-093, `conversation_repository.py:794-829` |

No AI tool sends a message; no service calls `WhatsAppClient.send_*` outside the adapter (sweep: only `template_service.py:169` - template sync - and `media_worker.py:369` - the media client factory - construct a `WhatsAppClient` outside `app/integrations/whatsapp`).

---

## 6. Provider vs channel model

The model now separates the three levels the brief asks about:

| Level | Represented by | Example |
| --- | --- | --- |
| Provider | the adapter (code), `Provider` telemetry label | Meta - `WhatsAppAdapter`, `Provider.WHATSAPP` |
| Channel | `Channel` enum on connections, conversations, events, identities | `whatsapp`, `instagram`, `messenger` |
| Connection | `channel_connections` row (+ provider extension table) | a phone number (`whatsapp_accounts`, same id) |
| External identity | `contact_identities` row, scoped | `phone`/workspace, `bsuid`/provider_account(WABA), `psid`/`igsid`/connection |

*Provider* is not a column: the provider is implied by the channel (Meta for all three). That is adequate while each channel has one provider; a channel served by two providers (WhatsApp through a BSP, say) would need it. Not needed for Instagram or Messenger. **Classification: CHANNEL-NEUTRAL.**

---

## 7. Connection model

`channel_connections` (`app/db/models/channel.py:185`) holds what every connection has - workspace, channel, `external_account_id` (<=128), status, tenure `[ownership_started_at, released_at)`, health, credential expiry, send window - and nothing provider-specific; WABA, display number, verified name, proof and token stay on `whatsapp_accounts`, which shares the id and mirrors lifecycle by trigger (`trg_whatsapp_accounts_channel_connection`). Live uniqueness is `(channel, external_account_id) WHERE released_at IS NULL`; the tenure index serves ADR-101's "who held it then" for every channel. A Page or an Instagram professional account fits as a row plus (if needed) an extension table - no `instagram_account_id` column on a shared table. Conversations and events reference `(tenant_id, id, channel)`, so a conversation's channel and its connection's channel are one fact (C4, validated keys).

Gaps, none blocking: lifecycle is still *written* on `whatsapp_accounts` and mirrored (O8); the per-connection send allowance is one deployment-wide number (OMNI-052); only `AUTH_FAILED` health is ever written, and only on HTTP 401/code 190 (OMNI-035). **Classification: CHANNEL-NEUTRAL. Readiness: READY.**

---

## 8. Customer / external identity

**What identity means now.** A contact is the person; `contact_identities` holds each address a provider uses, unique on `(tenant_id, channel, kind, scope, scope_ref, value)` - never on the value alone, never per workspace alone. Scopes: `workspace` (WhatsApp phone), `provider_account` (BSUID, by WABA), `connection` (PSID/IGSID). Keys tie an identity to its own contact and connection inside its workspace (C4). Linking happens only by provider assertion in one signed payload (phone + BSUID); conflicts are counted and routed to the anchor kind, never merged.

**The example from the brief** - Mohamed on WhatsApp (+201...), Instagram (`ig_user_123`), Messenger (`psid_456`) and email: today that is **three contacts** (WhatsApp contact with a phone identity and a BSUID per WABA; an Instagram contact; a Messenger contact) and no email identity. Nothing duplicates *within* a channel and nothing merges by name (mutant M8 killed); cross-channel linking is the deferred O7 and needs person-level erasure first (OMNI-019 / R21). That is the safe default ADR-118 chose.

Uniqueness semantics match each provider's documented scope: phone per workspace; BSUID per portfolio-user (Meta), stored per WABA (over-splits only); IGSID per professional account (Meta, I3) and PSID per Page (F1), both stored per connection.

Weak points: the compatibility trigger on `contacts.wa_id` writes `source = provider` for any writer, refuses a changed `wa_id` (P7: `UniqueViolation uq_contact_identities_one_whatsapp_phone`) and ignores a NULL - harmless for today's writers, but phone-number change, BSUID rotation and erasure will each need explicit identity writes (OMNI-047); BSUID rotation via the `system` message is not processed (documented). Identity creation and pairing are logged, not written to `audit_logs`; linking (O7) must be audited when it exists. **Readiness: READY** (external identity); **linking: NEEDS REMEDIATION** (deferred by design).

---

## 9. Conversation model

A conversation is one contact on one connection, `UNIQUE(tenant_id, contact_id, account_id)`, pinned to `participant_identity_id` with the key `(tenant_id, contact_id, participant_identity_id, channel) -> contact_identities` - the participant is the conversation's own contact's identity, on its channel (C4). The brief's collision cases:

| Case | Result | Evidence |
| --- | --- | --- |
| same customer on WhatsApp and Instagram | two contacts, two conversations; never one thread | model; ADR-119 |
| same external id on two connected accounts (connection-scoped ids) | two identities (scope `connection`), two contacts, two conversations | P4b; with the *same* provider message id on both, the second delivery is discarded first (P4, OMNI-043) |
| same Instagram user messaging two tenants | two contacts, no shared row | P4 (`contacts_b = 1`, cross-tenant reads refused) |
| same WhatsApp customer messaging two business numbers | one contact (phone is workspace-scoped), two conversations | identity suite |

State (`status`, `mode`, assignment, priority, `last_inbound_at`, `last_message_at`) is channel-neutral; the WhatsApp 24-hour window is *computed* by WhatsApp's policy from `last_inbound_at`, not stored as a generic invariant. Two defects in that state: `touch_inbound` assigns `last_inbound_at`/`last_message_at` unconditionally, so a late delivery moves them backwards (OMNI-036, P10); and conversation order is the insertion order `sequence`, so imported or late history sorts after newer messages (OMNI-038, P10). **Readiness: NEEDS REMEDIATION** (OMNI-036 is a one-line monotonic fix; OMNI-038 is Coexistence-only).

---

## 10. Message model

| Field | Belongs to | State |
| --- | --- | --- |
| `wa_message_id` (255) | provider message identity; API also exposes it as `provider_message_id` | neutral in use, WhatsApp-named until O8 |
| `connection_id` | the conversation's connection, set by the sequence trigger, keyed `(tenant_id, conversation_id, connection_id) -> conversations (tenant_id, id, account_id)` | neutral |
| `kind` | `message_kind`: text, image, document, audio, video, location, interactive, template, unsupported | WhatsApp's vocabulary, adequate for IG/Messenger content; no reaction, edit, unsend, postback (OMNI-053) |
| `status` / `delivery_state` | neutral lifecycle (ADR-093) | neutral; failure text is WhatsApp's (OMNI-044) |
| `template_name`/`template_language` | WhatsApp templates | provider-specific, nullable, harmless |
| `origin` | customer, human, agent, campaign, follow_up, system | neutral; `BUSINESS_APP` anticipated for Coexistence, not added |
| `message_media` | ordered `(message_id, position)`, neutral `locator_kind`/`locator`/`locator_expires_at`, `wa_media_id` kept | neutral |

Provider identity is unique per connection (`uq_messages_tenant_id_connection_id_wa_message_id`) **and still per workspace** (`uq_messages_tenant_id_wa_message_id`, C5); the stricter legacy key is what actually decides today (OMNI-043). Replies (`context.id`, `reply_to`) are carried on the event and not persisted; internal relationships use Wasla message ids everywhere they exist. **Readiness: READY** (message model); **provider message identity: NEEDS REMEDIATION** (OMNI-029, OMNI-043).

---

## 11. Inbound webhook architecture

The edge is provider-specific by design and the core is not. `POST /api/v1/webhooks/whatsapp` verifies Meta's HMAC over the raw bytes, strips NULs, and hands the decoded payload to `WhatsAppIngestionService`, a 68-line facade: `adapter.parse(payload)` then `ChannelIngestionService.ingest(delivery)`. **Normalisation happens exactly once, in `WhatsAppAdapter.parse` / `parse_webhook`**; no WhatsApp DTO crosses into shared code (`channel_ingestion_service.py` imports only `app.channels.*` types). The parser requires `object == whatsapp_business_account` and `field == messages`, refuses everything else by a closed `RefusalReason`, and every refusal is counted (`wasla_inbound_entries_refused_total{channel, reason}`) - nothing parses to silence (the prior stage's P5/P6 hold; mutant B4 killed).

**Meta event routing for more products.** Meta lets each product (WhatsApp Business Account, Page, Instagram) have its own callback URL, so the clean shape is *one route per product, one shared verifier* - `app/integrations/meta/signature.py` already serves any Meta route. A monolithic Meta handler is neither needed nor present. Because the signature module takes the secret as a parameter, a product signed with a different secret (OMNI-050, unverified for Instagram Login) costs a setting, not a redesign.

**Security boundary.** Raw unverified payloads never reach domain services: the signature check precedes parsing, a public deployment without a secret answers 503, and `ConnectionDirectory` resolves the workspace from the connection key at the event's instant - never from the sender (mutant M14 killed). What the boundary gets wrong is size: the anonymous-body cap is 1 MiB where Meta documents 3 MB (OMNI-034).

**Inbound normalisation: PARTIAL** - neutral and contract-tested, but the WhatsApp adapter drops the content of button and interactive replies (OMNI-030) and the event/message identity contract is implicit (OMNI-032).

---

## 12. Dedup and idempotency

| Boundary | Key | Scope today | Evidence |
| --- | --- | --- | --- |
| webhook event | `whatsapp_events (tenant_id, account_id, event_id)` **and** legacy `(tenant_id, event_id)` | workspace (legacy decides) | C5; P4 |
| inbound message | screen by `(connection, provider id)`, direction-aware; legacy `(tenant_id, wa_message_id)` | connection, then workspace | `conversation_service.py:93-119` |
| echo / own send | `ECHO` stored, never projected; an id naming Wasla's own send is a collision | connection | echo/dedup suite, P9 |
| status | event id `{wamid}:{status}`; monotonic `advance_status` | the connection's holders | status contract |
| agent turn | `AgentTurn` claim by `trigger_message_id`; backstop refuses non-customer triggers | conversation | mutants B1, B2 killed |
| outbound intent | `idempotency_key` per workspace; delivery state CLAIMED/REQUESTED | workspace | ADR-093, ADR-103 |
| campaign copy | `campaign_recipients (campaign_id, contact_id)` + `message_id` link | campaign | campaign suites |
| follow-up | one pending per conversation, claim token | conversation | CRM suites |

**The brief's cases.** Same provider id replayed -> duplicate, nothing re-projected (pass). Same id on two channels -> distinct (the key includes the connection, which carries the channel). Same id on two connections in **one workspace** -> the second delivery is **not stored at all**: the legacy event key refuses it, `ChannelEventRepository.store` returns no row (`channel_event_repository.py:96-127`) and the delivery is only counted as a collision (P4: `stored 0, collisions 1`, payload discarded) - harmless for Meta, whose ids are globally unique, and message loss for any channel whose ids are unique only per connection (OMNI-043). Same id in two workspaces -> both stored (P4). Concurrent delivery -> one row (race suites green in the model lane).

A hidden contract sits under recovery: `InboundRecoveryWorker._recover` (`inbound_recovery.py:224-225`) and the stranded-media sweep (`media_repository.py:502-507`) look the projected message up by `provider_message_id = event.event_id`, so they work only while an adapter's event id equals its message id - which the `InboundEvent` type does not require and the contract suite does not check (`test_channel_adapter_contract.py:184` asserts only that both exist). P3: with split ids, recovery abandons the event as `projection_missing` and the customer is never answered (OMNI-032).

**Dedup: PARTIAL.**

---

## 13. Outbound routing

Routing is pinned and checked twice. `_dispatch` reads the conversation, takes **its** channel's adapter (no adapter -> `ChannelUnavailableError`, never WhatsApp's), checks the body in the channel's unit and the media family against its capabilities, asks the policy whether this origin may send this kind now, re-reads the connection (`populate_existing`, so a number disabled a moment ago reads disabled), reads the pinned participant, and lets the adapter address it - an identity of another channel or an unaddressable kind is refused before anything is staged (`IdentityNotAddressableError`). The database repeats it: the conversation's channel equals its connection's, the participant is its own contact's identity on that channel (C4).

There is **no default channel** and no `customer.phone -> WhatsApp` fallback anywhere: nothing on the send path reads `contacts.wa_id` (its only readers are the compatibility trigger, the identity service and the deprecated opt-out field). Campaigns send only through their own connection and refuse a conversation whose pin no longer matches (M10 killed). **Wrong-channel-send prevention: PASS.**

What the seam cannot carry is *how* to send: `ChannelSender.send(recipient, content)` (`app/channels/adapter.py:110`) receives no origin and no policy decision, so a Messenger or Instagram adapter cannot know that a person (not the AI) is replying on day three and must send `messaging_type: MESSAGE_TAG, tag: HUMAN_AGENT` (OMNI-033). Outbound media assumes an upload step; Instagram sends video, audio and files by URL only (OMNI-040). **Outbound adapter boundary: PARTIAL.**

---

## 14. Message / status normalization

Statuses are a neutral, monotonic lifecycle - `RECEIVED/PENDING < SENT < FAILED < DELIVERED < READ` - with each timestamp written once and `failed` unable to undo a delivery (`conversation_repository.py:794-850`). Provider words are mapped at the adapter (`DELIVERY_STATUSES`); unmapped ones are stored as evidence and never projected (Meta's new `played` would be one); a watermark primitive advances only this conversation's outbound messages on this connection sent at or before the watermark. Statuses that skip states (Instagram reports reads only) are handled: the order is a rank, not a sequence.

Defects: resolving which message a status names is a full scan (OMNI-029); the watermark compares Meta's send-time watermark with Wasla's post-response `sent_at`, so the newest message is missed (OMNI-042; P5: watermark 1 s before local `sent_at` advances 0, after it advances 1); the failure text recorded for every channel is "WhatsApp reported this message as failed." (OMNI-044). **Status normalization: PARTIAL.**

---

## 15. Channel capabilities

`ChannelCapabilities` (`app/channels/policy.py:68`) declares text limit **and unit**, reply budget, attachments per message, media families, receipt model, echoes, reply-to, reactions, unsend, templates, out-of-window mechanism and id scope; `require_sendable_text` and `prepare_channel_reply` bound in the channel's unit without splitting a character (Arabic proven against a 1,000-byte policy). Shared code asks; it does not assume WhatsApp - with these exceptions: outbound media limits are Wasla's global ones, not the provider's per-type MIME and size limits (OMNI-045); follow-up scheduling and the AI follow-up tool validate against WhatsApp's 4,096 characters and template registry (OMNI-039, P6); capability flags for reactions, unsend and edits exist but no event can carry those facts (OMNI-053); `OutOfWindow` has `TEMPLATE` and `NOTHING` but no `TAG` (OMNI-033). Typing indicators and presence are not modelled; nothing shared depends on them, so their absence on a channel breaks no flow. **Channel capability handling: PARTIAL.**

---

## 16. WhatsApp policy isolation

The 24-hour window, the template escape, 4,096 characters, the reply budget and "You are replying over WhatsApp" now live only in `app/integrations/whatsapp/policy.py`; shared services ask `ChannelRegistry.policy_for(conversation.channel)`. The agent prompt's channel sentence comes from the conversation's policy (`orchestrator.py:230-241`), follow-up decisions from `policy.follow_up` (`follow_up_service.py:707`). Leaks that remain are constants, not decisions: `MessagingService` re-exports `SERVICE_WINDOW`/`WHATSAPP_TEXT_MAX_CHARS`; the API request schema caps text at 4,096 and captions at 1,024; `prepare_channel_reply` defaults to WhatsApp's capabilities (OMNI-044). One WhatsApp rule rests on shared state that is wrong in one case: the window's anchor `last_inbound_at` can move backwards (OMNI-036). **Classification: CHANNEL-ADAPTED (correct).**

---

## 17. Templates

Templates are WhatsApp's alone and correctly so: the registry (`whatsapp_templates`, FK to `whatsapp_accounts`), sync through the Graph API (`template_service.py`), campaign composition and the withdrawal write-back. Shared concepts are distinct: a follow-up carries free text *and/or* a template; `SendKind.TEMPLATE` is refused by any policy whose capabilities say `templates=False`; Instagram and Messenger have no templates and are never forced into them. The one WhatsApp lookup in shared code is `send_template` consulting `WhatsAppTemplateRepository` before `_dispatch` (harmless on another channel, where the policy refuses the send anyway). Saved replies do not exist. **Classification: PROVIDER-SPECIFIC BY DESIGN.**

---

## 18. AI agent readiness

The AI layer works on neutral objects: `AgentJob` carries tenant, conversation and trigger ids; memory is built from `messages` by `sequence`; the channel's instructions and reply budget come from the conversation's policy; `refusal_now` checks the connection's status through `channel_connections`; the reply is bounded in the channel's unit before `send_text`. The AI never sees `phone_number_id`, a wamid or a WABA. Memory is per conversation, so identities linked in future cannot pull another connection's or workspace's messages into context.

Gaps: (a) what a customer *tapped* reaches the model as `[interactive]` (OMNI-030); (b) Messenger and Instagram require automated experiences to disclose they are automated at the start, after long gaps and on human-to-AI transitions - a stateful obligation the static `agent_instructions()` cannot express (OMNI-041); (c) a reply sent from the provider's own app is invisible to the model and does not stop it (OMNI-037, P9); (d) memory order is insertion order (OMNI-038). **Readiness: READY** (architecture), with OMNI-041 a policy decision before AI goes live on Messenger or Instagram.

---

## 19. AI tools readiness

Tools receive a server-built `ToolContext(tenant_id, conversation_id, session)` and never a phone or a channel identifier; none sends a message, so none can choose a channel. Lead tools tie leads to the conversation's contact; knowledge search is tenant-scoped; handoff is neutral. The one tool with channel consequences, `schedule_follow_up`, is not capability-aware: its description advises "1440 for tomorrow, 10080 for next week" and its body bound is WhatsApp's 4,096 characters, so on a channel with no out-of-window mechanism a next-week nudge is guaranteed to be skipped, and on a byte-limited channel an accepted body is refused only when it falls due - and then retried as if the refusal were transient (P6: 720 Arabic characters / 1,320 bytes accepted at scheduling; dispatch refused "A Synthetic message may be at most 1000 bytes."; still `pending` after attempt 1) (OMNI-039). No tool could send a WhatsApp template into an Instagram conversation: tools take no template, and the policy refuses one. **Readiness: NEEDS REMEDIATION** (OMNI-039).

---

## 20. Human handoff / team

Mode, assignment, ownership, priority, close and reopen are conversation columns judged against the committed row (ADR-111) and know nothing about channels; the inbox lists every channel and filters by `channel` and `connection_id`, tenant-scoped (M7 killed). Two cross-channel concerns: a colleague replying from Instagram's or Messenger's own app (or the WhatsApp Business app under Coexistence) produces only an echo, so Wasla neither records the reply nor hands the conversation over (OMNI-037); and WhatsApp's "prompt, clear, and direct escalation paths" requirement is met by the handoff tool and human mode (M25). **Readiness: READY.**

---

## 21. CRM / leads / follow-ups

Leads reference contact and conversation (tenant-agreed, CRM-14); `LeadSource.WHATSAPP` exists but nothing writes it (agent-created leads are `agent`), so a lead's channel is derivable from its conversation rather than stored. A lead's phone is a typed CRM field and links nothing (ADR-118). Lead search matches name, email, phone and interest; there is no contact or conversation search, so "find the customer by Instagram handle" has no surface yet. Follow-ups are per conversation, so they know the channel, connection and identity implicitly; a customer writing on Instagram does not cancel a WhatsApp nudge (separate threads, by design). Dispatch obeys the conversation's policy; scheduling does not (OMNI-039). **CRM: READY. Follow-ups: NEEDS REMEDIATION (OMNI-039).**

---

## 22. Campaigns

Campaigns are WhatsApp campaigns: `campaigns.account_id -> whatsapp_accounts`, `template_id -> whatsapp_templates` (C3), approved templates only, audience from conversations on the campaign's number, recipients pinned to the identity of their conversation (ADR-119.4), one copy per person per campaign (`UNIQUE(campaign_id, contact_id)`), opt-out re-read at send time. Nothing generalises them and nothing should yet: Instagram allows no business-initiated messaging at all ("Only after an Instagram user has sent ... a message"), and Messenger's out-of-window promotion is Sponsored Messages, a different product. A multi-channel campaign is a product decision; the recipient -> identity model would carry it. Consent defects that affect campaigns today: button opt-outs are lost (OMNI-030) and `user_preferences`/131050 are not recorded (OMNI-046). **Readiness: READY (WhatsApp-only by design).**

---

## 23. Media

Inbound: every attachment is a `message_media` row at its provider position with a neutral locator (`handle` or `url`, <=4,096, optional expiry), keyed to its message, conversation and workspace together (OMNI-022 closed, C4). Fetching is the adapter's (`media_fetcher`): WhatsApp's two-step handle fetch, or the shared `UrlMediaFetcher` with SSRF validation on every hop, a provider host allow-list, the token only to allowed hosts, expiry refused before any request and the byte cap enforced mid-stream. Hashing, sniffing, caps, AI reading, storage, retention and purge are shared. Lookups are tenant-scoped (P4: another workspace's media list is empty).

Outbound is where channels differ and the seam does not yet: Wasla uploads bytes and never sends by link (a deliberate privacy choice), but Instagram accepts video, audio and files **only by URL** and `MediaStorage` (`app/core/storage.py:69-98`: `put_at`, `get`, `delete`, `exists`) cannot issue one (OMNI-040); provider per-type limits are not capabilities (OMNI-045). An Instagram adapter's host roots also need Instagram's CDN family beside `fbsbx.com`/`fbcdn.net` - configuration, not code. **Readiness: NEEDS REMEDIATION** (OMNI-040 for Instagram media).

---

## 24. Workers / queues

| Job | Payload | Class |
| --- | --- | --- |
| `AgentJob` | tenant, conversation, trigger message, agent | generic |
| `MediaJob` | tenant, media | generic (fetch through the adapter) |
| `InboundRecoveryWorker` | event rows of every channel | generic (OMNI-032) |
| `InboundEventSweep`, `WebhookPayloadRetention` | every channel's events | generic |
| follow-up / campaign workers | row ids; sends through `_dispatch` | generic (campaigns WhatsApp-only by data) |
| template sync | WABA | provider-specific by design |

Queue payloads are internal UUIDs only. Retry classification is by neutral outcome types (`SendNotAttemptedError`, `UncertainDeliveryError`, `ProviderAuthError`, `RateLimitedError`, `ConnectionThrottledError`), never by provider text - but the WhatsApp adapter maps Meta's errors onto those types by HTTP status rather than by Meta's documented codes, so throttling and connection-level refusals become per-message declines (OMNI-035). The agent queue is one global FIFO (`agent:jobs`) across tenants and channels, so a burst on one channel delays every other channel's turns (OMNI-051, the pre-existing WQ-09). Per-connection rate limiting exists as a shared allowance on the connection row (ADR-123), one number for all connections (OMNI-052). The hottest worker-side query - status resolution - is a full scan (OMNI-029). **Readiness: NEEDS REMEDIATION** (OMNI-029, OMNI-032).

---

## 25. Notifications

There is no in-app notification, push, websocket or server-sent-event subsystem; the only outbound notifications are account and billing emails, and inbox clients poll the API. Nothing channel-coupled exists to fix. When a notification system is built, its events should be keyed by conversation and connection (both exist) rather than by provider identifiers. **Readiness: READY (nothing to couple).**

---

## 26. Analytics

`analytics_events` records handoffs per conversation; tenant analytics (`TenantMetricsRepository`) count conversations, messages, leads, sentiment and campaigns over windows - across all channels, with no channel or connection breakdown, although both are one join away (`conversations.channel`, `messages.connection_id`). Platform analytics add `connections_by_channel` counts. Metrics label inbound outcomes and refusals by `channel` (closed enum), never by connection or customer (ADR-072). The channel-filtered inbox has no index of its own (E5: bitmap scan of the workspace's conversations, then a filter on channel). **Readiness: NEEDS REMEDIATION** (Low, OMNI-048).

---

## 27. Usage / quotas / billing implications

Messages are metered as `whatsapp_message_received` and `whatsapp_message_sent` (`app/channels/metering.py`), and `period_messages` sums exactly those two (`entitlement_service.py:87-88`, `invoice_service.py:74-75`); `LimitKey.WHATSAPP_NUMBERS` counts WhatsApp numbers only (`plan_admin.py:178`). Whether Instagram or Messenger messages count against the same allowance, their own, or none is undecided (ADR-122), and **the code refuses to guess**: `ChannelRegistry` raises if an adapter is registered for a channel with no meter (`registry.py:50-51`). Billing logic reads normalised usage events, not message tables; annual billing and `PlanPrice` are unaffected. **Readiness: NEEDS REMEDIATION - the ADR-122 product decision gates registration.**

---

## 28. Authorization / tenancy

Every repository on the path is a `TenantScopedRepository` except the deliberate, connection-keyed directories (`ConnectionDirectory`, `OutboundMessageDirectory`, the health census, the sweeps), none reachable from a route. Composite tenant-agreed keys cover conversation->contact, conversation->connection (with channel), conversation->participant identity, message->conversation (with connection), media->message/conversation, event->connection (with channel), identity->contact/connection and campaign recipient->identity (C4). The inbox filters cannot reach another workspace's connection (a foreign id matches nothing). Cross-tenant probes (P4): the same external id in two workspaces gives two contacts; another workspace's identity raises `TenantIsolationError`; its message by provider id resolves to nothing; its media list is empty; a conversation lookup on the wrong connection finds nothing. The tenant-isolation, route-authorization and platform suites passed (184 tests in the model lane). **Authorization/tenancy: PASS.**

---

## 29. Security

Signature verification is shared and constant-time; the verify-token handshake is constant-time and never echoes on failure; a public deployment without a secret fails closed (503). Credentials: envelope `v2` binds tenant, connection and channel (a ciphertext moved to another connection does not decrypt); `v1` values still read until re-sealed; the platform-token fallback is WhatsApp's alone. URL media goes through the SSRF guard and provider host roots on every hop. Identifiers are bounded (connection 128, identity 255, locator 4,096). Raw payloads are redacted 30 days after processing for every channel (DB-011). Nothing on the new paths logs a phone, BSUID or message text. The availability defect at the edge - Meta-sized deliveries refused 413 - is OMNI-034. **Security: PASS.**

---

## 30. API contracts

| Field / endpoint | Class | Note |
| --- | --- | --- |
| `ConversationRead.account_id` | harmless presentation | duplicated as `connection_id` |
| `ConversationRead.connection_id`, `channel`, `participant {id, channel, kind}`, `reply_policy` | additive channel fields | identifier value deliberately absent |
| `ConversationRead.service_window_open` | WhatsApp meaning kept | `reply_policy` is the neutral rule |
| `MessageRead.wa_message_id` | deprecated, kept | `provider_message_id` carries the value |
| `ContactOptOutRead.wa_id` | deprecated, kept | `identities[]` lists every address |
| `SendTextRequest.body` <= 4,096, caption 1,024, upload 16 MB | WhatsApp limits used as global ceilings | the service applies each channel's own limit (OMNI-044) |
| `/whatsapp` (connect, list, disable, enable, reverify, release) | must remain WhatsApp-specific | no neutral `/connections` listing yet |
| `GET /conversations` | **future breaking problem** | 422 for the whole page if one conversation's channel has no adapter (OMNI-031) |
| contacts / identities read API | missing | opt-out endpoints only |

**API compatibility: PARTIAL** (additive and documented, except OMNI-031).

---

## 31. Database coupling

Every WhatsApp-named schema object on a migration-built database (census C1, C2, C3, C7, C8):

| Object | Table | Classification | Note |
| --- | --- | --- | --- |
| `phone_number_id`, `waba_id`, `display_phone_number`, `verified_name`, `access_token_encrypted`, `whatsapp_account_status` | `whatsapp_accounts` | provider table - correct | extension of `channel_connections`, same id |
| whole table | `whatsapp_templates` (FK -> `whatsapp_accounts`) | provider table - correct | |
| table name, `whatsapp_event_kind`/`_state` type names | `whatsapp_events` | shared table, historical name - harmless | ORM `ChannelEvent`; rename at leisure (O8) |
| `account_id` | `conversations`, `whatsapp_events`, `campaigns` | shared, neutral meaning, historical name - harmless | API also reports `connection_id` |
| `wa_id VARCHAR(32)` nullable + `uq_contacts_tenant_id_wa_id` + `trg_contacts_phone_identity` | `contacts` | shared table, compatibility column - historical, requires migration (O8) | kept equal to the phone identity; NULL for non-WhatsApp contacts, so it blocks no channel |
| `wa_message_id VARCHAR(255)` + `uq_messages_tenant_id_wa_message_id` | `messages` | shared table, provider identity in neutral use; **the workspace-wide unique is the part that blocks** | OMNI-043; `uq_messages_tenant_id_connection_id_wa_message_id` is the neutral key |
| `wa_media_id` | `message_media` | shared table, compatibility metadata - harmless | neutral `locator` is what fetching reads |
| `uq_whatsapp_events_tenant_id_event_id` | `whatsapp_events` | shared table, legacy key - **blocks per-connection-id channels** | OMNI-043 |
| `fk_campaigns_account_id_whatsapp_accounts`, `fk_campaigns_template_id_whatsapp_templates` | `campaigns` | feature table, WhatsApp-only by design - correct for now | a multi-channel campaign would key `channel_connections` |
| `trg_whatsapp_accounts_channel_connection` | `whatsapp_accounts` | compatibility mirror - removed at O8 | |
| `trg_conversations_default_participant` | `conversations` | compatibility default - harmless | refuses rather than guesses when a contact has 0 or 2+ identities |
| `audit_action` `whatsapp_account_*` (5 labels) | enum | vocabulary - harmless | a new channel adds its own actions |
| `lead_source.whatsapp` | enum | vocabulary - harmless, unused | |
| `topup_entitlement.whatsapp_numbers`, `LimitKey.WHATSAPP_NUMBERS` | enum | commercial, decided as WhatsApp-only (ADR-122) | |
| `usage_event_type.whatsapp_message_received/sent` | enum | metered vocabulary, **must not be renamed** (usage reproducibility) | other channels need decided meters |
| `leads.phone VARCHAR(32)` | `leads` | CRM field, not an identity - harmless | |

Constraint health on the migrated catalog: **0** NOT VALID constraints, **0** invalid indexes, every new key validated (C4, C10, C11).

---

## 32. Index / performance readiness

Measured on 200 workspaces / 10,000 conversations / 200,000 messages built through the real triggers (Appendix B):

| Future query | Index today | Plan | Verdict |
| --- | --- | --- | --- |
| status -> message by provider id across the connection's holders (`wa_message_id = ? AND connection_id IN (...) AND direction = 'outbound'`) | **none usable** (no index leads with `connection_id` or `wa_message_id`) | E1: Parallel Seq Scan, 4,878 buffers, 22.7 ms; E3 (id not ours) the same; E7 seqscan off: full scan of `uq_messages_tenant_id_wa_message_id`, 106 buffers | **Blocker - OMNI-029** |
| the same lookup with the holders' tenant ids (pre-remediation shape) | `uq_messages_tenant_id_wa_message_id` | E2: Index Scan, 4 buffers, 0.07 ms | the fix needs no migration |
| screen / recovery: tenant + connection + provider id | `uq_messages_tenant_id_connection_id_wa_message_id` | E4, E6: Index Scan, 4 buffers | ready |
| connection + external identity | `uq_contact_identities_scoped_value (tenant, channel, kind, scope, scope_ref, value)` | equality on every column | ready |
| a contact's identities | `ix_contact_identities_tenant_id_contact_id` | | ready |
| workspace + connection inbox | `ix_conversations_tenant_id_account_id_last_message_at` | | ready |
| workspace + channel inbox | none | E5: bitmap scan of the workspace's conversations, filter on channel | Low - OMNI-048 |
| connection routing (live / at instant) | `uq_channel_connections_live_external_account`, `ix_channel_connections_external_account_tenure` | | ready |
| event dedup per connection | `uq_whatsapp_events_tenant_id_account_id_event_id` | | ready |
| watermark advance | `ix_messages_conversation_id_created_at` + filter | bounded by one conversation | ready (Messenger may want a partial index, as the remediation noted) |

**Data volume.** The neutral design adds one row per identity (1-2 per WhatsApp contact), one per connection, and no per-message rows: provider identity stays on `messages`, so no inbound message joins through JSON or an extra table. JSON (`whatsapp_events.payload`) is raw evidence only - routing, dedup and identity never read it (Appendix B, C1: every identity column is relational).

---

## 33. Privacy / lifecycle

- **Workspace purge** includes the new tables in a valid order (`contact_identities` before `contacts`, `channel_connections` after `whatsapp_accounts`; `workspace_purge_service.py:139-155`), and the purge-partition test passes.
- **Person-level erasure and export do not exist** (OMNI-019, deferred to R21). One person already holds several identities (a phone and a BSUID per WABA); erasure must cover every identity, every conversation on every connection, their media and their raw events, and must precede any cross-channel linking.
- **Raw payloads** are redacted 30 days after processing for every channel (DB-011); Coexistence `history` would bring 180 days of a business's past messages into that store at once.
- **Unsend** (`is_deleted` on Instagram) has no representation: a message a customer withdrew would stay in the transcript and in AI memory (OMNI-053).
- **Identity unlinking** has nothing to undo yet (no linking). When O7 exists, moving an identity between contacts must also move or re-pin the conversations that reference it - the participant key `(tenant_id, contact_id, participant_identity_id, channel)` will refuse otherwise (*inference*).

---

## 34. Observability

| Question an operator will ask | Answerable today? |
| --- | --- |
| Which channel is failing inbound? | yes - `wasla_inbound_events_total{channel, outcome}`, `wasla_inbound_entries_refused_total{channel, reason}`, `wasla_unprocessed_inbound_events_by_channel` |
| Which connection? | partly - `wasla_channel_connections{channel, status, health}` counts; logs carry `account_id`; metrics deliberately carry no connection id (ADR-072) |
| Which provider is failing outbound / its latency? | WhatsApp only - `wasla_provider_requests_total{provider}` is written by the WhatsApp client; `Provider` has no Instagram/Messenger value and a new adapter must record its own calls |
| Webhook deliveries refused before the route? | **no** - a 413 from the body limit is logged (`request.body_too_large`) and counted nowhere (OMNI-034) |
| Rate limits by connection? | no - and Meta's throttling codes are not recognised as throttling (OMNI-035) |
| Connection credential health? | `AUTH_FAILED` on HTTP 401 / code 190 only (OMNI-035) |

Alerts: WhatsApp-specific ones stay WhatsApp's correctly (`WhatsAppInboundStopped`, `WhatsAppWebhookSignatureFailures`, `WhatsAppSendFailureRate`, `WhatsAppRateLimited`); neutral ones exist (`UnprocessedInboundBacklog`, `InboundEntriesRefused`, `InboundForeignPayloads`, `ChannelConnectionCredentialRefused`). Missing before a second channel: a per-channel "inbound stopped" rule and provider labels for the new adapter's calls. **Observability: NEEDS REMEDIATION.**

---

## 35. WhatsApp Coexistence

Kept as its own track. Meta's current onboarding page (M27) defines what Coexistence requires; against it:

| Requirement (Meta) | Wasla today | Blocker class |
| --- | --- | --- |
| subscribe `history`, `smb_app_state_sync`, `smb_message_echoes` | all refused as `unsupported_field` | parser + projection |
| synchronise 180 days of history within **24 hours** of onboarding, chunked (`phase`, `chunk_order`, `progress`) | no history path; insertion-order `sequence` would place history after live messages (OMNI-038); `touch_inbound` would move windows back (OMNI-036); history must not open windows | **design** |
| Business-app sends arrive as `smb_message_echoes` | echo kind exists, but echoes are never projected (OMNI-037), so the business app's replies would not appear and the AI would talk over them | **design** |
| Business-app messages "do not create, extend, or affect Cloud API conversation windows" | consistent with echoes never touching the window | ready |
| contacts sync (`state_sync` with name and phone) | no path; would create contacts from a non-signed-message source (an identity source ADR-118 does not have) | product + design |
| fixed **20 mps** throughput | allowance is one deployment-wide number (OMNI-052) | config |
| large payloads | 1 MiB body cap (OMNI-034) | config + alerting |
| onboarding through Embedded Signup's Business-app flow | connect flow is ownership-proof only | adapter |
| `MessageOrigin.BUSINESS_APP` | anticipated in the enum's docstring, not added | schema (enum label) |

Prerequisites the foundation already supplies: BSUID identity, the echo kind, field discrimination, neutral event path, per-connection identity. **WhatsApp Coexistence readiness: BLOCKED** - by design gaps (history ordering, echo projection) that Instagram and Messenger do not need, plus OMNI-034/036/052.

---

## 36. Instagram Direct walkthrough

| Step | Hypothetical Instagram DM | State |
| --- | --- | --- |
| Meta webhook | own route (e.g. `/webhooks/instagram`), shared `verify_signature`; payload `object: instagram` (I1) | requires adapter (route + parser); signing secret to confirm (OMNI-050) |
| account / connection | `entry[].id` = professional account -> `channel_connections(channel=instagram, external_account_id)`, created by an Instagram connect flow | requires adapter (connect flow; registry needs a decided meter - ADR-122) |
| external user identity | `sender.id` IGSID -> `contact_identities(kind=igsid, scope=connection)` | works unchanged (synthetic adapter proves it) |
| customer | new contact per IGSID per connection; never linked to the WhatsApp contact | works unchanged (ADR-118) |
| conversation | one per contact x connection, pinned to the IGSID | works unchanged |
| message | `mid` as provider id; text, URL attachments (`url` locators, `UrlMediaFetcher`); `is_echo` -> `ECHO`; `is_deleted`, edits, reactions, postbacks unrepresented (OMNI-053) | requires adapter; echoes of native-app replies need a decision (OMNI-037) |
| AI / human | agent instructions and 1,000-**byte** reply bound from the IG policy; disclosure obligation (OMNI-041) | works unchanged for bounding; **blocked by OMNI-041** for AI replies |
| reply | `graph.instagram.com/<IG_ID>/messages`, `recipient {id: IGSID}`, text; images by URL or `attachment_id`; video/audio/file by URL only | requires adapter; media needs OMNI-040; human replies after 24 h need OMNI-033 |
| status | `read {mid}` per message -> `advance_status(READ)` (no delivered receipts; rank order handles the skip) | works unchanged |
| inbox | listed with WhatsApp; filters by channel/connection | works unchanged - **except OMNI-031** when the adapter is disabled |
| status lookup cost | same full scan as WhatsApp | **blocked by OMNI-029** |

**Instagram Direct readiness: NEEDS REMEDIATION.**

---

## 37. Messenger walkthrough

| Step | Hypothetical Messenger message | State |
| --- | --- | --- |
| Page event | own route, `object: page`, same Meta signature | requires adapter |
| connection | `recipient.id` = Page id -> `channel_connections(channel=messenger)`; Page access token (credential `v2` envelope) | requires adapter + connect flow |
| PSID identity | page-scoped (F1) -> `identity_kind=psid, scope=connection` | works unchanged |
| conversation / message | as Instagram; `mid`; `quick_reply.payload` beside text; `reply_to.mid`; attachments by URL | requires adapter (a payload field has nowhere neutral to go: OMNI-030's `action`) |
| AI / human | 2,000 characters (characters, not bytes); disclosure obligation | policy work; **blocked by OMNI-041** for AI |
| reply | `messaging_type` required on every send: `RESPONSE` within 24 h; `MESSAGE_TAG` + `HUMAN_AGENT` for a person within 7 days | `RESPONSE` can be hard-coded inside the window; the 7-day human reply needs OMNI-033 |
| status | `message_deliveries` / `message_reads` **watermarks** (F2) | primitive exists; compares the wrong clock (OMNI-042) |
| statuses at scale, rollback | same as Instagram | OMNI-029, OMNI-031 |

PSIDs and IGSIDs have different semantics (Page-scoped vs professional-account-scoped) but the same storage shape; Wasla does not assume they are interchangeable. **Messenger readiness: NEEDS REMEDIATION.**

---

## 38. Multi-connection scenarios

One workspace with two WhatsApp numbers, one Instagram account and two Facebook Pages:

| Concern | Behaviour |
| --- | --- |
| inbox | one list, newest first, filterable by channel and by connection (connection filter indexed; channel filter not - OMNI-048) |
| routing | each event resolves its own connection by `(channel, external_account_id)` at the event's instant; replies leave through the conversation's connection |
| conversation uniqueness | one per contact x connection: up to 5 threads for one person |
| campaigns | per WhatsApp number only |
| analytics | totals only; no per-channel or per-connection breakdown |
| assignments | channel-agnostic; one owner per thread |
| billing / usage | WhatsApp messages metered; the other three connections cannot be registered until meters are decided (ADR-122); `WHATSAPP_NUMBERS` limits only the two numbers |
| throughput | each connection has its own allowance row, but every connection gets the same configured number (OMNI-052) |
| one channel disabled | its conversations break the unified inbox page (OMNI-031) |

---

## 39. Cross-channel identity scenarios

**Customer X** writes to WhatsApp number A and number B (same phone), to Instagram and to Messenger:

| | Today | Target for the first second channel |
| --- | --- | --- |
| contacts | **3** - one WhatsApp contact (phone is workspace-scoped, so A and B share it), one Instagram, one Messenger | the same; linking is O7 and needs person-level erasure first |
| identities | **4-5** - phone, BSUID (one per WABA: 1 or 2), IGSID, PSID | the same |
| conversations | **4** - A, B, Instagram, Messenger | the same: separate threads, one inbox (ADR-119) |

Nothing is merged by name, email, lead phone or display name (mutant M8 killed in the prior stage; code unchanged). **Same Instagram user U messaging workspaces A and B**: two contacts, two identities, two conversations, no shared row; another workspace's identity is unreadable (P4). The same holds for PSIDs (Page-scoped, so never shared across Pages either) and for WhatsApp phones (workspace-scoped).

**Cross-channel reply safety.** An Instagram inbound cannot be answered over WhatsApp: the reply follows the conversation's pinned connection and participant, the participant key forces the participant's channel to equal the conversation's, and `adapter.address` refuses another channel's identity (M3, M6 killed; Appendix D P4). **What must stay pinned on each outbound message**: its conversation (hence connection and participant) - and it is.

---

## 40. Findings

| ID | Severity | Subsystem | Finding | Second-channel blocker? |
| --- | --- | --- | --- | --- |
| OMNI-029 | **High** | Status lifecycle / DB | Delivery-status resolution scans all of `messages` (no index serves the remediation's new predicate) | **Yes** |
| OMNI-030 | **High** | Inbound normalisation (WhatsApp, present-day) | Quick-reply `button` and `interactive` replies lose their content; the "Stop promotions" opt-out button is not honoured | No - fix first |
| OMNI-031 | **High** | API / rollout safety | The unified inbox answers 422 for the whole page when any conversation's channel has no registered adapter | **Yes** |
| OMNI-032 | Medium | Workers / adapter contract | Recovery and the stranded-media sweep assume `event_id == message_id`; the contract does not say so | Should |
| OMNI-033 | Medium | Outbound seam / policy | The policy's decision cannot reach the adapter: no origin, `messaging_type` or `HUMAN_AGENT` | Should (Messenger, Instagram) |
| OMNI-034 | Medium | Inbound webhook (present-day) | Body cap 1 MiB versus Meta's documented 3 MB; refusals are invisible | Should; Coexistence: yes |
| OMNI-035 | Medium | Outcome taxonomy (present-day) | Meta errors classified by HTTP status, not Meta's documented codes | Should |
| OMNI-036 | Medium | Conversation state (present-day) | An out-of-order delivery moves `last_inbound_at`/`last_message_at` backwards | Should; Coexistence: yes |
| OMNI-037 | Medium | Echoes / handoff | Echoes of replies sent outside Wasla (native app, Business app) are never projected | Should (product decision); Coexistence: yes |
| OMNI-038 | Medium | Conversation order / AI memory | Order is insertion order, so imported or late history sorts after newer messages | Coexistence: yes |
| OMNI-039 | Medium | Follow-ups / AI tools | Scheduling validates against WhatsApp; a deterministic channel refusal is retried as transient | Should |
| OMNI-040 | Medium | Media (outbound) | Instagram sends video/audio/files by URL only; Wasla can only upload | Should (Instagram media) |
| OMNI-041 | Medium | AI / channel policy (compliance) | Messenger/Instagram automation-disclosure obligation is not modelled | **Yes** (for AI replies on those channels) |
| OMNI-042 | Medium | Status lifecycle (Messenger) | Watermarks compared with Wasla's post-response clock - *architectural risk / inference* | Should (Messenger) |
| OMNI-043 | Medium | Dedup / DB | Legacy workspace-wide uniques still decide; a colliding second-connection event is discarded | Should (O8); per-connection-id channels: yes |
| OMNI-044 | Low | Shared code | WhatsApp strings, limits and log names in shared code | No |
| OMNI-045 | Low | Media capabilities | Provider per-type MIME/size limits are not capabilities | No |
| OMNI-046 | Low | Consent (present-day) | `user_preferences` and 131050 never reach the contact's opt-out | No |
| OMNI-047 | Low | Identity compatibility trigger | `source=provider` for every writer; `wa_id` immutable; NULL leaves a stale identity | No |
| OMNI-048 | Low | Analytics / inbox | No channel/connection breakdown in analytics; no channel inbox index | No |
| OMNI-049 | Low | Dead code | `ContactRepository.upsert`/`get_by_wa_id`, `OutboundMessageDirectory.find_by_wa_message_id` unused | No |
| OMNI-050 | Low | Meta webhook operations | 36-hour retry horizon for Instagram/Messenger; Instagram-Login signing secret unverified; one `META_APP_SECRET` | No |
| OMNI-051 | Low | Workers | One global agent FIFO across tenants and channels | No |
| OMNI-052 | Low | Throughput | One allowance value for every connection (Coexistence is fixed at 20 mps) | No; Coexistence: should |
| OMNI-053 | Low | Message vocabulary | Reactions, edits, unsend and postbacks have no neutral representation | No |
| OMNI-054 | Low | BSUID limits | Authentication templates to a BSUID not refused early; BSUID rotation (`system` message) unprocessed | No |
| OMNI-055 | Low | Operations | Default Graph API `v21.0` is available until 2027-01-21 | No |
| OMNI-056 | Info | Architecture | Verified channel-neutral foundation | - |
| OMNI-057 | Info | Provider conformance | 24 Meta contracts verified as implemented | - |
| OMNI-058 | Info | Scope | Campaigns/templates WhatsApp-only by design; no notification subsystem to couple | - |

### OMNI-029 - Delivery-status resolution is a full scan of `messages` (High)

- **Subsystem:** status lifecycle, workers, database.
- **Current behaviour:** `ChannelIngestionService._status_owner` (`channel_ingestion_service.py:622-643`) resolves every mapped status through `OutboundMessageDirectory.find_by_provider_message_id` (`conversation_repository.py:102-124`): `WHERE wa_message_id = ? AND connection_id IN (holders) AND direction = 'outbound' LIMIT 1` - no tenant predicate. No index leads with `connection_id` or `wa_message_id` (C5, C6). Introduced by `5c66a66`; the base used `find_by_wa_message_id(tenant_ids=holders)` (`354db53:app/services/whatsapp_service.py:468-470`), which the `(tenant_id, wa_message_id)` index serves. That method is now dead.
- **Evidence:** EXPLAIN ANALYZE on 200,000 seeded messages (Appendix B): E1 **Parallel Seq Scan**, 4,878 buffers, 22.7 ms; E3 (an id Wasla never sent) the same; E7 with `enable_seqscan = off`: full scan of `uq_messages_tenant_id_wa_message_id` with the id as a non-leading condition, 106 buffers; E2 (tenant-scoped form) **Index Scan, 4 buffers, 0.07 ms**.
- **Why it matters:** every channel's statuses take this path (it is the neutral path), WhatsApp reports `sent`, `delivered` and `read` separately, so roughly three full scans per outbound message, and the cost grows with the whole platform's message count, not the workspace's.
- **Failure scenario:** a campaign of 10,000 messages produces ~30,000 status webhooks; at tens of millions of messages each is a scan of gigabytes; webhook latency climbs, Meta retries, the database saturates for every workspace.
- **Recommended remediation:** pass the holders' tenant ids as well (`tenant_id IN (...) AND connection_id IN (...) AND wa_message_id = ?`) so `uq_messages_tenant_id_connection_id_wa_message_id` is an index seek - no migration; or add `CREATE INDEX CONCURRENTLY ... ON messages (connection_id, wa_message_id) WHERE direction = 'outbound'`. Delete the dead `find_by_wa_message_id`.
- **Schema impact:** none for the predicate fix; one concurrent partial index for the alternative. **API impact:** none. **Migration impact:** none / online index.
- **Testing required:** an EXPLAIN-based regression test on a seeded table that fails on `Seq Scan` for the status lookup; the existing handover (MSG-04) suites must still pass.
- **Second-channel blocker:** **Yes** - and a pre-production blocker for `3473068` at scale.

### OMNI-030 - Button and interactive replies lose their content; the opt-out button is not honoured (High, present-day WhatsApp)

- **Subsystem:** inbound normalisation (WhatsApp adapter), consent, AI.
- **Current behaviour:** Meta delivers a template quick-reply tap as `"type": "button", "button": {"payload", "text"}` and a reply-button or list choice as `"type": "interactive"` with `interactive.button_reply|list_reply {id, title}` (M12, M13). `MESSAGE_KINDS` maps both to `INTERACTIVE` (`adapter.py:92-93`) and `_message_text` returns text only for `text` and media captions (`payload.py:185-199`), so the event has no text. `build_window` renders such a message as `[interactive]` (`agents/memory.py:209`), an agent turn is still queued (only `UNSUPPORTED` is skipped), and `is_stop_request(None)` is false.
- **Evidence:** P1 (Appendix D) through `WhatsAppIngestionService` on PostgreSQL: `button` "Stop promotions" -> `body null, model_sees "[interactive]", opted_out false, queued_agent_jobs 1`; `button_reply` "Yes, book it" and `list_reply` "Pro plan" -> the same; control `text` "Stop promotions" -> `opted_out true`. `app/services/opt_out.py:45-47` names "stop promotions" as "the wording WhatsApp's own opt-out button sends". Meta's WhatsApp Business Policy: "You must respect all requests (either on or off WhatsApp) by a person to block, discontinue, or otherwise opt out of communications from you via WhatsApp." No test feeds a `button`- or `interactive`-typed inbound message (repository grep).
- **Why it matters:** present-day consent defect on templates Wasla does send; the AI pays for an inference to answer content it cannot see; and the neutral `InboundEvent` has no structured slot for a reply action, which Messenger quick replies (`quick_reply.payload`) and Instagram/Messenger postbacks (`postback {title, payload}`) need as well.
- **Failure scenario:** a customer taps "Stop promotions" under a marketing template; Wasla records nothing; the next campaign reaches them; complaints lower the number's quality rating. Separately, a customer taps "Yes, book it" and the agent replies "Could you clarify?".
- **Recommended remediation:** in the WhatsApp adapter, carry `button.text` and the interactive `title` as the event's text and add a neutral `action` (`payload`/`id`, `title`) to `InboundEvent` and the stored message (so routing never depends on display text, as `docs/WHATSAPP.md` already argues); honour a quick reply whose text is a stop phrase (or whose payload the workspace's template marks as the marketing opt-out); include the context message id.
- **Schema impact:** optional `messages.action_payload` (or JSONB `action`) - additive. **API impact:** additive field on `MessageRead`. **Migration impact:** nullable column; no backfill (raw events exist for 30 days for a replay if wanted).
- **Testing required:** fixtures in Meta's documented `button`, `button_reply`, `list_reply` shapes through ingestion; opt-out from a button; AI window renders the title; an adapter-contract case "a reply action survives normalisation".
- **Second-channel blocker:** No (present-day WhatsApp; fix first). The neutral `action` field should exist before the Messenger adapter.

### OMNI-031 - The unified inbox fails closed for a channel without an adapter (High)

- **Subsystem:** API, rollout and rollback safety.
- **Current behaviour:** `_present_all` (`app/api/v1/conversations.py:90-106`) computes `messaging.window_open()` and `messaging.reply_policy()` for every conversation on the page; both call `ChannelRegistry.policy_for`, which raises `ChannelUnavailableError` (a 422) for any channel without a registered adapter (`app/channels/registry.py:58-65`). The registry is the only switch a channel has - there is no runtime channel flag or workspace allowlist.
- **Evidence:** P2 through the real ASGI app on PostgreSQL: a workspace with one WhatsApp conversation and one Instagram-labelled conversation, default registry -> `GET /api/v1/conversations` **422 "Wasla cannot operate this channel."**; `?channel=whatsapp` -> 200 with 1 item; `GET /conversations/{whatsapp_id}` -> 200. The second-channel suite tests the repository filter, not the route (`test_omnichannel_second_channel.py:526`).
- **Why it matters:** the rollout principle is that an Instagram problem must not break WhatsApp; the natural rollback for a faulty Instagram adapter - stop registering it - breaks the default inbox of every workspace that has an Instagram thread.
- **Failure scenario:** Instagram ships, a defect forces a rollback that removes the adapter, and every affected workspace's inbox returns 422 until Instagram conversations are deleted or the adapter is restored.
- **Recommended remediation:** render a conversation whose channel is not operable with `reply_policy` = "unavailable" (no free text, no templates) and `service_window_open = false` instead of raising; add a registry state "known but paused" (adapter registered for reads, refusing sends and fetches) or a per-workspace channel flag in configuration, so rollback is a flag, not a deploy.
- **Schema impact:** none (a config table only if flags are chosen). **API impact:** additive (`reply_policy` value for an unavailable channel). **Migration impact:** none.
- **Testing required:** route test listing a page that mixes an operable and a non-operable channel; mutant "raise in policy_for during render" must be killed; a rollback rehearsal test (adapter removed, WhatsApp inbox unchanged).
- **Second-channel blocker:** **Yes.**

### OMNI-032 - Recovery assumes `event_id == message_id` (Medium)

- **Subsystem:** workers, adapter contract.
- **Current behaviour:** `InboundEvent` has separate `event_id` ("the deduplication key within the connection") and `message_id`. Ingestion stores the message under `message_id`, but `InboundRecoveryWorker._recover` finds it with `provider_message_id=event.event_id` (`inbound_recovery.py:224-225`) and the stranded-media sweep joins `ChannelEvent.event_id == Message.wa_message_id` (`media_repository.py:502-507`). WhatsApp and the synthetic adapter happen to set both to the same value; the contract suite asserts only that both are present (`test_channel_adapter_contract.py:184`).
- **Evidence:** P3: identical ids -> recovery queues 1 turn, event `processed`; split ids (`evt.<mid>` vs `<mid>`) -> recovery abandons, event `failed`/`projection_missing`, 0 turns.
- **Why it matters:** the inbound recovery path is what keeps a Redis blip from becoming an unanswered customer (ADR-102). An adapter that composes event ids (as WhatsApp already does for statuses) silently loses that guarantee, and the media sweep's exclusion stops working, so two sweeps can race one file.
- **Failure scenario:** a second adapter uses `"<mid>:message"` as its event id; Redis refuses an enqueue; the sweep abandons the event; the customer is never answered and an operator sees `projection_missing`.
- **Recommended remediation:** either make the invariant part of the type (an `InboundEvent` for `MESSAGE`/`ECHO` must have `event_id == message_id`, checked in `__post_init__` and the contract suite), or store the projected message's id on the event row and have recovery and the media sweep use it.
- **Schema impact:** none, or a nullable `whatsapp_events.message_id` with a tenant-agreed key. **API impact:** none. **Migration impact:** none / additive.
- **Testing required:** contract case for the invariant; recovery test with split ids; media-sweep race test.
- **Second-channel blocker:** Should (a must for any adapter whose event identity differs).

### OMNI-033 - The send seam cannot carry the policy's decision (Medium)

- **Subsystem:** outbound adapter boundary, channel policy.
- **Current behaviour:** `ChannelPolicy.may_send(conversation, origin, kind, now)` returns only allow/refuse; `ChannelSender.send(recipient, content)` (`app/channels/adapter.py:110`) receives no origin, window state or mechanism; `OutOfWindow` knows `TEMPLATE` and `NOTHING` (`policy.py:43-49`); `ReplyPolicy` has no per-origin rule.
- **Evidence:** Meta: Messenger needs `messaging_type` on every send (`RESPONSE`/`UPDATE` inside 24 h, `MESSAGE_TAG` outside), and `HUMAN_AGENT` lets a person reply within 7 days - "required for Instagram Messaging API" (F3, I5).
- **Why it matters:** a human agent replying to an Instagram or Messenger customer on day two to seven must be sent with a tag the adapter cannot know to add; the AI must never use it.
- **Failure scenario:** a Messenger policy allows a human reply on day three; the adapter sends without the tag; Meta refuses; or, worse, an adapter that tags everything lets the AI use a human-agent tag, a policy violation.
- **Recommended remediation:** pass a `SendContext` (origin, the policy's `SendDecision` with a mechanism such as `standard_window` / `human_agent_tag` / `template`) to `send`; add `OutOfWindow.TAG`; let `reply_policy` answer per origin.
- **Schema impact:** none. **API impact:** additive (`reply_policy` per origin). **Migration impact:** none.
- **Testing required:** outbound contract: an AI origin can never produce a human-agent mechanism; a human origin inside 7 days does; WhatsApp unchanged.
- **Second-channel blocker:** Should (Messenger and Instagram can launch with in-window replies only).

### OMNI-034 - Webhook body cap 1 MiB versus Meta's 3 MB, and refusals are invisible (Medium, present-day)

- **Subsystem:** inbound webhook, observability.
- **Current behaviour:** `webhook_max_request_bytes = 1 MiB` (`config.py:650`; comment: "a WhatsApp delivery is a few kilobytes of JSON"), enforced on `/api/v1/webhooks/*` before the signature (`limits.py:177-190`); a refusal is a 413 logged as `request.body_too_large` and counted by no metric or alert.
- **Evidence:** Meta: "Webhook payloads can be up to 3 MB", retried "for up to 7 days" (M3, M4). P11: well-formed deliveries of 1,468,122 B (200 long Arabic texts) and 2,568,972 B -> **413**; 432,251 B (1,000 statuses, Meta's maximum batch) -> passes to the signature check.
- **Why it matters:** a refused delivery is refused identically on every retry, so its messages are lost after 7 days; Arabic text is two bytes per character, so bursts reach the cap sooner; Coexistence history chunks are large by nature.
- **Failure scenario:** a promotion triggers many long replies batched into one 1.4 MB delivery; every attempt is refused; the messages never arrive; no alert fires (`WhatsAppInboundStopped` stays quiet because other deliveries succeed).
- **Recommended remediation:** raise the webhook cap to at least Meta's 3 MB (keep it far below the 32 MB general cap), count body-limit refusals by route, alert on any on the webhook route.
- **Schema / API / migration impact:** none.
- **Testing required:** a 2.9 MB signed delivery accepted; a 3.5 MB one refused and counted; an alert-rule test.
- **Second-channel blocker:** Should; **Coexistence: yes**.

### OMNI-035 - Meta errors are classified by HTTP status, not by Meta's codes (Medium, present-day)

- **Subsystem:** outcome taxonomy, workers, connection health.
- **Current behaviour:** `WhatsAppClient._post` treats only HTTP 429 as a rate limit and only HTTP 401 or code 190 as a credential failure (`client.py:992-1049`); every other 4xx is `SendNotAttemptedError` (a per-message decline).
- **Evidence:** Meta's error catalogue documents throttling as codes 4, 80007, 130429, 131056 and does not specify their HTTP status; it documents connection-level failures as 0, 3, 10, 200-299, 131005 (permissions), 368, 131031 (account restricted), 133010 (not registered) (M21, M22).
- **Why it matters:** the neutral taxonomy is the seam every adapter maps onto; mapping by status hides the two facts a sweep acts on - "wait" and "stop, the connection is broken" - and health is recorded only for 401/190.
- **Failure scenario:** Meta answers 400 with code 130429 during a campaign; each recipient is marked failed instead of the campaign backing off; or a revoked permission (code 10) burns every recipient's attempt one by one with the connection still reading healthy.
- **Recommended remediation:** classify by Meta code first (throttling -> `RateLimitedError`; 0/3/10/200-299/131005/368/131031/133010 -> a connection-level refusal that stops the sweep and records health); keep status-based fallbacks; record `RATE_LIMITED`/`PERMISSION_MISSING` health.
- **Schema / API / migration impact:** none.
- **Testing required:** outbound contract cases per code with HTTP 400; campaign and follow-up sweeps stop or wait accordingly.
- **Second-channel blocker:** Should (Instagram and Messenger use the same Graph error envelope).

### OMNI-036 - Out-of-order delivery moves the service window backwards (Medium, present-day)

- **Subsystem:** conversation state, WhatsApp policy.
- **Current behaviour:** `ConversationRepository.touch_inbound` assigns `last_inbound_at = at` and `last_message_at = at` unconditionally (`conversation_repository.py:474-483`), with `at` the provider timestamp; unchanged since `69703d6`. Contact `last_seen_at` uses a max; this does not.
- **Evidence:** P10: a message stamped *now* then one stamped a day earlier -> `last_inbound_at` = the older time. Meta: retries for up to 7 days, no ordering guarantee (M3).
- **Why it matters:** the 24-hour window (and any future channel's window) is computed from `last_inbound_at`; the inbox orders by `last_message_at`.
- **Failure scenario:** message A (T-25 h) fails delivery during an outage; message B (T) succeeds; Meta's retry of A arrives at T+1 h; the window now reads closed and every free-text reply - human or AI - is refused until the customer writes again, although Meta's window (from B) is open.
- **Recommended remediation:** `last_inbound_at = max(existing, at)`, the same for `last_message_at` (and for outbound sends).
- **Schema / API / migration impact:** none.
- **Testing required:** out-of-order ingestion keeps the newer anchor; window open after a late older message.
- **Second-channel blocker:** Should; **Coexistence: yes** (history would set windows months back).

### OMNI-037 - Echoes of replies sent outside Wasla are never projected (Medium)

- **Subsystem:** echoes, handoff, transcript.
- **Current behaviour:** every `ECHO` is stored as evidence and nothing else (`channel_ingestion_service.py:331-337`); there is no distinction between the echo of Wasla's own send and the echo of a reply a colleague typed in the provider's app.
- **Evidence:** P9 (synthetic channel): customer "price?" -> agent turn queued; echo of a native-app reply "It is 500." -> `echoes 1`, transcript only `["inbound", "price?"]`, mode `ai`, the queued turn untouched. Meta: Instagram `is_echo`; Coexistence `smb_message_echoes` "describes any new messages the business customer sends with the WhatsApp Business app" (I1, M27).
- **Why it matters:** where staff also answer from the native app, the AI and the person talk over each other, the AI's memory omits the human's words, and the inbox shows half a conversation.
- **Failure scenario:** a colleague answers from the Instagram app; seconds later Wasla's AI answers the same question differently.
- **Recommended remediation (product decision):** match an echo to Wasla's outbound message by provider id (confirm, nothing else); project an unmatched echo as an outbound message with an external origin (`BUSINESS_APP`/external human), and either hand the conversation to a person or suppress pending AI turns.
- **Schema impact:** a `message_origin` label. **API impact:** additive origin value. **Migration impact:** `ALTER TYPE ... ADD VALUE`.
- **Testing required:** echo of own send changes nothing; an external echo appears in the transcript, cancels the queued turn, switches mode per decision.
- **Second-channel blocker:** Should (decide before Instagram/Messenger launch); **Coexistence: yes**.

### OMNI-038 - Conversation order is insertion order (Medium)

- **Subsystem:** conversation ordering, AI memory.
- **Current behaviour:** `wasla_assign_message_sequence` gives each message the next position at insert (AI-01); the transcript, the inbox and the AI window read by `sequence` (`agents/memory.py:118`, `conversation_repository.py:518-522`). The design is deliberate for live traffic.
- **Evidence:** P10: newer message first, older second -> `sequence 1 = newer, 2 = older`; the model reads them in that order. Coexistence history: 180 days, chunked by phase (M27).
- **Why it matters:** imported history (Coexistence, or any channel's history import) and late retries are placed after newer messages; the AI reads a scrambled conversation.
- **Failure scenario:** Coexistence onboarding imports six months of chat after the first live message; the agent's window shows old messages as the latest.
- **Recommended remediation:** for imports, assign positions by provider time in a dedicated path (or keep a separate sort key for history and render history before live messages); never touch windows from history.
- **Schema impact:** possibly an `imported_at`/`provider_sent_at` column. **API impact:** none. **Migration impact:** additive.
- **Testing required:** history import ordering, window untouched, no agent turns.
- **Second-channel blocker:** No for Instagram/Messenger; **Coexistence: yes**.

### OMNI-039 - Scheduling validates against WhatsApp; channel refusals are retried (Medium)

- **Subsystem:** follow-ups, AI tools.
- **Current behaviour:** `FollowUpService.schedule` validates the body against `MAX_BODY_LENGTH = 4096` (`app/db/models/follow_up.py:44`) and a template against the WhatsApp registry, never against the conversation's policy (`follow_up_service.py:214-316`); the AI tool's description recommends multi-day delays regardless of channel (`agents/registry.py:812-814`); a `ValidationError` at dispatch goes through `_fail`, which keeps the follow-up pending for retry (`follow_up_service.py:947`).
- **Evidence:** P6: 720 Arabic characters / 1,320 bytes accepted at scheduling on a 1,000-byte channel; dispatch refused "A Synthetic message may be at most 1000 bytes."; status `pending`, attempts 1, nothing sent.
- **Why it matters:** the customer-facing promise ("I'll get back to you next week") becomes a guaranteed skip on channels with no out-of-window mechanism, and deterministic refusals consume retries.
- **Recommended remediation:** validate the body in the conversation channel's unit and limit at scheduling; refuse (or warn the model) when the delay passes the channel's window and it has no out-of-window mechanism; treat a policy refusal as terminal (`SKIPPED`/`FAILED`), not retryable; make the tool description channel-aware.
- **Schema / API / migration impact:** none.
- **Testing required:** schedule-time refusal on a byte-bounded policy; terminal refusal at dispatch; tool description per channel.
- **Second-channel blocker:** Should.

### OMNI-040 - Instagram outbound media needs URLs; Wasla can only upload (Medium)

- **Subsystem:** media (outbound), adapter seam, storage.
- **Current behaviour:** `send_media` uploads bytes and sends by provider id ("Uploaded rather than sent by link, deliberately", `messaging_service.py:395-399`); `ChannelSender.prepare` models an upload; `MediaStorage` exposes `put_at/get/delete/exists` only.
- **Evidence:** Meta: Instagram image by `url` or `attachment_id`, **video, audio and file by `url` only**; image 8 MB, audio/video/PDF 25 MB (I4).
- **Why it matters:** an Instagram adapter cannot send a video, voice clip or PDF at all without a URL Meta can fetch.
- **Recommended remediation:** a storage capability to issue short-lived signed URLs for one object (with the privacy trade-off recorded as an ADR), and a `prepare` that returns a provider reference (upload id or URL) instead of mutating the sender.
- **Schema impact:** none. **API impact:** none. **Migration impact:** none.
- **Testing required:** media contract: URL issued only for the message's own object, expires, never logged; Instagram text-and-image launch path proven without it.
- **Second-channel blocker:** Should (a must if Instagram media sends are in the first release).

### OMNI-041 - The automation-disclosure obligation is not modelled (Medium, compliance)

- **Subsystem:** AI, channel policy.
- **Current behaviour:** `agent_instructions()` is a static sentence per channel; nothing tracks when a conversation last disclosed automation or when it moved from a person back to the AI (`resume AI`).
- **Evidence:** Meta's Messenger and IG Messaging policy: "Automated chat experiences must disclose that a person is interacting with an automated service" at the start of a conversation, after significant time lapses, and when transitioning from human to automated (F4).
- **Why it matters:** Wasla's product is an AI employee; on Messenger and Instagram this is a platform-policy requirement for every AI conversation.
- **Recommended remediation:** a policy capability ("disclosure required") and a conversation-level marker (last disclosure, last human-to-AI transition), with the disclosure either sent as a system line or injected into the first AI reply; decide the wording per language.
- **Schema impact:** a nullable timestamp on `conversations`. **API impact:** none. **Migration impact:** additive.
- **Testing required:** first AI reply discloses; a reply after a long gap discloses; resuming AI after a human discloses; WhatsApp unchanged unless the product chooses otherwise.
- **Second-channel blocker:** **Yes** for AI replies on Messenger and Instagram (human-only use could launch without it).

### OMNI-042 - Messenger watermarks compared with the wrong clock (Medium, *architectural risk / inference*)

- **Subsystem:** status lifecycle.
- **Current behaviour:** `advance_to_watermark` advances outbound messages with `sent_at <= watermark` (`conversation_repository.py:607-640`); `sent_at` is Wasla's clock taken after the Send API answers (`messaging_service.py:863-868`).
- **Evidence:** Meta: "All messages that were sent before or at this timestamp were read" (epoch ms, provider clock) (F2). P5: watermark 1 s before local `sent_at` -> 0 advanced; 1 s after -> 1. Not verified against real Messenger traffic.
- **Why it matters:** the newest message - the one a person actually reads - is the one most likely to sit after the watermark by a round trip and clock skew, so it is never marked read.
- **Recommended remediation:** record the provider's timestamp for each outbound message (Messenger echoes carry it) and compare that; otherwise compare with a bounded tolerance; prove with recorded Messenger payloads.
- **Schema impact:** optional `messages.provider_sent_at`. **API impact:** none. **Migration impact:** additive.
- **Testing required:** Messenger status contract with a watermark equal to the provider timestamp of the last message.
- **Second-channel blocker:** Should (Messenger only).

### OMNI-043 - Legacy workspace-wide uniques still decide (Medium)

- **Subsystem:** dedup, database.
- **Current behaviour:** `uq_whatsapp_events_tenant_id_event_id` and `uq_messages_tenant_id_wa_message_id` remain beside the per-connection keys (C5). `ChannelEventRepository.store` inserts with a target-less `ON CONFLICT DO NOTHING`; when the workspace-wide key refuses an id that another connection holds, nothing is stored and the delivery is counted as a collision (`channel_event_repository.py:96-127`).
- **Evidence:** P4: the same id on a second connection of one workspace -> `stored 0, collisions 1`, payload not kept; in another workspace -> stored.
- **Why it matters:** the neutral identity is not yet the authoritative one. For Meta's globally unique ids this costs nothing; for a provider whose ids are unique per chat or per account it is silent message loss, contradicting "nothing refused disappears".
- **Recommended remediation:** O8 as planned (drop both workspace-wide uniques after a release on the neutral path); until then, store a collided event as `failed` evidence (on its own connection) rather than discarding it.
- **Schema impact:** two `DROP INDEX CONCURRENTLY`s (O8). **API impact:** none. **Migration impact:** online.
- **Testing required:** same id on two connections stored twice after O8; the Y1 collision regression still holds.
- **Second-channel blocker:** Should for Instagram/Messenger; **yes** for any channel with per-connection ids.

### OMNI-044 - WhatsApp strings, limits and log names in shared code (Low)

`PROVIDER_REPORTED_FAILURE = "WhatsApp reported this message as failed."` is written for every channel's failed status (`conversation_repository.py:835`); shared `MessagingService` logs `whatsapp.outbound_failed/uncertain/replayed` for every channel and re-exports WhatsApp constants; request schemas cap text at 4,096 (`schemas/conversation.py:40`), captions at 1,024 and uploads at 16 MB (`api/v1/conversations.py:81-85`); `prepare_channel_reply` defaults to WhatsApp's capabilities and `_mostly_arabic` samples by a WhatsApp constant (`agents/reply.py:85,133`); `_DISABLED` holds WhatsApp's sentence only. *Impact:* misleading text and log names on another channel; schema ceilings tighter than a future channel's limits. *Remediation:* neutral wording per channel policy, `channel.*` log events, ceilings from the policy. No schema, API (beyond text) or migration impact. Not a blocker.

### OMNI-045 - Provider per-type media limits are not capabilities (Low)

Meta limits images to JPEG/PNG <= 5 MB, video to MP4/3GPP <= 16 MB, audio <= 16 MB, documents <= 100 MB (M18); Instagram allows images 8 MB, audio/video/PDF 25 MB (I4). Wasla checks only the family and its own caps, so an over-limit file is staged, uploaded and refused by Meta (recorded undelivered). The handle's 7-day expiry and Meta's optional `phone_number_id` binding on media retrieval are not used (M16). *Remediation:* per-family MIME and size limits in `ChannelCapabilities`, checked before staging; record handle expiry. Not a blocker.

### OMNI-046 - `user_preferences` and 131050 never reach the contact's opt-out (Low, present-day)

The `user_preferences` webhook (`category: marketing_messages`, `value: stop|resume`) is refused as `unsupported_field` (counted, not alerted), and a 131050 refusal ("chosen to stop receiving marketing messages ... Don't retry") is recorded only on the message (M24). Meta blocks those deliveries itself, so the cost is wasted sends and a CRM that shows consent it does not have. *Remediation:* consume `user_preferences` (subscribe the field) into `marketing_opt_out_at` with a provider source, and record 131050 the same way. Not a blocker.

### OMNI-047 - Compatibility trigger semantics (Low)

`trg_contacts_phone_identity` writes `source = provider` for any writer of `contacts.wa_id` (fixtures, the unused legacy upsert); an `UPDATE` to a different `wa_id` is refused by `uq_contact_identities_one_whatsapp_phone`; setting it to NULL leaves the phone identity behind (P7). Harmless for today's writers, which only ever fill a NULL; number changes, BSUID rotation and erasure will each need explicit identity writes, and O8 removes the trigger. Not a blocker.

### OMNI-048 - Analytics have no channel dimension; the channel inbox filter has no index (Low)

Tenant analytics report totals across channels (`analytics_service.py:125-154`); a channel breakdown is a join away. The channel-filtered inbox walks the workspace's conversations and filters (E5). *Remediation:* `group by channel` in `TenantMetricsRepository`; a `(tenant_id, channel, last_message_at DESC NULLS LAST, id DESC)` index when a second channel has volume. Not a blocker.

### OMNI-049 - Dead legacy lookups (Low)

`ContactRepository.upsert` and `get_by_wa_id` (contacts created by `wa_id`, bypassing `ContactIdentityService`) and `OutboundMessageDirectory.find_by_wa_message_id` have no caller in `app/` (tests only). A future caller of the first would create a phone identity through the trigger with the wrong provenance. *Remediation:* remove with O8 (move the tests to the identity service). Not a blocker.

### OMNI-050 - Meta webhook operations differ by product (Low)

Instagram and Messenger webhooks (Graph) are retried "over the next 36 hours" (I6), WhatsApp's for 7 days: the outage-recovery posture (answer 5xx, let Meta retry) has a shorter horizon there, and the runbook should say so. Instagram's documentation says payloads are signed with "your app's App Secret" without saying which secret an Instagram-Login app uses (I7); Wasla has one `META_APP_SECRET`. *Remediation:* verify at implementation; per-product secrets if needed (the verifier already takes the secret as a parameter). Not a blocker.

### OMNI-051 - One global agent FIFO across tenants and channels (Low)

`agent:jobs` is a single queue (`workers/queue.py:71`); a burst on one channel or tenant delays every other's AI turns (the pre-existing WQ-09). *Remediation:* per-tenant or per-connection fairness when volume warrants. Not a blocker.

### OMNI-052 - One allowance value for every connection (Low)

`CONNECTION_SENDS_PER_MINUTE` is a deployment-wide number applied to each connection's own window (ADR-123); Meta gives a number 80 mps by default and up to 1,000 by automatic upgrade, and fixes Coexistence numbers at 20 mps (M21, M27), so one value cannot describe every connection. *Remediation:* a per-connection override column when Coexistence or tiered limits are implemented. Not a blocker.

### OMNI-053 - Reactions, edits, unsend and postbacks have no neutral representation (Low)

`ChannelCapabilities` has `reactions` and `unsend` flags, but `InboundKind` is message/status/echo and `MessageKind` has no reaction; WhatsApp `reaction` is stored as unsupported (P8). Instagram sends `reaction`, `message_edit`, `is_deleted` and `postback` (I1). An edit with the original `mid` would be screened as a duplicate and ignored; an unsent message stays in the transcript and AI memory. *Remediation:* decide per event (edit -> revision, unsend -> redaction, reaction -> annotation, postback -> message with action). Not a blocker.

### OMNI-054 - BSUID limitations not policy-checked; rotation unprocessed (Low)

Authentication templates cannot be sent to a BSUID (M10) and the policy does not refuse them before staging; a phone change regenerates the BSUID with a `system` message (M8), which is stored as unsupported, so the customer's next message becomes a new contact (documented in ADR-118). *Remediation:* refuse authentication-category templates on BSUID-pinned conversations; process `system` number-change messages with the provider assertion they carry. Not a blocker.

### OMNI-055 - Graph API `v21.0` default expires 2027-01-21 (Low)

Meta's changelog lists v21.0 "available through January 21, 2027" (M26); `meta_api_version` defaults to it and `META_API_SUNSETS` agrees, so the built-in warning starts on 2026-10-23. Every Meta adapter shares the version. *Remediation:* plan the move to a current version (v24.0+), re-verify the contracts in section 4a. Not a blocker.

### OMNI-056 - Verified channel-neutral foundation (Info)

Preserved and verified on the merged tree: neutral connections with tenure and mirrored WhatsApp lifecycle; scoped identities with provider-asserted pairing and race convergence; the pinned participant and the routing chain with database keys behind it; per-connection provider identity and the echo-safe screen; the neutral inbound event, ingestion, event log, recovery and retention for every channel; channel policy and capabilities with byte-aware bounding; ordered media with neutral locators and the shared URL fetcher; the outcome taxonomy; the per-connection allowance; channel-labelled metrics; tenant-agreed composite keys; online migrations (fresh build 11 s, single head, nothing left NOT VALID or INVALID); one outbound choke point.

### OMNI-057 - Provider conformance verified (Info)

Nineteen of the 38 Meta contracts in section 4a conform as implemented or as modelled: M1-M3, M5-M7, M9, M11, M14-M17, M19, M20, M23, M25 and M26 for WhatsApp, I3 (IGSID scope) for Instagram and F1 (PSID scope and event shape) for Messenger. The byte-aware text bounding Instagram's 1,000-byte rule needs (I4) is also already in place.

### OMNI-058 - WhatsApp-only by design where it should be (Info)

Campaigns and templates are WhatsApp features with WhatsApp foreign keys (C3), correctly: Instagram has no business-initiated messaging and Messenger's equivalent is a different product. There is no in-app notification subsystem, so nothing there is coupled.

---

## 41. Blockers before second channel

Only what genuinely must land before Instagram or Messenger is switched on for customers:

| # | ID | Why it must come first |
| --- | --- | --- |
| 1 | **OMNI-029** | every channel's statuses take the full-scan path; a second channel adds volume to a query whose cost already grows with the platform |
| 2 | **OMNI-031** | without it, the rollback of a faulty second channel breaks WhatsApp's inbox |
| 3 | **OMNI-041** | Meta's Messenger/Instagram policy requires automation disclosure for every AI conversation (a human-only inbox could launch without it) |
| gate | **ADR-122 decisions** | meters and quota, connection limit and price, opt-out scope across channels; `ChannelRegistry` refuses to register an adapter until the meter is decided |

**Count: 3 findings + 1 product gate.** Fix-first present-day WhatsApp defects (OMNI-030, and soon after OMNI-034, OMNI-036) are not second-channel blockers and are not listed here; they are at the top of the backlog regardless (section 51). Coexistence has its own blockers (section 35).

---

## 42. WhatsApp-specific code that should remain WhatsApp-specific

Properly isolated and not to be abstracted:

- `whatsapp_accounts` (phone number id, WABA, display number, verified name, ownership proof, encrypted token) as the extension of `channel_connections`.
- The connect / reverify / release flows and `MetaOwnershipVerifier` (ADR-037, ADR-101).
- `WhatsAppChannelPolicy`: the 24-hour customer-service window, the template escape, 4,096 characters, the reply budget, the agent sentence.
- Template registry, sync, categories, approval and the withdrawal write-back (`TemplateWithdrawnError`, codes 132001/132015/132016).
- The parser and its `object`/`field` discrimination, `MESSAGE_KINDS`, `DELIVERY_STATUSES`, BSUID handling and `to`-or-`recipient` addressing.
- The Graph media two-step fetch, the Meta host roots and the upload API.
- The platform-token fallback (`CredentialService.resolve`), reachable only through the WhatsApp adapter.
- Campaigns (approved templates only) and `LimitKey.WHATSAPP_NUMBERS`.
- The `whatsapp_message_received/sent` meters (renaming them would break usage reproducibility).
- `/webhooks/whatsapp`, `Provider.WHATSAPP` and the WhatsApp alerts.
- The `WhatsAppEvent*` aliases and `account_id` names until O8.

---

## 43. Target architecture

The target is what is built, plus five additions. In current model names:

```
channel_connections (id, tenant_id, channel, external_account_id, status, tenure, health,
                     credential_expires_at, send window [+ per-connection allowance override])
   +-- whatsapp_accounts (same id)        +-- [instagram_accounts / messenger_pages, same id, if needed]

contact_identities (tenant_id, contact_id, channel, kind, scope, scope_ref, connection_id, value, source)
   UNIQUE (tenant_id, channel, kind, scope, scope_ref, value)

conversations (tenant_id, contact_id, account_id = connection, channel, participant_identity_id,
               last_inbound_at = max(...)  [OMNI-036], automation_disclosed_at [OMNI-041])
   UNIQUE (tenant_id, contact_id, account_id)

messages (id, conversation_id, connection_id, sequence, wa_message_id = provider id, origin [+ business_app],
          [action payload OMNI-030], [provider_sent_at OMNI-042])
   UNIQUE (tenant_id, connection_id, wa_message_id)      -- workspace-wide unique dropped at O8

whatsapp_events = channel event log (tenant_id, account_id, channel, event_id, kind, state, payload)
   UNIQUE (tenant_id, account_id, event_id)              -- workspace-wide unique dropped at O8
   [message_id of the projected message, OMNI-032]
```

`ProviderMessageReference` as a separate table is **not** recommended: one provider id per message per connection is what every Meta channel does, it is indexed on the hot table already, and a join per status would cost more than it buys. **ChannelAdapter**: the existing protocol (`parse`, `identity_scope`, `address`, `sender`, `media_fetcher`, `locator_expired`, `policy`) is the right shape; it needs a send context and a provider reference from `prepare` (section 44).

---

## 44. Minimum viable refactor

No rewrite. The smallest set of changes that makes the existing foundation safe for a Meta second channel:

1. **Status lookup** (OMNI-029): add the holders' tenant ids to `find_by_provider_message_id` (or a concurrent partial index); delete `find_by_wa_message_id`.
2. **Tolerant inbox** (OMNI-031): render a non-operable channel's conversations with an "unavailable" reply policy; add a paused/known registry state or a channel flag.
3. **Adapter interface additions**, derived from the gaps rather than imposed:

```python
class ChannelSender(Protocol):
    async def prepare(self, content: OutboundContent) -> PreparedContent: ...      # upload id or URL (OMNI-040)
    async def send(self, recipient: Recipient, content: PreparedContent,
                   context: SendContext) -> ProviderReceipt: ...                     # origin + mechanism (OMNI-033)

@dataclass(frozen=True)
class SendContext:
    origin: MessageOrigin
    mechanism: Literal["standard_window", "template", "human_agent_tag"]

# InboundEvent: event_id == message_id for MESSAGE/ECHO, enforced (OMNI-032);
#               action: ReplyAction | None  (payload/id + title)                  (OMNI-030)
# ChannelPolicy: may_send(...) -> SendDecision(allowed, reason, mechanism);
#                capabilities: + media_limits, + disclosure_required              (OMNI-045, OMNI-041)
```

What moves behind the adapter: nothing more - WhatsApp's code is already there. What stays shared: ingestion, identity resolution, projection, recovery, retention, the delivery protocol, bounding, metering, AI turns, the inbox.

4. **Monotonic window** (OMNI-036) and **webhook cap + counter** (OMNI-034) - one line and one setting.
5. Then the Instagram adapter (route, parser, connect flow, sender, URL fetcher configuration, policy with disclosure).

---

## 45. Database migration strategy

The foundation's migrations (0082-0084) are done and verified online. What remains, in order:

| Step | Change | Online technique | Rollback |
| --- | --- | --- | --- |
| D1 (OMNI-029, if the index route is chosen) | `messages (connection_id, wa_message_id) WHERE direction = 'outbound'` | `CREATE INDEX CONCURRENTLY`, rebuild an INVALID leftover first (0084's pattern) | `DROP INDEX CONCURRENTLY` |
| D2 (OMNI-030) | nullable `messages.action_payload` / `action_title` | metadata-only `ADD COLUMN` | drop column (refuse if non-null rows, as 0082 does) |
| D3 (OMNI-032, if the column route is chosen) | nullable `whatsapp_events.message_id` + tenant-agreed key `NOT VALID` then `VALIDATE` | as 0084 | drop |
| D4 (OMNI-041) | nullable `conversations.automation_disclosed_at` | metadata-only | drop |
| D5 (OMNI-037) | `message_origin` + `business_app` / external label | `ALTER TYPE ... ADD VALUE` in its own autocommit block (0082's pattern) | labels cannot be dropped; unused label is harmless |
| D6 (OMNI-042) | nullable `messages.provider_sent_at` | metadata-only | drop |
| D7 = **O8** (OMNI-043) | drop `uq_messages_tenant_id_wa_message_id`, `uq_whatsapp_events_tenant_id_event_id`, the mirror and phone-identity triggers, `contacts.wa_id`, `message_media.wa_media_id`; move lifecycle writes to `channel_connections` | `DROP INDEX CONCURRENTLY`; columns dropped one release after readers stop | restore uniques with `CREATE UNIQUE INDEX CONCURRENTLY` (fails loudly if a collision was stored meanwhile) |

**Existing data backfill** (already done by 0082/0083, re-verified on a migration-built database here): each number -> a connection with the same id; each contact's `wa_id` -> exactly one phone identity (`source = backfill`, nothing else consulted); each conversation -> its contact's phone identity; each message -> its conversation's connection; each media handle -> a `handle` locator; each campaign recipient -> its conversation's participant where one exists. **Ambiguous cases are refused, not guessed**: a number claimed live twice, empty identifiers, a conversation whose number or contact is another workspace's, a conversation whose contact holds no phone identity (0082/0083 prechecks). The BSUID pairing evidence still retained in raw payloads (Q1 census) must be run on a production copy **before the 30-day redaction window passes** - after it, those pairs are unrecoverable.

**Zero-downtime: CONDITIONAL** - feasible with the techniques above (nullable-first, `NOT VALID`/`VALIDATE`, `CONCURRENTLY`, autocommit enum additions, batched keyset backfills, refusing downgrades), on condition that O8 waits for (a) a release on the neutral path, (b) `omnichannel_invariants verify` = 0 on production, (c) API clients off `wa_message_id`/`wa_id`, (d) a rehearsed PITR restore. **Technical complexity: MEDIUM.**

---

## 46. API compatibility strategy

- Keep `account_id`, `wa_message_id` and `ContactOptOutRead.wa_id` (deprecated in OpenAPI and `docs/API.md`) until O8; never repurpose them.
- Additive only: `reply_policy` gains an "unavailable" state (OMNI-031) and per-origin rules (OMNI-033); `MessageRead` gains `action` (OMNI-030) and, when decided, the external-echo origin (OMNI-037).
- New read surfaces before a second channel is visible to customers: `GET /connections` (all channels, tenant-scoped, counts and status only), `GET /contacts/{id}/identities` (kinds and values, member-gated as personal data).
- `service_window_open` keeps its WhatsApp meaning forever; clients that need another channel's rule read `reply_policy`.
- Remove deprecated fields only after a published deprecation period and an access-log check that no client reads them.

---

## 47. Rollback strategy

| Phase | Rollback | Must not happen |
| --- | --- | --- |
| R26 status lookup | revert the predicate or drop the index concurrently | - |
| R28 tolerant inbox | revert; harmless | - |
| D2-D6 columns | drop columns (downgrades refuse while non-null data exists, 0082's rule) | losing an action or disclosure record silently |
| Instagram adapter launch | **flag the channel paused** (after R28): inbound stored and visible, outbound and AI refused, inbox intact | unregistering the adapter before R28 (OMNI-031) |
| Messenger adapter launch | same as Instagram | |
| O7 linking (future) | unlink by moving identities back; audit every move | merging without an audit row |
| O8 cleanup | restore uniques concurrently; restore columns from the last backup only via PITR rehearsal | dropping before the release-on-neutral-path condition |

Isolation principle, checked: an Instagram or Messenger *provider* outage cannot break WhatsApp - connections, credentials, health, policies, adapters and webhook routes are separate; the shared parts are the database, the event log, the global agent FIFO (OMNI-051) and the inbox renderer (OMNI-031). After R28, the only cross-channel coupling left is queue fairness.

---

## 48. Future test strategy

Required before the first second-channel merge, building on the existing contract suites (`test_channel_*_contract.py`, status contract, oracles, races):

| Area | Tests |
| --- | --- |
| provider contracts | the five contract suites run against the real Instagram (or Messenger) adapter with recorded, redacted Meta payloads: text, attachments array, echo, `is_deleted`, `message_edit`, reaction, postback, `read {mid}` / watermark, quick reply |
| identity | IGSID per connection; same IGSID on two connections -> two contacts; same in two workspaces -> two; display name never links |
| connection isolation | events of connection A never route to B; tenure handover for a Page |
| tenant isolation | the cross-tenant matrix extended with Instagram rows |
| dedup | replay, echo of own send, external echo, edit with the original `mid` |
| wrong-channel send | Instagram inbound never answered over WhatsApp; WhatsApp template refused on Instagram |
| status mapping | per-message read (Instagram); watermark with the provider timestamp equal to the last message (Messenger, OMNI-042) |
| status performance | EXPLAIN shows an index path for status resolution (OMNI-029) |
| inbox | a page mixing an operable and a paused channel answers 200 (OMNI-031) |
| recovery | split event/message ids rejected by the contract or recovered (OMNI-032) |
| worker retries | Meta throttling codes back off; connection-level codes stop the sweep and set health (OMNI-035) |
| media | URL issuance for Instagram outbound (OMNI-040); per-type limits refuse before staging (OMNI-045) |
| AI | disclosure on first reply, after a gap, after resume (OMNI-041); 1,000-byte bounding on Arabic |
| handoff | external echo projection per the product decision (OMNI-037) |
| CRM / follow-ups | schedule-time refusal in the channel's unit; next-week nudge refused where no out-of-window mechanism exists (OMNI-039) |
| campaigns | an Instagram connection cannot be a campaign's connection |
| webhook edge | a 2.9 MB signed delivery accepted; 413s counted (OMNI-034) |
| migration | D1-D6 up/down on populated 0084 databases; O8 rehearsal |

---

## 49. Recommended implementation sequence

| Stage | Status at `3473068` | Schema | Code | API | Tests | Migration | Rollback | Risk |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| **O1** neutral primitives | done (0082-0084) | - | - | - | schema, migrations | done | 0084/0082 downgrades refuse rather than lose | low |
| **O2** WhatsApp backfill | done; Q1 census on a production copy pending | - | - | - | migration suite | done | - | low (time-bound: 30-day redaction) |
| **O3** WhatsApp on the neutral path | done **with regressions**: OMNI-029, plus pre-existing OMNI-030/034/036 | D1, D2 | R26, R27, R33, R34 | `action` | EXPLAIN test, button fixtures, out-of-order, 2.9 MB | online | revert | medium (hot path) |
| **O4** contract tests | done **with gaps**: event/message identity, interactive shapes, query plans, inbox with a paused channel | D3 optional | R28, R29 | `reply_policy` "unavailable" | the four gaps | none / additive | revert | low |
| **O5** Instagram adapter | not started; needs O3/O4 fixes, ADR-122 decisions, OMNI-041 | D4, D5 | adapter, route, connect flow, policy with disclosure, sender (text, images), R31, optional R36 for media | `/connections`, identities read | section 48 | additive | channel flag paused | medium |
| **O6** Messenger adapter | not started; after O5 | D6 | adapter, watermark with provider time, `messaging_type`, `HUMAN_AGENT` | - | watermark contract | additive | flag | medium |
| **O7** cross-channel linking | not started; needs person-level erasure/export (R21) first | audit of identity moves | merge/unlink UI, audit | identities write API | linking oracles | additive | unlink | high (privacy) |
| **O8** compatibility cleanup | not started; conditions in section 45 | D7 | lifecycle writes on connections; drop mirror; rename at leisure | remove deprecated fields | invariants verify on production | drops | PITR rehearsal | medium |

**Recommended first new channel (technical view only): Instagram Direct, text and images first.** Instagram's per-message read receipts fit the existing status model (Messenger's watermarks need OMNI-042); its byte-limited text is already handled (`TextUnit.UTF8_BYTES`, proven against Arabic); IGSIDs map to the existing connection scope; it needs no `messaging_type` on in-window sends. Its costs: a separate host and token type (`graph.instagram.com`, Instagram User token), video/audio/file sends by URL only (OMNI-040), and the signing-secret question (OMNI-050). Messenger's costs are the watermark clock (OMNI-042) and `messaging_type` on every send; its media, by contrast, fits Wasla's upload-only design through the Attachment Upload API (F3). So the choice turns on scope: **Instagram first if the first release is text and images; Messenger first if outbound video, audio or documents must ship on day one and OMNI-040 is not yet done.** Evidence for the comparison is documentary (section 4a), not from a running adapter.

**Coexistence track: after the neutral-foundation fixes (O3/O4), in parallel with or after Instagram.** It shares the ingestion path, so it inherits OMNI-029/034/036 fixes, and it additionally needs OMNI-037 (echo projection) and OMNI-038 (history ordering), which Instagram does not; it does not depend on Instagram or Messenger adapters.

---

## 50. Readiness score

| Dimension | Score | Deductions |
| --- | --- | --- |
| Domain neutrality | **11 / 15** | WhatsApp constants and strings in shared code (OMNI-044); follow-up scheduling and the AI tool WhatsApp-validated (OMNI-039); meters undecided (ADR-122, product); echoes and history unrepresented (OMNI-037/038) |
| Identity model | **12 / 15** | BSUID scoped by WABA (over-splits); rotation unprocessed (OMNI-054); no merge/unlink/erasure (deferred); trigger provenance (OMNI-047) |
| Conversation / message model | **10 / 15** | window anchor regresses (OMNI-036); insertion-order history (OMNI-038); legacy uniques still decide and discard (OMNI-043); no edit/unsend/reaction/action (OMNI-030/053); `reply_to` not persisted |
| Provider adapter boundaries | **10 / 15** | no send context (OMNI-033); upload-only media (OMNI-040); content dropped at the WhatsApp edge (OMNI-030); error taxonomy by status (OMNI-035); implicit event/message identity (OMNI-032) |
| Workers / idempotency | **6 / 10** | status resolution full scan on the hottest path (OMNI-029); recovery contract hole (OMNI-032); deterministic refusals retried (OMNI-039); one global FIFO (OMNI-051) |
| API compatibility | **7 / 10** | inbox fails closed (OMNI-031); no connections/identities API; WhatsApp ceilings in schemas (OMNI-044) |
| Database migration readiness | **8 / 10** | missing index for the neutral status lookup (OMNI-029); no channel inbox index (OMNI-048); O8 pending; Q1 census on production pending |
| Security / tenancy | **4 / 5** | `v1` credentials not yet re-sealed; otherwise every probe isolated |
| Observability / testing | **3 / 5** | 413s invisible (OMNI-034); no per-channel inbound-stopped alert; contract suites missed four gaps (OMNI-029/030/031/032) |
| **Total** | **71 / 100** | |

The remediation reported 82. The 11-point difference is the four things its contract suites did not exercise - the query plan of the neutral status lookup, the route-level inbox with a non-operable channel, the event/message identity assumption, and the shapes of button replies - and the provider-conformance gaps found by reading Meta's documentation against the code.

**Readiness matrix**

| Subsystem | Status | WhatsApp coupling | Second-channel work |
| --- | --- | --- | --- |
| Connections | READY | number is an extension with the same id | connect flow per channel |
| Customer identities | READY | phone kept in `contacts.wa_id` for compatibility | IGSID/PSID writes (adapter) |
| Conversations | NEEDS REMEDIATION | none structural | OMNI-036 (monotonic window) |
| Messages | READY | `wa_message_id` name | OMNI-030 `action` (Messenger quick replies) |
| Inbound webhook | NEEDS REMEDIATION | route per product, by design | route + parser; OMNI-034 cap |
| Outbound sending | NEEDS REMEDIATION | adapter-only | OMNI-033 context, OMNI-040 URLs |
| Status lifecycle | NEEDS REMEDIATION | failure text | OMNI-029, OMNI-042 |
| Media | NEEDS REMEDIATION | handle fetch is the adapter's | OMNI-040, OMNI-045 |
| AI | READY | none | OMNI-041 disclosure (policy) |
| RAG | READY | none (tenant-scoped knowledge, no channel input) | none |
| Tools | NEEDS REMEDIATION | follow-up tool advice | OMNI-039 |
| CRM | READY | `lead_source.whatsapp` unused | none |
| Handoff | READY | none | OMNI-037 decision |
| Campaigns | READY (WhatsApp-only by design) | templates, FKs | none (product decision for other channels) |
| Notifications | READY | none (no subsystem) | none |
| Analytics | NEEDS REMEDIATION | none | OMNI-048 |
| Usage / quotas | DEFERRED PRODUCT DECISION | meters named for WhatsApp, enforced by registry | ADR-122 |
| Workers | NEEDS REMEDIATION | none in payloads | OMNI-029, OMNI-032, OMNI-035 |
| Security | READY | platform token WhatsApp-only | signing secret per product (OMNI-050) |
| Authorization | READY | none | none |
| Observability | NEEDS REMEDIATION | WhatsApp alerts by design | provider labels, per-channel inbound alert, 413 counter |
| API | NEEDS REMEDIATION | deprecated `wa_*` fields | OMNI-031, connections/identities reads |
| Database | READY WITH ADAPTER | `whatsapp_*` names, legacy uniques | D1-D6, O8 |
| Migration | READY WITH ADAPTER | - | online techniques proven |

**Migration complexity** (technical, not time):

| Area | Complexity | Why |
| --- | --- | --- |
| Connections | LOW | rows plus an optional extension table |
| Identities | LOW | new kinds already in the enum |
| Conversations | LOW | monotonic anchor; disclosure column |
| Messages | MEDIUM | status-lookup index on the largest table; O8 uniques |
| Workers | LOW | predicate change, contract check |
| API | LOW | additive fields and two read endpoints |
| CRM | LOW | nothing structural |
| Campaigns | LOW (none needed) / HIGH if multi-channel campaigns are wanted | product decision |
| Media | MEDIUM | signed URL issuance and its privacy trade-off |
| Analytics | LOW | group-bys and one index |

---

## 51. Final verdict

**Can Wasla add Instagram Direct, Facebook Messenger and future channels without redesigning or duplicating its core systems?** Yes. The core is channel-neutral and WhatsApp runs on it; a second channel is an adapter. It is not yet safe to *switch one on*: the neutral status lookup scans the whole messages table, the unified inbox breaks when a channel is paused, and Meta's automation-disclosure rule for Messenger and Instagram has no home - all three local fixes - and the product must take the ADR-122 decisions the code already refuses to guess.

**FINAL VERDICT: OMNICHANNEL READY WITH LIMITED REMEDIATION**

| Channel | Readiness |
| --- | --- |
| Instagram Direct | **NEEDS REMEDIATION** |
| Facebook Messenger | **NEEDS REMEDIATION** |
| WhatsApp Coexistence | **BLOCKED** |

**Remediation backlog**

*MUST before second channel* (5)

| ID | Closes | Work |
| --- | --- | --- |
| OMNI-R26 | OMNI-029 | index-backed status resolution (tenant ids in the predicate or a concurrent partial index); EXPLAIN regression test; remove the dead lookup |
| OMNI-R27 | OMNI-030 | normalise `button` and `interactive` replies (text + neutral `action`); honour opt-out quick replies; Meta-shaped fixtures - **present-day, do first** |
| OMNI-R28 | OMNI-031 | tolerant inbox rendering and a paused channel state / channel flag; route test; rollback rehearsal |
| OMNI-R29 | OMNI-041 | automation-disclosure capability and conversation marker; decide wording; tests on first reply, gaps and resume |
| OMNI-R30 | ADR-122 | product decisions: meters and quota, connection limit and price, opt-out scope across channels |

*SHOULD before second channel* (10)

| ID | Closes | Work |
| --- | --- | --- |
| OMNI-R31 | OMNI-032 | enforce `event_id == message_id` for message events, or store the projected message id on the event |
| OMNI-R32 | OMNI-033 | `SendContext` (origin, mechanism); `OutOfWindow.TAG`; per-origin `reply_policy` |
| OMNI-R33 | OMNI-034 | webhook cap >= 3 MB; count and alert body-limit refusals - **present-day** |
| OMNI-R34 | OMNI-036 | monotonic `last_inbound_at` / `last_message_at` - **present-day** |
| OMNI-R35 | OMNI-035 | classify Meta errors by code; record connection health |
| OMNI-R36 | OMNI-039 | channel-aware scheduling, terminal policy refusals, channel-aware tool description |
| OMNI-R37 | OMNI-040 | signed-URL issuance and `prepare` returning a provider reference (a MUST if Instagram media is in the first release) |
| OMNI-R38 | OMNI-037 | external-echo projection policy (product decision) and implementation |
| OMNI-R39 | OMNI-042 | provider timestamps for watermark comparison (before Messenger) |
| OMNI-R40 | OMNI-043, OMNI-044, OMNI-045, OMNI-048 | keep colliding events as evidence until O8; neutral wording/log names and policy-derived ceilings; per-type media limits; analytics by channel |

*CAN DEFER* (6)

| ID | Closes | Work |
| --- | --- | --- |
| OMNI-R41 | OMNI-038 | provider-time ordering for imported history (Coexistence track) |
| OMNI-R42 | OMNI-046 | consume `user_preferences` and 131050 into contact consent |
| OMNI-R43 | OMNI-047, OMNI-049 | compatibility-trigger provenance; remove dead legacy lookups (with O8) |
| OMNI-R44 | OMNI-050, OMNI-055 | per-product signing secrets if Instagram Login needs them; retry-horizon runbook; Graph API upgrade before 2027-01-21 |
| OMNI-R45 | OMNI-051, OMNI-052 | queue fairness; per-connection allowance override |
| OMNI-R46 | OMNI-053, OMNI-054 | reactions/edits/unsend/postbacks representation; BSUID authentication-template refusal and rotation |

Plus the deferred items carried from the previous stage and still open: person-level export/erasure (R21, before O7), O7 linking, O8 cleanup, re-sealing `v1` credentials, the Q1 census on a production copy (time-bound), and the six real-Meta checks listed in the remediation report's section 38. This audit adds to that list: a real-Meta check that a template quick-reply tap and an interactive reply arrive as documented, and that a delivery above 1 MiB is now accepted.

---

## Appendix A - What was read

Documents: `README.md`, `DECISIONS.md` (ADR-089..123, emphasis on ADR-093, 100-103, 110-111, 117-123), `ARCHITECTURE.md` (5.4 and the webhook flow; its older index list in the persistence section still names `UNIQUE(message_id)` on media and an unconditional `UNIQUE(phone_number_id)`, both superseded - stale prose, not code), `docs/WHATSAPP.md`, `docs/API.md`, `docs/CAMPAIGNS.md`, `docs/MEDIA.md`, `docs/OBSERVABILITY.md`, `docs/RUNBOOK.md` (omnichannel section), the prior audit `OMNICHANNEL_READINESS_AUDIT.md` (`49e2e98`) and `OMNICHANNEL_READINESS_FINDINGS_REMEDIATION.md`.

Code, end to end: `app/channels/*` (all eight modules); `app/integrations/whatsapp/{adapter,payload,policy,client,signature}.py`, `app/integrations/meta/signature.py`; `app/db/models/{channel,channel_event,conversation,whatsapp,media,campaign,lead,analytics,usage}.py`; migrations `0082`, `0083`, `0084`; `app/api/v1/{webhooks,conversations,contacts}.py`; `app/schemas/{conversation,campaign}.py`; `app/services/{whatsapp_service,channel_ingestion_service,conversation_service,contact_identity_service,messaging_service,credential_service,follow_up_service,campaign_service,media_service,analytics_service,opt_out}.py`; `app/repositories/{channel_repository,conversation_repository,channel_event_repository,media_repository}.py`; `app/agents/{memory,reply,lifecycle,orchestrator,registry}.py`; `app/workers/{inbound_recovery,queue,media_queue,ai_worker}.py`; `app/core/{limits,config,crypto,telemetry}.py` (relevant parts); `deploy/monitoring/alerts.yml` (messaging group); `tests/channel_fakes.py` and the omnichannel suites' harnesses. Whole-application sweeps for `wa_id`, `wa_message_id`, `phone_number_id`, `waba`, `whatsapp`, `from app.integrations.whatsapp`, `WhatsAppClient(`, `MessagingService(` and every `send_*` call site.

---

## Appendix B - Independent SQL

All on this audit's own PostgreSQL 16 container. **Catalog database** built only by `alembic upgrade head` from empty (11 s, `0084`), then seeded for the plans with 200 tenants, 200 numbers (mirror -> 200 connections), 10,000 contacts (trigger -> 10,000 phone identities), 10,000 conversations (trigger -> participants), 200,000 messages (half outbound, sequence/connection by trigger), then `ANALYZE`. Every query below ran in `BEGIN READ ONLY ... ROLLBACK` except the two seeded writes and the non-vacuity check, which was rolled back.

**Catalog census** (results summarised in sections 31-32):

| Query | Purpose | Result |
| --- | --- | --- |
| C1 | columns whose name matches the regular expression `whatsapp`, `^wa_`, `wamid`, `phone_number_id`, `waba` or `phone` | 7: `contacts.wa_id` (32, null), `leads.phone`, `message_media.wa_media_id` (255), `messages.wa_message_id` (255), `whatsapp_accounts.{display_phone_number, phone_number_id, waba_id}` |
| C2 | `whatsapp_*` tables | `whatsapp_accounts`, `whatsapp_events`, `whatsapp_templates` |
| C3 | foreign keys into `whatsapp_*` | `whatsapp_templates.account_id`, `campaigns.account_id`, `campaigns.template_id` |
| C4 | foreign keys into `channel_connections` / `contact_identities` | 5, all `convalidated = t` |
| C5 | unique indexes on messaging tables | 38 (both legacy and neutral provider-id keys on `messages` and `whatsapp_events`) |
| C6 | other indexes on conversations/messages/identities/connections/events | 20; none leads with `messages.connection_id` or `messages.wa_message_id` |
| C7 | triggers | mirror, phone identity, default participant, message sequence (+ billing triggers) |
| C8 | enum labels mentioning WhatsApp | 20 (section 31) |
| C9 | channel and identity enums | `channel_kind {whatsapp, instagram, messenger}`, `identity_kind {phone, bsuid, psid, igsid}`, `identity_scope {workspace, provider_account, connection}` |
| C10 | NOT VALID constraints | **0** |
| C11 | invalid indexes | **0** |

**Query plans** (`EXPLAIN (ANALYZE, BUFFERS)`):

| Query | SQL shape | Plan | Buffers | Time |
| --- | --- | --- | --- | --- |
| E1 status lookup, as issued | `wa_message_id = $1 AND connection_id IN ($2) AND direction = 'outbound' LIMIT 1` | **Parallel Seq Scan on messages** | 4,878 | 22.7 ms |
| E2 pre-remediation shape | `wa_message_id = $1 AND tenant_id IN ($2) AND direction = 'outbound' LIMIT 1` | Index Scan `uq_messages_tenant_id_wa_message_id` | 4 | 0.07 ms |
| E3 status for an id not ours | as E1 | Parallel Seq Scan | 4,879 | 18.0 ms |
| E4 screen | `tenant_id = $1 AND connection_id = $2 AND wa_message_id = $3` | Index Scan `uq_messages_tenant_id_connection_id_wa_message_id` | 4 | 0.08 ms |
| E5 inbox filtered by channel | `tenant_id = $1 AND status <> 'closed' AND channel = 'instagram' ORDER BY last_message_at DESC NULLS LAST, id DESC LIMIT 50` | Bitmap Heap Scan by tenant, filter on channel, sort | 956 | 1.3 ms |
| E6 recovery lookup | as E4 | Index Scan | 4 | 0.02 ms |
| E7 as E1 with `enable_seqscan = off` | as E1 | Index Scan `uq_messages_tenant_id_wa_message_id`, `wa_message_id` as a **non-leading** condition (a full index scan) | 106 | 1.3 ms |

Heap 38 MB and the neutral provider-id index 23 MB at 200,000 rows; E1 and E7 grow linearly with both.

**Read-only checks for a production copy** (all executed against the seeded catalog database; Q4 and Q5 proven non-vacuous by injecting one violation each in a rolled-back transaction - both counted 1):

```sql
BEGIN READ ONLY;
-- Q1 participants their channel cannot address (expect 0)
SELECT count(*) FROM conversations c JOIN contact_identities i ON i.id = c.participant_identity_id
 WHERE (c.channel = 'whatsapp' AND i.kind NOT IN ('phone','bsuid')) OR i.channel <> c.channel;
-- Q2 provider ids held by more than one connection of a workspace (0 while the legacy uniques stand)
SELECT count(*) FROM (SELECT tenant_id, wa_message_id FROM messages WHERE wa_message_id IS NOT NULL
  GROUP BY 1,2 HAVING count(DISTINCT connection_id) > 1) d;
-- Q3 message events with no projected message under their event_id (OMNI-032 exposure)
SELECT count(*) FROM whatsapp_events e WHERE e.kind = 'message' AND NOT EXISTS (
  SELECT 1 FROM messages m WHERE m.tenant_id = e.tenant_id AND m.connection_id = e.account_id
     AND m.wa_message_id = e.event_id);
-- Q4 window anchors older than the newest inbound message (OMNI-036 already happened)
SELECT count(*) FROM conversations c WHERE c.last_inbound_at < (
  SELECT max(m.sent_at) FROM messages m WHERE m.conversation_id = c.id AND m.tenant_id = c.tenant_id
     AND m.direction = 'inbound');
-- Q5 inbound interactive messages stored without text (OMNI-030 exposure)
SELECT count(*) FROM messages WHERE direction = 'inbound' AND kind = 'interactive' AND body IS NULL;
-- Q6 retained button taps that were a stop phrase (OMNI-030 consent exposure; raw payloads live 30 days)
SELECT count(*) FROM whatsapp_events WHERE kind = 'message' AND payload IS NOT NULL
   AND payload->>'type' = 'button' AND lower(payload#>>'{button,text}') IN ('stop promotions','stop','unsubscribe');
-- Q7 contacts whose wa_id disagrees with their WhatsApp phone identity (expect 0)
SELECT count(*) FROM contacts c LEFT JOIN contact_identities i ON i.tenant_id = c.tenant_id
   AND i.contact_id = c.id AND i.channel = 'whatsapp' AND i.kind = 'phone'
 WHERE (c.wa_id IS NULL) <> (i.id IS NULL) OR c.wa_id <> i.value;
-- Q8 surviving evidence of refusals: failed events by reason
SELECT coalesce(error, '-') AS reason, count(*) FROM whatsapp_events WHERE state = 'failed' GROUP BY 1 ORDER BY 2 DESC;
-- Q9 shared tables with foreign keys into WhatsApp tables
SELECT conrelid::regclass, conname FROM pg_constraint
 WHERE contype = 'f' AND confrelid::regclass::text LIKE 'whatsapp%' AND conrelid::regclass::text NOT LIKE 'whatsapp%';
ROLLBACK;
```

The remediation's operator tool was also run, read-only, against the same populated migration-built database (200 connections, 10,000 contacts, identities and conversations, 200,000 messages) - it had previously been run only against an empty one: `python -m scripts.omnichannel_invariants verify` -> **ok, all 21 invariants 0**; `census` -> `q8_shared_keys_into_whatsapp_tables: 2` (the two campaign keys, matching C3/Q9), every other count 0 on synthetic data. Its invariants do not cover what Q4-Q6 measure.

Q4 and Q6 are the two to run first on production: they measure whether OMNI-036 and the OMNI-030 opt-out loss have already happened to real customers. Q6 must run before the 30-day payload redaction removes the evidence. Plus the remediation's own `python -m scripts.omnichannel_invariants census|verify`.

---

## Appendix C - Test lanes

| Lane | Database | Result |
| --- | --- | --- |
| Static | - | ruff clean; black 812 unchanged; mypy 719 files clean |
| Model-built `pytest tests/` | own database, own Redis | **6,337 passed, 16 skipped, 2 deselected, 0 failed, 0 errors**, 30 m 46 s |
| Migration-built schema parity | own database | **4 passed** |
| Migration-built `tests/integration tests/e2e` | same | **3,091 passed, 2 deselected (CI's two), 0 skipped, 0 failed, 0 errors** (29 m 09 s), after schema parity 4 passed |
| Audit probes | own database | 17 passed |

Model-built results by area (JUnit, all passed): omnichannel suites 181; WhatsApp suites 259; messaging core (delivery protocol, live turn, projection, conversation and contact endpoints, templates, throughput, opt-out) 100; media 463; AI 332; tools 92; CRM, follow-ups, leads and sentiment 344; campaigns 76; tenancy, authorization and platform 184; usage, entitlements and metering 104. The counts equal the remediation's report at `5f8a788` (6,337), as expected for an unchanged tree.

What the green suites do **not** cover, each now shown by a probe: the status lookup's query plan (Appendix B, E1), the inbox route with a non-operable channel (P2), split event/message ids (P3), Meta's button and interactive shapes (P1), deliveries above 1 MiB (P11), out-of-order windows (P10), external echoes (P9).

---

## Appendix D - Probe evidence

Driven through production code on a real PostgreSQL 16 (model-built schema, its own database), synthetic identifiers only, no network. The probe file was written for this audit, run, archived outside the repository and removed from the worktree; it is not committed. Each line is the probe's recorded output.

| Probe | Input | Output |
| --- | --- | --- |
| P1.template_quick_reply_stop | WhatsApp `{"type":"button","button":{"text":"Stop promotions","payload":"STOP-PAYLOAD"}}` | `stored 1, kind interactive, body null, model_sees "[interactive]", opted_out false, opt_outs_counted 0, queued_agent_jobs 1` |
| P1.interactive_button_reply | `interactive.button_reply {id "book-yes", title "Yes, book it"}` | `kind interactive, body null, model_sees "[interactive]", queued 1` |
| P1.interactive_list_reply | `interactive.list_reply {id "plan-2", title "Pro plan"}` | the same |
| P1.location | `location {latitude, longitude, name}` | `kind location, body null, model_sees "[location]", queued 1` |
| P1.text_stop_control | `text.body "Stop promotions"` | `body "Stop promotions", opted_out true, opt_outs_counted 1` |
| P2.inbox_fail_closed | one WhatsApp + one Instagram-labelled conversation, default registry, real ASGI app | `GET /conversations` -> **422** `{"code":"validation_error","message":"Wasla cannot operate this channel."}`; `?channel=whatsapp` -> 200, 1 item; `GET /conversations/{whatsapp}` -> 200 |
| P3.split_ids=False | synthetic message, Redis refuses the enqueue, then `InboundRecoveryWorker.run_once` | `recovery_agent_jobs 1, event processed` |
| P3.split_ids=True | same, event id `evt.<mid>`, message id `<mid>` | `recovery_agent_jobs 0, abandoned 1, event failed, error projection_missing` |
| P4.scoping | same IGSID-like sender and same `mid` on connection A1, A2 (workspace A) and B1 (workspace B) | A1 `stored 1`; A2 `stored 0, collisions 1` (event not kept); B1 `stored 1`; contacts A 1, B 1; wrong-connection conversation lookup `null`; B reading A's identity `TenantIsolationError`; B finding A's message `null`; B listing A's media `[]` |
| P4b.same_sender_two_connections | the same IGSID-like sender on two connections of one workspace, distinct message ids | `stored 1, 1`; contacts 2; identities 2, both scope `connection`, each on its own connection; conversations 2 |
| P5.watermark | synthetic send, then watermarks 1 s before / after its local `sent_at` | `before 0, after 1` |
| P6.follow_up | 720 Arabic characters (1,320 bytes) on a 1,000-byte synthetic channel | scheduling `pending` (accepted); dispatch `pending`, attempts 1, detail "A Synthetic message may be at most 1000 bytes.", provider sends 0 |
| P7.contact_phone_trigger | contact created with `wa_id`, then `wa_id` changed | identity `source provider`; change refused `UniqueViolation uq_contact_identities_one_whatsapp_phone` |
| P8.kinds | WhatsApp types `reaction, contacts, order, system, sticker, unsupported, request_welcome` | all `unsupported` except `sticker -> image` |
| P9.native_echo | customer "price?", then an echo of a native-app reply "It is 500." | `echoes 1`, transcript `[inbound "price?"]` only, mode `ai`, agent jobs 1 |
| P10.ordering | message stamped now, then one stamped a day earlier | sequence 1 = newer, 2 = older; model reads newer then older; `last_inbound_at` = **the older timestamp** |
| P11.webhook_cap | real app, app secret configured, invalid signature | 1,000 statuses 432,251 B -> 403 (passed the cap); 200 long Arabic messages 1,468,122 B -> **413**; 350 -> 2,568,972 B -> **413**; cap 1,048,576 |

---

## Appendix E - Hygiene

- **Application code changed: no.** The only tracked change on the audit branch is this report.
- Probe source, logs, SQL files and the scratch catalog database are outside the repository; the probe file was removed from the worktree before the commit.
- Containers `wasla-omni-final-pg`, `-redis`, `-redis2`, `-minio` hold test data only and are left running for the verification stage to inspect or remove.
- **Secret scan:** **secret leaks = 0.** The report and the branch diff were scanned for Meta (`EAA...`) and OpenAI (`sk-...`) token shapes, JWTs, credentialed PostgreSQL/Redis/MySQL URLs, private keys, AWS keys, bearer tokens, `password=`/`secret_key=` assignments, ngrok hosts, Egyptian phone-number shapes and this run's own database password: every pattern 0; the only long hexadecimal strings are the two commit hashes. Identifiers in the report are synthetic or redacted.
- Nothing merged, pushed or deployed; no Meta configuration, webhook, real number or customer touched; no real message sent.
